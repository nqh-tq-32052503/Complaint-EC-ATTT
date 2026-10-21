import asyncio
from typing import Optional, Dict, Any, List
from tasks.celery_app import celery_app
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
import time

# Worker process instance singletons (initialized lazily per worker process)
_pdf_extractor: Optional[PDFExtractor] = None
_tree_parser: Optional[TreeParser] = None
_semantic_chunker: Optional[SemanticChunker] = None
_heading_extractor: Optional[HeadingExtractor] = None
_complaint_classifier: Optional[ComplaintClassifier] = None
_metadata_extractor: Optional[MetadataExtractor] = None
_heading_normalizer: Optional[HeadingNormalizer] = None
_paragrapher: Optional[Paragrapher] = None


def get_tree_parser() -> TreeParser:
    global _tree_parser
    if _tree_parser is None:
        _tree_parser = TreeParser()
    return _tree_parser


def get_pdf_extractor() -> PDFExtractor:
    global _pdf_extractor
    if _pdf_extractor is None:
        _pdf_extractor = PDFExtractor()
    return _pdf_extractor


def get_semantic_chunker() -> SemanticChunker:
    global _semantic_chunker
    if _semantic_chunker is None:
        _semantic_chunker = SemanticChunker()
    return _semantic_chunker


def get_heading_extractor() -> HeadingExtractor:
    global _heading_extractor
    if _heading_extractor is None:
        _heading_extractor = HeadingExtractor()
    return _heading_extractor


def get_complaint_classifier() -> ComplaintClassifier:
    global _complaint_classifier
    if _complaint_classifier is None:
        _complaint_classifier = ComplaintClassifier()
    return _complaint_classifier


def get_metadata_extractor() -> MetadataExtractor:
    global _metadata_extractor
    if _metadata_extractor is None:
        _metadata_extractor = MetadataExtractor()
    return _metadata_extractor


def get_heading_normalizer() -> HeadingNormalizer:
    global _heading_normalizer
    if _heading_normalizer is None:
        _heading_normalizer = HeadingNormalizer()
    return _heading_normalizer


def get_paragrapher() -> Paragrapher:
    global _paragrapher
    if _paragrapher is None:
        _paragrapher = Paragrapher()
    return _paragrapher


