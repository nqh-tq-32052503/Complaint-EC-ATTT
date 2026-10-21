"""
FastAPI service: Trích xuất & phân loại kiến nghị cử tri.

Response schema của endpoint /extract tuân thủ 100% file schema_2608.json
(JSON Schema draft 2020-12):
  - additionalProperties: false  -> model_config = ConfigDict(extra="forbid")
  - required + nullable fields   -> Optional[...] nhưng vẫn bắt buộc có key
  - enum, minLength, minItems/maxItems, uniqueItems -> validator tương ứng

Chạy thử:
    pip install fastapi "pydantic>=2" uvicorn
    # Xem docs tự sinh tại http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import os
import json
import asyncio
import uuid
import logging
import psycopg2
from psycopg2 import pool, sql

logger = logging.getLogger(__name__)
from enum import Enum

from config.env_loader import load_project_env
load_project_env()
from datetime import datetime, timezone, timedelta
from typing import List, Literal, Optional, Dict, Tuple, Any, Annotated
from dataclasses import dataclass, field
import time

from fastapi import Request, FastAPI, File, Form, HTTPException, UploadFile, Response, BackgroundTasks
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from modules.pipeline import Pipeline, file_hash
from modules.pdf_extractor import TableContentUnsupportedError
from modules.postprocessor import should_exclude_by_headers, filter_and_reindex_kien_nghi
from modules.minio_utils import (
    sync_get_json_from_minio,
    sync_write_json_to_minio_bucket,
    MINIO_CACHE_BUCKET,
    MINIO_BUCKET_NAME,
)


# ---------------------------------------------------------------------------
# 1. Pydantic models khớp schema_2608.json
# ---------------------------------------------------------------------------

class ErrorInfo(BaseModel):
    """error: mã lỗi và thông báo lỗi khi không thể trích xuất."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["TABLE_CONTENT_UNSUPPORTED"] = Field(
        ..., description="Mã lỗi"
    )
    message: str = Field(..., description="Thông báo chi tiết lỗi")


class Metadata(BaseModel):
    """metadata: các trường bắt buộc phải có mặt nhưng giá trị có thể null."""

    model_config = ConfigDict(extra="forbid")

    ky_hop: Optional[str] = Field(None, description="Ví dụ: Kỳ họp thứ 9")
    quoc_hoi_khoa: Optional[str] = Field(None, description="Ví dụ: XV")
    hinh_thuc: Optional[str] = Field(None, description="Trước kỳ họp hoặc Sau kỳ họp")
    document_number: Optional[str] = Field(None, description="Số công văn")
    issue_date: Optional[str] = Field(None, description="Ngày ban hành")
    signer: Optional[str] = Field(None, description="Người ký")
    ten_doan_db: Optional[str] = Field(None, description="Tên Đoàn đại biểu Quốc hội")


class DonViNhan(BaseModel):
    """Một ứng viên cơ quan thẩm quyền trong top-5 don_vi_nhan."""

    model_config = ConfigDict(extra="forbid")

    ten_don_vi: str = Field(..., min_length=1, description="Tên cơ quan thẩm quyền")
    muc_do_tin_cay: Literal["high", "medium", "low"] = Field(
        ..., description="Mức độ tin cậy của kết quả: cao (high), trung bình (medium), thấp (low)"
    )
    phuong_phap: str = Field(
        ...,
        min_length=1,
        description="Mã phương pháp tạo ra kết quả, ví dụ: rule_based, llm, hybrid hoặc một phương pháp khác",
    )
    is_mapped: Optional[bool] = Field(
        None,
        description="Trạng thái mapping đơn vị ứng viên với danh sách cơ quan thẩm quyền lấy từ database",
    )


class KienNghiItem(BaseModel):
    """Một kiến nghị trong danh sách kien_nghi."""

    model_config = ConfigDict(extra="forbid")

    stt: int = Field(..., ge=1, description="STT toàn bộ kiến nghị")
    don_vi_nhan: List[DonViNhan] = Field(
        ...,
        min_length=1,
        max_length=5,
        description="Top 5 cơ quan thẩm quyền dự đoán, sắp xếp theo mức độ phù hợp giảm dần",
    )
    so_thu_tu_kien_nghi: int = Field(
        ...,
        ge=1,
        description="STT trong cơ quan thẩm quyền được chọn làm kết quả chính (phần tử đầu tiên của don_vi_nhan)",
    )
    noi_dung: str = Field(..., min_length=1, description="Nội dung kiến nghị")
    headers: List[str] = Field(
        default_factory=list,
        description="Các cấp tiêu đề chứa kiến nghị này",
    )


