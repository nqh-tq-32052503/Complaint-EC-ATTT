from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field


from modules.minio_utils import write_to_minio, write_json_to_minio

def load_txt(path):
    with open(path, "r", encoding="utf-8") as f:
        data = f.read()
        return data

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        return data 

def load_markdown(path):
    with open(path, "r", encoding="utf-8") as f:
        data = f.read()
        return data

def write_markdown(path, content):
    write_to_minio(path, content, content_type="text/markdown; charset=utf-8")

def write_txt(path, content):
    write_to_minio(path, content, content_type="text/plain; charset=utf-8")

def write_json(path, content):
    write_json_to_minio(path, content)

# --------------------------------------------------------------------------- #
# 1. Atomic unit segmentation (rule-based, over-segment theo thiết kế)
# --------------------------------------------------------------------------- #

HEADER_RE = re.compile(r"^#{1,6}\s")
BULLET_RE = re.compile(r"^[-+]\s")
CU_TRI_RE = re.compile(r"^\*[- ]?[Cc]ử\s+tri.*?\*\s*:")
BOLD_NUM_RE = re.compile(r"^\*\*\d+(\.\d+)*\.\*\*")
INLINE_NUM_RE = re.compile(r"^\(([ivxlcdm]+|\d+|[a-z])\)\s", re.IGNORECASE)
NEW_PETITION_CUE_RE = re.compile(
    r"(?<=[.;])\s+(?=(?:Đề nghị|Kiến nghị|Đồng thời[, ]+đề nghị|"
    r"Ngoài ra,?\s*đề nghị|Bên cạnh đó,?\s*đề nghị)\b)"
)


@dataclass
class Unit:
    id: str
    text: str
    index: int


def _split_paragraph_into_units(paragraph: str) -> list[str]:
    """
    Tách một đoạn văn (đã tách theo blank-line) thành các unit nhỏ hơn dựa
    trên các cue "khởi đầu kiến nghị mới" nằm NGAY TRONG đoạn văn (trường hợp
    nhiều kiến nghị bị viết liền không xuống dòng, như mô tả ở sample 2).
    """
    # Tách theo cue "Đề nghị/Kiến nghị..." xuất hiện sau dấu câu kết thúc câu trước (non-capturing group).
    parts = NEW_PETITION_CUE_RE.split(paragraph)
    return [p.strip() for p in parts if p.strip()]


