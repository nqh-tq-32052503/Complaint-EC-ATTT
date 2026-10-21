from __future__ import annotations
import os
import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
import pandas as pd
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
from modules.prompts import (
    get_fully_criteria_system_prompt,
    GENERALIZED_TREE_COMPLAINT_EXTRACTION_AND_CLASSIFICATION_PROMPT,
)
from modules.postprocessor import clean_noi_dung, clean_complaints_parallel_llm
from datetime import datetime, timezone, timedelta

def get_gmt7_date_string():
    gmt7 = timezone(timedelta(hours=7))
    now_gmt7 = datetime.now(timezone.utc).astimezone(gmt7)
    time_string = now_gmt7.strftime("%y %m %d")
    return time_string

END_THINK_TAG = "</think>"


class LLMResponseValidationError(Exception):
    """Raised khi response không đúng cấu trúc mong đợi."""
    pass


VALID_CONFIDENCE = {"high", "medium", "low"}
REQUIRED_ITEM_KEYS = {"authority", "confidence", "detail"}


def extract_json_from_raw(raw_response: str) -> str:
    """
    Loại bỏ markdown code fence (```json ... ``` hoặc ``` ... ```)
    nếu có, trả về chuỗi JSON thuần.
    """
    text = raw_response.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            body = text[first_newline + 1:]
            last_fence = body.rfind("```")
            if last_fence != -1:
                return body[:last_fence].strip()
    return text


def validate_item(item: Dict[str, Any], index: int) -> None:
    """Validate từng phần tử trong result list."""
    if not isinstance(item, dict):
        raise LLMResponseValidationError(f"Item {index} không phải dict.")

    missing_keys = REQUIRED_ITEM_KEYS - item.keys()
    if missing_keys:
        raise LLMResponseValidationError(
            f"Item {index} thiếu key: {missing_keys}"
        )

    if not isinstance(item["authority"], str) or not item["authority"].strip():
        raise LLMResponseValidationError(
            f"Item {index}: 'authority' phải là string không rỗng."
        )

    if item["confidence"] not in VALID_CONFIDENCE:
        raise LLMResponseValidationError(
            f"Item {index}: 'confidence' phải thuộc {VALID_CONFIDENCE}, "
            f"nhận được '{item['confidence']}'."
        )


def parse_and_validate_llm_response(raw_response: str) -> Dict[str, Any]:
    """
    Parse raw response từ LLM thành dict và validate theo cấu trúc:
    {
        "result": [
            {
                "authority": str,
                "confidence": "high" | "medium" | "low",
                "detail": {
                    "keywords": str,
                    "objects": str,
                    "context": str
                }
            },
            ...
        ]
    }

    Raises:
        LLMResponseValidationError: nếu JSON không parse được hoặc sai cấu trúc.
    """
    json_str = extract_json_from_raw(raw_response)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise LLMResponseValidationError(f"Không parse được JSON: {e}") from e

    if not isinstance(data, dict) or "result" not in data:
        raise LLMResponseValidationError("Response thiếu key 'result' ở top-level.")

    result = data["result"]
    if not isinstance(result, list):
        raise LLMResponseValidationError("'result' phải là một list.")

    for idx, item in enumerate(result):
        validate_item(item, idx)

    return data
 
def _init_valid_authorities():
    possible_paths = [
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "DS_KN_TRA_CUU_1784690544201 - DanhSach.csv")),
        "DS_KN_TRA_CUU_1784690544201 - DanhSach.csv",
    ]
    csv_path = None
    for p in possible_paths:
        if os.path.exists(p):
            csv_path = p
            break
    if not csv_path:
        return []
    df = pd.read_csv(csv_path)
    list_valid_authories = df["Thẩm quyền giải quyết"].to_list()
    list_valid_authories = list(set(list_valid_authories))
    return list_valid_authories

VALID_AUTHORITIES = _init_valid_authorities()
FALLBACK_LABEL = "Văn Phòng Chính Phủ"


def append_to_json(file_path, new_record):
    if file_path:
        from modules.minio_utils import append_json_to_minio
        append_json_to_minio(file_path, new_record)

