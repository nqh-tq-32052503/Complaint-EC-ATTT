"""
map_chunks_to_headings.py
==========================

Bài toán: cho một list các chunk (mỗi chunk là NGUYÊN VĂN một kiến nghị,
đã được trích từ file markdown gốc), xác định heading path (breadcrumb)
mà chunk đó thuộc về trong cây heading của văn bản gốc.

ĐÂY LÀ BÀI TOÁN THUẦN TẤT ĐỊNH (deterministic), KHÔNG dùng LLM:
    1. Parse toàn bộ heading trong văn bản gốc kèm offset ký tự tuyệt đối,
       dựng breadcrumb (danh sách heading tổ tiên) tại mỗi heading.
    2. Với mỗi chunk, ĐỊNH VỊ offset của nó trong văn bản gốc bằng
       anchor-matching + con trỏ tìm kiếm đơn điệu tăng (khai thác việc
       các chunk xuất hiện theo đúng thứ tự gốc trong văn bản).
    3. Tra breadcrumb của heading gần nhất phía trước offset đó (bisect).

Nếu pipeline chunking phía trước còn giữ offset ký tự gốc của từng unit
(khuyến nghị bổ sung field này vào Unit ở semantic_chunk_kien_nghi.py),
có thể bỏ qua hoàn toàn bước 2 (định vị) và tra breadcrumb trực tiếp theo
offset đã biết — chính xác tuyệt đối, không cần string matching.
"""

from __future__ import annotations

import bisect
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

class HeadingExtractor(object):
    def __init__(self):
        """Khởi tạo module trích xuất tiêu đề (stateless)."""
        pass
    
    def extract_heading(self, raw_chunks: list, source_text: str):
        if raw_chunks and isinstance(raw_chunks[0], dict):
            chunks = [c["content"] for c in raw_chunks]
        else:
            chunks = raw_chunks
    
        print(f"[1/2] Đang định vị + tra heading cho {len(chunks)} chunk...", file=sys.stderr)
        results = assign_headings_to_chunks(chunks, source_text)
    
        n_failed = sum(1 for r in results if r.heading_path is None)
        if n_failed:
            print(f"    !! {n_failed}/{len(results)} chunk KHÔNG định vị được — cần review thủ công.",
                    file=sys.stderr)
        n_iou = sum(1 for r in results if r.match_method and "IOU_MATCH" in r.match_method)
        if n_iou:
            print(f"    ?? {n_iou} chunk dùng IoU-match (lệch dấu/khoảng trắng so với gốc).",
                    file=sys.stderr)
    
        print("[2/2] Exporting result", file=sys.stderr)
        output = [
            {
                "content": r.chunk,
                "heading_path": r.heading_path if r.heading_path else [],          # list từ root -> heading trực tiếp chứa chunk
                "heading_immediate": r.heading_path[-1] if r.heading_path else "",
                "match_method": r.match_method,
            }
            for r in results
        ]
        return output


HEADER_LINE_RE = re.compile(r"^[ \t]*(#{1,6})[ \t]+(\S.*)", re.MULTILINE)


# --------------------------------------------------------------------------- #
# 1. Parse heading + breadcrumb theo offset
# --------------------------------------------------------------------------- #

@dataclass
class HeadingEntry:
    offset: int          # vị trí ký tự bắt đầu dòng heading trong văn bản gốc
    level: int           # số dấu '#'
    raw_line: str        # nguyên văn dòng heading (kèm '#')
    path: list[str] = field(default_factory=list)  # breadcrumb, gồm cả chính nó, root -> leaf


def parse_headings_with_breadcrumbs(source_text: str) -> list[HeadingEntry]:
    """
    Duyệt tuần tự các heading theo offset xuất hiện, dựng breadcrumb bằng
    stack (level nhỏ hơn thì pop). path lưu tại mỗi heading = full path từ
    root đến chính heading đó (list các raw_line).
    """
    entries: list[HeadingEntry] = []
    stack: list[HeadingEntry] = []  # tăng dần theo level

    for m in HEADER_LINE_RE.finditer(source_text):
        level = len(m.group(1))
        raw_line = m.group(0).strip()
        offset = m.start()

        while stack and stack[-1].level >= level:
            stack.pop()

        path = [h.raw_line for h in stack] + [raw_line]
        entry = HeadingEntry(offset=offset, level=level, raw_line=raw_line, path=path)
        entries.append(entry)
        stack.append(entry)

    return entries


def heading_path_for_offset(offset: int, headings: list[HeadingEntry]) -> Optional[list[str]]:
    """
    Tìm heading gần nhất có offset <= offset truy vấn (bisect trên danh sách
    đã sắp xếp theo offset, vốn tự nhiên tăng dần do parse tuần tự).
    Trả về breadcrumb path của heading đó, hoặc None nếu offset nằm trước
    mọi heading (ví dụ chunk lọt vào phần mở đầu văn bản, hiếm khi xảy ra
    vì phần này thường đã bị loại ở bước chunking).
    """
    offsets = [h.offset for h in headings]
    idx = bisect.bisect_right(offsets, offset) - 1
    if idx < 0:
        return None
    return headings[idx].path


# --------------------------------------------------------------------------- #
# 2. Định vị chunk trong văn bản gốc (anchor-matching + con trỏ đơn điệu)
# --------------------------------------------------------------------------- #

def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _get_anchor(chunk_text: str, min_len: int = 20, max_len: int = 60) -> str:
    """Lấy đoạn đầu chunk làm anchor: ưu tiên dòng đầu tiên có độ dài >= min_len, cắt bớt nếu quá dài."""
    lines = [l.strip() for l in chunk_text.splitlines() if l.strip()]
    if not lines:
        return ""
    for line in lines:
        if len(line) >= min_len:
            return line[:max_len]
    return " ".join(lines)[:max_len]


