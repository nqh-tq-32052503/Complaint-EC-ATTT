import os
import io
import re
import hashlib
import unicodedata
import requests
import logging
from typing import Optional, Tuple, Any, List, Dict

logger = logging.getLogger(__name__)




OCR_SERVICE_URL = os.environ.get("OCR_SERVICE_URL", "").rstrip("/")


class TableContentUnsupportedError(Exception):
    """Lỗi khi phát hiện file PDF có chứa bảng biểu (table) không được hỗ trợ."""
    def __init__(self, message: str = "Tài liệu PDF chứa bảng biểu (table), hiện tại hệ thống chưa hỗ trợ trích xuất dữ liệu từ bảng."):
        self.message = message
        self.code = "TABLE_CONTENT_UNSUPPORTED"
        super().__init__(self.message)


def remove_vietnamese_diacritics(text: str) -> str:
    """Loại bỏ dấu tiếng Việt và chuẩn hóa về ký tự ASCII không dấu (D, d thay cho Đ, đ)."""
    text = text.replace('đ', 'd').replace('Đ', 'D')
    text = unicodedata.normalize('NFD', text)
    return ''.join(c for c in text if unicodedata.category(c) != 'Mn')


DOI_VOI_PATTERN = re.compile(
    r'(?<![a-zA-Z0-9_\u00C0-\u1EF9])'
    r'([dđDĐ][a-zA-Z\u00C0-\u1EF9]{2})'
    r'(\s+)'
    r'([vV][a-zA-Z\u00C0-\u1EF9]{2})'
    r'(?![a-zA-Z0-9_\u00C0-\u1EF9])'
)


def normalize_doi_voi(markdown_text: str) -> str:
    """
    Chuẩn hóa các biến thể sai dấu của cụm từ 'Đối với' trong nội dung Markdown:
    Tìm tất cả các cụm từ mà khi loại bỏ dấu tiếng Việt có dạng 'Doi voi' / 'doi voi',
    sau đó chuẩn hóa thành 'Đối với' (bảo toàn quy tắc viết hoa / viết thường).
    """
    if not markdown_text:
        return markdown_text

    text = unicodedata.normalize('NFC', markdown_text)

    def _replace_match(m):
        w1, sep, w2 = m.group(1), m.group(2), m.group(3)
        if remove_vietnamese_diacritics(w1).lower() == 'doi' and remove_vietnamese_diacritics(w2).lower() == 'voi':
            if w1.isupper() and w2.isupper():
                return f'ĐỐI{sep}VỚI'
            elif w1[0].isupper():
                if w2[0].isupper():
                    return f'Đối{sep}Với'
                return f'Đối{sep}với'
            else:
                return f'đối{sep}với'
        return m.group(0)

    return DOI_VOI_PATTERN.sub(_replace_match, text)


PHU_LUC_LINE_PATTERN = re.compile(
    r"^[ \t#*_]*(?:[0-9a-zA-ZđĐ]+[.):/-]\s+)?(?:PHẦN\s+)?PHỤ LỤC\b"
)


def truncate_at_phu_luc(markdown_text: str) -> str:
    """
    Nếu tìm thấy dòng nào có cụm từ PHỤ LỤC (đúng format chữ viết hoa),
    có thể kèm theo các ký tự #, thì xóa toàn bộ nội dung từ dòng đó trở xuống,
    giữ lại phần nội dung bên trên và return.
    """
    if not markdown_text:
        return markdown_text

    lines = markdown_text.split("\n")
    cutoff_idx = None

    for idx, line in enumerate(lines):
        if PHU_LUC_LINE_PATTERN.search(line):
            cutoff_idx = idx
            break

    if cutoff_idx is not None:
        return "\n".join(lines[:cutoff_idx]).rstrip()

    return markdown_text


def _is_footnote_node(child: dict) -> bool:
    node_type = str(child.get("node_type", "")).lower()
    node_id = str(child.get("node_id", "")).lower()
    return node_type in ("footnote", "page_footer", "footer") or node_id.startswith("footnote")


def _filter_tree_node(node: dict):
    if not isinstance(node, dict):
        return
    children = node.get("children")
    if not isinstance(children, list):
        return

    filtered_children = []
    for child in children:
        if isinstance(child, dict):
            if _is_footnote_node(child):
                continue
            _filter_tree_node(child)
        filtered_children.append(child)
    node["children"] = filtered_children