class ComplaintClassifier(object):
    def __init__(self):
        self.model_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1")
        self.model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")
        self.model_api_key = os.environ.get("LLM_GATEWAY_API_KEY", os.environ.get("OPENAI_API_KEY", ""))    
        self.client = AsyncOpenAI(base_url=self.model_url, api_key=self.model_api_key or "empty")
        self.semaphore = asyncio.Semaphore(32)
        self.fuzzy_classifier = FuzzyClassifier()
        self.system_prompt = get_fully_criteria_system_prompt()
        
    async def _classify_one(
        self,
        content: str,
        max_retries: int = 3,
        max_tokens: int = 16384,
        log_dir: str = ""
    ):
        async with self.semaphore:
            for attempt in range(1, max_retries + 1):
                try:
                    response = await self.client.chat.completions.create(
                        model=self.model_name,
                        temperature=0.0,
                        max_tokens=max_tokens,
                        messages=[
                            {"role": "system", "content": self.system_prompt},
                            {"role": "user", "content": content},
                        ],
                        timeout=30.0
                    )
                    raw_output = response.choices[0].message.content or ""
                    if log_dir:
                        from modules.minio_utils import append_txt_to_minio
                        append_txt_to_minio(log_dir + "/3_raw_response.txt", raw_output + "\n\n\n")
                    if END_THINK_TAG in raw_output:
                        _, final_answer = raw_output.rsplit(END_THINK_TAG, 1)
                    else:
                        final_answer = raw_output
                    final_answer = final_answer.strip()
                    result = parse_and_validate_llm_response(final_answer)["result"]
                    priority = {
                        "high": 1,
                        "medium": 2,
                        "low": 3
                    }
                    result = sorted(result, key=lambda x: priority.get(x.get("confidence", "").lower(), 99))
                    return result

                except Exception:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue

            return [{"authority": FALLBACK_LABEL, "confidence": "low", "detail": {}}]

    async def classify_text(self, text: str, max_tokens: int = 16384, max_retries: int = 3, log_dir: str = "") -> list[dict]:
        """
        Nhận input là nội dung văn bản của một phản ánh kiến nghị,
        output là danh sách các đề xuất cơ quan thẩm quyền giải quyết (kèm confidence và detail).
        """
        return await self._classify_one(
            content=text,
            max_retries=max_retries,
            max_tokens=max_tokens,
            log_dir=log_dir,
        )

    @staticmethod
    def _parse_single_complaint_result(res: Any) -> tuple[list[str], list[str]]:
        authorities = []
        confidences = []
        if res:
            for r in res:
                conf = r.get("confidence", "high")
                raw_auth = r.get("authority")
                auth = raw_auth if isinstance(raw_auth, str) else ""
                authorities.append(auth if auth else FALLBACK_LABEL)
                confidences.append(conf)
        if not authorities:
            authorities = [FALLBACK_LABEL]
            confidences = ["low"]
        return authorities, confidences

    async def classify_complaint(self, contents: list[str] | str, max_tokens: int = 16384, max_retries: int = 3, log_dir: str = ""):
        if isinstance(contents, str):
            contents = [contents]
        tasks = [
            self._classify_one(
                content=content,
                max_retries=max_retries,
                max_tokens=max_tokens,
                log_dir=log_dir
            )
            for content in contents
        ]

        results = await asyncio.gather(*tasks)
        return [self._parse_single_complaint_result(res) for res in results]
    
    def _clean_heading_path(self, heading_paths: list[str]):
        if not heading_paths:
            return []
        headers = []
        for heading in heading_paths:
            if heading:
                heading = heading.replace("#", "").strip()
                headers.append(heading)
        return headers
    
    async def run(self, list_complaints: list, log_dir: str):
        overall_results = []
        need_to_call_llm = {}
        dict_authorities = {}
        for c_index, complaint in enumerate(list_complaints):
            matching_heading_result = self.fuzzy_classifier.match(heading=complaint["heading_immediate"], org_names=VALID_AUTHORITIES)
            if len(matching_heading_result) == 0:
                need_to_call_llm[c_index] = complaint
            else:
                dict_authorities[c_index] = [matching_heading_result[0], "rule-based", "high"]
        print("[INFO] Total number of complaints: ", len(list_complaints))
        print("[INFO] Number of complaints that need to be processed by LLM: ", len(need_to_call_llm))
        print("[INFO] Indices of them: ", need_to_call_llm.keys())
        complaint_contents = {index : need_to_call_llm[index]['content'] for index in need_to_call_llm}
        matching_llm_results = await self.classify_complaint(list(complaint_contents.values()), log_dir=log_dir)
        for c_index, (authorities, confidences) in zip(list(need_to_call_llm.keys()), matching_llm_results):
            dict_authorities[c_index] = [authorities, "llm", confidences]
        for c_index, complaint in enumerate(list_complaints):
            complaint["authority"] = dict_authorities[c_index][0]
            complaint["match_method"] = dict_authorities[c_index][1]
            complaint["confidence"] = dict_authorities[c_index][2]
            complaint["heading"] = complaint["heading_immediate"]
            complaint["headers"] = self._clean_heading_path(complaint["heading_path"])
            if "heading_path" in complaint:
                del complaint["heading_path"]
            if "heading_immediate" in complaint:
                del complaint["heading_immediate"]
            overall_results.append(complaint)

        # Sử dụng LLM Prompting làm sạch đồng thời toàn bộ kiến nghị (parallel async requests)
        print(f"[INFO] Bắt đầu làm sạch song song {len(overall_results)} kiến nghị bằng LLM Prompting...")
        overall_results = await clean_complaints_parallel_llm(overall_results, client=self.client, model_name=self.model_name)
        print(f"[INFO] Hoàn thành làm sạch song song {len(overall_results)} kiến nghị.")
        return overall_results

    def _resolve_candidate_unit_info(self, u: Any, idx: int) -> dict:
        """Trích xuất và chuẩn hóa thông tin của một CandidateUnit."""
        uid = getattr(u, "candidate_id", None) or (u.get("candidate_id") if isinstance(u, dict) else f"unit_{idx + 1}")
        breadcrumbs = getattr(u, "breadcrumbs", None) or (u.get("breadcrumbs") if isinstance(u, dict) else [])
        content = getattr(u, "content", None) or (u.get("content") if isinstance(u, dict) else "")
        suggested = getattr(u, "suggested_authority", None) or (u.get("suggested_authority") if isinstance(u, dict) else None)

        if not suggested and breadcrumbs:
            for h in reversed(breadcrumbs):
                matched = self.fuzzy_classifier.match(h, VALID_AUTHORITIES)
                if matched:
                    suggested = matched[0]
                    break

        return {
            "uid": str(uid),
            "unit": u,
            "breadcrumbs": breadcrumbs,
            "content": content,
            "suggested_authority": suggested,
        }

    def _build_tree_batch_prompt(self, batch: list) -> tuple[dict, str]:
        prompt_lines = [
            "DƯỚI ĐÂY LÀ CÁC KHỐI VĂN BẢN (CANDIDATE UNITS) TỪ BÁO CÁO TỔNG HỢP KIẾN NGHỊ CỬ TRI:",
            "Hãy bóc tách, làm sạch nội dung từng kiến nghị và phân loại cơ quan thẩm quyền theo đúng quy định.\n",
        ]
        unit_map = {}
        for idx, u in enumerate(batch):
            info = self._resolve_candidate_unit_info(u, idx)
            uid = info["uid"]
            unit_map[uid] = info

            bc_str = " > ".join(info["breadcrumbs"]) if info["breadcrumbs"] else "Không có"
            sug_str = info["suggested_authority"] or "Không có"
            prompt_lines.append(
                f"═══════════════════════════════════════════════════════════════\n"
                f"[{uid}]\n"
                f"Đường dẫn tiêu đề (Breadcrumbs): {bc_str}\n"
                f"Gợi ý cơ quan từ tiêu đề: {sug_str}\n"
                f"Nội dung thô:\n{info['content']}\n"
            )
        return unit_map, "\n".join(prompt_lines)

    @staticmethod
    def _parse_single_raw_don_vi(d: Any) -> Optional[dict]:
        """Parse 1 phần tử đơn vị nhận từ LLM."""
        if isinstance(d, dict):
            name = str(d.get("ten_don_vi", "")).strip()
            if not name:
                return None
            conf = str(d.get("muc_do_tin_cay", "high")).strip().lower()
            return {
                "ten_don_vi": name,
                "muc_do_tin_cay": conf if conf in VALID_CONFIDENCE else "medium",
                "phuong_phap": "llm",
                "is_mapped": False,
            }
        if isinstance(d, str) and d.strip():
            return {
                "ten_don_vi": d.strip(),
                "muc_do_tin_cay": "low",
                "phuong_phap": "llm",
                "is_mapped": False,
            }
        return None

    @classmethod
    def _parse_don_vi_list(cls, raw_don_vi: Any, suggested_auth: Optional[str]) -> list[dict]:
        don_vi_list = []
        if isinstance(raw_don_vi, list):
            for d in raw_don_vi:
                parsed = cls._parse_single_raw_don_vi(d)
                if parsed:
                    don_vi_list.append(parsed)

        if not don_vi_list:
            auth_name = suggested_auth or FALLBACK_LABEL
            don_vi_list.append({
                "ten_don_vi": auth_name,
                "muc_do_tin_cay": "high" if suggested_auth else "low",
                "phuong_phap": "rule-based" if suggested_auth else "fallback",
                "is_mapped": bool(suggested_auth),
            })
        return don_vi_list

    @staticmethod
    def _resolve_unit_meta(comp_uid: str, unit_map: dict) -> dict:
        """Tìm metadata tương ứng với unit_id trong unit_map."""
        meta = unit_map.get(comp_uid)
        if meta:
            return meta
        for k, v in unit_map.items():
            if k.lower() == comp_uid.lower():
                return v
        return next(iter(unit_map.values())) if unit_map else {}

    def _parse_tree_complaints(self, complaints: Any, unit_map: dict) -> list[dict]:
        if not isinstance(complaints, list):
            return []
        batch_results = []
        for comp in complaints:
            if not isinstance(comp, dict):
                continue
            noi_dung = str(comp.get("noi_dung", "")).strip()
            if not noi_dung:
                continue

            comp_uid = str(comp.get("unit_id", "")).strip()
            meta = self._resolve_unit_meta(comp_uid, unit_map)
            breadcrumbs = meta.get("breadcrumbs", [])
            suggested_auth = meta.get("suggested_authority")
            don_vi_list = self._parse_don_vi_list(comp.get("don_vi_nhan", []), suggested_auth)

            batch_results.append({
                "noi_dung": noi_dung,
                "headers": self._clean_heading_path(breadcrumbs),
                "don_vi_nhan": don_vi_list,
            })
        return batch_results

    def _build_unit_fallback_items(self, meta: dict) -> list[dict]:
        """Tạo các kết quả fallback từ một unit khi LLM lỗi."""
        u = meta["unit"]
        breadcrumbs = meta["breadcrumbs"]
        suggested_auth = meta["suggested_authority"]
        texts = getattr(u, "texts", None) or (u.get("texts") if isinstance(u, dict) else [])
        content = meta["content"]
        lines = texts if texts else [content]
        auth = suggested_auth or FALLBACK_LABEL

        unit_items = []
        for line in lines:
            cleaned = clean_noi_dung(line)
            if cleaned and len(cleaned) > 10:
                unit_items.append({
                    "noi_dung": cleaned,
                    "headers": self._clean_heading_path(breadcrumbs),
                    "don_vi_nhan": [{
                        "ten_don_vi": auth,
                        "muc_do_tin_cay": "high" if suggested_auth else "low",
                        "phuong_phap": "rule-based" if suggested_auth else "fallback",
                        "is_mapped": bool(suggested_auth),
                    }],
                })
        return unit_items

    def _build_fallback_tree_results(self, unit_map: dict) -> list[dict]:
        fallback_results = []
        for meta in unit_map.values():
            fallback_results.extend(self._build_unit_fallback_items(meta))
        return fallback_results

    async def _process_tree_batch(
        self,
        batch: list,
        max_retries: int = 3,
        max_tokens: int = 8192,
        log_dir: str = "",
    ) -> list[dict]:
        """
        Xử lý 1 batch CandidateUnit từ cây OCR:
        - Gửi prompt trích xuất, làm sạch và phân loại thẩm quyền theo GENERALIZED_TREE_COMPLAINT_EXTRACTION_AND_CLASSIFICATION_PROMPT.
        - Parse kết quả JSON trả về.
        - Fallback an toàn nếu LLM timeout hoặc trả về JSON lỗi.
        """
        unit_map, user_prompt = self._build_tree_batch_prompt(batch)

        async with self.semaphore:
            for attempt in range(1, max_retries + 1):
                try:
                    response = await self.client.chat.completions.create(
                        model=self.model_name,
                        temperature=0.0,
                        max_tokens=max_tokens,
                        messages=[
                            {"role": "system", "content": GENERALIZED_TREE_COMPLAINT_EXTRACTION_AND_CLASSIFICATION_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        timeout=45.0,
                    )
                    raw_output = response.choices[0].message.content or ""
                    if log_dir:
                        from modules.minio_utils import append_txt_to_minio
                        append_txt_to_minio(log_dir + "/3_tree_raw_response.txt", raw_output + "\n\n---\n\n")

                    if END_THINK_TAG in raw_output:
                        _, final_answer = raw_output.rsplit(END_THINK_TAG, 1)
                    else:
                        final_answer = raw_output
                    final_answer = final_answer.strip()
                    json_str = extract_json_from_raw(final_answer)
                    data = json.loads(json_str)

                    complaints = data.get("complaints", [])
                    batch_results = self._parse_tree_complaints(complaints, unit_map)
                    if batch_results:
                        return batch_results

                except Exception as e:
                    print(f"[Warning] Batch tree classification error (attempt {attempt}/{max_retries}): {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(min(2 ** attempt, 8))

        # Fallback an toàn nếu LLM lỗi toàn bộ retry
        print(f"[Fallback] Sử dụng fallback cho batch {len(batch)} units")
        return self._build_fallback_tree_results(unit_map)

    async def process_tree_candidates(
        self,
        candidate_units: list,
        batch_size: int = 4,
        log_dir: str = "",
    ) -> list[dict]:
        """
        Bóc tách và phân loại thẩm quyền trực tiếp từ danh sách CandidateUnit của cấu trúc cây OCR.
        Chia batch và xử lý song song qua LLM.
        """
        if not candidate_units:
            return []

        batches = [
            candidate_units[i : i + batch_size]
            for i in range(0, len(candidate_units), batch_size)
        ]
        print(f"[INFO] Bắt đầu xử lý {len(candidate_units)} Candidate Units qua {len(batches)} batches LLM...")

        tasks = [
            self._process_tree_batch(b, log_dir=log_dir)
            for b in batches
        ]
        batch_outputs = await asyncio.gather(*tasks)

        all_complaints = []
        for out in batch_outputs:
            if isinstance(out, list):
                all_complaints.extend(out)

        print(f"[INFO] Hoàn thành trích xuất {len(all_complaints)} kiến nghị từ cấu trúc cây.")
        return all_complaints

        
            
            

from rapidfuzz import fuzz, utils
import re
import pandas as pd
import unicodedata
from pathlib import Path

MATCHER_NAME = "fuzzy"
THRESHOLD = 85
THRESHOLD_NORM = 88
REMOVAL_AUTHORITIES_LIST = ['Ủy ban thường vụ Quốc hội', 'Văn phòng Chủ tịch nước', 'Tổng thư ký Quốc hội', 'Ủy ban Dân nguyện và giám sát']
class FuzzyClassifier(object):
    def __init__(self):
        """Khởi tạo bộ phân loại so khớp mờ FuzzyClassifier (stateless)."""
        pass
    
    def remove_accents(self, text: str) -> str:
        nfkd = unicodedata.normalize("NFKD", text)
        return "".join(c for c in nfkd if not unicodedata.category(c).startswith("M"))

    def _slide_match(self, org_proc: str, heading_tokens: list[str], threshold: int) -> bool:
        org_tokens = org_proc.split()
        n = len(org_tokens)
        if n == 0:
            return False
        for i in range(len(heading_tokens) - n + 1):
            window = ' '.join(heading_tokens[i:i + n])
            if fuzz.ratio(org_proc, window) >= threshold:
                return True
        return False


    def match(self, heading: str, org_names: list[str]) -> list[str]:
        if not isinstance(heading, str):
            heading = ""
        heading_proc = utils.default_process(heading)
        heading_tokens = heading_proc.split()
        heading_norm = self.remove_accents(heading_proc)
        heading_norm_tokens = heading_norm.split()

        results = []
        for org in org_names:
            if not isinstance(org, str):
                continue
            org_proc = utils.default_process(org)
            if self._slide_match(org_proc, heading_tokens, THRESHOLD):
                results.append(org)
                continue
            org_norm = self.remove_accents(org_proc)
            if self._slide_match(org_norm, heading_norm_tokens, THRESHOLD_NORM):
                results.append(org)

        return results


# --------------------------------------------------------------------------- #
# 3. Validate + parse 1 output JSON
# --------------------------------------------------------------------------- #
 
def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()
 
 
def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            body = text[first_newline + 1:]
            if body.endswith("```"):
                return body[:-3].strip()
    return text
 
 
def _validate_data_fields(data: dict, errors: list[str]) -> None:
    required_fields = {"step_used", "authority", "evidence_span", "confidence"}
    missing = required_fields - data.keys()
    if missing:
        errors.append(f"Thiếu field bắt buộc: {missing}")

    confidence = data.get("confidence")
    if confidence not in ("high", "medium", "low"):
        errors.append(f"confidence='{confidence}' không hợp lệ.")


def _validate_and_normalize_authority(data: dict, fuzzy_classifier: FuzzyClassifier) -> None:
    authority = data.get("authority")
    if not isinstance(authority, str):
        data["authority"] = ""
        return
    if authority not in VALID_AUTHORITIES and authority != FALLBACK_LABEL:
        authority_result = fuzzy_classifier.match(authority, VALID_AUTHORITIES)
        if len(authority_result) > 0:
            data["authority"] = authority_result[0]


def _validate_step_and_evidence(data: dict, source_content: str, errors: list[str]) -> None:
    step_used = data.get("step_used")
    if step_used not in (1, 2, 3):
        errors.append(f"step_used={step_used} không hợp lệ (phải là 1, 2, hoặc 3).")
    else:
        if step_used >= 2 and not data.get("keywords"):
            errors.append(f"step_used={step_used} nhưng thiếu 'keywords'.")
        if step_used == 3 and not data.get("requested_action"):
            errors.append("step_used=3 nhưng thiếu 'requested_action'.")

    authority = data.get("authority", "")
    evidence_span = data.get("evidence_span")
    if authority != FALLBACK_LABEL:
        if not evidence_span:
            errors.append("Thiếu 'evidence_span' (bắt buộc trừ khi KHÔNG XÁC ĐỊNH ĐƯỢC).")
        else:
            norm_source = _normalize_ws(source_content)
            norm_evidence = _normalize_ws(evidence_span)
            if norm_evidence not in norm_source:
                errors.append(
                    "'evidence_span' KHÔNG phải substring của nội dung kiến nghị gốc "
                    "— khả năng model đã diễn giải/bịa thay vì trích dẫn nguyên văn."
                )


def parse_and_validate(raw_output: str, source_content: str, fuzzy_classifier: FuzzyClassifier) -> tuple[dict, list[str]]:
    """
    Parse JSON output của model + validate theo các ràng buộc đã thiết kế:
        - Đúng schema tối thiểu (có đủ field bắt buộc)
        - authority thuộc VALID_AUTHORITIES hoặc FALLBACK_LABEL
        - evidence_span (nếu có) phải là substring (whitespace-normalized)
          của source_content — nếu không, đây là dấu hiệu model bịa/diễn giải
          thay vì trích dẫn thật, cần cảnh báo.
        - step_used hợp lệ (1/2/3) và các field phụ thuộc (keywords,
          requested_action) được điền đúng theo step_used.
    Trả về (parsed_dict, list_errors). parsed_dict rỗng nếu JSON không parse
    được (lỗi nghiêm trọng, cần retry).
    """
    cleaned = strip_code_fence(raw_output)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return {}, [f"JSON không hợp lệ: {e}"]

    if not isinstance(data, dict):
        return {}, ["Output không phải JSON object."]

    errors: list[str] = []
    _validate_data_fields(data, errors)
    _validate_and_normalize_authority(data, fuzzy_classifier)
    _validate_step_and_evidence(data, source_content, errors)

    return data, errors