def run_async(coro):
    """Utility to run an async coroutine inside a synchronous Celery task worker."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Celery Tasks for Pipeline
# ---------------------------------------------------------------------------

@celery_app.task(name="tasks.pipeline_tasks.detect_tables_task", bind=True, max_retries=3)
def detect_tables_task(self, pdf_input: Any) -> tuple:
    """Step 0: Table Detection Task (Queue: workerX_queue)"""
    try:
        pdf_extractor = get_pdf_extractor()
        has_table, tables = pdf_extractor.detect_tables(pdf_input)
        print(f"[Celery Worker] Table Detection finished (has_table={has_table}, count={len(tables)})")
        return has_table, tables
    except Exception as exc:
        print(f"[Celery Worker Error] Table Detection failed: {exc}")
        raise self.retry(exc=exc, countdown=1)


@celery_app.task(name="tasks.pipeline_tasks.full_pipeline_task", bind=True, max_retries=3)
def full_pipeline_task(self, pdf_input: Any, log_dir: Optional[str] = None, use_cache: bool = True) -> Dict[str, Any]:
    """Single unified Celery task running the full tree-native pipeline end-to-end for a single PDF file."""
    execution_times = {}
    t_pipeline_start = time.perf_counter()
    try:
        pdf_extractor = get_pdf_extractor()
        tree_parser = get_tree_parser()
        metadata_extractor = get_metadata_extractor()
        complaint_classifier = get_complaint_classifier()

        # Step 1: OCR Extraction (Nhận Canonical JSON cấu trúc cây)
        t0 = time.perf_counter()
        canonical_data, is_text_based = run_async(pdf_extractor.run_ocr(pdf_input, use_cache=use_cache))
        execution_times["step_1_ocr"] = round(time.perf_counter() - t0, 4)
        print(f"[Celery Worker] Step 1 OCR finished (is_text_based={is_text_based})")
        if log_dir:
            write_json(log_dir + "/1.canonical_ocr.json", canonical_data)

        # Step 2: Parse Cây Văn Bản & Kiểm tra bảng biểu
        t0 = time.perf_counter()
        metadata_text, candidate_units, has_table = tree_parser.parse_document_tree(canonical_data)
        execution_times["step_2_tree_parsing"] = round(time.perf_counter() - t0, 4)

        if has_table:
            print("[Celery Worker] Phát hiện bảng biểu trong cây OCR. Dừng pipeline.")
            raise TableContentUnsupportedError(
                "File PDF chứa bảng biểu (table), hiện tại hệ thống chưa hỗ trợ trích xuất dữ liệu từ bảng."
            )

        if log_dir:
            write_txt(log_dir + "/2.metadata_text.txt", metadata_text)
            write_json(
                log_dir + "/2.candidate_units.json",
                [
                    {
                        "candidate_id": u.candidate_id,
                        "breadcrumbs": u.breadcrumbs,
                        "content": u.content,
                    }
                    for u in candidate_units
                ],
            )

        # Step 3: Metadata Extraction & Complaint Classification chạy song song
        t0 = time.perf_counter()
        async def _execute_parallel():
            meta_task = metadata_extractor.extract(metadata_text, log_dir=log_dir)
            class_task = complaint_classifier.process_tree_candidates(candidate_units, log_dir=log_dir)
            return await asyncio.gather(meta_task, class_task)

        metadata_dictionary, classification_results = run_async(_execute_parallel())
        execution_times["step_3_metadata_and_classification"] = round(time.perf_counter() - t0, 4)
        print("[Celery Worker] Step 3 Metadata & Classification finished")
        if log_dir:
            write_json(log_dir + "/3.metadata.json", metadata_dictionary)
            write_json(log_dir + "/6.classification_results.json", classification_results)

        # Step 4: Postprocessing (Chuẩn hóa CQTQ, tự động thêm Bộ Nội Vụ, Văn Phòng Chính Phủ)
        t0 = time.perf_counter()
        postprocessed_results = postprocess_results(classification_results)
        execution_times["step_4_postprocessing"] = round(time.perf_counter() - t0, 4)
        execution_times["total_pipeline"] = round(time.perf_counter() - t_pipeline_start, 4)
        print("[Celery Worker] Step 4 Postprocessing finished (Full Pipeline completed)")
        if log_dir:
            write_json(log_dir + "/9.postprocessed_results.json", postprocessed_results)
            write_json(log_dir + "/execution_times.json", execution_times)

        return {
            "postprocessed_results": postprocessed_results,
            "metadata_dictionary": metadata_dictionary,
            "execution_times": execution_times,
        }
    except TableContentUnsupportedError:
        raise
    except Exception as exc:
        print(f"[Celery Worker Error] Full Pipeline Task failed: {exc}")
        raise self.retry(exc=exc, countdown=1)


@celery_app.task(name="tasks.pipeline_tasks.step_1_ocr_task", bind=True, max_retries=3)
def step_1_ocr_task(self, pdf_input: Any, log_dir: Optional[str] = None, process_table: bool = False, keep_phu_luc: bool = False, use_cache: bool = True):
    """Step 1: OCR Extraction Task (Queue: step_1_ocr)"""
    try:
        pdf_extractor = get_pdf_extractor()
        canonical_data, is_text_based = run_async(pdf_extractor.run_ocr(pdf_input, process_table=process_table, keep_phu_luc=keep_phu_luc, use_cache=use_cache))
        print(f"[Celery Worker] Step 1 OCR finished (is_text_based={is_text_based})")
        if log_dir:
            write_json(log_dir + "/1.canonical_ocr.json", canonical_data)
        return canonical_data, is_text_based
    except TableContentUnsupportedError:
        raise
    except Exception as exc:
        print(f"[Celery Worker Error] Step 1 OCR failed: {exc}")
        raise self.retry(exc=exc, countdown=1)


@celery_app.task(name="tasks.pipeline_tasks.step_2_paragraphing_task", bind=True, max_retries=3)
def step_2_paragraphing_task(self, pdf_content: str, log_dir: Optional[str] = None) -> Dict[str, str]:
    """Step 2: Paragraphing Task (Queue: step_2_paragraphing)"""
    try:
        paragrapher = get_paragrapher()
        paragraphs = run_async(paragrapher.run_paragraphing(pdf_content))
        metadata_paragraph = paragraphs["metadata"]
        summary_paragraph = paragraphs["summary"]
        main_content = paragraphs["main_content"]
        if log_dir:
            write_markdown(log_dir + "/2.paragraph_metadata.md", metadata_paragraph)
            write_markdown(log_dir + "/2.paragraph_summary.md", summary_paragraph)
            write_markdown(log_dir + "/2.paragraph_main_content.md", main_content)
        print("[Celery Worker] Step 2 Paragraphing finished")
        return paragraphs
    except Exception as exc:
        print(f"[Celery Worker Error] Step 2 Paragraphing failed: {exc}")
        raise self.retry(exc=exc, countdown=1)


@celery_app.task(name="tasks.pipeline_tasks.step_3_chunk_and_norm_task", bind=True, max_retries=3)
def step_3_chunk_and_norm_task(self, main_content: str, log_dir: Optional[str] = None) -> Dict[str, Any]:
    """Step 3: Semantic Chunking & Heading Normalization Task (Queue: step_3_chunk_and_norm)"""
    try:
        semantic_chunker = get_semantic_chunker()
        heading_normalizer = get_heading_normalizer()

        async def _execute_parallel():
            task1 = semantic_chunker.run_chunking(main_content)
            task2 = heading_normalizer.normalize(main_content, log_dir)
            return await asyncio.gather(task1, task2)

        raw_chunks, normalized_main_content = run_async(_execute_parallel())
        if log_dir:
            write_json(log_dir + "/3.semantic_chunking.json", raw_chunks)
            write_markdown(log_dir + "/3.normalized_main_content.md", normalized_main_content)
        print("[Celery Worker] Step 3 Chunking & Normalization finished")
        return {
            "raw_chunks": raw_chunks,
            "normalized_main_content": normalized_main_content,
        }
    except Exception as exc:
        print(f"[Celery Worker Error] Step 3 Chunk & Norm failed: {exc}")
        raise self.retry(exc=exc, countdown=1)


@celery_app.task(name="tasks.pipeline_tasks.step_4_heading_extraction_task", bind=True, max_retries=3)
def step_4_heading_extraction_task(
    self, raw_chunks: List[Dict[str, Any]], normalized_main_content: str, log_dir: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Step 4: Heading Extraction Task (Queue: step_4_heading)"""
    try:
        heading_extractor = get_heading_extractor()
        chunks_with_heading = heading_extractor.extract_heading(raw_chunks, normalized_main_content)
        if log_dir:
            write_json(log_dir + "/4.chunks_with_heading.json", chunks_with_heading)
        print("[Celery Worker] Step 4 Heading Extraction finished")
        return chunks_with_heading
    except Exception as exc:
        print(f"[Celery Worker Error] Step 4 Heading Extraction failed: {exc}")
        raise self.retry(exc=exc, countdown=1)