def stitch_broken_lines(lines: list[str]) -> list[str]:
    """
    Nối các dòng bị ngắt giữa chừng do OCR sang trang hoặc wrap dòng.
    Nếu dòng trước chưa kết thúc bằng dấu chấm câu (. : ! ?) và dòng sau
    không phải là một tiêu đề (#, I., II., 1. Đối với...) thì ghép lại liền mạch.
    """
    stitched: list[str] = []
    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
        if not stitched:
            stitched.append(line_str)
            continue
        prev = stitched[-1]
        is_heading = bool(
            HEADER_RE.match(line_str)
            or line_str.startswith(("I.", "II.", "III.", "IV.", "V."))
            or (len(line_str) > 3 and line_str[:2] in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.") and line_str[2] in (".", " "))
            or line_str.startswith(("- ", "+ ", "* "))
        )
        prev_ends_punc = prev.endswith((".", ":", "!", "?", "..."))
        if not prev_ends_punc and not is_heading:
            stitched[-1] = prev + " " + line_str
        else:
            stitched.append(line_str)
    return stitched


def segment_units_simple(source_text: str) -> list[Unit]:
    # Nối các dòng bị ngắt ngang qua trang trước khi tách unit
    raw_lines = source_text.strip().split("\n")
    stitched_lines = stitch_broken_lines(raw_lines)
    units: list[Unit] = []
    counter = 0
    idx = 0

    for para in stitched_lines:
        para = para.strip()
        if not para:
            idx += 1
            continue

        # Kiểm tra nếu đoạn là Header
        if HEADER_RE.match(para):
            counter += 1
            units.append(Unit(id=f"U{counter}", text=para, index=idx))
            idx += 1
            continue

        # Tách tiếp nếu đoạn văn có nhiều kiến nghị độc lập viết liền
        sub_parts = _split_paragraph_into_units(para)
        for sp in sub_parts:
            sp = sp.strip()
            if not sp:
                continue
            counter += 1
            units.append(Unit(id=f"U{counter}", text=sp, index=idx))
            idx += 1

    return units


def segment_units(source_text: str) -> list[Unit]:
    raw_paragraphs = re.split(r"\n\s*\n+", source_text.strip())
    units: list[Unit] = []
    counter = 0
    idx = 0

    for para in raw_paragraphs:
        para = para.strip("\n")
        if not para.strip():
            idx += 1
            continue

        stripped = para.strip()

        if HEADER_RE.match(stripped):
            # Heading luôn là 1 unit riêng (sẽ bị loại ở bước LLM vì không được tham chiếu).
            counter += 1
            units.append(Unit(id=f"U{counter}", text=para, index=idx))
            idx += 1
            continue

        # Nếu đoạn văn chứa nhiều bullet / bold-numbering / cue khởi đầu kiến
        # nghị mới ở giữa, tách nhỏ hơn nữa trước khi coi là 1 unit.
        sub_parts = _split_paragraph_into_units(para)
        for sp in sub_parts:
            sp = sp.strip()
            if not sp:
                continue
            counter += 1
            units.append(Unit(id=f"U{counter}", text=sp, index=idx))
            idx += 1

    return units

def strip_code_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```$", text)
    return m.group(1).strip() if m else text


# --------------------------------------------------------------------------- #
# 4. Validate referential integrity + resolve chunk text
# --------------------------------------------------------------------------- #

@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    discarded_ids: list[str] = field(default_factory=list)


def _validate_single_group(
    group: list[str],
    units_by_id: dict[str, Unit],
    id_position: dict[str, int],
    seen: set[str],
    report: ValidationReport,
) -> Optional[list[str]]:
    """Kiểm tra tính hợp lệ và sắp xếp lại một nhóm unit."""
    if not isinstance(group, list) or not group:
        report.errors.append(f"Nhóm không hợp lệ: {group}")
        return None

    valid_group = []
    for ref_id in group:
        if ref_id not in units_by_id:
            report.errors.append(f"ID không tồn tại, bị loại bỏ: {ref_id}")
            continue
        if ref_id in seen:
            report.errors.append(f"ID {ref_id} bị tham chiếu >1 lần, chỉ giữ lần đầu.")
            continue
        seen.add(ref_id)
        valid_group.append(ref_id)

    if not valid_group:
        return None

    sorted_group = sorted(valid_group, key=lambda i: id_position[i])
    if sorted_group != valid_group:
        report.warnings.append(
            f"Nhóm {valid_group} không đúng thứ tự gốc, đã sắp xếp lại: {sorted_group}."
        )

    positions = [id_position[i] for i in sorted_group]
    if positions and (max(positions) - min(positions) + 1) != len(positions):
        report.warnings.append(
            f"Nhóm {sorted_group} không liên tục trong văn bản gốc — "
            f"khi ghép sẽ dùng separator mặc định thay vì khoảng trắng gốc chính xác."
        )

    return sorted_group


def validate_and_resolve(
    llm_output_raw: str,
    units_by_id: dict[str, Unit],
    all_ids_ordered: list[str],
) -> tuple[list[list[str]], ValidationReport]:
    report = ValidationReport()
    cleaned = strip_code_fence(llm_output_raw)
    try:
        raw_groups = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM output không phải JSON hợp lệ: {e}\nRaw: {llm_output_raw}") from e

    if not isinstance(raw_groups, list):
        raise ValueError("Output phải là JSON array of arrays.")

    seen: set[str] = set()
    validated: list[list[str]] = []
    id_position = {uid: i for i, uid in enumerate(all_ids_ordered)}

    for group in raw_groups:
        res = _validate_single_group(group, units_by_id, id_position, seen, report)
        if res is not None:
            validated.append(res)

    report.discarded_ids = [i for i in all_ids_ordered if i not in seen]
    return validated, report


def resolve_chunk_text(group: list[str], units_by_id: dict[str, Unit]) -> str:
    """
    Ghép nối text của các unit trong 1 nhóm, tra cứu trực tiếp từ dict.
    Nếu nhóm liên tục trong all_ids_ordered, join bằng '\\n\\n' (xấp xỉ khoảng
    trắng gốc); nếu không liên tục (có unit bị discard xen giữa), vẫn join
    bằng '\\n\\n' nhưng đã được cảnh báo ở bước validate.
    """
    return "\n\n".join(units_by_id[i].text for i in group)