def remove_footnotes_from_canonical(canonical_data: dict) -> dict:
    """
    Loại bỏ các element footer/footnote khỏi canonical OCR JSON response:
    1. Đệ quy loại bỏ các node có node_type == 'footnote' hoặc node_id bắt đầu bằng 'footnote'
       (hoặc node_type thuộc nhóm page_footer/footer) khỏi cây document tree.
    2. Xóa các footer trong page_artifacts nếu có.
    """
    if not isinstance(canonical_data, dict):
        return canonical_data

    tree = canonical_data.get("tree")
    if isinstance(tree, dict):
        _filter_tree_node(tree)

    # 2. Xóa footers trong page_artifacts nếu có
    page_artifacts = canonical_data.get("page_artifacts")
    if isinstance(page_artifacts, dict) and "footers" in page_artifacts:
        page_artifacts["footers"] = []

    return canonical_data


def _to_bytes(pdf_input) -> bytes:
    """Chuẩn hoá pdf_input (str path | bytes | file-like) về dạng bytes."""
    if isinstance(pdf_input, (bytes, bytearray)):
        return bytes(pdf_input)
    if hasattr(pdf_input, "read"):
        if hasattr(pdf_input, "seek"):
            try:
                pdf_input.seek(0)
            except Exception as exc:
                logger.debug("pdf_input.seek(0) không thành công: %s", exc)
        return pdf_input.getvalue() if hasattr(pdf_input, "getvalue") else pdf_input.read()
    with open(pdf_input, "rb") as f:
        return f.read()