def calculate_char_iou(s1: str, s2: str) -> float:
    """
    Tính tỷ lệ IoU (Intersection over Union) ký tự giữa hai chuỗi s1 và s2.
    Đã chuẩn hóa khoảng trắng và chuyển thành chữ thường.
    """
    if not s1 or not s2:
        return 0.0
    s1_norm = _normalize_ws(s1).lower()
    s2_norm = _normalize_ws(s2).lower()
    if not s1_norm or not s2_norm:
        return 0.0

    c1 = Counter(s1_norm)
    c2 = Counter(s2_norm)

    intersection = sum((c1 & c2).values())
    union = sum((c1 | c2).values())

    return intersection / union if union > 0 else 0.0


def _find_exact_match(
    chunk_text: str,
    source_text: str,
    search_from: int,
    anchor: str,
) -> tuple[Optional[int], Optional[str]]:
    """Thử exact match của anchor và các dòng ứng viên từ search_from trở đi."""
    if anchor:
        pos = source_text.find(anchor, search_from)
        if pos != -1:
            return pos, "EXACT_ANCHOR"

    lines = [l.strip() for l in chunk_text.splitlines() if len(l.strip()) >= 20]
    for l in lines:
        cand = l[:60]
        pos = source_text.find(cand, search_from)
        if pos != -1:
            return pos, "EXACT_LINE_ANCHOR"
    return None, None


def _extract_candidate_blocks(window: str, search_from: int) -> list[tuple[int, str]]:
    """Tách các đoạn văn và dòng đơn lẻ trong cửa sổ tìm kiếm."""
    blocks: list[tuple[int, str]] = []
    seen_offsets = set()
    para_pattern = re.compile(r'[^\n]+(?:\n[^\n]+)*')
    for m in para_pattern.finditer(window):
        text = m.group(0).strip()
        if text:
            offset = search_from + m.start()
            blocks.append((offset, text))
            seen_offsets.add(offset)

    line_pattern = re.compile(r'[^\n]+')
    for m in line_pattern.finditer(window):
        text = m.group(0).strip()
        if text:
            offset = search_from + m.start()
            if offset not in seen_offsets:
                blocks.append((offset, text))
                seen_offsets.add(offset)
    return blocks


def _find_best_iou_match(
    blocks: list[tuple[int, str]],
    chunk_text: str,
    anchor: str,
    min_iou_threshold: float,
) -> tuple[Optional[int], str]:
    """Tìm block có tỷ lệ IoU ký tự cao nhất."""
    best_offset = None
    best_iou = 0.0

    for offset, block_text in blocks:
        iou_full = calculate_char_iou(chunk_text, block_text)
        iou_anchor = calculate_char_iou(anchor, block_text) if anchor else 0.0
        iou = max(iou_full, iou_anchor)

        if iou > best_iou:
            best_iou = iou
            best_offset = offset

    if best_offset is not None and best_iou >= min_iou_threshold:
        return best_offset, f"IOU_MATCH ({best_iou:.2f})"

    return None, "NOT_FOUND"


def locate_chunk_offset(
    chunk_text: str,
    source_text: str,
    search_from: int,
    min_iou_threshold: float = 0.3,
) -> tuple[Optional[int], str]:
    """
    Định vị vị trí bắt đầu của chunk_text trong source_text từ vị trí search_from trở đi.

    1. Thử Exact Match của anchor chính.
    2. Nếu không khớp, thử exact match các dòng ứng viên khác trong chunk.
    3. Nếu vẫn không khớp, duyệt các đoạn văn (candidate blocks) từ search_from trở đi
       và tìm đoạn văn có tỷ lệ IoU ký tự cao nhất với chunk_text / anchor.
    """
    anchor = _get_anchor(chunk_text)
    if not anchor and not chunk_text:
        return None, "EMPTY_ANCHOR"

    pos, method = _find_exact_match(chunk_text, source_text, search_from, anchor)
    if pos is not None and method is not None:
        return pos, method

    window = source_text[search_from:]
    if not window.strip():
        return None, "NOT_FOUND"

    blocks = _extract_candidate_blocks(window, search_from)
    return _find_best_iou_match(blocks, chunk_text, anchor, min_iou_threshold)


# --------------------------------------------------------------------------- #
# 3. Pipeline chính: gán heading path cho từng chunk
# --------------------------------------------------------------------------- #

@dataclass
class ChunkHeadingResult:
    chunk: str
    heading_path: Optional[list[str]]
    match_offset: Optional[int]
    match_method: str


def assign_headings_to_chunks(
    chunks: list[str],
    source_text: str,
) -> list[ChunkHeadingResult]:
    headings = parse_headings_with_breadcrumbs(source_text)
    results: list[ChunkHeadingResult] = []
    cursor = 0  # con trỏ đơn điệu tăng, giả định chunks đúng thứ tự văn bản gốc

    for chunk in chunks:
        offset, method = locate_chunk_offset(chunk, source_text, search_from=cursor)
        if offset is None:
            results.append(ChunkHeadingResult(
                chunk=chunk, heading_path=None, match_offset=None, match_method=method,
            ))
            # Không advance cursor nếu không tìm thấy, để chunk tiếp theo vẫn
            # được tìm từ vị trí cũ (tránh mất luôn khả năng match các chunk sau).
            continue

        path = heading_path_for_offset(offset, headings)
        results.append(ChunkHeadingResult(
            chunk=chunk, heading_path=path, match_offset=offset, match_method=method,
        ))
        cursor = offset + len(_get_anchor(chunk))  # advance cursor để giữ tính đơn điệu

    return results