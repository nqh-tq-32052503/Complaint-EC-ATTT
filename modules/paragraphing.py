import os
import sys
import json, re
import unicodedata
from openai import AsyncOpenAI
from modules.prompts import METADATA_PARAGRAPHING_SYSTEM_PROMPT


TINH_HINH = "tinh hinh"
KIEN_NGHI = "kien nghi"
CU_THE = "cu the"


class Paragrapher(object):
    # Mỗi rule là 1 list các token BẮT BUỘC đều phải xuất hiện (dạng substring, không cần liền 
    # kề) trong heading đã normalize thì heading đó mới được coi là khớp.
    # Token-based thay vì match nguyên cụm để chịu được từ chèn giữa (vd "kiến nghị CỦA CỬ TRI cụ thể").
    CHUNG_TOKEN_RULES = [
        [TINH_HINH, "chung"],
        ["khai quat", "chung"],
        ["khai quat", TINH_HINH],
        ["dac diem", TINH_HINH],
        ["y kien", "chung"],
        [KIEN_NGHI, "chung"],
        ["danh gia", "chung"],
        ["nhan dinh", "chung"],
        ["tong quan", "chung"],
        ["phan chung"],
    ]

    CU_THE_TOKEN_RULES = [
        [KIEN_NGHI, CU_THE],
        ["y kien", CU_THE],
        ["phan anh", CU_THE],
        ["noi dung", CU_THE],
        ["noi dung", KIEN_NGHI],
        ["kien nghi cua cu tri thuoc tham quyen giai quyet cua cac co quan trung uong"]
    ]

    def __init__(self):
        self.model_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1")
        self.model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")
        self.model_api_key = os.environ.get("LLM_GATEWAY_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        self.client = AsyncOpenAI(base_url=self.model_url, api_key=self.model_api_key or "empty")

    # ==========================================================
    # JSON parsing tiện ích
    # ==========================================================
    def extract_json_object(self, raw: str) -> dict:
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

    # ==========================================================
    # Chunking theo dòng
    # ==========================================================
    def chunk_by_line(self, md_text: str):
        """Chunk theo \n, bỏ dòng trắng, lưu lại vị trí gốc để khôi phục sau."""
        raw_lines = md_text.split("\n")
        chunks = []          # [(U_id, content)]
        blank_positions = [] # vị trí dòng trắng gốc, để reinsert khi ráp file
        u_idx = 0
        for orig_idx, line in enumerate(raw_lines):
            if line.strip() == "":
                blank_positions.append(orig_idx)
                continue
            u_idx += 1
            chunks.append((f"U{u_idx}", line))
        return chunks, blank_positions, raw_lines

    def build_prompt(self, chunks):
        return "\n".join(f"{uid}: {content}" for uid, content in chunks)

    # ==========================================================
    # STAGE 1 — Bóc tách loai_1 (metadata: header + footer) bằng LLM
    # ==========================================================
    def validate_loai_1(self, result: dict, all_uids: list[str]) -> tuple[bool, str]:
        """
        loai_1 chỉ được phép xuất hiện thành 1 cụm ở ĐẦU và/hoặc 1 cụm ở CUỐI,
        không được xen giữa thân bài.
        """
        l1 = result.get("loai_1", [])
        l1_set = set(l1)

        if len(l1) != len(l1_set):
            return False, "Có Uk bị gán trùng trong loai_1"
        if not l1_set.issubset(set(all_uids)):
            extra = l1_set - set(all_uids)
            return False, f"loai_1 chứa Uk không tồn tại (hallucinated): {extra}"

        ordered = sorted(all_uids, key=lambda u: int(u[1:]))
        flags = [u in l1_set for u in ordered]  # True nếu thuộc loai_1
        n = len(flags)

        i = 0
        while i < n and flags[i]:
            i += 1
        head_end = i  # dòng đầu tiên không thuộc loai_1

        j = n - 1
        while j >= 0 and flags[j]:
            j -= 1
        tail_start = j + 1  # dòng đầu tiên của cụm loai_1 ở cuối

        if tail_start < head_end:
            # toàn bộ văn bản là loai_1 (edge case văn bản cực ngắn) -> vẫn hợp lệ
            middle_ok = True
        else:
            middle_ok = not any(flags[head_end:tail_start])

        if not middle_ok:
            return False, "loai_1 xuất hiện xen giữa thân bài (không chỉ ở đầu/cuối)"

        return True, "OK"

    async def _call_llm_loai_1(
        self,
        prompt_body: str,
        temperature: float = 0.0,
        max_tokens: int = 8000,
        max_retries: int = 3,
    ) -> dict:
        count = 0
        last_error = None
        while count < max_retries:
            try:
                response = await self.client.chat.completions.create(
                    model=self.model_name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": METADATA_PARAGRAPHING_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt_body},
                    ],
                    timeout=30.0,
                )
                fully_response = response.choices[0].message.content
                if "</think>" in fully_response:
                    _, final_answer = fully_response.rsplit("</think>", 1)
                else:
                    final_answer = fully_response
                result = self.extract_json_object(final_answer.strip())
                print(result)
                return result
            except Exception as e:
                last_error = e
                print(e)
                print(f"[RETRY loai_1 {count + 1}/{max_retries}]")
                count += 1
        raise ValueError(f"Không xác định được loai_1 sau {max_retries} lần thử: {last_error}")

    # ==========================================================
    # STAGE 2 — Phân loại loai_2 / loai_3 THUẦN BẰNG REGEX (không gọi LLM)
    # Chỉ nhìn TÊN MỤC/HEADING, không đọc nội dung bên trong chunk.
    # ==========================================================
    @staticmethod
    def _matches_token_rules(norm: str, rules: list[list[str]]) -> bool:
        """Khớp nếu TỒN TẠI ít nhất 1 rule mà TẤT CẢ token trong rule đó đều là substring của 
        norm (không yêu cầu liền kề nhau, không yêu cầu đúng thứ tự)."""
        return any(all(tok in norm for tok in rule) for rule in rules)

    @classmethod
    def _is_chung_heading(cls, line: str) -> bool:
        norm = cls._normalize_heading_text(line)
        return cls._matches_token_rules(norm, cls.CHUNG_TOKEN_RULES)

    @classmethod
    def _is_cu_the_heading(cls, line: str) -> bool:
        norm = cls._normalize_heading_text(line)
        return cls._matches_token_rules(norm, cls.CU_THE_TOKEN_RULES)
    @staticmethod
    def _strip_diacritics(text: str) -> str:
        text = unicodedata.normalize("NFD", text)
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        return text.replace("đ", "d").replace("Đ", "D")

    @classmethod
    def _normalize_heading_text(cls, raw: str) -> str:
        text = re.sub(r"^#{1,6}\s*", "", raw)                 # bỏ markdown #
        text = re.sub(r"^\**\s*[IVXLC]+\.\s*", "", text)      # bỏ số La Mã "I.", "II."
        text = re.sub(r"^\**\s*\d+\.\s*", "", text)           # bỏ số "1.", "2."
        text = re.sub(r"\*+", "", text)                       # bỏ bold **
        text = cls._strip_diacritics(text.lower()).strip()
        return text.rstrip(".,:;")

    @staticmethod
    def _is_heading_line(line: str) -> bool:
        return bool(re.match(r"^#{1,6}\s+\S", line.strip()))

    @staticmethod
    def _heading_level(line: str) -> int:
        return len(re.match(r"^#+", line.strip()).group())

    def classify_loai_2_3(self, remaining_chunks: list[tuple[str, str]]):
        """
        remaining_chunks: list (uid, content) theo đúng thứ tự, đã loại bỏ loai_1 (từ stage 1).

        Trả về 3 nhóm:
        - extra_loai_1_uids: đoạn nằm TRƯỚC heading "tình hình chung" (nếu có) -- đoạn mô tả 
          ngắn/kính gửi còn sót lại giữa loai_1(đầu) và loai_2. Nhóm này sẽ được GỘP VÀO loai_1 
          ở bước run_paragraphing, KHÔNG gộp vào loai_3.
        - loai_2_uids: toàn bộ nội dung từ heading "tình hình chung" đến trước heading 
          "kiến nghị cụ thể".
        - loai_3_uids: phần còn lại (từ heading "kiến nghị cụ thể" trở đi).
        """
        n = len(remaining_chunks)
        start_idx = None
        end_idx = None

        for i, (uid, line) in enumerate(remaining_chunks):
            if self._is_heading_line(line) and self._is_chung_heading(line):
                start_idx = i
                break

        if start_idx is not None:
            for j in range(start_idx + 1, n):
                _, line = remaining_chunks[j]
                if self._is_heading_line(line) and self._is_cu_the_heading(line):
                    end_idx = j
                    break

        if start_idx is None:
            # Không tìm thấy mục "chung" -> không có extra, toàn bộ là loai_3
            extra_loai_1_uids = []
            loai_2_uids = []
            loai_3_uids = [uid for uid, _ in remaining_chunks]
        else:
            if end_idx is None:
                end_idx = n  # không tìm thấy điểm kết -> loai_2 chạy tới hết văn bản

            # Đoạn TRƯỚC start_idx -> gộp vào loai_1, KHÔNG phải loai_3
            extra_loai_1_uids = [uid for uid, _ in remaining_chunks[:start_idx]]
            loai_2_uids = [uid for uid, _ in remaining_chunks[start_idx:end_idx]]
            loai_3_uids = [uid for uid, _ in remaining_chunks[end_idx:]]

        return extra_loai_1_uids, loai_2_uids, loai_3_uids

    # ==========================================================
    # Validate tổng hợp (chạy sau khi kết hợp cả 2 stage, để chắc chắn)
    # ==========================================================
    def validate_grouping(self, result: dict, all_uids: list[str]) -> tuple[bool, str]:
        l1, l2, l3 = result.get("loai_1", []), result.get("loai_2", []), result.get("loai_3", [])

        combined = l1 + l2 + l3
        if len(combined) != len(set(combined)):
            return False, "Có Uk bị gán trùng vào nhiều nhóm"
        if set(combined) != set(all_uids):
            missing = set(all_uids) - set(combined)
            extra = set(combined) - set(all_uids)
            return False, f"Thiếu: {missing}, Thừa/hallucinated: {extra}"

        label_of = {}
        for u in l1: label_of[u] = 1
        for u in l2: label_of[u] = 2
        for u in l3: label_of[u] = 3

        ordered = sorted(all_uids, key=lambda u: int(u[1:]))
        seq = [label_of[u] for u in ordered]

        runs = []
        for x in seq:
            if not runs or runs[-1] != x:
                runs.append(x)

        num_2_runs = sum(1 for r in runs if r == 2)
        if num_2_runs > 1:
            return False, f"loai_2 xuất hiện thành nhiều cụm rời rạc, runs={runs}"

        return True, "OK"

    def reconstruct_and_check(self, chunks, result):
        """Ráp lại 3 phần theo đúng vị trí gốc để double-check không mất nội dung."""
        content_map = dict(chunks)

        def get_text(uids):
            uids_sorted = sorted(uids, key=lambda u: int(u[1:]))
            return "\n".join(content_map[u] for u in uids_sorted)

        loai_1_text = get_text(result["loai_1"])
        loai_2_text = get_text(result.get("loai_2", []))
        loai_3_text = get_text(result["loai_3"])

        total_lines = len(result["loai_1"]) + len(result.get("loai_2", [])) + len(result["loai_3"])
        if total_lines != len(chunks):
            raise ValueError("Mất/lệch nội dung so với bản gốc")

        return loai_1_text, loai_2_text, loai_3_text

    @staticmethod
    def _protect_tail_metadata(all_uids: list[str], loai_1_uids: list[str], content_map: dict[str, str]) -> list[str]:
        """Bảo vệ phần đuôi: Đảm bảo không cắt nhầm đoạn kiến nghị gần cuối vào metadata."""
        footer_cues = [
            "trên đây là", "nơi nhận", "tm.", "kt.",
            "trưởng đoàn", "phó trưởng đoàn", "kính báo cáo", "trân trọng báo cáo"
        ]
        head_uids = []
        tail_candidates = []
        half_len = len(all_uids) // 2
        for u in sorted(all_uids, key=lambda x: int(x[1:])):
            if u in loai_1_uids:
                if int(u[1:]) <= half_len:
                    head_uids.append(u)
                else:
                    tail_candidates.append(u)

        real_tail_start = None
        for u in tail_candidates:
            text_lower = content_map[u].lower()
            if any(cue in text_lower for cue in footer_cues):
                real_tail_start = int(u[1:])
                break

        tail_uids = [
            u for u in tail_candidates if int(u[1:]) >= real_tail_start
        ] if real_tail_start is not None else tail_candidates
        return head_uids + tail_uids

    # ==========================================================
    # Entry point — GIỮ NGUYÊN format input/output cho pipeline
    # ==========================================================
    async def run_paragraphing(self, md_text: str):
        chunks, _, _ = self.chunk_by_line(md_text)
        all_uids = [uid for uid, _ in chunks]
        content_map = dict(chunks)

        # ---- Stage 1: LLM xác định loai_1 (metadata) ----
        prompt_body = self.build_prompt(chunks)
        loai_1_result = await self._call_llm_loai_1(prompt_body)
        loai_1_uids = loai_1_result["loai_1"]

        # Bảo vệ phần đuôi: Đảm bảo không cắt nhầm đoạn kiến nghị gần cuối vào metadata
        loai_1_uids = self._protect_tail_metadata(all_uids, loai_1_uids, content_map)
        loai_1_set = set(loai_1_uids)

        # ---- Stage 2: phân loại loai_2 / loai_3 trên phần còn lại ----
        remaining_chunks = [(uid, content_map[uid]) for uid in all_uids if uid not in loai_1_set]
        extra_loai_1_uids, loai_2_uids, loai_3_uids = self.classify_loai_2_3(remaining_chunks)

        # Gộp đoạn nằm giữa loai_1(đầu) và loai_2 vào loai_1, không gộp vào loai_3
        loai_1_uids = loai_1_uids + extra_loai_1_uids

        result = {"loai_1": loai_1_uids, "loai_2": loai_2_uids, "loai_3": loai_3_uids}

        ok, msg = self.validate_grouping(result, all_uids)
        if not ok:
            print(f"[WARN] Validation grouping notice: {msg}")

        loai_1_text, loai_2_text, loai_3_text = self.reconstruct_and_check(chunks, result)

        # Đảm bảo các kiến nghị thuộc Phần I (nếu có trong loai_2) không bị bỏ sót khi đưa vào chunking
        if loai_2_text and ("kiến nghị" in loai_2_text.lower() or "đề nghị" in loai_2_text.lower()):
            full_main_content = f"{loai_2_text}\n\n{loai_3_text}".strip()
        else:
            full_main_content = loai_3_text

        return {"metadata": loai_1_text, "summary": loai_2_text, "main_content": full_main_content}