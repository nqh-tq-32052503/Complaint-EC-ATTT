from modules.prompts import (
    HEADING_NORMALIZATION_CONTEXT_BLOCK_TEMPLATE,
    HEADING_NORMALIZATION_SYSTEM_PROMPT,
    HEADING_NORMALIZATION_USER_PROMPT_TEMPLATE,
    HEADING_VALIDATION_SYSTEM_PROMPT,
    HEADING_TREE_NUMBERING_SYSTEM_PROMPT,
    HEADING_TREE_NUMBERING_USER_PROMPT_TEMPLATE,
)

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
from openai import AsyncOpenAI


MODEL_NAME = "google/gemma-4-26B-A4B-it"
NO_HEADING_TEXT = "(không có heading nào)"

# Regex trích xuất heading và level (H1, H2,...) từ từng dòng của cây heading (tree_text)
TREE_HEADING_REGEX = re.compile(
    r"^[│├└─ \t]*(?P<heading>[^│├└─ \t\[\r\n][^\[\r\n]*)\[(?P<level>H\d+)\]\s*$"
)

# Regex nhận diện tiền tố số thứ tự (số Ả Rập, số La Mã, chữ cái kèm ngoặc đơn a), b)..., số kèm ngoặc đơn 1), 2)..., hoặc trong ngoặc (a), (1), [1])
NUMBER_PREFIX_REGEX = re.compile(
    r"^\s*("
    r"[0-9]+(?:\.[0-9]+)*(?:\s*[\.\)\:\-\/])"                       # 1., 2., 1.1., 1.2.3., 1), 2), 1.1), 1 -, 1:
    r"|[IVXLCDMivxlcdm]+(?:\s*[\.\)\:\-\/])"                        # I., II., III., IV., I), II), IV -, I/, i), ii)
    r"|[a-zA-ZđĐ](?:\.[0-9]+)*(?:\s*[\.\)\:\-\/])"                  # a), b), c), đ), a., b., a.1., a -, A), B)
    r"|\([0-9a-zA-ZđĐIVXLCDMivxlcdm]+(?:\.[0-9a-zA-ZđĐ]+)*\)"       # (1), (2), (a), (b), (đ), (I), (i), (1.1), (a.1)
    r"|\[[0-9a-zA-ZđĐIVXLCDMivxlcdm]+(?:\.[0-9a-zA-ZđĐ]+)*\]"       # [1], [2], [a], [I], [i]
    r")\s+",
    re.UNICODE
)

def has_number_prefix(text: str) -> bool:
    """Kiểm tra xem heading có bắt đầu bằng số, số La Mã, chữ cái thứ tự hay không."""
    return bool(NUMBER_PREFIX_REGEX.match(text.strip()))

def detect_numbering_prefix(text: str) -> Optional[str]:
    """Trích xuất chuỗi tiền tố số thứ tự nếu có (ví dụ: '1.', 'II.', 'a)')."""
    m = NUMBER_PREFIX_REGEX.match(text.strip())
    return m.group(1).strip() if m else None
@dataclass
class Heading:
    title: str
    level: str


@dataclass
class Chunk:
    index: int
    text: str
    start_line: int
    end_line: int