class KienNghiTongHop(BaseModel):
    """Root object — khớp chính xác schema_2608.json."""

    model_config = ConfigDict(extra="forbid")

    status: Optional[Literal["success", "failed"]] = Field(
        None, description="Trạng thái xử lý trích xuất"
    )
    error: Optional[ErrorInfo] = Field(
        None, description="Thông tin lỗi nếu có"
    )
    metadata: Metadata
    kien_nghi: List[KienNghiItem]
    tong_so_kien_nghi: int = Field(..., ge=0, description="Tổng số kiến nghị")
    cac_don_vi_nhan: List[str] = Field(
        ...,
        description="Danh sách cơ quan thẩm quyền duy nhất xuất hiện trong các kết quả dự đoán",
    )

    @field_validator("cac_don_vi_nhan")
    @classmethod
    def _unique_items(cls, v: List[str]) -> List[str]:
        # uniqueItems: true trong JSON Schema
        if len(v) != len(set(v)):
            raise ValueError("cac_don_vi_nhan phải chứa các giá trị duy nhất (uniqueItems)")
        return v

    @model_validator(mode="after")
    def _consistency_checks(self) -> "KienNghiTongHop":
        # tong_so_kien_nghi phải khớp số phần tử thực tế của kien_nghi
        if self.tong_so_kien_nghi != len(self.kien_nghi):
            raise ValueError(
                f"tong_so_kien_nghi ({self.tong_so_kien_nghi}) phải bằng "
                f"số phần tử trong kien_nghi ({len(self.kien_nghi)})"
            )
        return self



# ---------------------------------------------------------------------------
# 1.1 Job status & response schema cho 2 phương án giao tiếp
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PROCESSING = "PROCESSING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


class ExtractResponse(BaseModel):
    request_id: str
    status: JobStatus
    result: Optional[KienNghiTongHop] = None
    error: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


# PostgreSQL Configuration (Loaded solely from .env)
PG_HOST = os.environ.get("POSTGRES_HOST")
PG_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
PG_DBNAME = os.environ.get("POSTGRES_DB", "shared_projects")
PG_USER = os.environ.get("POSTGRES_USER", "platform_developer")
PG_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
PG_SCHEMA = os.environ.get("POSTGRES_SCHEMA", "x2_dannguyen_u1")
PG_TABLE = os.environ.get("POSTGRES_TABLE", "request_logs")

_pg_pool: Optional[pool.ThreadedConnectionPool] = None


def get_pg_pool() -> pool.ThreadedConnectionPool:
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=PG_HOST,
            port=PG_PORT,
            dbname=PG_DBNAME,
            user=PG_USER,
            password=PG_PASSWORD,
            connect_timeout=5,
        )
    return _pg_pool


def init_db():
    try:
        p = get_pg_pool()
        conn = p.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {};").format(sql.Identifier(PG_SCHEMA))
                )
                cur.execute(
                    sql.SQL("""
                        CREATE TABLE IF NOT EXISTS {}.{} (
                            request_id VARCHAR(255) PRIMARY KEY,
                            status VARCHAR(50) NOT NULL,
                            result JSONB,
                            error TEXT,
                            created_at TIMESTAMPTZ,
                            updated_at TIMESTAMPTZ
                        );
                    """).format(sql.Identifier(PG_SCHEMA), sql.Identifier(PG_TABLE))
                )
                conn.commit()
        finally:
            p.putconn(conn)
    except Exception as exc:
        logger.warning("Không thể khởi tạo schema PostgreSQL (Database có thể đang khởi động hoặc offline): %s", exc)


init_db()


def create_job(request_id: str):
    now = datetime.now(timezone.utc)
    p = get_pg_pool()
    conn = p.getconn()
    try:
        with conn.cursor() as cur:
            query = sql.SQL("""
                INSERT INTO {}.{} (request_id, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (request_id) DO UPDATE
                SET status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at
            """).format(sql.Identifier(PG_SCHEMA), sql.Identifier(PG_TABLE))
            cur.execute(
                query,
                (request_id, JobStatus.PROCESSING.value, now, now),
            )
            conn.commit()
    finally:
        p.putconn(conn)


def update_job_status(request_id: str, status: JobStatus, result: Optional[dict] = None, error: Optional[str] = None):
    now = datetime.now(timezone.utc)
    result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
    p = get_pg_pool()
    conn = p.getconn()
    try:
        with conn.cursor() as cur:
            query = sql.SQL("""
                UPDATE {}.{}
                SET status = %s, result = %s, error = %s, updated_at = %s
                WHERE request_id = %s
            """).format(sql.Identifier(PG_SCHEMA), sql.Identifier(PG_TABLE))
            cur.execute(
                query,
                (status.value, result_json, error, now, request_id),
            )
            conn.commit()
    finally:
        p.putconn(conn)


