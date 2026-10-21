import os
import sys
import asyncio
from openai import AsyncOpenAI
from modules.chunking_utils import Unit, segment_units, resolve_chunk_text, validate_and_resolve, segment_units_simple
from modules.prompts import SEMANTIC_CHUNKING_SYSTEM_PROMPT

class SemanticChunker(object):
    def __init__(self):
        self.model_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1")
        self.model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")
        self.model_api_key = os.environ.get("LLM_GATEWAY_API_KEY", os.environ.get("OPENAI_API_KEY", ""))    
        self.client = AsyncOpenAI(base_url=self.model_url, api_key=self.model_api_key or "empty")
        
    def _build_prompt(self, units: list[Unit]) -> str:
        return "\n\n".join(f"[{u.id}] {u.text}" for u in units)
    
    async def _call_llm(self, prompt_body: str, temperature: float = 0.0, max_tokens: int = 8000, max_retries=3) -> tuple[str, str]:
        count = 0
        while count < max_retries:
            try:
                async with asyncio.timeout(30.0):
                    response = await self.client.chat.completions.create(
                        model=self.model_name,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        messages=[
                            {"role": "system", "content": SEMANTIC_CHUNKING_SYSTEM_PROMPT},
                            {"role": "user", "content": prompt_body},
                        ],
                    )
                fully_response = response.choices[0].message.content
                if "</think>" in fully_response:
                    thinking_progress, final_answer = fully_response.rsplit("</think>", 1)
                else:
                    final_answer = fully_response
                    thinking_progress = ""
                final_answer = final_answer.strip()
                return thinking_progress, final_answer
            except Exception as e:
                print(e)
                print(f"[RETRY {count + 1}/{max_retries}]")
                count += 1
        return "", ""
            
    async def run_chunking(self, source_text: str):
        if not source_text or not source_text.strip():
            print("[INFO] source_text is empty, returning empty chunks.", file=sys.stderr)
            return []
        print("[1/4] Tách atomic unit (rule-based, over-segment)...", file=sys.stderr)
        print("Source text: ", source_text.replace("\n", r"\n"))
        units = segment_units_simple(source_text)
        units_by_id = {u.id: u for u in units}
        all_ids_ordered = [u.id for u in units]
        print(f"    -> {len(units)} unit.", file=sys.stderr)
        print("[2/4] Gọi LLM để gộp unit thành từng kiến nghị...", file=sys.stderr)
        prompt_body = self._build_prompt(units)
        _, raw_output = await self._call_llm(prompt_body, temperature=0.0, max_tokens=32768)
        
        print("[3/4] Validate referential integrity...", file=sys.stderr)
        validated_groups, report = validate_and_resolve(raw_output, units_by_id, all_ids_ordered)
        for e in report.errors:
            print(f"    !! ERROR: {e}", file=sys.stderr)
        for w in report.warnings:
            print(f"    ?? WARNING: {w}", file=sys.stderr)
        print(f"    -> {len(validated_groups)} kiến nghị được giữ lại, "
                f"{len(report.discarded_ids)} unit bị loại bỏ.", file=sys.stderr)
    
        print("[4/4] Export result", file=sys.stderr)
        chunks = [
            {
                "chunk_id": f"chunk_{i+1}",
                "source_unit_ids": group,
                "content": resolve_chunk_text(group, units_by_id),
            }
            for i, group in enumerate(validated_groups)
        ]
        print("Hoàn tất.", file=sys.stderr)
        return chunks
        
    
    