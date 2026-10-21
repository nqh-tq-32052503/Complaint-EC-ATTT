# -*- coding: utf-8 -*-
"""
modules/tree_parser.py
-----------------------
Parser cấu trúc cây Canonical OCR (v1.0.0):
1. Cắt tỉa cây: loại bỏ footnotes, page_headers, page_footers.
2. Tách vùng Metadata (Header & Footer) cho MetadataExtractor.
3. Bóc tách và nhóm các đoạn văn/mục danh sách thân bài thành các Candidate Units
   dựa trên tiêu đề Section (Breadcrumbs) và Regex Boundary Markers (số thứ tự, bullet...).
"""

import re
from typing import List, Dict, Any, Tuple, Optional
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Regex nhận diện ranh giới bắt đầu một kiến nghị mới
# ---------------------------------------------------------------------------
BOUNDARY_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,2}(?:\.\d{1,2})*[\.:\)\-]?"                     # 1., 1.1., 2.1, 1), 1:
    r"|\([0-9a-zA-ZđĐivxIVX]+(?:\.[0-9]+)*\)[\.:]?"        # (1), (2), (i), (1).
    r"|\[[0-9a-zA-ZđĐivxIVX]+\][\.:]?"                     # [1], [2]
    r"|[a-zA-ZđĐ][\.\)]"                                  # a), b), a., b.
    r"|[-+*•▪●○◦‣∙·]"                                     # Bullets: -, +, *, •...
    r"|(?:ý\s*kiến|kiến\s*nghị|nội\s*dung)\s*(?:số|thứ)?\s*\d+[\.:\-]?"
    r")\s+",
    re.IGNORECASE | re.UNICODE
)

COMPLAINT_KEYWORD_RE = re.compile(
    r"cử tri (?:(?:kiến|đề) nghị|phản ánh)|kiến nghị cử tri|(?:kiến|đề) nghị (?:chính phủ|bộ)",
    re.IGNORECASE,
)

RESOLUTION_START_RE = re.compile(
    r"^\s*(?:cử tri (?:kiến|đề) nghị|(?:kiến|đề) nghị (?:chính phủ|bộ|quốc hội|:))",
    re.IGNORECASE,
)

PROBLEM_START_RE = re.compile(
    r"(?:cử\s*tri\s+phản\s*ánh|phản\s*ánh\s*:)",
    re.IGNORECASE | re.UNICODE
)

FOOTER_TRIGGER_RE = re.compile(
    r"^\s*(?:nơi nhận:?|trên đây là (?:báo cáo|tổng hợp)|(?:tm|kt)\.\s*(?:đoàn đại biểu quốc hội|trưởng đoàn))",
    re.IGNORECASE,
)

BODY_SECTION_TRIGGER_RE = re.compile(
    r"^\s*(?:[IVXLCDM0-9]+[\.\s]+(?:ý kiến|tình hình|nội dung|kiến nghị|lĩnh vực|đối với)|kiến nghị cụ thể|nội dung kiến nghị)",
    re.IGNORECASE,
)


@dataclass
class CandidateUnit:
    candidate_id: str
    texts: List[str]
    content: str
    breadcrumbs: List[str]
    suggested_authority: Optional[str] = None


def _is_artifact_node(child: dict) -> bool:
    ntype = str(child.get("node_type", "")).lower()
    nid = str(child.get("node_id", "")).lower()
    return ntype in ("footnote", "page_header", "page_footer") or nid.startswith("footnote")


def _filter_node(node: dict):
    if not isinstance(node, dict):
        return
    children = node.get("children")
    if not isinstance(children, list):
        return
    filtered = []
    for ch in children:
        if isinstance(ch, dict):
            if _is_artifact_node(ch):
                continue
            _filter_node(ch)
        filtered.append(ch)
    node["children"] = filtered