@celery_app.task(name="tasks.pipeline_tasks.step_6_metadata_and_classification_task", bind=True, max_retries=3)
def step_6_metadata_and_classification_task(
    self, metadata_input: str, units_or_chunks: List[Any], log_dir: Optional[str] = None
) -> Dict[str, Any]:
    """Step 5 (Classification): Metadata Extraction & Complaint Classification Task (Queue: step_6_classification)"""
    try:
        metadata_extractor = get_metadata_extractor()
        complaint_classifier = get_complaint_classifier()

        async def _execute_parallel():
            meta_task = metadata_extractor.extract(metadata_input, log_dir=log_dir)
            if units_or_chunks and (
                isinstance(units_or_chunks[0], CandidateUnit)
                or (isinstance(units_or_chunks[0], dict) and ("breadcrumbs" in units_or_chunks[0] or "candidate_id" in units_or_chunks[0]))
            ):
                class_task = complaint_classifier.process_tree_candidates(units_or_chunks, log_dir=log_dir)
            else:
                class_task = complaint_classifier.run(units_or_chunks, log_dir)
            return await asyncio.gather(meta_task, class_task)

        metadata_dictionary, classification_results = run_async(_execute_parallel())
        if log_dir:
            write_json(log_dir + "/3.metadata.json", metadata_dictionary)
            write_json(log_dir + "/6.classification_results.json", classification_results)
        print("[Celery Worker] Step 5 Metadata & Classification finished")
        return {
            "metadata_dictionary": metadata_dictionary,
            "classification_results": classification_results,
        }
    except Exception as exc:
        print(f"[Celery Worker Error] Step 5 Metadata & Classification failed: {exc}")
        raise self.retry(exc=exc, countdown=1)


@celery_app.task(name="tasks.pipeline_tasks.step_7_postprocessing_task", bind=True, max_retries=3)
def step_7_postprocessing_task(
    self, classification_results: List[Dict[str, Any]], log_dir: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Step 6 (Postprocessing): Postprocess Results Task (Queue: step_7_postprocessing)"""
    try:
        postprocessed_results = postprocess_results(classification_results)
        if log_dir:
            write_json(log_dir + "/9.postprocessed_results.json", postprocessed_results)
        print("[Celery Worker] Step 6 Postprocessing finished")
        return postprocessed_results
    except Exception as exc:
        print(f"[Celery Worker Error] Step 6 Postprocessing failed: {exc}")
        raise self.retry(exc=exc, countdown=1)

