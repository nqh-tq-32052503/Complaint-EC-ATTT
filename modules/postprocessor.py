# -*- coding: utf-8 -*-
"""
postprocess_noi_dung.py
------------------------
Hậu xử lý (postprocessing) trường "noi_dung" trong các file JSON kết quả
trích xuất thông tin từ văn bản hành chính (PDF -> LLM raw output).

Chỉ dùng regex (không dùng thêm model/NLP) để loại bỏ các "rác" định dạng
sinh ra do PDF -> Markdown, bao gồm:

  1. Ký tự markdown: **bold**, *italic*, _italic_, #, ##, ### (heading),
     thẻ HTML còn sót: <sup>...</sup> (chú thích chân trang), <u>...</u>
  2. Prefix dạng "Ý kiến số 01:", "Ý kiến thứ 3:"...
  3. Đề mục/số thứ tự: "1.1.", "4.3.", "3.5.1.", "1. ", "(1)", "(2)", "(i)"...
  4. Ký tự bullet đầu dòng: "-", "*", "•", "▪", "●", "○", "◦", "‣", "∙", "·", "+"
  5. Các dòng "nhãn form" đứng riêng một dòng, không mang nội dung thật
     (VD: dòng chỉ có mỗi chữ "Đề nghị", "Kiến nghị", "Đồng thời, đề nghị"...
     -- đây là tiêu đề field của mẫu văn bản, câu nội dung thật nằm ở dòng
     kế tiếp và thường lặp lại đúng động từ đó ở đầu câu).
  6. Chuẩn hoá xuống dòng:
     - Nếu \n (bao gồm \n\n, \n\n\n...) xuất hiện ở giữa câu (trước \n không phải
       dấu chấm câu . ! ? : hoặc sau \n là chữ cái viết thường) thì loại bỏ và thay bằng khoảng trắng.
     - Giữ lại \n\n giữa các đoạn văn tách biệt (giữa 2 câu hoàn chỉnh).
  7. Loại bỏ các ký tự đặc biệt ở đầu văn bản (nếu có), sau đó loại bỏ khoảng trắng ở đầu văn bản.

Cách dùng:
    python postprocess_noi_dung.py <input_dir_or_zip> <output_dir>

Hoặc import hàm clean_noi_dung(text) để dùng trực tiếp trên 1 chuỗi.
"""

import json
import os
import re
import sys
import glob
import shutil
import zipfile
import asyncio
import unicodedata
from typing import Any, Optional, Dict, List
from openai import AsyncOpenAI
from modules.prompts import COMPLAINT_TEXT_REFINEMENT_SYSTEM_PROMPT


# =============================================================================
# 1. CÁC PATTERN REGEX
# =============================================================================

# ---- 1a. Thẻ HTML còn sót lại từ PDF -> markdown ---------------------------
# <u>...</u> là gạch chân -> bỏ thẻ, giữ lại chữ bên trong
UNDERLINE_TAG_RE = re.compile(r"<u>(.*?)</u>", flags=re.IGNORECASE | re.DOTALL)

# ---- 1b. Markdown bold / italic --------------------------------------------
# Xử lý bold TRƯỚC italic vì bold dùng 2 dấu *, nếu làm ngược lại sẽ vỡ cặp.
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", flags=re.DOTALL)
ITALIC_UNDERSCORE_RE = re.compile(r"(?<!_)_([^_\n]+?)_(?!_)", flags=re.DOTALL)

# ---- 1c. Markdown heading ở đầu dòng: #, ##, ### ... -----------------------
HEADING_RE = re.compile(r"^\s*#{1,6}\s*")

# ---- 1d. Prefix "Ý kiến số 01:", "Ý kiến thứ 3:" ---------------------------
Y_KIEN_PREFIX_RE = re.compile(
    r"^[ \t]*ý[ \t]*kiến[ \t]*(?:(?:số|thứ)[ \t]+)?\d+[ \t]*[:.\-]?[ \t]*",
    flags=re.IGNORECASE,
)

