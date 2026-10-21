import os
import json
import re
import time
import hashlib
import asyncio
from collections import Counter
from typing import Optional, Any, Tuple, Dict, List

from modules.pdf_extractor import PDFExtractor, TableContentUnsupportedError
from modules.tree_parser import TreeParser, CandidateUnit
from modules.semantic_chunker import SemanticChunker
from modules.heading_extractor import HeadingExtractor
from modules.complaint_classifier import ComplaintClassifier
from modules.metadata_extractor import MetadataExtractor
from modules.postprocessor import postprocess_results
from modules.heading_normalizer import HeadingNormalizer
from modules.paragraphing import Paragrapher
from modules.chunking_utils import write_markdown, write_json, write_txt

from tasks.pipeline_tasks import (
    full_pipeline_task,
    detect_tables_task,
    step_1_ocr_task,
    step_2_paragraphing_task,
    step_3_chunk_and_norm_task,
    step_4_heading_extraction_task,
    step_6_metadata_and_classification_task,
    step_7_postprocessing_task,
)


def file_hash(path_or_bytes: str | bytes, chunk_size: int = 1024 * 1024) -> str:
    if hasattr(hashlib, "blake3"):
        h = hashlib.blake3()
    else:
        h = hashlib.sha256()
    if isinstance(path_or_bytes, bytes):
        h.update(path_or_bytes)
        return h.hexdigest()
    with open(path_or_bytes, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def wait_for_celery_task(task_result: Any, timeout: float = 600.0, poll_interval: float = 0.2) -> Any:
    """
    Chờ Celery task hoàn thành bằng cách poll task_result.ready() qua Redis GET command.
    Hàm này 100% thread-safe khi gọi song song từ nhiều coroutine/thread.
    """
    start_time = time.perf_counter()
    while not task_result.ready():
        if time.perf_counter() - start_time > timeout:
            raise TimeoutError(f"Celery task {task_result.id} timed out after {timeout}s")
        time.sleep(poll_interval)
    return task_result.get(propagate=True)


class Pipeline(object):
    NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "8"))
    WORKER_QUEUES = [f"x2.dannguyen.uc1.worker{i}_queue" for i in range(1, NUM_WORKERS + 1)]
    _request_counter = 0

    def __init__(self, use_celery: Optional[bool] = None):
        if use_celery is None:
            self.use_celery = os.environ.get("USE_CELERY", "true").lower() == "true"
        else:
            self.use_celery = use_celery

        # In-process module instances used for fallback when use_celery=False
        self.pdf_extractor = PDFExtractor()
        self.tree_parser = TreeParser()
        self.semantic_chunker = SemanticChunker()
        self.heading_extractor = HeadingExtractor()
        self.complaint_classifier = ComplaintClassifier()
        self.metadata_extractor = MetadataExtractor()
        self.heading_normalizer = HeadingNormalizer()
        self.paragrapher = Paragrapher()

    def postprocess(self, results):
        """
        Hậu xử lý kết quả:
        1) Tự động bổ sung 'Bộ Nội Vụ' và 'Văn Phòng Chính Phủ' nếu chưa có trong kết quả.
        2) Chuẩn hóa tên cơ quan theo danh sách CQTQ.txt.
        """
        return postprocess_results(results)

    async def process_metadata(self, metadata_paragraph: str, log_dir: str):
        return await self.metadata_extractor.extract(metadata_paragraph, log_dir=log_dir)

    def _resolve_celery_readiness(self, use_celery_param: Optional[bool]) -> bool:
        """Kiểm tra và quyết định chế độ chạy Celery hay In-process."""
        active_use = self.use_celery if use_celery_param is None else use_celery_param
        if not active_use:
            return False

        from modules.celery_health import check_celery_and_queue_health
        is_healthy, health_reason = check_celery_and_queue_health()
        if not is_healthy:
            print(f"[Celery Health Check] Celery/Queue không khả dụng ({health_reason}). Tự động fallback sang Python in-process.")
            return False
        print(f"[Celery Health Check] Celery/Queue sẵn sàng: {health_reason}")
        return True

    async def _extract_via_celery(
        self,
        pdf_input: Any,
        log_dir: Optional[str],
        use_cache: bool,
        t_pipeline_start: float,
    ) -> Tuple[Any, Any, Dict[str, float]]:
        """Điều phối và thực thi tác vụ full pipeline phân tán qua Celery."""
        target_queue = Pipeline.WORKER_QUEUES[Pipeline._request_counter % len(Pipeline.WORKER_QUEUES)]
        Pipeline._request_counter += 1
        print(f"[INFO] Assigning request to Celery Worker Queue: {target_queue}")

        task = full_pipeline_task.apply_async(args=[pdf_input, log_dir, use_cache], queue=target_queue, priority=5)
        step_res = await asyncio.to_thread(wait_for_celery_task, task)
        postprocessed_results = step_res["postprocessed_results"]
        metadata_dictionary = step_res["metadata_dictionary"]
        execution_times = step_res.get("execution_times", {})
        execution_times["total_pipeline"] = round(time.perf_counter() - t_pipeline_start, 4)

        if log_dir:
            write_json(log_dir + "/3.metadata.json", metadata_dictionary)
            write_json(log_dir + "/9.postprocessed_results.json", postprocessed_results)
            write_json(log_dir + "/execution_times.json", execution_times)

        return postprocessed_results, metadata_dictionary, execution_times

    async def _run_ocr_step(
        self, pdf_input: Any, use_cache: bool, max_retries: int, log_dir: Optional[str]
    ) -> Any:
        """Thực thi OCR trích xuất Canonical OCR Tree với retry."""
        canonical_data = None
        for attempt in range(1, max_retries + 1):
            try:
                canonical_data, is_text_based = await self.pdf_extractor.run_ocr(pdf_input, use_cache=use_cache)
                print(f"[INFO] Finish OCR Canonical Tree (is_text_based={is_text_based})")
                break
            except TableContentUnsupportedError:
                raise
            except Exception as e:
                print(f"[WARNING] OCR failed (attempt {attempt}/{max_retries}): {e}")
                if attempt == max_retries:
                    raise e
                await asyncio.sleep(1)

        if log_dir:
            write_json(log_dir + "/1.canonical_ocr.json", canonical_data)
        return canonical_data

    async def _run_metadata_and_classification_step(
        self,
        metadata_text: str,
        candidate_units: List[CandidateUnit],
        max_retries: int,
        log_dir: Optional[str],
    ) -> Tuple[Any, Any]:
        """Trích xuất metadata và phân loại kiến nghị song song với retry."""
        metadata_dict, class_results = None, None
        for attempt in range(1, max_retries + 1):
            try:
                meta_task = self.process_metadata(metadata_text, log_dir=log_dir)
                class_task = self.complaint_classifier.process_tree_candidates(candidate_units, log_dir=log_dir)
                metadata_dict, class_results = await asyncio.gather(meta_task, class_task)
                if log_dir:
                    write_json(log_dir + "/3.metadata.json", metadata_dict)
                    write_json(log_dir + "/6.classification_results.json", class_results)
                print("[INFO] Metadata & Complaint Classification completed")
                return metadata_dict, class_results
            except Exception as e:
                print(f"[WARNING] Classification failed (attempt {attempt}/{max_retries}): {e}")
                if attempt == max_retries:
                    raise e
                await asyncio.sleep(1)
        return metadata_dict, class_results

    async def _run_postprocessing_step(
        self, class_results: Any, max_retries: int, log_dir: Optional[str]
    ) -> Any:
        """Hậu xử lý kết quả phân loại với retry."""
        postprocessed = None
        for attempt in range(1, max_retries + 1):
            try:
                postprocessed = self.postprocess(class_results)
                if log_dir:
                    write_json(log_dir + "/9.postprocessed_results.json", postprocessed)
                print("[INFO] Postprocessing completed")
                return postprocessed
            except Exception as e:
                print(f"[WARNING] Postprocessing failed (attempt {attempt}/{max_retries}): {e}")
                if attempt == max_retries:
                    raise e
                await asyncio.sleep(1)
        return postprocessed

    async def _extract_in_process(
        self,
        pdf_input: Any,
        log_dir: Optional[str],
        max_retries: int,
        use_cache: bool,
        t_pipeline_start: float,
    ) -> Tuple[Any, Any, Dict[str, float]]:
        """Thực thi pipeline trực tiếp trong tiến trình Python nội bộ (In-process fallback)."""
        execution_times = {}

        # Step 1: OCR
        t0 = time.perf_counter()
        canonical_data = await self._run_ocr_step(pdf_input, use_cache, max_retries, log_dir)
        execution_times["step_1_ocr"] = round(time.perf_counter() - t0, 4)

        # Step 2: Parse Cây Văn Bản
        t0 = time.perf_counter()
        metadata_text, candidate_units, has_table = self.tree_parser.parse_document_tree(canonical_data)
        execution_times["step_2_tree_parsing"] = round(time.perf_counter() - t0, 4)

        if has_table:
            print("[Pipeline Warning] Phát hiện bảng biểu trong cây OCR. Dừng xử lý pipeline.")
            raise TableContentUnsupportedError(
                "File PDF chứa bảng biểu (table), hiện tại hệ thống chưa hỗ trợ trích xuất dữ liệu từ bảng."
            )

        if log_dir:
            write_txt(log_dir + "/2.metadata_text.txt", metadata_text)
            write_json(
                log_dir + "/2.candidate_units.json",
                [{"candidate_id": u.candidate_id, "breadcrumbs": u.breadcrumbs, "content": u.content} for u in candidate_units],
            )

        # Step 3: Metadata & Classification
        t0 = time.perf_counter()
        metadata_dict, class_results = await self._run_metadata_and_classification_step(
            metadata_text, candidate_units, max_retries, log_dir
        )
        execution_times["step_3_metadata_and_classification"] = round(time.perf_counter() - t0, 4)

        # Step 4: Postprocessing
        t0 = time.perf_counter()
        postprocessed = await self._run_postprocessing_step(class_results, max_retries, log_dir)
        execution_times["step_4_postprocessing"] = round(time.perf_counter() - t0, 4)

        execution_times["total_pipeline"] = round(time.perf_counter() - t_pipeline_start, 4)
        if log_dir:
            write_json(log_dir + "/execution_times.json", execution_times)

        return postprocessed, metadata_dict, execution_times

    async def extract(
        self,
        pdf_input: Any,
        log_dir: Optional[str] = None,
        max_retries: int = 3,
        use_celery: Optional[bool] = None,
        use_cache: bool = True,
    ):
        t_pipeline_start = time.perf_counter()
        active_use_celery = self._resolve_celery_readiness(use_celery)

        if active_use_celery:
            try:
                return await self._extract_via_celery(pdf_input, log_dir, use_cache, t_pipeline_start)
            except TableContentUnsupportedError:
                raise
            except Exception as celery_err:
                print(f"[Celery Runtime Error] Gặp sự cố kết nối/thực thi Celery: {celery_err}. Tự động fallback sang Python.")

        return await self._extract_in_process(pdf_input, log_dir, max_retries, use_cache, t_pipeline_start)