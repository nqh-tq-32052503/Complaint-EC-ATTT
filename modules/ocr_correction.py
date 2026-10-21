from __future__ import annotations

import os
import asyncio
import difflib
import json
import re
import unicodedata
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Any

logger = logging.getLogger(__name__)

from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
from modules.prompts import (
    OCR_METADATA_ALIGNMENT,
    OCR_CONTENT_COMPLAINT_SYSTEM_PROMPT,
    get_ocr_metadata_alignment_prompt,
)

@dataclass
class DiffCorrectionResult:
    corrected_text: str
    raw_diff: dict[str, str] = field(default_factory=dict)
    applied: dict[str, str] = field(default_factory=dict)
    skipped_ambiguous: dict[str, str] = field(default_factory=dict)
    skipped_not_found: dict[str, str] = field(default_factory=dict)


def _apply_diff(content: str, diff: dict[str, str], strict: bool = True) -> DiffCorrectionResult:
    """Reconstruct the corrected string by applying {wrong_phrase: right_phrase}
    pairs against the original content.

    strict=True  -> ambiguous keys (>=2 occurrences) are SKIPPED (left
                     unmodified) and logged for manual review. Favors safety
                     (no wrong-position edits) over full automation.
    strict=False -> ambiguous keys are resolved by replacing the first
                     occurrence only.
    """
    result = DiffCorrectionResult(corrected_text=content, raw_diff=diff)
    text = content

    for wrong, right in diff.items():
        occurrences = text.count(wrong)
        if occurrences == 0:
            result.skipped_not_found[wrong] = right
            print("Diff key not found in content, skipped: %r -> %r", wrong, right)
            continue
        if occurrences > 1 and strict:
            result.skipped_ambiguous[wrong] = right
            print("Diff key ambiguous (%d occurrences), skipped: %r -> %r", occurrences, wrong, right)
            continue
        text = text.replace(wrong, right, 1)
        result.applied[wrong] = right

    result.corrected_text = text
    return text

def _build_metadata_schema(metadata: dict[str, Any]) -> dict:
    """Build a JSON Schema mirroring the input's keys/types for guided decoding.
    Enforces that the model cannot add, drop, or rename keys, or change a
    string field into null (or vice versa)."""
    props: dict[str, Any] = {}
    for k, v in metadata.items():
        props[k] = {"type": ["string", "null"]} if v is None else {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "metadata": {
                "type": "object",
                "properties": props,
                "required": list(metadata.keys()),
                "additionalProperties": False,
            }
        },
        "required": ["metadata"],
        "additionalProperties": False,
    }


class OCRCorrector:
    def __init__(self, max_concurrency=50):
        self.max_concurrency = max_concurrency
        self.model_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1")
        self.model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")
        self.api_key = os.environ.get("LLM_GATEWAY_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        self.client = AsyncOpenAI(base_url=self.model_url, api_key=self.api_key or "empty")
        self.metadata_prompt = get_ocr_metadata_alignment_prompt()

    async def correct_metadata(self, metadata: dict[str, Any], max_retries=3) -> dict[str, Any]:
        """Correct diacritics in a single metadata dict.

        Always overrides 'signer' with the original input value after parsing,
        regardless of what the model returned -- defense in depth beyond the
        prompt-level exclusion rule.
        """
        payload = {"metadata": metadata}
        schema = _build_metadata_schema(metadata)
        for attempt in range(max_retries + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": self.metadata_prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    temperature=0.0,
                    max_tokens=2048,
                    extra_body={"guided_json": schema},
                    timeout=30.0
                )
                raw = resp.choices[0].message.content
                parsed = json.loads(raw)
                corrected = parsed.get("metadata", metadata)

                # Structural safety check: reject a key set different from input.
                if set(corrected.keys()) != set(metadata.keys()):
                    raise ValueError(
                        f"Key mismatch: got {set(corrected.keys())}, expected {set(metadata.keys())}"
                    )

                # Hard override: 'signer' must never be modified, regardless of model output.
                if "signer" in metadata:
                    corrected["signer"] = metadata["signer"]

                return corrected

            except Exception as e:  # JSON parse error, schema violation, API error, etc.
                print("correct_metadata attempt %d failed: %s", attempt, e)
        return metadata


    async def _correct_content_diff_for_one_paragraph(self, content: str, strict: bool = True, max_retries=3) -> DiffCorrectionResult:
        """Correct diacritics in a single content chunk using the diff-based prompt.

        No guided_json here: valid model outputs are a union of the bare literal
        "GOOD" and a JSON object, which JSON Schema cannot express directly.
        Reliability instead depends on temperature=0 plus defensive parsing.
        """
        for _ in range(max_retries + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": OCR_CONTENT_COMPLAINT_SYSTEM_PROMPT},
                        {"role": "user", "content": content},
                    ],
                    temperature=0, 
                    max_tokens=8192,
                )
                raw = (resp.choices[0].message.content or "").strip()

                if raw.upper() == "GOOD":
                    return content

                # Defensive parsing: strip accidental code fences if the model adds them
                # despite the instruction not to.
                cleaned = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                diff = json.loads(cleaned)
                if not isinstance(diff, dict):
                    raise ValueError(f"Expected a JSON object, got {type(diff)}")

                return _apply_diff(content, diff, strict=strict)

            except Exception as exc:
                logger.debug("Không thể áp dụng diff correction cho đoạn: %s", exc)
        return content
    
    async def correct_content(self, contents: list[str], strict: bool = True,) -> list[DiffCorrectionResult]:
        """Process a list of content chunks concurrently, bounded by
        cfg.max_concurrency, preserving input order in the output list."""
        sem = asyncio.Semaphore(self.max_concurrency)

        async def _worker(text: str) -> DiffCorrectionResult:
            async with sem:
                return await self._correct_content_diff_for_one_paragraph(text, strict=strict)

        return await asyncio.gather(*(_worker(c) for c in contents))