def get_job(request_id: str) -> Optional[dict]:
    p = get_pg_pool()
    conn = p.getconn()
    try:
        with conn.cursor() as cur:
            query = sql.SQL("""
                SELECT request_id, status, result, error
                FROM {}.{}
                WHERE request_id = %s
            """).format(sql.Identifier(PG_SCHEMA), sql.Identifier(PG_TABLE))
            cur.execute(
                query,
                (request_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            req_id, status_str, result_val, error_str = row
            if isinstance(result_val, str):
                result_dict = json.loads(result_val)
            elif isinstance(result_val, dict):
                result_dict = result_val
            else:
                result_dict = None

            return {
                "request_id": req_id,
                "status": JobStatus(status_str),
                "result": result_dict,
                "error": error_str,
            }
    finally:
        p.putconn(conn)


# ---------------------------------------------------------------------------
# 1.2 Single-Flight Request Coalescing Manager
# ---------------------------------------------------------------------------

@dataclass
class Flight:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[dict] = None
    error: Optional[str] = None
    leader_request_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class SingleFlightManager:
    """
    Quản lý Request Coalescing (Single-Flight) cho các request có cùng file PDF (mã hash).
    Phân tách theo 2 nhóm:
      - force_reprocess = False (Nhóm dùng cache / chờ leader bất kỳ)
      - force_reprocess = True  (Nhóm bắt buộc làm mới / chỉ chờ leader force_reprocess=True)
    """

    def __init__(self):
        self._flights: Dict[Tuple[str, bool], Flight] = {}
        self._lock = asyncio.Lock()

    async def acquire_or_join(
        self, pdf_hash: str, force_reprocess: bool, request_id: str
    ) -> Tuple[Literal["LEADER", "FOLLOWER"], Flight]:
        """
        Đăng ký một request vào hệ thống Single-Flight.
        Trả về:
          - role: "LEADER" (tác vụ chính sẽ chạy AI pipeline) hoặc "FOLLOWER" (chờ kết quả từ Leader)
          - flight: Đối tượng Flight để theo dõi sự kiện hoàn thành
        """
        async with self._lock:
            if not force_reprocess:
                # 1. Nhóm force_reprocess = False:
                # Nếu đã có Leader Group False đang chạy -> trở thành Follower
                if (pdf_hash, False) in self._flights:
                    return "FOLLOWER", self._flights[(pdf_hash, False)]

                # Hoặc nếu đang có Leader Group True đang chạy -> kết quả tươi của Group True hoàn toàn dùng được
                if (pdf_hash, True) in self._flights:
                    return "FOLLOWER", self._flights[(pdf_hash, True)]

                # Chưa có Leader nào đang chạy cho pdf_hash -> Đăng ký làm Leader Group False
                flight = Flight(leader_request_id=request_id)
                self._flights[(pdf_hash, False)] = flight
                return "LEADER", flight
            else:
                # 2. Nhóm force_reprocess = True:
                # Nếu đã có Leader Group True đang chạy -> trở thành Follower của Group True
                if (pdf_hash, True) in self._flights:
                    return "FOLLOWER", self._flights[(pdf_hash, True)]

                # Chưa có Leader Group True nào đang chạy -> Đăng ký làm Leader Group True
                flight = Flight(leader_request_id=request_id)
                self._flights[(pdf_hash, True)] = flight
                return "LEADER", flight

    async def complete(
        self,
        pdf_hash: str,
        force_reprocess: bool,
        result: Optional[dict],
        error: Optional[str],
    ):
        """
        Được gọi bởi Leader khi hoàn tất (hoặc lỗi) để cập nhật kết quả và đánh thức tất cả Follower.
        """
        async with self._lock:
            key = (pdf_hash, force_reprocess)
            flight = self._flights.pop(key, None)
            if flight is not None:
                flight.result = result
                flight.error = error
                flight.event.set()


single_flight_mgr = SingleFlightManager()


async def process_pdf_leader(
    request_id: str,
    pdf_bytes: bytes,
    filename: str,
    pdf_hash: str,
    force_reprocess: bool,
):
    """
    Tác vụ chạy nền cho LEADER:
    - Thực thi AI pipeline trích xuất PDF.
    - Lưu kết quả vào MinIO cache (với tên {pdf_hash}.json) và ghi log.
    - Cập nhật trạng thái job trong PostgreSQL thành FINISHED (hoặc FAILED).
    - Đánh thức tất cả các FOLLOWER đang chờ cùng mã hash qua SingleFlightManager.
    """
    result_dict = None
    error_str = None
    try:
        pipeline = get_pipeline()
        request_log_dir = create_request_log_dir(base_dir="logs")
        # Khi force_reprocess=True vẫn giữ use_cache=True đối với service OCR
        results, metadata_information, execution_times = await pipeline.extract(
            pdf_bytes, log_dir=request_log_dir, use_cache=True
        )
        result_response = format_results_to_response(results, metadata_information, status="success")
        result_dict = result_response.model_dump()

        # Lưu kết quả vào MinIO bucket cache với tên file là mã hash
        sync_write_json_to_minio_bucket(f"{pdf_hash}.json", result_dict, bucket_name=MINIO_CACHE_BUCKET)

        save_logs_to_minio(
            request_log_dir=request_log_dir,
            filename=filename,
            pdf_bytes=pdf_bytes,
            results=results,
            metadata_information=metadata_information,
            execution_times=execution_times,
        )

        update_job_status(request_id, JobStatus.FINISHED, result=result_dict)
    except TableContentUnsupportedError as exc:
        print(f"[Table Detected] PDF contains table for request {request_id} (hash: {pdf_hash}): {exc}")
        result_response = create_table_error_response(message=str(exc))
        result_dict = result_response.model_dump()

        # Lưu cache kết quả lỗi vào MinIO bucket cache
        sync_write_json_to_minio_bucket(f"{pdf_hash}.json", result_dict, bucket_name=MINIO_CACHE_BUCKET)

        update_job_status(request_id, JobStatus.FINISHED, result=result_dict)
    except Exception as exc:
        print(f"[Leader Error] Processing failed for request {request_id} (hash: {pdf_hash}): {exc}")
        error_str = f"Lỗi xử lý: {str(exc)}"
        update_job_status(request_id, JobStatus.FAILED, error=error_str)
    finally:
        # Báo cho SingleFlightManager giải phóng và đánh thức tất cả Follower
        await single_flight_mgr.complete(pdf_hash, force_reprocess, result_dict, error_str)


FOLLOWER_TIMEOUT_SECONDS = 600.0


async def process_pdf_follower(
    request_id: str,
    flight: Flight,
):
    """
    Tác vụ chạy nền cho FOLLOWER:
    - Không chạy lại AI pipeline.
    - Lắng nghe sự kiện hoàn thành từ Leader qua flight.event.
    - Khi Leader hoàn thành: sao chép kết quả (hoặc lỗi), cập nhật job của riêng mình trong DB.
    """
    try:
        # Chờ Leader hoàn thành (với timeout an toàn)
        async with asyncio.timeout(FOLLOWER_TIMEOUT_SECONDS):
            await flight.event.wait()
        if flight.error:
            update_job_status(request_id, JobStatus.FAILED, error=flight.error)
        else:
            update_job_status(request_id, JobStatus.FINISHED, result=flight.result)
    except asyncio.TimeoutError:
        print(f"[Follower Timeout] Timeout waiting for leader on request {request_id}")
        update_job_status(request_id, JobStatus.FAILED, error="Timeout chờ kết quả từ tác vụ xử lý chính.")
    except Exception as exc:
        print(f"[Follower Error] Error waiting for leader on request {request_id}: {exc}")
        update_job_status(request_id, JobStatus.FAILED, error=f"Lỗi theo dõi: {str(exc)}")


# ---------------------------------------------------------------------------
# 2. Request: người dùng upload file PDF, không nhận text trực tiếp nữa
# ---------------------------------------------------------------------------

ALLOWED_CONTENT_TYPES = {"application/pdf"}
MAX_PDF_SIZE_BYTES = 30 * 1024 * 1024  # 20MB, tuỳ chỉnh theo nhu cầu

class OCRError(Exception):
    """Lỗi phát sinh trong bước OCR trung gian."""


# ---------------------------------------------------------------------------
# 3. Pipeline global & helper functions
# ---------------------------------------------------------------------------

pipeline_instance: Optional[Pipeline] = None


def get_pipeline() -> Pipeline:
    global pipeline_instance
    if pipeline_instance is None:
        pipeline_instance = Pipeline()
    return pipeline_instance


def get_log_timestamp() -> str:
    """Trả về timestamp dạng YYYY-MM-DD_HH-MM-SS theo múi giờ GMT+7."""
    gmt7 = timezone(timedelta(hours=7))
    now = datetime.now(timezone.utc).astimezone(gmt7)
    return now.strftime("%Y-%m-%d_%H-%M-%S")


def create_request_log_dir(base_dir: str = "logs") -> str:
    """Trả về chuỗi prefix log dạng logs/YYYY-MM-DD_HH-MM-SS cho MinIO mà không tạo thư mục đĩa cục bộ."""
    timestamp_str = get_log_timestamp()
    return os.path.join(base_dir, timestamp_str)

def format_metadata_to_response(metadata_information: dict) -> Metadata:
    if not isinstance(metadata_information, dict):
        metadata_information = {}
    info = metadata_information.get("metadata") if isinstance(metadata_information.get("metadata"), dict) else metadata_information
    if not isinstance(info, dict):
        info = {}

    def _clean_str(val: Any) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        return s if s else None

    return Metadata(
        ky_hop=_clean_str(info.get("ky_hop")),
        quoc_hoi_khoa=_clean_str(info.get("quoc_hoi_khoa")),
        hinh_thuc=_clean_str(info.get("hinh_thuc")),
        document_number=_clean_str(info.get("document_number")),
        issue_date=_clean_str(info.get("issue_date")),
        signer=_clean_str(info.get("signer")),
        ten_doan_db=_clean_str(info.get("ten_doan_db")),
    )


def create_table_error_response(message: str = "File PDF chứa bảng biểu (table), hiện tại hệ thống chưa hỗ trợ trích xuất dữ liệu từ bảng.") -> KienNghiTongHop:
    """Tạo response lỗi TABLE_CONTENT_UNSUPPORTED tuân thủ đúng schema_2608.json."""
    return KienNghiTongHop(
        status="failed",
        error=ErrorInfo(
            code="TABLE_CONTENT_UNSUPPORTED",
            message=message,
        ),
        metadata=Metadata(
            ky_hop=None,
            quoc_hoi_khoa=None,
            hinh_thuc=None,
            document_number=None,
            issue_date=None,
            signer=None,
            ten_doan_db=None,
        ),
        kien_nghi=[],
        tong_so_kien_nghi=0,
        cac_don_vi_nhan=[],
    )


def _normalize_confidence(conf: Any, fallback: str = "low") -> Literal["high", "medium", "low"]:
    if isinstance(conf, list):
        conf = conf[0] if conf else fallback
    conf_str = str(conf).strip().lower() if conf is not None else fallback
    if conf_str in ("high", "medium", "low"):
        return conf_str
    return "low"


DEFAULT_AUTHORITY = "Văn Phòng Chính Phủ"


def _extract_headers_list(res: dict) -> list[str]:
    raw_headers = res.get("headers")
    if raw_headers is None:
        raw_headers = res.get("heading_path") or res.get("heading") or res.get("header") or []

    if isinstance(raw_headers, list):
        return [str(h).replace("#", "").strip() for h in raw_headers if str(h).strip()]
    if isinstance(raw_headers, str) and raw_headers.strip():
        return [raw_headers.replace("#", "").strip()]
    return []


def _parse_single_raw_don_vi_item(item: Any, seen: set) -> Optional[DonViNhan]:
    """Parse 1 phần tử raw_don_vi_nhan thành đối tượng DonViNhan."""
    if isinstance(item, dict):
        ten = str(item.get("ten_don_vi", "")).strip()
        if not ten or ten in seen:
            return None
        seen.add(ten)
        muc_do = _normalize_confidence(item.get("muc_do_tin_cay"), fallback="low")
        phuong_phap = str(item.get("phuong_phap") or "llm").strip() or "llm"
        is_mapped_raw = item.get("is_mapped")
        is_mapped = bool(is_mapped_raw) if is_mapped_raw is not None else None
        return DonViNhan(
            ten_don_vi=ten,
            muc_do_tin_cay=muc_do,
            phuong_phap=phuong_phap,
            is_mapped=is_mapped,
        )
    if isinstance(item, str) and item.strip():
        ten = item.strip()
        if ten in seen:
            return None
        seen.add(ten)
        return DonViNhan(
            ten_don_vi=ten,
            muc_do_tin_cay="low",
            phuong_phap="llm",
            is_mapped=False,
        )
    return None


def _parse_raw_don_vi_items(raw_don_vi_nhan: list, seen: set) -> list[DonViNhan]:
    don_vi_nhan_list: List[DonViNhan] = []
    for item in raw_don_vi_nhan:
        parsed = _parse_single_raw_don_vi_item(item, seen)
        if parsed:
            don_vi_nhan_list.append(parsed)
    return don_vi_nhan_list


def _extract_authority_names(auth_raw: Any) -> list[str]:
    """Trích xuất danh sách tên cơ quan từ trường authority raw."""
    if isinstance(auth_raw, str):
        return [auth_raw.strip()] if auth_raw.strip() else [DEFAULT_AUTHORITY]
    if isinstance(auth_raw, list):
        filtered = [str(a).strip() for a in auth_raw if isinstance(a, str) and str(a).strip()]
        return filtered or [DEFAULT_AUTHORITY]
    return [DEFAULT_AUTHORITY]


def _build_authority_fallback_don_vi(res: dict, seen: set) -> list[DonViNhan]:
    authorities = _extract_authority_names(res.get("authority", []))
    phuong_phap = str(res.get("match_method") or "llm").strip() or "llm"
    default_conf = "high" if phuong_phap == "rule-based" else "medium"
    conf_val = _normalize_confidence(res.get("confidence"), fallback=default_conf)

    don_vi_nhan_list: List[DonViNhan] = []
    for auth in authorities:
        if not auth or auth in seen:
            continue
        seen.add(auth)
        don_vi_nhan_list.append(
            DonViNhan(
                ten_don_vi=auth,
                muc_do_tin_cay=conf_val,
                phuong_phap=phuong_phap,
                is_mapped=(phuong_phap == "rule-based"),
            )
        )

    if not don_vi_nhan_list:
        don_vi_nhan_list.append(
            DonViNhan(
                ten_don_vi=DEFAULT_AUTHORITY,
                muc_do_tin_cay="low",
                phuong_phap="fallback",
                is_mapped=False,
            )
        )
    return don_vi_nhan_list


def _resolve_don_vi_nhan(res: dict) -> list[DonViNhan]:
    seen: set = set()
    raw_don_vi_nhan = res.get("don_vi_nhan")
    if isinstance(raw_don_vi_nhan, list) and raw_don_vi_nhan:
        don_vi_nhan_list = _parse_raw_don_vi_items(raw_don_vi_nhan, seen)
        if don_vi_nhan_list:
            return don_vi_nhan_list[:5]

    don_vi_nhan_list = _build_authority_fallback_don_vi(res, seen)
    return don_vi_nhan_list[:5]


def format_results_to_response(
    results: list,
    metadata_information: dict,
    status: Optional[Literal["success", "failed"]] = "success",
    error: Optional[ErrorInfo] = None,
) -> KienNghiTongHop:
    """Chuyển đổi kết quả trích xuất từ pipeline thành response KienNghiTongHop khớp schema_2608.json."""
    items: List[KienNghiItem] = []
    all_don_vi: List[str] = []
    agency_counts: Dict[str, int] = {}

    for res in results:
        if not isinstance(res, dict):
            continue

        headers_list = _extract_headers_list(res)
        if should_exclude_by_headers(headers_list):
            continue

        don_vi_nhan_list = _resolve_don_vi_nhan(res)
        for d in don_vi_nhan_list:
            if d.ten_don_vi not in all_don_vi:
                all_don_vi.append(d.ten_don_vi)

        primary_agency = don_vi_nhan_list[0].ten_don_vi
        agency_counts[primary_agency] = agency_counts.get(primary_agency, 0) + 1
        so_thu_tu = agency_counts[primary_agency]

        noi_dung = str(res.get("noi_dung") or res.get("content") or "").strip()
        if not noi_dung:
            noi_dung = "Không có nội dung"

        items.append(
            KienNghiItem(
                stt=len(items) + 1,
                don_vi_nhan=don_vi_nhan_list,
                so_thu_tu_kien_nghi=so_thu_tu,
                noi_dung=noi_dung,
                headers=headers_list,
            )
        )

    return KienNghiTongHop(
        status=status,
        error=error,
        metadata=format_metadata_to_response(metadata_information),
        kien_nghi=items,
        tong_so_kien_nghi=len(items),
        cac_don_vi_nhan=all_don_vi,
    )


def save_logs_to_minio(
    request_log_dir: str,
    filename: str,
    pdf_bytes: bytes,
    results: list,
    metadata_information: dict,
    execution_times: Optional[dict] = None,
):
    """
    Hàm thực thi trong BackgroundTask của FastAPI:
    Gửi dữ liệu vào ThreadPoolExecutor của minio_utils để ghi bất đồng bộ hoàn toàn (fire-and-forget),
    không làm treo hay giữ HTTP Request / BackgroundTask của Uvicorn/Starlette.
    """
    try:
        from modules.minio_utils import write_to_minio, write_json_to_minio

        # 1. Ghi file PDF gốc (bất đồng bộ qua thread pool)
        minio_pdf_path = os.path.join(request_log_dir, filename)
        write_to_minio(minio_pdf_path, pdf_bytes, content_type="application/pdf")

        # 2. Ghi kết quả metadata
        write_json_to_minio(os.path.join(request_log_dir, "3.metadata.json"), metadata_information)

        # 3. Ghi kết quả trích xuất postprocessed
        write_json_to_minio(os.path.join(request_log_dir, "9.postprocessed_results.json"), results)

        # 4. Ghi execution_times nếu có
        if execution_times:
            write_json_to_minio(os.path.join(request_log_dir, "execution_times.json"), execution_times)

    except Exception as exc:
        print(f"[Background Task Error] Lỗi khi gửi log MinIO tại {request_log_dir}: {exc}")


# ---------------------------------------------------------------------------
# 4. FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Kien Nghi Extraction & Classification API",
    description="Trích xuất và phân loại kiến nghị cử tri theo cơ quan thẩm quyền",
    version="1.0.0",
)


def _fix_openapi_schema(s):
    if not isinstance(s, dict):
        return
    if "anyOf" in s:
        for item in s["anyOf"]:
            _fix_openapi_schema(item)
    if s.get("type") == "array" and "items" in s:
        _fix_openapi_schema(s["items"])
        if s["items"].get("contentMediaType") == "application/octet-stream":
            del s["items"]["contentMediaType"]
            s["items"]["format"] = "binary"
    elif s.get("contentMediaType") == "application/octet-stream":
        del s["contentMediaType"]
        s["format"] = "binary"
    if "properties" in s:
        for prop in s["properties"].values():
            _fix_openapi_schema(prop)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    # Khắc phục lỗi tương thích Swagger UI với OpenAPI 3.1 cho UploadFile
    # Chuyển OpenAPI version về 3.0.3 và chuyển contentMediaType -> format: binary
    openapi_schema["openapi"] = "3.0.3"

    for schema in openapi_schema.get("components", {}).get("schemas", {}).values():
        _fix_openapi_schema(schema)

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = time.perf_counter() - start_time
    response.headers["X-Process-Time"] = f"{process_time:.4f}s"
    return response


def _extract_org_names_from_kien_nghi(kien_nghi_list: list) -> list[str]:
    """Trích xuất danh sách tên đơn vị nhận duy nhất từ danh sách kiến nghị."""
    seen = set()
    cached_orgs = []
    for it in kien_nghi_list:
        if not (isinstance(it, dict) and "don_vi_nhan" in it):
            continue
        for elem in it["don_vi_nhan"]:
            name = elem.get("ten_don_vi") if isinstance(elem, dict) else elem
            if name and name not in seen:
                seen.add(name)
                cached_orgs.append(name)
    return cached_orgs


def _process_cached_result(cached_result: dict, cache_key: str) -> None:
    if "kien_nghi" not in cached_result or not isinstance(cached_result["kien_nghi"], list):
        return
    has_excluded = any(
        should_exclude_by_headers(it.get("headers"))
        for it in cached_result["kien_nghi"]
        if isinstance(it, dict)
    )
    if not has_excluded:
        return
    cached_result["kien_nghi"] = filter_and_reindex_kien_nghi(cached_result["kien_nghi"])
    cached_result["tong_so_kien_nghi"] = len(cached_result["kien_nghi"])
    cached_result["cac_don_vi_nhan"] = _extract_org_names_from_kien_nghi(cached_result["kien_nghi"])
    sync_write_json_to_minio_bucket(cache_key, cached_result, bucket_name=MINIO_CACHE_BUCKET)


def _handle_cached_pdf(cache_key: str, pdf_hash: str) -> Optional[ExtractResponse]:
    cached_result = sync_get_json_from_minio(cache_key, bucket_name=MINIO_CACHE_BUCKET)
    if cached_result is None:
        return None

    _process_cached_result(cached_result, cache_key)

    print(f"[INFO] Cache hit in MinIO bucket '{MINIO_CACHE_BUCKET}' for hash: {pdf_hash}")
    request_id = str(uuid.uuid4())
    create_job(request_id)
    update_job_status(request_id, JobStatus.FINISHED, result=cached_result)
    return ExtractResponse(
        request_id=request_id,
        status=JobStatus.FINISHED,
        result=KienNghiTongHop(**cached_result)
    )


def _dispatch_single_flight(
    role: str,
    flight: Flight,
    request_id: str,
    pdf_bytes: bytes,
    filename: str,
    pdf_hash: str,
    force_reprocess: bool,
    background_tasks: BackgroundTasks,
) -> None:
    if role == "LEADER":
        print(f"[SingleFlight] Request {request_id} is LEADER for hash: {pdf_hash} (force_reprocess={force_reprocess})")
        background_tasks.add_task(
            process_pdf_leader,
            request_id=request_id,
            pdf_bytes=pdf_bytes,
            filename=filename,
            pdf_hash=pdf_hash,
            force_reprocess=force_reprocess,
        )
    else:
        print(f"[SingleFlight] Request {request_id} is FOLLOWER waiting for leader {flight.leader_request_id} on hash: {pdf_hash} (force_reprocess={force_reprocess})")
        background_tasks.add_task(
            process_pdf_follower,
            request_id=request_id,
            flight=flight,
        )


@app.post(
    "/extract",
    response_model_exclude_unset=True,
    responses={
        413: {"description": "Payload Too Large - File vượt quá kích thước cho phép"},
        415: {"description": "Unsupported Media Type - Chỉ chấp nhận file PDF"},
    },
)
async def extract(
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File(description="File PDF công văn cần trích xuất")],
    force_reprocess: Annotated[bool, Form(description="Tùy chọn ép buộc xử lý lại không dùng cache")] = False,
) -> ExtractResponse:
    """
    Endpoint POST /extract tiếp nhận file PDF và khởi tạo tác vụ trích xuất bất đồng bộ (Polling Pattern):
    - Param `force_reprocess`:
      + False (mặc định): Kiểm tra trong MinIO bucket 'cache'. Nếu đã có kết quả mã hash, trả về ngay kết quả JSON.
      + True: Không kiểm tra trong MinIO bucket 'cache', xử lý bình thường qua pipeline, sau đó hash và update/ghi đè kết quả vào MinIO bucket 'cache'.
    """
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Chỉ chấp nhận file PDF (application/pdf), nhận được: {file.content_type}",
        )

    pdf_bytes = await file.read()

    if len(pdf_bytes) > MAX_PDF_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File vượt quá kích thước cho phép ({MAX_PDF_SIZE_BYTES // (1024 * 1024)}MB)",
        )

    filename = file.filename or "uploaded.pdf"
    pdf_hash = file_hash(pdf_bytes)
    cache_key = f"{pdf_hash}.json"

    # Nếu force_reprocess = False, check cache trong MinIO cache bucket
    if not force_reprocess:
        cached_resp = _handle_cached_pdf(cache_key, pdf_hash)
        if cached_resp is not None:
            return cached_resp

    # Nếu force_reprocess = True hoặc cache miss: áp dụng Single-Flight Request Coalescing
    request_id = str(uuid.uuid4())

    # Kiểm tra trạng thái Celery & Queue ngay khi nhận request
    from modules.celery_health import check_celery_and_queue_health
    is_celery_active, celery_reason = check_celery_and_queue_health()
    if is_celery_active:
        print(f"[Request {request_id}] Đã nhận file '{filename}' (hash: {pdf_hash}). Celery & Queues sẵn sàng ({celery_reason}).")
    else:
        print(f"[Request {request_id}] Đã nhận file '{filename}' (hash: {pdf_hash}). CẢNH BÁO: Celery/Queue không hoạt động ({celery_reason}) -> Tự động Fallback sang Python in-process (không đẩy vào queue).")

    # Tạo bản ghi trong DB với trạng thái PROCESSING cho request hiện tại
    create_job(request_id)

    # Đăng ký vai trò Leader hoặc Follower trong nhóm SingleFlight
    role, flight = await single_flight_mgr.acquire_or_join(pdf_hash, force_reprocess, request_id)
    _dispatch_single_flight(
        role, flight, request_id, pdf_bytes, filename, pdf_hash,
        force_reprocess, background_tasks
    )

    return ExtractResponse(
        request_id=request_id,
        status=JobStatus.PROCESSING
    )


@app.get(
    "/extract/{request_id}",
    response_model_exclude_unset=True,
    responses={404: {"description": "Request ID not found"}},
)
def get_extract_status(request_id: str) -> ExtractResponse:
    """
    Client định kỳ gọi endpoint này để kiểm tra trạng thái xử lý (Polling).
    - PROCESSING: Trả về status PROCESSING
    - FINISHED: Trả về status FINISHED kèm kết quả JSON trích xuất
    - FAILED: Trả về status FAILED kèm thông tin lỗi
    - request_id không tồn tại: HTTP 404 {"detail": "request_id not found"}
    """
    job = get_job(request_id)
    if not job:
        raise HTTPException(status_code=404, detail="request_id not found")

    return ExtractResponse(
        request_id=job["request_id"],
        status=job["status"],
        result=KienNghiTongHop(**job["result"]) if job["result"] else None,
        error=job["error"],
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/health/celery")
def celery_health_check(force_check: bool = False) -> dict:
    """Kiểm tra trực tiếp trạng thái sẵn sàng của Broker, Result Backend và các Celery Workers."""
    from modules.celery_health import check_celery_and_queue_health
    is_ok, msg = check_celery_and_queue_health(force_check=force_check)
    return {
        "celery_active": is_ok,
        "detail": msg,
        "mode": "celery_distributed" if is_ok else "in_process_fallback"
    }


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8077"))
    uvicorn.run("app:app", host=host, port=port, reload=True)