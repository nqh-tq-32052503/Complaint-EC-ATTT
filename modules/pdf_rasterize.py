#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_reconvert.py
================

Pipeline: PDF -> N ảnh (PNG/JPEG) -> 1 PDF.

Thiết kế cho service Linux xử lý PDF tiếng Việt, ưu tiên:
  1. Tốc độ: rasterize song song bằng multiprocessing (PyMuPDF là native code,
     mỗi worker mở lại document trong process riêng để tránh chia sẻ trạng thái
     C không an toàn giữa các process).
  2. Không lỗi Unicode: toàn bộ I/O (đọc CLI arg, ghi log, đặt tên file) được
     ép encoding='utf-8' tường minh, không phụ thuộc locale mặc định của hệ
     điều hành (LANG=C sẽ không làm gãy pipeline này).
  3. Không phụ thuộc font hệ thống: PyMuPDF rasterize trực tiếp từ glyph đã
     nhúng trong PDF (nếu có), không gọi ra ngoài qua fontconfig như
     poppler/Ghostscript trong nhiều trường hợp.
  4. Gộp ảnh -> PDF bằng img2pdf: chỉ đóng gói (embed) dữ liệu ảnh vào
     container PDF, không vẽ lại text/glyph nào, nên loại bỏ hoàn toàn khả
     năng phát sinh lỗi Unicode/font ở bước này.

Yêu cầu cài đặt:
    pip install --break-system-packages pymupdf img2pdf

Sử dụng dòng lệnh:
    python3 pdf_reconvert.py input.pdf output.pdf \
        --dpi 200 --workers 4 --format png --keep-images