# ---- 1e. Ký tự bullet ở đầu dòng -------------------------------------------
BULLET_CHARS = r"\-\*\u2022\u25AA\u25CF\u25CB\u25E6\u2023\u2219\u00B7\+"
BULLET_RE = re.compile(rf"^\s*[{BULLET_CHARS}]\s+")

# ---- 1f. Đề mục số ở đầu dòng: 1.1. / 4.3. / 3.5.1. / 1. -------------------
# Nhiều cấp (1.1., 4.3., 3.5.1.): mỗi cấp tối đa 2 chữ số, dùng (?!\d) để
# tránh nuốt nhầm số có dấu chấm phân cách hàng nghìn kiểu "15.000".
NUMBERING_MULTI_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){1,3}(?!\d)[\.:\-]?\s*")
# Một cấp (1. , 3. ...): bắt buộc có khoảng trắng theo sau để không nuốt
# nhầm số dạng "5.000" (không có khoảng trắng ngay sau dấu chấm).
NUMBERING_SINGLE_RE = re.compile(r"^\s*\d{1,2}\.\s+")

# ---- 1g. Đề mục dạng ngoặc đơn: (1), (2), (i), (ii)... ---------------------
# Đây là marker liệt kê các ý nhỏ NẰM NGAY TRONG một đoạn (không nhất thiết
# ở đầu dòng), nên xử lý bằng sub toàn văn bản thay vì theo từng dòng.
PAREN_NUMBERING_RE = re.compile(r"\(\s*(?:\d{1,2}|[ivxIVX]{1,4})\s*\)")

# ---- 1h. Các dòng "nhãn form" đứng riêng một dòng --------------------------
STANDALONE_LABELS = [
    r"đề nghị",
    r"kiến nghị",
    r"đồng thời,?\s+đề nghị",
    r"bên cạnh đó,?\s+đề nghị",
    r"do đó,?\s+(?:cử tri\s+)?kiến nghị",
]
STANDALONE_LABEL_RE = re.compile(
    r"^\s*(?:%s)(?:\s*:)?$" % "|".join(STANDALONE_LABELS),
    flags=re.IGNORECASE,
)

# ---- 1i. Ký tự markdown lẻ còn sót + khoảng trắng thừa ---------------------
LEFTOVER_MD_RE = re.compile(r"[`*_#]+")
MULTI_SPACE_RE = re.compile(r"[ \t]+")
SPACE_BEFORE_PUNCT_RE = re.compile(r" ([,.;:!?])")

# ---- 1j. Regex loại bỏ \n (dù là \n, \n\n, \n\n\n...) ở giữa câu -----------
VN_LOWERCASE = r"[a-zàáảạãăằắẳặtẵâầấẩậẫèéẻẹẽêềếểệễìíỉịĩòóỏọõôồốổộỗơờớởợỡùúủụũưừứửựữỳýỷỹđ]"
# Ký tự trước \n+ không thuộc dấu kết thúc câu (. ! ? :)
INLINE_NEWLINE_NO_PUNCT_RE = re.compile(r"(?<=[^.!?:\s])\n+[ \t]*")
# Ký tự sau \n+ là chữ cái viết thường
INLINE_NEWLINE_LOWERCASE_RE = re.compile(rf"\n+[ \t]*(?={VN_LOWERCASE})")

# ---- 1k. Ký tự đặc biệt ở đầu text ------------------------------------------
# Loại bỏ các ký tự không phải chữ/số (punctuation, symbol, _) ở đầu chuỗi
LEADING_SPECIAL_CHARS_RE = re.compile(r"^[\W_]+")


