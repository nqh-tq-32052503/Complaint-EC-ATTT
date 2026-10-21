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
from modules.prompts import METADATA_EXTRACTION_SYSTEM_PROMPT
from datetime import datetime, timezone, timedelta

class MetadataExtractor(object):
    def __init__(self):
        self.model_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1")
        self.model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")
        self.model_api_key = os.environ.get("LLM_GATEWAY_API_KEY", os.environ.get("OPENAI_API_KEY", ""))   
        self.client = AsyncOpenAI(base_url=self.model_url, api_key=self.model_api_key or "empty")
        self.semaphore = asyncio.Semaphore(32) 
    
    async def extract(self, pdf_content: str, max_retries: int = 3, max_tokens: int = 16384, log_dir: str = "") :
        result = {}
        async with self.semaphore:
            for attempt in range(1, max_retries + 1):
                try:
                    response = await self.client.chat.completions.create(
                        model=self.model_name,
                        temperature=0.0,
                        max_tokens=max_tokens,
                        messages=[
                            {"role": "system", "content": METADATA_EXTRACTION_SYSTEM_PROMPT},
                            {"role": "user", "content": pdf_content},
                        ],
                        timeout=30.0
                    )
                    raw_output = response.choices[0].message.content or ""
                    if log_dir:
                        from modules.minio_utils import append_txt_to_minio, append_json_to_minio
                        append_txt_to_minio(log_dir + "/1.5_metadata.txt", raw_output + "\n\n\n")
                    if "</think>" in raw_output:
                        _, final_answer = raw_output.rsplit("</think>", 1)
                    else:
                        final_answer = raw_output
                    final_answer = final_answer.strip()
                    output = parse_and_validate_metadata(final_answer)
                    if output["valid"]:
                        return output["data"]
                    else:
                        if log_dir:
                            append_json_to_minio(log_dir + "/1.5_metadata_logging.json", output)
                    return result

                except (APITimeoutError, RateLimitError, APIError):
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                
import json
import re
from typing import Any, Optional
 
 
REQUIRED_FIELDS = [
    "tinh",
    "hinh_thuc",
    "ky_hop",
    "quoc_hoi_khoa",
    "document_number",
    "issue_date",
    "signer",
    "ten_doan_db",
]
 
VALID_HINH_THUC = {"trước", "sau", "truoc"}
 
# YYYY-MM-DD, kiểm tra hợp lệ ở mức định dạng (không kiểm tra ngày trong tháng chi tiết)
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
 
# Số hiệu văn bản dạng "123/BC-ĐĐBQH" — phải có số phía trước dấu "/"
DOC_NUMBER_PATTERN = re.compile(r"^\d+/.+$")
 
# Khóa Quốc hội: số La Mã hoặc số thường
QH_KHOA_PATTERN = re.compile(r"^[IVXLCDM0-9]+$", re.IGNORECASE)
 
 
def _strip_code_fence(text: str) -> str:
    """Loại bỏ ```json ... ``` hoặc ``` ... ``` nếu LLM lỡ bọc code fence quanh JSON."""
    text = text.strip()
    fence_match = re.match(r"^```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```$", text)
    if fence_match:
        return fence_match.group(1).strip()
    return text
 
 
def _extract_json_object(text: str) -> str:
    """Nếu LLM lỡ thêm text thừa trước/sau, cố gắng cắt ra đúng object JSON đầu tiên."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start:end + 1]
 
 
def parse_and_validate_metadata(raw_output: str) -> dict:
    """
    Parse chuỗi output thô của LLM thành JSON và validate theo schema metadata.
 
    Trả về dict:
        {
            "valid": bool,
            "data": dict | None,   # JSON đã parse (nếu parse được)
            "errors": list[str],   # danh sách lỗi (rỗng nếu hợp lệ)
        }
    """
    cleaned = _strip_code_fence(raw_output)
    cleaned = _extract_json_object(cleaned)
 
    # 1. Parse JSON
    try:
        data = json.loads(cleaned)
        return {
            "valid" : True,
            "data" : data,
            "errors" : []
        }
    except json.JSONDecodeError as e:
        return {
            "valid": False,
            "data": None,
            "errors": [f"Không parse được JSON: {e}"],
        }
 
    