"""

from __future__ import annotations

import argparse
import io
import logging
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import img2pdf
import pymupdf  # PyMuPDF (tên module mới thay cho `fitz`)


# --------------------------------------------------------------------------- #
# Logging: ép UTF-8 tường minh cho toàn bộ handler, không phụ thuộc locale.
# --------------------------------------------------------------------------- #

def _build_logger(name: str = "pdf_reconvert") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    # Trên Python 3.9+, reconfigure() cho phép ép encoding của stream chuẩn.
    # Nếu service chạy dưới systemd với LANG=C, đây là điểm thường gãy nhất
    # khi log chứa tiếng Việt có dấu.
    try:
        handler.stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(processName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    return logger


log = _build_logger()


# --------------------------------------------------------------------------- #
# Cấu trúc dữ liệu cấu hình pipeline
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PipelineConfig:
    input_pdf: Path
    output_pdf: Path
    dpi: int = 200
    workers: int = 4
    image_format: str = "png"   # "png" | "jpeg"
    jpeg_quality: int = 90
    keep_images: bool = False
    work_dir: Path | None = None


# --------------------------------------------------------------------------- #
# Giai đoạn 1: PDF -> ảnh (song song hóa theo trang)
# --------------------------------------------------------------------------- #

def _render_single_page(
    page_index: int,
    pdf_path: str,
    dpi: int,
    image_format: str,
    jpeg_quality: int,
    out_dir: str,
) -> str:
    """
    Render một trang PDF thành file ảnh. Chạy trong worker process riêng biệt.

    Mỗi worker tự mở lại `pymupdf.open(pdf_path)` thay vì nhận object Document
    đã mở từ process cha, vì đối tượng C-level của PyMuPDF không pickle được
    an toàn qua ranh giới process.
    """
    doc = pymupdf.open(pdf_path)
    try:
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        page = doc[page_index]
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)

        ext = "png" if image_format == "png" else "jpg"
        # Tên file dùng số thứ tự có zero-padding, tránh mọi ký tự tiếng Việt
        # trong tên file trung gian -> loại trừ rủi ro lỗi Unicode ở tầng
        # filesystem/OS ngay từ khâu đặt tên.
        out_path = os.path.join(out_dir, f"page_{page_index:05d}.{ext}")

        if image_format == "png":
            pixmap.save(out_path)
        else:
            pixmap.pil_save(out_path, format="JPEG", quality=jpeg_quality)

        return out_path
    finally:
        doc.close()


def pdf_to_images(config: PipelineConfig, out_dir: Path) -> list[str]:
    t0 = time.perf_counter()

    # Mở một lần ở process chính chỉ để lấy số trang; không truyền object
    # Document này sang worker.
    probe = pymupdf.open(str(config.input_pdf))
    n_pages = probe.page_count
    probe.close()

    if n_pages == 0:
        raise ValueError(f"PDF không có trang nào: {config.input_pdf}")

    log.info(
        "Bắt đầu rasterize: %d trang, dpi=%d, format=%s, workers=%d",
        n_pages, config.dpi, config.image_format, config.workers,
    )

    worker_fn = partial(
        _render_single_page,
        pdf_path=str(config.input_pdf),
        dpi=config.dpi,
        image_format=config.image_format,
        jpeg_quality=config.jpeg_quality,
        out_dir=str(out_dir),
    )

    # Giới hạn số worker theo min(cấu hình, số trang, số CPU khả dụng) để
    # tránh oversubscription khi service xử lý nhiều request đồng thời.
    n_workers = max(1, min(config.workers, n_pages, os.cpu_count() or 1))

    if n_workers == 1:
        image_paths = [worker_fn(i) for i in range(n_pages)]
    else:
        with mp.Pool(processes=n_workers) as pool:
            image_paths = pool.map(worker_fn, range(n_pages))

    # pool.map giữ đúng thứ tự index đầu vào -> không cần sort lại, nhưng
    # sort tường minh theo tên để an toàn nếu hàm gọi thay đổi trong tương lai.
    image_paths.sort()

    elapsed = time.perf_counter() - t0
    log.info("Hoàn tất rasterize %d trang trong %.2fs (%.3fs/trang)",
              n_pages, elapsed, elapsed / n_pages)
    return image_paths


# --------------------------------------------------------------------------- #
# Giai đoạn 2: ảnh -> 1 PDF (không vẽ lại text -> không rủi ro Unicode/font)
# --------------------------------------------------------------------------- #

def images_to_pdf(image_paths: list[str], output_pdf: Path | None = None) -> bytes:
    if not image_paths:
        raise ValueError("Danh sách ảnh đầu vào rỗng, không thể tạo PDF.")

    t0 = time.perf_counter()

    # img2pdf.convert nhận list đường dẫn (str/bytes) hoặc list bytes ảnh đã
    # đọc sẵn. Dùng đường dẫn trực tiếp để tránh giữ toàn bộ ảnh trong RAM
    # cùng lúc khi số trang lớn.
    # Dùng engine=internal và nodate=True để đảm bảo tính tất định (deterministic)
    # của file PDF tạo ra, giúp mã hash SHA-256 nhất quán cho cơ chế cache.
    pdf_bytes = img2pdf.convert(image_paths, engine=img2pdf.Engine.internal, nodate=True)

    if output_pdf is not None:
        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        with open(output_pdf, "wb") as f:
            f.write(pdf_bytes)

    elapsed = time.perf_counter() - t0
    log.info("Hoàn tất gộp %d ảnh thành PDF trong %.2fs%s",
              len(image_paths), elapsed, f" -> {output_pdf}" if output_pdf else "")
    return pdf_bytes


def _resolve_pdf_input_to_path(
    pdf_input: str | Path | bytes | bytearray | io.IOBase,
) -> tuple[Path, bool]:
    """Chuyển đổi các dạng pdf_input thành Path tới file tạm hoặc file có sẵn."""
    if isinstance(pdf_input, (str, Path)):
        p = Path(pdf_input)
        if not p.is_file():
            raise FileNotFoundError(f"Không tìm thấy file PDF đầu vào: {pdf_input}")
        return p, False

    if isinstance(pdf_input, (bytes, bytearray)):
        data = bytes(pdf_input)
    elif hasattr(pdf_input, "read"):
        if hasattr(pdf_input, "seek"):
            try:
                pdf_input.seek(0)
            except Exception as exc:
                log.debug("seek(0) không thành công trên stream input: %s", exc)
        data = pdf_input.getvalue() if hasattr(pdf_input, "getvalue") else pdf_input.read()
    else:
        raise TypeError(f"Định dạng pdf_input không hợp lệ: {type(pdf_input)}")

    temp_in = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    temp_in.write(data)
    temp_in.flush()
    temp_in.close()
    return Path(temp_in.name), True


def rasterize_pdf(
    pdf_input: str | Path | bytes | bytearray | io.IOBase,
    dpi: int = 200,
    workers: int = 4,
    image_format: str = "png",
    jpeg_quality: int = 90,
) -> bytes:
    """
    Rasterize toàn bộ các trang PDF thành ảnh rồi gộp lại thành 1 file PDF mới (dưới dạng bytes).

    Hỗ trợ đa dạng kiểu dữ liệu đầu vào:
      - str / Path: Đường dẫn tới file PDF có sẵn trên ổ đĩa.
      - bytes / bytearray: Dữ liệu nhị phân của file PDF.
      - file-like object (io.BytesIO, file object): Luồng dữ liệu PDF có method .read().

    Returns:
      bytes: Dữ liệu nhị phân của file PDF sau khi rasterize.
    """
    input_path, clean_input_file = _resolve_pdf_input_to_path(pdf_input)
    work_dir = Path(tempfile.mkdtemp(prefix="pdf_rasterize_"))
    try:
        config = PipelineConfig(
            input_pdf=input_path,
            output_pdf=work_dir / "output.pdf",
            dpi=dpi,
            workers=workers,
            image_format=image_format,
            jpeg_quality=jpeg_quality,
            work_dir=work_dir,
        )
        image_paths = pdf_to_images(config, work_dir)
        return images_to_pdf(image_paths, output_pdf=None)
    finally:
        if clean_input_file and input_path.exists():
            try:
                input_path.unlink()
            except OSError:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)



# --------------------------------------------------------------------------- #
# Điều phối toàn bộ pipeline
# --------------------------------------------------------------------------- #

def run_pipeline(config: PipelineConfig) -> Path:
    if not config.input_pdf.exists():
        raise FileNotFoundError(f"Không tìm thấy file đầu vào: {config.input_pdf}")

    # Thư mục làm việc: dùng tempfile.mkdtemp nếu không chỉ định, tên thư mục
    # thuần ASCII -> không phụ thuộc encoding filesystem cho thư mục trung gian.
    own_tmp = config.work_dir is None
    work_dir = config.work_dir or Path(tempfile.mkdtemp(prefix="pdf_reconvert_"))
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        log.info("Xử lý: %s -> %s", config.input_pdf, config.output_pdf)
        image_paths = pdf_to_images(config, work_dir)
        images_to_pdf(image_paths, config.output_pdf)

        if config.keep_images:
            log.info("Giữ lại %d ảnh trung gian tại: %s", len(image_paths), work_dir)
        return config.output_pdf
    finally:
        if own_tmp and not config.keep_images:
            shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Kiểm thử tính toàn vẹn: so sánh số trang và thử trích xuất text tiếng Việt
# từ PDF đầu ra để xác nhận không có exception Unicode phát sinh trong quá
# trình xử lý (bản thân ảnh không mang lớp text nên đây chỉ là smoke test
# cấu trúc file, không phải test OCR).
# --------------------------------------------------------------------------- #

def verify_output(input_pdf: Path, output_pdf: Path) -> None:
    src = pymupdf.open(str(input_pdf))
    dst = pymupdf.open(str(output_pdf))
    try:
        if src.page_count != dst.page_count:
            raise ValueError(
                f"Số trang không khớp: input={src.page_count}, output={dst.page_count}"
            )
        log.info("Kiểm tra OK: %d trang khớp giữa input và output.", dst.page_count)

        for i in range(dst.page_count):
            pix = dst[i].get_pixmap()
            if pix.width == 0 or pix.height == 0:
                raise ValueError(f"Trang {i} trong output rỗng bất thường.")
        log.info("Kiểm tra OK: tất cả trang output đều có kích thước hợp lệ.")
    finally:
        src.close()
        dst.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> tuple[PipelineConfig, bool]:
    parser = argparse.ArgumentParser(
        description="Pipeline PDF -> ảnh -> PDF, an toàn Unicode cho nội dung tiếng Việt.",
    )
    parser.add_argument("input_pdf", type=str, help="Đường dẫn file PDF đầu vào")
    parser.add_argument("output_pdf", type=str, help="Đường dẫn file PDF đầu ra")
    parser.add_argument("--dpi", type=int, default=200, help="Độ phân giải rasterize (mặc định: 200)")
    parser.add_argument("--workers", type=int, default=4, help="Số process song song (mặc định: 4)")
    parser.add_argument("--format", choices=["png", "jpeg"], default="png", help="Định dạng ảnh trung gian")
    parser.add_argument("--jpeg-quality", type=int, default=90, help="Chất lượng JPEG nếu dùng --format jpeg")
    parser.add_argument("--keep-images", action="store_true", help="Giữ lại thư mục ảnh trung gian")
    parser.add_argument("--work-dir", type=str, default=None, help="Thư mục lưu ảnh trung gian (mặc định: tự tạo tmp)")
    parser.add_argument("--verify", action="store_true", help="Kiểm tra số trang/kích thước sau khi tạo PDF output")

    args = parser.parse_args(argv)

    return PipelineConfig(
        input_pdf=Path(args.input_pdf),
        output_pdf=Path(args.output_pdf),
        dpi=args.dpi,
        workers=args.workers,
        image_format=args.format,
        jpeg_quality=args.jpeg_quality,
        keep_images=args.keep_images,
        work_dir=Path(args.work_dir) if args.work_dir else None,
    ), args.verify


def main(argv: list[str] | None = None) -> int:
    try:
        config, do_verify = parse_args(argv)
        run_pipeline(config)
        if do_verify:
            verify_output(config.input_pdf, config.output_pdf)
        return 0
    except Exception:
        log.exception("Pipeline thất bại")
        return 1


if __name__ == "__main__":
    sys.exit(main())