def extract_json_object(raw: str) -> dict:
    """Parse JSON object từ output model, chịu lỗi bọc markdown fence / text thừa."""
    raw = raw.strip()
    raw = re.sub(r"^```(json)?", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(raw[start : end + 1])
    raise ValueError(f"Không parse được JSON từ output model:\n{raw[:300]}")

def check_vietnamese_chars(text: str) -> bool:
    vietnamese_pattern = r'[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]'
    return bool(re.search(vietnamese_pattern, text, re.IGNORECASE))

def _filter_valid_corrections(raw_corrections: dict) -> dict[str, str]:
    """Lọc và kiểm tra tính hợp lệ của các correction trả về từ LLM."""
    if not isinstance(raw_corrections, dict):
        return {}
    valid_corrections = {}
    for wrong, right in raw_corrections.items():
        wrong_str = str(wrong).strip()
        right_str = str(right).strip()
        if not wrong_str or not right_str or has_number_prefix(wrong_str):
            continue
        if has_number_prefix(right_str):
            clean_w = re.sub(r"\s+", " ", wrong_str.lower())
            clean_r = re.sub(r"\s+", " ", right_str.lower())
            if clean_w in clean_r:
                valid_corrections[wrong_str] = right_str
    return valid_corrections

class HeadingNormalizer(object):
    def __init__(self):
        self.model_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1")
        self.model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")
        self.model_api_key = os.environ.get("LLM_GATEWAY_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        self.client = AsyncOpenAI(api_key=self.model_api_key or "empty", base_url=self.model_url)
        self.visualizer = HeadingTreeVisualizer()

    def split_into_chunks(
        self,
        lines: list[str],
        chunk_size: int = 1000,
        overlap: int = 15,
    ) -> list[Chunk]:
        """Chia file thành các chunk theo số dòng, có overlap để không cắt đứt ngữ cảnh."""
        chunks: list[Chunk] = []
        step = max(chunk_size - overlap, 1)
        idx = 0
        chunk_id = 0
        n = len(lines)
        while idx < n:
            end = min(idx + chunk_size, n)
            text = "\n".join(lines[idx:end])
            chunks.append(Chunk(index=chunk_id, text=text, start_line=idx, end_line=end))
            chunk_id += 1
            if end == n:
                break
            idx += step
        return chunks

    async def verify_is_heading(
        self,
        text: str,
        max_retries: int = 3,
    ) -> bool:
        """Gọi model để xác minh MỘT đoạn text có phải heading thật sự hay không."""
        USER_PROMPT_TEMPLATE = """\
        Đoạn text cần xác minh:
        -----
        {text}
        -----
        Đây có phải là một heading (tiêu đề/nhãn mục) thực sự không? Trả lời theo đúng định dạng \
        JSON đã quy định.
        """
        user_prompt = USER_PROMPT_TEMPLATE.format(text=text)
        last_err: Optional[Exception] = None
    
        for attempt in range(1, max_retries + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=MODEL_NAME,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": HEADING_VALIDATION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    timeout=30.0
                )
                content = resp.choices[0].message.content or ""
                data = extract_json_object(content)
                return bool(data.get("is_heading", False))
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[verify] Lỗi lần {attempt}/{max_retries} cho text {text[:50]!r}: {e}", file=sys.stderr)
                await asyncio.sleep(1.5 * attempt)
    
        raise RuntimeError(f"Verify thất bại sau {max_retries} lần cho text {text[:50]!r}: {last_err}")
    
    
    def extract_json_object(self, raw: str) -> dict:
        """Cố gắng parse JSON object từ output model, chịu lỗi bọc markdown fence, text thừa."""
        raw = raw.strip()
        # Bỏ markdown code fence nếu có
        raw = re.sub(r"^```(json)?", "", raw.strip(), flags=re.IGNORECASE).strip()
        raw = re.sub(r"```$", "", raw.strip()).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # Fallback: lấy đoạn từ dấu { đầu tiên đến } cuối cùng
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = raw[start : end + 1]
            return json.loads(candidate)
        raise ValueError(f"Không parse được JSON từ output model:\n{raw[:500]}")


    async def call_model_for_chunk(
        self,
        chunk: Chunk,
        prev_headings: list[Heading],
        max_retries: int = 3,
        context_window: int = 6,
    ) -> list[Heading]:
        """Gọi model cho một chunk, có retry khi JSON lỗi."""
        if prev_headings:
            recent = prev_headings[-context_window:]
            prev_str = "\n".join(f'- (level {h.level}) "{h.heading}"' for h in recent)
            context_block = HEADING_NORMALIZATION_CONTEXT_BLOCK_TEMPLATE.format(prev_headings=prev_str)
        else:
            context_block = ""

        user_prompt = HEADING_NORMALIZATION_USER_PROMPT_TEMPLATE.format(
            context_block=context_block, chunk_text=chunk.text
        )

        last_err: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=MODEL_NAME,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": HEADING_NORMALIZATION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    timeout=180.0
                )
                content = resp.choices[0].message.content or ""
                data = self.extract_json_object(content)
                raw_headings = data.get("headings", [])
                result = []
                for item in raw_headings:
                    h = str(item.get("heading", "")).strip()
                    lvl = str(item.get("level", "")).strip()
                    if h and lvl:
                        result.append(Heading(title=h, level=lvl))
                return result
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(
                    f"[chunk {chunk.index}] Lỗi lần {attempt}/{max_retries}: {e}",
                    file=sys.stderr,
                )
                await asyncio.sleep(1.5 * attempt)
        raise RuntimeError(f"Chunk {chunk.index} thất bại sau {max_retries} lần: {last_err}")


    def dedupe_headings(self, all_headings: list[Heading]) -> list[Heading]:
        """Loại heading trùng lặp do vùng overlap giữa các chunk (so khớp nội dung liền kề)."""
        deduped: list[Heading] = []
        for h in all_headings:
            if deduped:
                prev = deduped[-1]
                if prev.title.strip() == h.title.strip():
                    continue
            deduped.append(h)
        return deduped


    def _strip_invalid_heading_hashes(self, lines: list[str], need_to_remove: list[str]):
        """Loại bỏ '#' ở các dòng bị nhận nhầm là heading."""
        for invalid_heading in need_to_remove:
            target = invalid_heading.replace("#", "").strip()
            if not target:
                continue

            best_idx = -1
            best_score = 0.0

            for idx, line in enumerate(lines):
                if not line.strip() or "#" not in line:
                    continue
                score = self.calculate_match_score(target, line)
                if score > best_score:
                    best_score = score
                    best_idx = idx

            if best_idx != -1 and best_score >= 5.0:
                lines[best_idx] = re.sub(r"^\s*#+\s*", "", lines[best_idx])

    async def reconstruct_headings(
        self,
        pdf_content: str,
        concurrency: int = 3,
        log_dir: str = ""
    ) -> tuple[list[dict], str]:
        lines = pdf_content.splitlines()
        chunks = [Chunk(index=0, text=pdf_content, start_line=0, end_line=len(lines))]
        print(f"Xử lý toàn bộ văn bản trong 1 chunk duy nhất ({len(lines)} dòng).")

        all_headings: list[Heading] = []
        semaphore = asyncio.Semaphore(concurrency)

        async def run_chunk(chunk: Chunk, prev_ctx: list[Heading]) -> list[Heading]:
            async with semaphore:
                return await self.call_model_for_chunk(chunk, prev_ctx)

        for chunk in chunks:
            headings = await run_chunk(chunk, all_headings)
            print(f"[chunk {chunk.index}] tìm thấy {len(headings)} heading.")
            all_headings.extend(headings)

        result_headings = self.dedupe_headings(all_headings)
        output_headings = [{"heading": h.title, "level": h.level} for h in result_headings if check_vietnamese_chars(h.title)]
        validation_tasks = [self.verify_is_heading(h["heading"]) for h in output_headings]
        validated_results = await asyncio.gather(*validation_tasks)
        validated_headings = []
        need_to_remove = []
        if log_dir:
            from modules.minio_utils import write_to_minio, write_json_to_minio
            df = {"heading" : [h["heading"] for h in output_headings], "is_valid" : validated_results}
            csv_str = pd.DataFrame(df).to_csv(index=False, encoding="utf-8")
            write_to_minio(log_dir + "/1_7_heading_validation.csv", csv_str, content_type="text/csv; charset=utf-8")
        for i in range(len(validated_results)):
            if validated_results[i]:
                validated_headings.append(output_headings[i])
            else:
                need_to_remove.append(output_headings[i]["heading"])
        if log_dir:
            write_json_to_minio(log_dir + "/1.7_need_to_remove.json", need_to_remove)

        self._strip_invalid_heading_hashes(lines, need_to_remove)

        pdf_content = "\n".join(lines)
        return validated_headings, pdf_content
    
    def _clean_text_for_matching(self, s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        return s.strip(" \t#*_\r\n")

    def _normalize_str_for_matching(self, s: str) -> str:
        cleaned = self._clean_text_for_matching(s).lower()
        return re.sub(r"\s+", " ", cleaned)

    def _calculate_token_score(self, h_tokens: list[str], l_tokens: list[str]) -> float:
        """Tính điểm tương đồng dựa trên tập token."""
        if not h_tokens or not l_tokens:
            return 0.0
        l_prefix_tokens = l_tokens[:len(h_tokens)]
        match_count = sum(1 for a, b in zip(h_tokens, l_prefix_tokens) if a == b)
        prefix_ratio = match_count / len(h_tokens)
        if prefix_ratio >= 0.8:
            return 5.0 * prefix_ratio

        h_set = set(h_tokens)
        l_set = set(l_tokens)
        intersection = h_set & l_set
        if intersection:
            heading_coverage = len(intersection) / len(h_set)
            iou = len(intersection) / len(h_set | l_set)
            return heading_coverage * 0.5 + iou * 0.2
        return 0.0

    def calculate_match_score(self, heading_text: str, line: str) -> float:
        h_norm = self._normalize_str_for_matching(heading_text)
        l_norm = self._normalize_str_for_matching(line)

        if not l_norm or not h_norm:
            return 0.0

        if l_norm == h_norm:
            return 10.0

        if l_norm.startswith(h_norm):
            remain = l_norm[len(h_norm):]
            if not remain or remain[0] in (' ', '.', ':', '-', ')', '\t', '*'):
                return 8.0

        if h_norm.startswith(l_norm):
            remain = h_norm[len(l_norm):]
            if not remain or remain[0] in (' ', '.', ':', '-', ')', '\t', '*'):
                return 7.0

        h_tokens = list(re.findall(r"\w+", h_norm))
        l_tokens = list(re.findall(r"\w+", l_norm))
        return self._calculate_token_score(h_tokens, l_tokens)

    def find_best_matching_line(
        self,
        lines: list[str],
        heading_text: str,
        used_lines: set[int],
        start_search_idx: int = 0,
    ) -> tuple[int, float]:
        best_idx, best_score = -1, 0.0

        def _scan(indices):
            nonlocal best_idx, best_score
            for idx in indices:
                if idx in used_lines or not lines[idx].strip():
                    continue
                score = self.calculate_match_score(heading_text, lines[idx])
                if score > best_score:
                    best_idx, best_score = idx, score
                    if best_score >= 8.0:
                        break

        _scan(range(start_search_idx, len(lines)))
        if best_score < 3.0:
            _scan(range(0, start_search_idx))

        return best_idx, best_score


    def extract_and_group_headings_from_tree(self, tree_text: str) -> dict[str, list[dict]]:
        """
        Bước 1: Duyệt qua cây heading, trích xuất và gom nhóm các heading theo level (H1, H2,...) bằng regex.
        """
        level_groups = defaultdict(list)
        for line in tree_text.splitlines():
            line_s = line.strip()
            if not line_s or line_s.startswith("```") or line_s == NO_HEADING_TEXT:
                continue
            m = TREE_HEADING_REGEX.match(line)
            if m:
                h_text = m.group("heading").strip()
                lvl = m.group("level").strip()
                level_groups[lvl].append({"heading": h_text, "level": lvl})
        return dict(level_groups)

    def _summarize_group_numbering(self, group: list[dict], level: str) -> tuple[str, str, str]:
        """
        Trích xuất và tóm tắt 2 thông tin chính của nhóm heading để gửi vào LLM:
        1) Thứ tự vị trí của các heading đang bị miss numbering trong nhóm.
        2) Các heading khác trong nhóm đang được đánh dấu bằng ký tự gì.
        """
        missing_items = []
        numbered_items = []

        for idx, item in enumerate(group, start=1):
            heading = item["heading"]
            prefix = detect_numbering_prefix(heading)
            if prefix:
                numbered_items.append({"order": idx, "heading": heading, "prefix": prefix})
            else:
                missing_items.append({"order": idx, "heading": heading})

        # 1) Missing info
        missing_info_lines = []
        for m in missing_items:
            h_name = m["heading"]
            order = m["order"]
            total = len(group)
            missing_info_lines.append(f'- Heading "{h_name}" đang ở vị trí thứ {order}/{total} trong nhóm {level} (bị thiếu numbering).')
        missing_info_text = "\n".join(missing_info_lines)

        # 2) Other headings numbering info
        if numbered_items:
            prefixes_str = ", ".join(f'"{item["prefix"]}"' for item in numbered_items)
            other_info_lines = [
                f"- Các heading khác trong nhóm ({len(numbered_items)}/{len(group)} heading) đang được đánh dấu bằng các tiền tố: {prefixes_str}"
            ]
            for item in numbered_items:
                h_name = item["heading"]
                order = item["order"]
                pfx = item["prefix"]
                other_info_lines.append(f'  + Vị trí {order}: "{h_name}" (tiền tố: "{pfx}")')
            other_info_text = "\n".join(other_info_lines)
        else:
            other_info_text = f"- Chưa có heading nào trong nhóm {level} có tiền tố đánh số."

        # Group text
        group_lines = []
        for idx, item in enumerate(group, start=1):
            heading = item["heading"]
            prefix = detect_numbering_prefix(heading)
            status = f'(Tiền tố: "{prefix}")' if prefix else "(THIẾU NUMBERING)"
            group_lines.append(f"- [Vị trí {idx}] {heading} {status}")
        group_text = "\n".join(group_lines)

        return group_text, missing_info_text, other_info_text

    async def _try_fix_group_numbering_once(
        self,
        current_group: list[dict],
        level: str,
        attempt: int,
        max_retries: int,
    ) -> bool:
        """Thực hiện một lần gọi LLM để sửa numbering cho nhóm heading. Trả về True nếu thành công."""
        group_text, missing_info_text, other_info_text = self._summarize_group_numbering(current_group, level)
        user_prompt = HEADING_TREE_NUMBERING_USER_PROMPT_TEMPLATE.format(
            level=level,
            group_headings_text=group_text,
            missing_info_text=missing_info_text,
            other_headings_numbering_info=other_info_text,
        )

        try:
            resp = await self.client.chat.completions.create(
                model=self.model_name,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": HEADING_TREE_NUMBERING_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                timeout=30.0,
            )
            content = resp.choices[0].message.content or ""
            data = self.extract_json_object(content)
            raw_corrections = data.get("corrections", {})
            valid_corrections = _filter_valid_corrections(raw_corrections)

            for item in current_group:
                h_curr = item["heading"].strip()
                if h_curr in valid_corrections:
                    item["heading"] = valid_corrections[h_curr]

            still_missing = [item for item in current_group if not has_number_prefix(item["heading"])]
            if not still_missing:
                print(f"[Heading Tree Normalization] Nhóm {level} đã chuẩn hóa thành công ở lần thử {attempt}.")
                return True
            print(
                f"[Heading Tree Normalization] Nhóm {level} vẫn còn {len(still_missing)} heading chưa có số sau lần {attempt}/{max_retries}. Đang thử lại...",
                file=sys.stderr,
            )
            await asyncio.sleep(1.0 * attempt)
        except Exception as e:
            print(f"[Heading Tree Normalization] Nhóm {level} gặp lỗi lần {attempt}/{max_retries}: {e}", file=sys.stderr)
            await asyncio.sleep(1.0 * attempt)

        return False

    async def normalize_level_group_numbering(
        self,
        level: str,
        group: list[dict],
        max_retries: int = 3,
    ) -> dict[str, str]:
        """
        Bước 2 & Bước 3:
        - Bước 2: Duyệt trong nhóm xem có tồn tại heading nào đang không bắt đầu bằng số hoặc ký tự La Mã hay không.
        - Bước 3: Nếu tồn tại, gọi LLM để đề xuất ký tự đánh dấu cho heading đang bị miss numbering.
        """
        has_missing = any(not has_number_prefix(item["heading"]) for item in group)
        if not has_missing:
            return {}

        current_group = [{"heading": item["heading"], "level": item["level"]} for item in group]

        for attempt in range(1, max_retries + 1):
            missing_now = [item for item in current_group if not has_number_prefix(item["heading"])]
            if not missing_now:
                break

            is_done = await self._try_fix_group_numbering_once(current_group, level, attempt, max_retries)
            if is_done:
                break

        # Trả về các thay đổi của nhóm
        group_corrections = {}
        for orig, curr in zip(group, current_group):
            orig_h = orig["heading"].strip()
            curr_h = curr["heading"].strip()
            if orig_h != curr_h:
                group_corrections[orig_h] = curr_h

        return group_corrections

    async def normalize_heading_tree_numbering(
        self,
        tree_text: str,
        max_retries: int = 3,
    ) -> tuple[bool, dict[str, str]]:
        """
        Chuẩn hóa cây heading theo 3 bước:
        - Bước 1: Duyệt qua cây heading, trích xuất và gom nhóm các heading theo level (H1, H2,...) bằng regex.
        - Bước 2: Duyệt trong mỗi nhóm xem có tồn tại heading nào đang không bắt đầu bằng số hoặc ký tự La Mã hay không.
        - Bước 3: Nếu tồn tại heading thiếu numbering, gọi LLM đề xuất ký tự đánh dấu, thay thế và kiểm tra lại bằng regex (có cơ chế retry).
        - Trả về tuple: (is_valid, corrections)
        """
        if not tree_text or not tree_text.strip() or tree_text.strip() == NO_HEADING_TEXT:
            return True, {}

        # Bước 1: Trích xuất và gom nhóm theo level
        level_groups = self.extract_and_group_headings_from_tree(tree_text)
        if not level_groups:
            return True, {}

        all_corrections: dict[str, str] = {}

        # Bước 2 & 3: Duyệt qua từng nhóm level và chuẩn hóa
        for level, group in level_groups.items():
            group_corrections = await self.normalize_level_group_numbering(
                level=level,
                group=group,
                max_retries=max_retries,
            )
            if group_corrections:
                all_corrections.update(group_corrections)

        if not all_corrections:
            return True, {}

        return False, all_corrections

    def normalize_heading_line(self, line: str, level: int, heading_text: Optional[str] = None) -> str:
        """
        Chuẩn hóa MỘT dòng heading:
        - Xóa hết các dấu '#' và khoảng trắng đầu/cuối dòng.
        - Nếu heading_text có thêm tiền tố số thứ tự mà dòng hiện tại chưa có, cập nhật nội dung heading_text.
        - Thêm `level` dấu '#' + một dấu cách vào đầu, trước phần nội dung.
        """
        rest = re.sub(r"^\s*#+\s*", "", line).strip()

        if heading_text:
            clean_h = heading_text.strip()
            clean_r = rest.strip()
            if (clean_h != clean_r and clean_r and clean_h.lower().endswith(clean_r.lower())) or (clean_h and not clean_r):
                rest = clean_h

        return f"{'#' * level} {rest}"


    def normalize_headings(
        self, 
        md_lines: list[str],
        headings: list[dict],
        min_iou_threshold: float = 0.0,
        avoid_reuse_lines: bool = True,
    ) -> tuple[list[str], list[dict]]:
        """
        Chuẩn hóa các dòng heading trong `md_lines` dựa trên `headings` (list dict có key
        "heading" và "level").
        """
        lines = list(md_lines)
        used_lines: set[int] = set()
        unmatched: list[dict] = []
        last_matched_idx = 0

        for element in headings:
            heading_text = str(element.get("heading", "")).strip()
            if not heading_text:
                continue
            try:
                level = int(str(element.get("level", "1")).strip())
            except ValueError:
                level = 1

            best_idx, best_score = self.find_best_matching_line(
                lines,
                heading_text,
                used_lines if avoid_reuse_lines else set(),
                start_search_idx=last_matched_idx,
            )

            if best_idx == -1 or best_score < min_iou_threshold:
                unmatched.append({**element, "best_iou": max(best_score, 0.0)})
                continue

            lines[best_idx] = self.normalize_heading_line(lines[best_idx], level, heading_text=heading_text)
            if avoid_reuse_lines:
                used_lines.add(best_idx)
            last_matched_idx = best_idx

        return lines, unmatched


    def normalize_markdown_file(
        self,
        pdf_content: str,
        headings: list,
        min_iou_threshold: float = 0.0,
    ) -> str:
        """Tiện ích đọc file .md + file .json, chuẩn hóa, ghi ra output_path."""
        md_lines = pdf_content.splitlines()

        new_lines, unmatched = self.normalize_headings(
            md_lines, headings, min_iou_threshold=min_iou_threshold
        )

        output_text = "\n".join(new_lines) + "\n"

        if unmatched:
            print(f"Cảnh báo: {len(unmatched)} heading không tìm được dòng khớp phù hợp:")
            for u in unmatched:
                print(f"  - (IoU={u['best_iou']:.2f}) {u['heading']!r}")

        return output_text
    
    async def normalize(self, pdf_content: str, log_dir: str = ""):
        if not pdf_content or not pdf_content.strip():
            return ""
        reconstructed_headings, pdf_content = await self.reconstruct_headings(pdf_content, log_dir=log_dir)
        tree_text = self.visualizer.visualize_heading_tree(reconstructed_headings, log_dir=log_dir)

        # Gọi LLM kiểm tra và chuẩn hóa number list cho heading tree
        is_valid, corrections = await self.normalize_heading_tree_numbering(tree_text)
        if not is_valid and corrections:
            print(f"[Heading Tree Normalization] Phát hiện {len(corrections)} heading thiếu number list. Tiến hành cập nhật:")
            for wrong, right in corrections.items():
                print(f"  - '{wrong}' -> '{right}'")

            # Cập nhật danh sách reconstructed_headings
            for item in reconstructed_headings:
                h_name = item.get("heading", "").strip()
                if h_name in corrections:
                    item["heading"] = corrections[h_name]

            # Ghi log cây heading sau khi đã chuẩn hóa number list
            self.visualizer.visualize_heading_tree(
                reconstructed_headings,
                log_dir=log_dir,
                filename="1.1_pdf_heading_tree_normalized.md"
            )

        normalized_pdf_content = self.normalize_markdown_file(pdf_content, reconstructed_headings)
        return normalized_pdf_content


class HeadingTreeVisualizer(object):
    def __init__(self):
        """Khởi tạo visualizer cho cây phân cấp tiêu đề (stateless)."""
        pass
    
    def build_tree(self, headings: list[dict]) -> list[dict]:
        """
        Chuyển list heading phẳng (có 'level') thành cây lồng nhau, dựa trên thuật toán
        stack: mỗi heading mới sẽ là con của heading gần nhất phía trước có level nhỏ hơn.

        Lưu ý: level không cần liên tục (vd nhảy từ 1 -> 3) vẫn xử lý đúng, vì ta luôn tìm
        tổ tiên gần nhất có level nhỏ hơn level hiện tại, bất kể có "khoảng trống" level nào
        ở giữa hay không.
        """
        roots: list[dict] = []
        stack: list[tuple[int, dict]] = []  # (level, node)

        for item in headings:
            try:
                level = int(str(item.get("level", "1")).strip())
            except ValueError:
                level = 1
            node = {"heading": str(item.get("heading", "")).strip(), "level": level, "children": []}

            # Loại bỏ khỏi stack tất cả node có level >= level hiện tại (không thể là cha)
            while stack and stack[-1][0] >= level:
                stack.pop()

            if stack:
                stack[-1][1]["children"].append(node)
            else:
                roots.append(node)

            stack.append((level, node))

        return roots


    def render_tree(
        self,
        nodes: list[dict],
        prefix: str = "",
        show_level: bool = True,
        lines: Optional[list[str]] = None,
    ) -> list[str]:
        """Render cây thành danh sách dòng text theo phong cách lệnh `tree` (├──, └──, │)."""
        if lines is None:
            lines = []

        for i, node in enumerate(nodes):
            is_last = i == len(nodes) - 1
            connector = "└── " if is_last else "├── "
            suffix = f"  [H{node['level']}]" if show_level else ""
            lines.append(f"{prefix}{connector}{node['heading']}{suffix}")

            extension = "    " if is_last else "│   "
            self.render_tree(node["children"], prefix + extension, show_level, lines)

        return lines


    def visualize_heading_tree(
        self,
        headings: list,
        log_dir: str = "",
        show_level: bool = True,
        filename: str = "1.1_pdf_heading_tree.md",
    ) -> str:
        """
        Đọc file JSON heading, build cây, ghi ra file markdown (code block).
        Trả về nội dung phần cây dạng text (để tiện in ra console hoặc tái sử dụng).
        """
        if not isinstance(headings, list) or not headings:
            tree_text = NO_HEADING_TEXT
        else:
            roots = self.build_tree(headings)
            tree_text = "\n".join(self.render_tree(roots, show_level=show_level))

        md_parts = []
        md_parts.append("```text")
        md_parts.append(tree_text)
        md_parts.append("```")

        if log_dir:
            from modules.minio_utils import write_to_minio
            write_to_minio(log_dir + f"/{filename}", "\n".join(md_parts) + "\n", content_type="text/markdown; charset=utf-8")

        return tree_text