# =============================================================================
# 2. HÀM CHÍNH: LÀM SẠCH BẰNG LLM PROMPTING & FALLBACK
# =============================================================================
async def clean_single_complaint_llm(
    text: str,
    client: AsyncOpenAI = None,
    model_name: str = None,
    max_tokens: int = 4096,
) -> str:
    """Làm sạch và định chuẩn 1 nội dung kiến nghị bằng LLM Prompting thay thế toàn bộ regex."""
    if not text or not text.strip():
        return text

    if client is None:
        model_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1")
        model_api_key = os.environ.get("LLM_GATEWAY_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        client = AsyncOpenAI(base_url=model_url, api_key=model_api_key or "empty")
    if model_name is None:
        model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")

    try:
        async with asyncio.timeout(30.0):
            response = await client.chat.completions.create(
                model=model_name,
                temperature=0.0,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": COMPLAINT_TEXT_REFINEMENT_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            )
        content = response.choices[0].message.content
        if "</think>" in content:
            _, content = content.rsplit("</think>", 1)
        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned if cleaned else text
    except Exception as e:
        print(f"[WARN] clean_single_complaint_llm failed: {e}, falling back to rule-based")
        return clean_noi_dung(text)


async def clean_complaints_parallel_llm(
    complaints: list[dict],
    client: AsyncOpenAI = None,
    model_name: str = None,
) -> list[dict]:
    """
    Làm sạch nội dung của toàn bộ danh sách kiến nghị bằng LLM Prompting,
    gọi đồng thời (parallel / concurrent) qua LLM Gateway endpoint.
    """
    if not complaints:
        return complaints

    if client is None:
        model_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1")
        model_api_key = os.environ.get("LLM_GATEWAY_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        client = AsyncOpenAI(base_url=model_url, api_key=model_api_key or "empty")
    if model_name is None:
        model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")

    tasks = []
    for c in complaints:
        raw_text = c.get("content") or c.get("noi_dung") or ""
        tasks.append(clean_single_complaint_llm(raw_text, client, model_name))

    cleaned_texts = await asyncio.gather(*tasks, return_exceptions=True)

    for i, c in enumerate(complaints):
        res = cleaned_texts[i]
        if isinstance(res, Exception):
            print(f"[WARN] Parallel cleaning exception on item {i}: {res}")
            raw_text = c.get("content") or c.get("noi_dung") or ""
            cleaned = clean_noi_dung(raw_text)
        else:
            cleaned = res

        if "content" in c:
            c["content"] = cleaned
        if "noi_dung" in c:
            c["noi_dung"] = cleaned

    return complaints


def clean_noi_dung(text: str) -> str:

    """Làm sạch 1 chuỗi noi_dung: bỏ ký tự/định dạng heading, prefix, đề mục (1.1, #, -...)...

    Loại bỏ ký tự xuống dòng (\n, \n\n...) nếu ở giữa câu, giữ lại \n\n nếu ở giữa các đoạn văn hoàn chỉnh.
    Loại bỏ ký tự đặc biệt ở đầu text nếu có, sau đó loại bỏ khoảng trắng ở đầu text.
    """
    if not text:
        return text

    # 0. Chuẩn hoá xuống dòng
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Bỏ thẻ HTML còn sót (gạch chân)
    text = UNDERLINE_TAG_RE.sub(r"\1", text)

    # 2. Bỏ markdown bold/italic (giữ chữ bên trong), làm bold trước italic
    text = BOLD_RE.sub(r"\1", text)
    text = ITALIC_STAR_RE.sub(r"\1", text)
    text = ITALIC_UNDERSCORE_RE.sub(r"\1", text)

    # 3. Bỏ marker liệt kê dạng ngoặc đơn: (1), (2), (i)... ở bất kỳ đâu (Đã tắt theo yêu cầu)
    # text = PAREN_NUMBERING_RE.sub(" ", text)

    # 4. Loại bỏ ký tự xuống dòng (\n, \n\n, \n\n\n...) ở giữa câu:
    # - Nếu ký tự trước \n+ không thuộc nhóm kết thúc câu (. ! ? :)
    text = INLINE_NEWLINE_NO_PUNCT_RE.sub(" ", text)
    # - Nếu ký tự sau \n+ là chữ cái viết thường
    text = INLINE_NEWLINE_LOWERCASE_RE.sub(" ", text)

    # 5. Xử lý theo từng dòng: heading, prefix "Ý kiến số", bullet, đề mục số
    cleaned_lines = []
    for raw_line in text.split("\n"):
        line = raw_line
        line = HEADING_RE.sub("", line)
        line = Y_KIEN_PREFIX_RE.sub("", line)

        # bullet rồi tới số đề mục có thể lồng nhau ("- 1.1. ...."),
        # nên thử xoá lặp lại vài lần cho chắc.
        for _ in range(2):
            line = BULLET_RE.sub("", line)
            line = NUMBERING_MULTI_RE.sub("", line)
            line = NUMBERING_SINGLE_RE.sub("", line)

        # Dòng chỉ chứa nhãn field của form (VD: "Đề nghị", "Kiến nghị")
        # -> bỏ hẳn cả dòng vì không mang nội dung, chỉ là tiêu đề lặp lại
        if STANDALONE_LABEL_RE.match(line):
            continue

        # Chuẩn hoá khoảng trắng trong từng dòng
        line = LEFTOVER_MD_RE.sub("", line)
        line = MULTI_SPACE_RE.sub(" ", line)
        line = SPACE_BEFORE_PUNCT_RE.sub(r"\1", line)
        cleaned_lines.append(line.strip())

    # 6. Gộp các dòng trong cùng 1 đoạn văn, giữ lại \n\n giữa các đoạn văn
    paragraphs = []
    current_para = []
    for line in cleaned_lines:
        if line:
            current_para.append(line)
        else:
            if current_para:
                para = " ".join(current_para)
                para = MULTI_SPACE_RE.sub(" ", para)
                para = SPACE_BEFORE_PUNCT_RE.sub(r"\1", para)
                paragraphs.append(para)
                current_para = []
    if current_para:
        para = " ".join(current_para)
        para = MULTI_SPACE_RE.sub(" ", para)
        para = SPACE_BEFORE_PUNCT_RE.sub(r"\1", para)
        paragraphs.append(para)

    result = "\n\n".join(paragraphs)

    # 7. Xoá ký tự đặc biệt ở đầu text nếu có, sau đó xoá khoảng trắng ở đầu nếu còn
    result = LEADING_SPECIAL_CHARS_RE.sub("", result)
    result = result.lstrip()

    # 8. Thay thế các cụm từ lặp theo cấu hình PHRASE_REPLACEMENTS
    result = replace_phrases_in_noi_dung(result)

    return result


# =============================================================================
# 3. DICTIONARY CÁC CỤM TỪ CẦN THAY THẾ TRONG NỘI DUNG KIẾN NGHỊ
# =============================================================================

PHRASE_REPLACEMENTS: dict[str, str] = {
    "Đề nghị Đề nghị": "Đề nghị",
    "Kiến nghị Kiến nghị": "Kiến nghị",
}


def replace_phrases_in_noi_dung(text: str, replacements: dict[str, str] = None) -> str:
    """
    Thay thế các cụm từ bị lặp hoặc không mong muốn trong nội dung kiến nghị
    dựa trên dictionary PHRASE_REPLACEMENTS.
    """
    if not text or not isinstance(text, str):
        return text
    if replacements is None:
        replacements = PHRASE_REPLACEMENTS

    for old_phrase, new_phrase in replacements.items():
        while old_phrase in text:
            text = text.replace(old_phrase, new_phrase)
    return text


# =============================================================================
# 4. CHUẨN HOÁ TÊN CƠ QUAN VÀ BỔ SUNG CƠ QUAN MẶC ĐỊNH (FROM x.py)
# =============================================================================

SPECIAL_ORGS = ["Bộ Nội Vụ", "Văn Phòng Chính Phủ"]


def load_cqtq_mapping(cqtq_path: str = None) -> dict[str, str]:
    """
    Đọc danh sách cơ quan thẩm quyền từ CQTQ.txt
    và trả về dict mapping: {tên_lowercase: tên_chuẩn_hóa}
    """
    if cqtq_path is None:
        cqtq_path = os.path.join(os.path.dirname(__file__), "CQTQ.txt")
        if not os.path.exists(cqtq_path):
            cqtq_path = "modules/CQTQ.txt"

    mapping = {}
    if os.path.exists(cqtq_path):
        with open(cqtq_path, "r", encoding="utf-8") as f:
            for line in f:
                name = line.strip()
                if name:
                    mapping[name.lower()] = name
    return mapping


CQTQ_MAPPING = load_cqtq_mapping()


def normalize_agency_name(name: str, cqtq_map: dict[str, str] = None) -> str:
    """
    Chuẩn hóa tên cơ quan thẩm quyền theo danh sách CQTQ.txt bằng cách:
    Lấy tên cơ quan -> lowercase -> so khớp trong CQTQ.txt -> trả về tên sau chuẩn hóa.
    """
    if not name or not isinstance(name, str):
        return name
    if cqtq_map is None:
        cqtq_map = CQTQ_MAPPING

    key = name.strip().lower()
    return cqtq_map.get(key, name)


def _is_contain_org(don_vi_nhan_list: list, special_org: str) -> bool:
    """
    Kiểm tra xem special_org (hoặc phiên bản lowercase của nó) đã có trong don_vi_nhan chưa.
    """
    if not isinstance(don_vi_nhan_list, list):
        return False
    target_lower = special_org.strip().lower()
    for item in don_vi_nhan_list:
        if isinstance(item, dict):
            org_name = item.get("ten_don_vi", "")
        elif isinstance(item, str):
            org_name = item
        else:
            continue
        if org_name and isinstance(org_name, str) and org_name.strip().lower() == target_lower:
            return True
    return False


def _init_don_vi_nhan_from_authority(kien_nghi: dict, cqtq_map: dict[str, str]):
    """Tự động khởi tạo don_vi_nhan từ authority nếu chưa có."""
    auth_raw = kien_nghi["authority"]
    if isinstance(auth_raw, str):
        auth_list = [auth_raw] if auth_raw else []
    elif isinstance(auth_raw, list):
        auth_list = [a for a in auth_raw if isinstance(a, str) and a.strip()]
    else:
        auth_list = []

    phuong_phap = kien_nghi.get("match_method", "llm")
    conf_raw = kien_nghi.get("confidence", "low")
    if isinstance(conf_raw, list):
        conf_val = conf_raw[0] if conf_raw else "low"
    else:
        conf_val = conf_raw if isinstance(conf_raw, str) else "low"

    kien_nghi["don_vi_nhan"] = [
        {
            "ten_don_vi": normalize_agency_name(auth, cqtq_map),
            "muc_do_tin_cay": conf_val if conf_val in ("high", "medium", "low") else "low",
            "phuong_phap": phuong_phap,
            "is_mapped": True if phuong_phap == "rule-based" else False,
        }
        for auth in auth_list
    ]


def _normalize_and_enrich_don_vi_nhan(kien_nghi: dict, cqtq_map: dict[str, str]):
    """Chuẩn hóa tên các cơ quan hiện có và tự động bổ sung SPECIAL_ORGS."""
    for idx, element in enumerate(kien_nghi["don_vi_nhan"]):
        if isinstance(element, dict) and "ten_don_vi" in element:
            element["ten_don_vi"] = normalize_agency_name(element["ten_don_vi"], cqtq_map)
        elif isinstance(element, str):
            normalized = normalize_agency_name(element, cqtq_map)
            kien_nghi["don_vi_nhan"][idx] = {
                "ten_don_vi": normalized,
                "muc_do_tin_cay": "low",
                "phuong_phap": "llm",
                "is_mapped": False,
            }

    for special_org in SPECIAL_ORGS:
        std_special_org = normalize_agency_name(special_org, cqtq_map)
        if not _is_contain_org(kien_nghi["don_vi_nhan"], std_special_org):
            kien_nghi["don_vi_nhan"].append({
                "ten_don_vi": std_special_org,
                "muc_do_tin_cay": "low",
                "phuong_phap": "llm",
                "is_mapped": False
            })

    if "cac_don_vi_nhan" in kien_nghi:
        kien_nghi["cac_don_vi_nhan"] = [
            elem["ten_don_vi"] if isinstance(elem, dict) else elem
            for elem in kien_nghi["don_vi_nhan"]
        ]


def _normalize_and_enrich_authority(kien_nghi: dict, cqtq_map: dict[str, str]):
    """Chuẩn hóa và đồng bộ trường authority kèm SPECIAL_ORGS."""
    if isinstance(kien_nghi["authority"], str):
        auth_list = [normalize_agency_name(kien_nghi["authority"], cqtq_map)]
    elif isinstance(kien_nghi["authority"], list):
        auth_list = [
            normalize_agency_name(item, cqtq_map) if isinstance(item, str) else item
            for item in kien_nghi["authority"]
        ]
    else:
        auth_list = []

    for special_org in SPECIAL_ORGS:
        std_special_org = normalize_agency_name(special_org, cqtq_map)
        target_lower = std_special_org.strip().lower()
        if not any(a.strip().lower() == target_lower for a in auth_list if isinstance(a, str)):
            auth_list.append(std_special_org)

    kien_nghi["authority"] = auth_list


def _postprocess_item_headers_and_content(kien_nghi: dict):
    """Hậu xử lý header và nội dung kiến nghị."""
    if "headers" in kien_nghi:
        kien_nghi["headers"] = postprocess_headers(kien_nghi["headers"])
    if "header" in kien_nghi:
        kien_nghi["header"] = postprocess_headers(kien_nghi["header"])
    if "heading" in kien_nghi and isinstance(kien_nghi["heading"], str):
        kien_nghi["heading"] = clean_header(kien_nghi["heading"])
    if "heading_path" in kien_nghi:
        kien_nghi["heading_path"] = postprocess_headers(kien_nghi["heading_path"])
    if "heading_immediate" in kien_nghi and isinstance(kien_nghi["heading_immediate"], str):
        kien_nghi["heading_immediate"] = clean_header(kien_nghi["heading_immediate"])

    if "content" in kien_nghi and isinstance(kien_nghi["content"], str):
        kien_nghi["content"] = replace_phrases_in_noi_dung(kien_nghi["content"])
    if "noi_dung" in kien_nghi and isinstance(kien_nghi["noi_dung"], str):
        kien_nghi["noi_dung"] = replace_phrases_in_noi_dung(kien_nghi["noi_dung"])


def postprocess_kien_nghi_item(kien_nghi: dict, cqtq_map: dict[str, str] = None) -> dict:
    """
    Hậu xử lý 1 đơn vị kiến nghị (1 item dict):
    1) Chuẩn hóa tên các cơ quan trong don_vi_nhan / authority theo CQTQ.txt (lowercase -> match -> chuẩn hóa)
    2) Tự động bổ sung "Bộ Nội Vụ" và "Văn Phòng Chính Phủ" nếu chưa có
    """
    if cqtq_map is None:
        cqtq_map = CQTQ_MAPPING

    # 1. Nếu chưa có don_vi_nhan nhưng có authority, tự động khởi tạo don_vi_nhan từ authority
    if ("don_vi_nhan" not in kien_nghi or not kien_nghi["don_vi_nhan"]) and "authority" in kien_nghi:
        _init_don_vi_nhan_from_authority(kien_nghi, cqtq_map)

    # 2. Chuẩn hóa tên các cơ quan hiện có trong don_vi_nhan
    if "don_vi_nhan" in kien_nghi and isinstance(kien_nghi["don_vi_nhan"], list):
        _normalize_and_enrich_don_vi_nhan(kien_nghi, cqtq_map)

    # 3. Chuẩn hóa và đồng bộ trường authority nếu có
    if "authority" in kien_nghi:
        _normalize_and_enrich_authority(kien_nghi, cqtq_map)

    # 4. Hậu xử lý headers và nội dung
    _postprocess_item_headers_and_content(kien_nghi)

    return kien_nghi


def clean_header(header: str) -> str:
    """
    Làm sạch 1 chuỗi header: loại bỏ ký tự markdown '*' và chuẩn hóa khoảng trắng thừa.
    """
    if not header or not isinstance(header, str):
        return header
    cleaned = header.replace("*", "").strip()
    cleaned = MULTI_SPACE_RE.sub(" ", cleaned)
    return cleaned


def postprocess_headers(headers: list | str) -> list | str:
    """
    Hậu xử lý trường header/headers: nếu đoạn text chứa ký tự '*' (ký tự của markdown) thì replace nó đi.
    Hỗ trợ xử lý cả chuỗi đơn và danh sách chuỗi các header.
    """
    if isinstance(headers, list):
        return [clean_header(h) if isinstance(h, str) else h for h in headers]
    elif isinstance(headers, str):
        return clean_header(headers)
    return headers


# Alias để tương thích cả dạng số ít/số nhiều
postprocess_header = postprocess_headers


def add_numbering_to_complaints(complaints: list) -> list:
    """
    Đánh số thứ tự từ 1 đến hết cho từng kiến nghị theo format: <number>. <nội dung kiến nghị>
    Ví dụ: kiến nghị 1 có nội dung là 'A' -> chuyển thành '1. A'
    """
    return complaints


# =============================================================================
# 5. CƠ CHẾ LỌC BỎ KIẾN NGHỊ CHUNG / TÌNH HÌNH CHUNG DỰA TRÊN HEADERS
# =============================================================================

EXCLUDED_HEADER_PHRASES = ["kien nghi chung", "tinh hinh chung"]


def remove_accents(text: str) -> str:
    """Loại bỏ dấu tiếng Việt và chuẩn hóa ký tự đ/Đ thành d/D."""
    if not text or not isinstance(text, str):
        return ""
    nfd = unicodedata.normalize("NFD", text)
    without_marks = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return without_marks.replace("đ", "d").replace("Đ", "D")


def should_exclude_by_headers(headers: list | str | None) -> bool:
    """
    Kiểm tra xem headers có chứa cụm từ loại trừ hay không.
    Nếu trong headers có bất kỳ phần tử nào mà khi bỏ dấu và lowercase chứa
    'kien nghi chung' hoặc 'tinh hinh chung' thì trả về True (loại bỏ khỏi final response).
    """
    if not headers:
        return False
    header_list = headers if isinstance(headers, list) else [headers]
    for h in header_list:
        if not h or not isinstance(h, str):
            continue
        h_norm = remove_accents(h).lower()
        h_norm = re.sub(r"\s+", " ", h_norm).strip()
        for phrase in EXCLUDED_HEADER_PHRASES:
            if phrase in h_norm:
                return True
    return False


def _resolve_primary_agency(item: dict) -> str:
    """Xác định cơ quan thẩm quyền chính của kiến nghị."""
    default_agency = "Văn Phòng Chính Phủ"
    don_vi_nhan = item.get("don_vi_nhan")
    if isinstance(don_vi_nhan, list) and don_vi_nhan:
        first = don_vi_nhan[0]
        if isinstance(first, dict):
            return first.get("ten_don_vi") or default_agency
        if isinstance(first, str):
            return first
    auth = item.get("authority")
    if isinstance(auth, list) and auth:
        return str(auth[0])
    if isinstance(auth, str) and auth:
        return auth
    return default_agency


def filter_and_reindex_kien_nghi(kien_nghi_list: list) -> list:
    """
    Lọc bỏ các phần tử kiến nghị có header chứa 'kien nghi chung' hoặc 'tinh hinh chung'.
    Đồng thời đánh số lại:
      - stt: tuần tự từ 1 đến N (không có khoảng trống).
      - so_thu_tu_kien_nghi: đánh lại số thứ tự tuần tự từ 1..k trong từng cơ quan thẩm quyền chính.
    """
    if not isinstance(kien_nghi_list, list):
        return kien_nghi_list

    filtered = []
    agency_counts = {}

    for item in kien_nghi_list:
        if not isinstance(item, dict):
            continue
        headers = item.get("headers") or item.get("header") or item.get("heading") or item.get("heading_path") or []
        if should_exclude_by_headers(headers):
            continue

        primary_agency = _resolve_primary_agency(item)
        agency_counts[primary_agency] = agency_counts.get(primary_agency, 0) + 1

        item_copy = dict(item)
        item_copy["stt"] = len(filtered) + 1
        item_copy["so_thu_tu_kien_nghi"] = agency_counts[primary_agency]
        filtered.append(item_copy)

    return filtered


def _extract_org_from_elem(elem: Any) -> Optional[str]:
    """Trích xuất tên cơ quan từ phần tử đơn lẻ."""
    if isinstance(elem, dict):
        return elem.get("ten_don_vi")
    return str(elem) if elem else None


def _collect_unique_orgs(kien_nghi_list: list) -> list[str]:
    """Thu thập danh sách tên cơ quan không trùng lặp từ danh sách kiến nghị."""
    seen = set()
    all_orgs = []
    for item in kien_nghi_list:
        if not (isinstance(item, dict) and "don_vi_nhan" in item):
            continue
        for elem in item["don_vi_nhan"]:
            name = _extract_org_from_elem(elem)
            if name and name not in seen:
                seen.add(name)
                all_orgs.append(name)
    return all_orgs


def _postprocess_full_report_dict(results: dict, cqtq_map: dict[str, str]) -> dict:
    """Hậu xử lý dictionary báo cáo đầy đủ chứa mảng kien_nghi."""
    processed_items = [
        postprocess_kien_nghi_item(item, cqtq_map) if isinstance(item, dict) else item
        for item in results["kien_nghi"]
    ]
    filtered_items = filter_and_reindex_kien_nghi(processed_items)
    results["kien_nghi"] = add_numbering_to_complaints(filtered_items)
    results["tong_so_kien_nghi"] = len(results["kien_nghi"])
    results["cac_don_vi_nhan"] = _collect_unique_orgs(results["kien_nghi"])

    if "headers" in results:
        results["headers"] = postprocess_headers(results["headers"])
    if "header" in results:
        results["header"] = postprocess_headers(results["header"])
    return results


def _postprocess_single_dict_item(results: dict, cqtq_map: dict[str, str]) -> dict:
    """Hậu xử lý 1 phần tử kiến nghị dict đơn lẻ."""
    headers = results.get("headers") or results.get("header") or results.get("heading") or []
    if should_exclude_by_headers(headers):
        return {}
    processed = postprocess_kien_nghi_item(results, cqtq_map)
    numbered = add_numbering_to_complaints([processed])
    return numbered[0] if numbered else {}


def _postprocess_results_list(results: list, cqtq_map: dict[str, str]) -> list:
    """Hậu xử lý danh sách các kiến nghị dạng list."""
    filtered_list = [
        item for item in results
        if isinstance(item, dict) and not should_exclude_by_headers(
            item.get("headers") or item.get("header") or item.get("heading") or item.get("heading_path")
        )
    ]
    for i, item in enumerate(filtered_list):
        if isinstance(item, dict):
            filtered_list[i] = postprocess_kien_nghi_item(item, cqtq_map)
    return add_numbering_to_complaints(filtered_list)


def postprocess_results(results: dict | list, cqtq_path: str = None) -> dict | list:
    """
    Hàm hậu xử lý kết quả chính (dành cho dict JSON, list các kiến nghị, hoặc 1 kiến nghị dict):
    1) Tự động bổ sung "Bộ Nội Vụ" và "Văn Phòng Chính Phủ" nếu chưa có trong kết quả.
    2) Chuẩn hóa lại tên các cơ quan theo danh sách trong CQTQ.txt (lowercase -> match CQTQ.txt -> trả về tên sau chuẩn hóa).
    3) Đánh số thứ tự từ 1 đến hết cho từng kiến nghị (<number>. <nội dung kiến nghị>).
    4) Hậu xử lý header (headers/header/heading): loại bỏ ký tự markdown '*' nếu có.
    5) Hậu xử lý nội dung kiến nghị: thay thế các cụm từ lặp theo dictionary PHRASE_REPLACEMENTS (ví dụ: "Đề nghị Đề nghị" -> "Đề nghị", "Kiến nghị Kiến nghị" -> "Kiến nghị").
    6) Lọc bỏ các phần tử có header chứa 'kien nghi chung' hoặc 'tinh hinh chung' (khi bỏ dấu, lowercase), đồng thời đánh số lại STT.
    """
    cqtq_map = load_cqtq_mapping(cqtq_path) if cqtq_path else CQTQ_MAPPING

    if isinstance(results, dict):
        if "kien_nghi" in results and isinstance(results["kien_nghi"], list):
            return _postprocess_full_report_dict(results, cqtq_map)
        item_keys = ("don_vi_nhan", "authority", "content", "noi_dung", "headers", "header", "heading")
        if any(k in results for k in item_keys):
            return _postprocess_single_dict_item(results, cqtq_map)
    elif isinstance(results, list):
        return _postprocess_results_list(results, cqtq_map)

    return results