class PDFExtractor(object):
    """
    Module PDFExtractor kết nối tới dịch vụ OCR tập trung:
    - Gửi file PDF tới OCR Service (hỗ trợ rasterize, Surya layout/recognition, sửa lỗi chính tả bằng LLM).
    - OCR Service tự quản lý bộ nhớ đệm (cache) trong bucket riêng của nó.
    - Tích hợp endpoint render canonical JSON sang Markdown chuẩn hóa cấp tiêu đề (#, ##, ###).
    - Tự động phát hiện bảng biểu và kiểm soát cắt bỏ Phụ lục.
    """

    def __init__(
        self,
        ocr_service_url: Optional[str] = None,
        process_table: bool = False,
        keep_phu_luc: bool = False,
        timeout: int = 300,
        **kwargs,
    ):
        self.ocr_service_url = (ocr_service_url or OCR_SERVICE_URL).rstrip("/")
        self.process_table = process_table
        self.keep_phu_luc = keep_phu_luc
        self.timeout = timeout



    def detect_tables(self, pdf_input) -> Tuple[bool, list]:
        """
        Kiểm tra xem file PDF có chứa bảng biểu (table) hay không qua OCR Service API.
        Sử dụng rasterize=false, enable_correction=false để tối ưu tốc độ.
        
        Args:
            pdf_input: str path | bytes | file-like object
            
        Returns:
            Tuple[bool, list]:
                - has_table (bool): True nếu phát hiện ít nhất 1 bảng trong PDF
                - tables (list): Danh sách chi tiết các bảng phát hiện được
        """
        pdf_bytes = _to_bytes(pdf_input)
        url = f"{self.ocr_service_url}/v1/ocr/documents"
        files = {"file": ("document.pdf", pdf_bytes, "application/pdf")}
        data = {
            "rasterize": "false",
            "enable_correction": "false",
            "process_table": "true",
            "use_celery": "false",
            "use_cache": "false",
        }
        try:
            res = requests.post(url, files=files, data=data, timeout=self.timeout)
            if res.status_code == 200:
                resp_data = res.json()
                tables_dict = resp_data.get("tables", {})
                tables = list(tables_dict.values())
                return len(tables) > 0, tables
            else:
                print(f"[Table Detection Warning] OCR service returned {res.status_code}: {res.text[:200]}")
        except Exception as e:
            print(f"[Table Detection Warning] Lỗi khi gọi OCR service detect_tables: {e}")
        return False, []

    def _render_canonical_to_markdown(
        self,
        canonical_data: dict,
        keep_phu_luc: bool,
        process_table: bool,
    ) -> str:
        """
        Gọi endpoint /v1/ocr/render để chuyển đổi canonical OCR output sang Markdown chuẩn
        với các cấp bậc tiêu đề (#, ##) và định dạng bảng biểu.
        """
        url = f"{self.ocr_service_url}/v1/ocr/render"
        exclude_roles = [] if keep_phu_luc else ["appendix"]
        payload = {
            "canonical": canonical_data,
            "options": {
                "format": "markdown",
                "include_tables": process_table,
                "include_footnotes": False,
                "exclude_roles": exclude_roles,
                "include_headings": True,
            },
        }
        try:
            res = requests.post(url, json=payload, timeout=60)
            if res.status_code == 200:
                rendered_data = res.json()
                return rendered_data.get("content", "")
            else:
                print(f"[Render Warning] OCR render trả về HTTP {res.status_code}: {res.text[:200]}")
        except Exception as e:
            print(f"[Render Warning] Lỗi khi gọi OCR render: {e}")

        # Fallback về content thô nếu render endpoint không khả dụng
        return canonical_data.get("content", "")

    def run_ocr_by_surya(
        self,
        pdf_input,
        process_table: Optional[bool] = None,
        allow_table: Optional[bool] = None,
        keep_phu_luc: Optional[bool] = None,
        truncate_phu_luc: Optional[bool] = None,
        keep_appendix: Optional[bool] = None,
    ) -> str:
        """Alias tương thích ngược với code cũ, gọi sang OCR service tập trung."""
        content, _ = self._run_ocr_sync(
            pdf_input,
            process_table=process_table,
            allow_table=allow_table,
            keep_phu_luc=keep_phu_luc,
            truncate_phu_luc=truncate_phu_luc,
            keep_appendix=keep_appendix,
        )
        return content

    def _run_ocr_sync(
        self,
        pdf_input,
        use_cache: bool = True,
        rasterize: bool = True,
        process_table: Optional[bool] = None,
        allow_table: Optional[bool] = None,
        keep_phu_luc: Optional[bool] = None,
        truncate_phu_luc: Optional[bool] = None,
        keep_appendix: Optional[bool] = None,
    ) -> Tuple[str, bool]:
        """Thực thi OCR đồng bộ gọi tới OCR Service."""
        if process_table is None:
            process_table = allow_table if allow_table is not None else getattr(self, "process_table", False)

        pdf_bytes = _to_bytes(pdf_input)

        # 1. Gửi request tới OCR Service (OCR Service tự quản lý bộ nhớ đệm cache trong bucket riêng)
        url = f"{self.ocr_service_url}/v1/ocr/documents"
        files = {"file": ("document.pdf", pdf_bytes, "application/pdf")}
        data = {
            "rasterize": "true" if rasterize else "false",
            "enable_correction": "true",
            "process_table": "true",
            "use_celery": "true",
            "use_cache": "true" if use_cache else "false",
        }

        print(f"[PDFExtractor] Gửi tài liệu tới OCR Service ({url}, use_cache={use_cache})...")
        res = requests.post(url, files=files, data=data, timeout=self.timeout)
        if res.status_code != 200:
            raise RuntimeError(f"OCR Service trả về mã lỗi {res.status_code}: {res.text[:300]}")

        canonical_data = res.json()

        # 2. Kiểm tra bảng biểu trong tài liệu
        tables = canonical_data.get("tables", {})
        if tables:
            if not process_table:
                print(f"[PDFExtractor Warning] Phát hiện {len(tables)} bảng biểu trong tài liệu.")
                raise TableContentUnsupportedError(
                    f"Tài liệu PDF chứa {len(tables)} bảng biểu (table), hiện tại hệ thống chưa hỗ trợ trích xuất dữ liệu từ bảng."
                )
            else:
                print(f"[PDFExtractor Info] Phát hiện {len(tables)} bảng biểu trong tài liệu, tiếp tục xử lý (process_table=True).")

        # 3. Loại bỏ footer / footnote khỏi canonical data
        canonical_data = remove_footnotes_from_canonical(canonical_data)

        return canonical_data, False


    async def run_ocr(
        self,
        pdf_input,
        use_cache: bool = True,
        rasterize: bool = True,
        process_table: Optional[bool] = None,
        allow_table: Optional[bool] = None,
        keep_phu_luc: Optional[bool] = None,
        truncate_phu_luc: Optional[bool] = None,
        keep_appendix: Optional[bool] = None,
    ) -> Tuple[str, bool]:
        """
        Trích xuất PDF sang Canonical OCR Tree trực tiếp qua OCR Service.
        Hàm async tương thích 100% với luồng pipeline hiện tại.
        """
        import asyncio
        return await asyncio.to_thread(
            self._run_ocr_sync,
            pdf_input,
            use_cache=use_cache,
            rasterize=rasterize,
            process_table=process_table,
            allow_table=allow_table,
            keep_phu_luc=keep_phu_luc,
            truncate_phu_luc=truncate_phu_luc,
            keep_appendix=keep_appendix,
        )