class TreeParser(object):
    """
    Parser duyệt cây phân cấp Canonical OCR:
    - Loại bỏ node rác (footnote, header/footer artifacts).
    - Phân tách vùng Metadata Header, Body, và Metadata Footer.
    - Nhóm các đoạn văn của thân bài thành các CandidateUnit có breadcrumbs đầy đủ.
    """

    def __init__(self):
        """Khởi tạo TreeParser (stateless utility)."""
        pass

    @staticmethod
    def clean_heading_text(heading: str) -> str:
        if not heading:
            return ""
        h = str(heading).strip()
        h = re.sub(r"^#{1,6}\s*", "", h)
        return h.strip()

    def remove_footnotes_and_artifacts(self, canonical_data: dict) -> dict:
        """Loại bỏ các node footnote và page artifacts ra khỏi cây logic."""
        if not isinstance(canonical_data, dict):
            return canonical_data

        tree = canonical_data.get("tree")
        if isinstance(tree, dict):
            _filter_node(tree)

        return canonical_data

    def parse_document_tree(
        self, canonical_data: dict
    ) -> Tuple[str, List[CandidateUnit], bool]:
        """
        Duyệt cây Canonical OCR và phân rã thành:
        - metadata_text: chuỗi text chứa thông tin hành chính (header + footer)
        - candidate_units: danh sách CandidateUnit của phần thân bài
        - has_table: boolean nếu tài liệu chứa bảng biểu
        """
        if not isinstance(canonical_data, dict):
            return "", [], False

        tables = canonical_data.get("tables", {})
        has_table = bool(tables and len(tables) > 0)

        canonical_data = self.remove_footnotes_and_artifacts(canonical_data)
        tree = canonical_data.get("tree", {})

        raw_items = _extract_raw_items_from_tree(tree)
        header_texts, body_items, footer_texts = _split_sections(raw_items)

        header_part = "\n".join(header_texts)
        footer_part = "\n".join(footer_texts)
        metadata_text = f"{header_part}\n\n---\n{footer_part}".strip()

        candidate_units = _group_candidate_units(body_items)
        return metadata_text, candidate_units, has_table


def _extract_raw_items_from_tree(tree: dict) -> List[dict]:
    """Thu thập toàn bộ các đoạn văn / list item theo thứ tự đọc kèm breadcrumbs."""
    raw_items = []

    def _walk(node: dict, path: List[str]):
        ntype = node.get("node_type")
        current_path = list(path)
        if ntype == "section":
            h_obj = node.get("heading", {})
            h_text = TreeParser.clean_heading_text(h_obj.get("text", ""))
            if h_text:
                current_path.append(h_text)
        elif ntype in ("paragraph", "list_item"):
            txt = str(node.get("text", "")).strip()
            if txt:
                raw_items.append({
                    "node_id": node.get("node_id"),
                    "node_type": ntype,
                    "text": txt,
                    "path": current_path,
                })

        for child in node.get("children", []):
            if isinstance(child, dict):
                _walk(child, current_path)

    _walk(tree, [])
    return raw_items


def _split_sections(raw_items: List[dict]) -> Tuple[List[str], List[dict], List[str]]:
    """Phân tách Metadata Header, Body Items, và Metadata Footer."""
    header_texts: List[str] = []
    body_items: List[dict] = []
    footer_texts: List[str] = []

    in_body = False
    in_footer = False

    for item in raw_items:
        txt = item["text"]
        path = item["path"]
        path_str = " > ".join(path)

        if not in_footer and (FOOTER_TRIGGER_RE.search(txt) or ("nơi nhận" in path_str.lower())):
            in_footer = True

        if in_footer:
            footer_texts.append(txt)
            continue

        if not in_body:
            is_body_trigger = (
                any(BODY_SECTION_TRIGGER_RE.search(p) for p in path)
                or BOUNDARY_RE.search(txt)
                or COMPLAINT_KEYWORD_RE.search(txt)
            )
            if is_body_trigger:
                in_body = True
            else:
                header_texts.append(txt)
                continue

        body_items.append(item)

    return header_texts, body_items, footer_texts


def _is_new_candidate_unit(current_unit: Optional[dict], txt: str, path: List[str]) -> bool:
    """Xác định xem item hiện tại có bắt đầu một CandidateUnit mới hay không."""
    if current_unit is None:
        return True
    if current_unit["path"] != path and (path or current_unit["path"]):
        return True
    if BOUNDARY_RE.search(txt):
        return True
    if RESOLUTION_START_RE.search(txt):
        prev_text = " ".join(current_unit["texts"])
        if not PROBLEM_START_RE.search(prev_text):
            return True
    return False


def _group_candidate_units(body_items: List[dict]) -> List[CandidateUnit]:
    """Phân đoạn Body Items thành các Candidate Units."""
    candidate_units: List[CandidateUnit] = []
    current_unit: Optional[dict] = None

    for item in body_items:
        txt = item["text"]
        path = item["path"]

        if _is_new_candidate_unit(current_unit, txt, path):
            if current_unit:
                candidate_units.append(
                    CandidateUnit(
                        candidate_id=f"unit_{len(candidate_units) + 1}",
                        texts=current_unit["texts"],
                        content="\n".join(current_unit["texts"]),
                        breadcrumbs=current_unit["path"],
                    )
                )
            current_unit = {"path": path, "texts": [txt]}
        else:
            current_unit["texts"].append(txt)

    if current_unit:
        candidate_units.append(
            CandidateUnit(
                candidate_id=f"unit_{len(candidate_units) + 1}",
                texts=current_unit["texts"],
                content="\n".join(current_unit["texts"]),
                breadcrumbs=current_unit["path"],
            )
        )

    return candidate_units
