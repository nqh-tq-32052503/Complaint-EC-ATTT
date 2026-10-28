import os
import sys
import uuid
import pytest
from unittest.mock import MagicMock, patch

# Thiết lập biến môi trường phục vụ môi trường test
os.environ["TESTING"] = "true"
os.environ["SERVICE_API_KEY"] = "test-secret-api-key-8077"
os.environ["MINIO_ENDPOINT"] = "127.0.0.1:9000"
os.environ["POSTGRES_HOST"] = "127.0.0.1"
os.environ["POSTGRES_PORT"] = "5432"

# Mock MinIO helpers trước khi các module phụ thuộc được import
import modules.minio_utils
modules.minio_utils.sync_get_spelling_error_from_minio = MagicMock(return_value={})
modules.minio_utils.sync_get_knowledge_base_from_minio = MagicMock(return_value={})

import app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_job_store(monkeypatch):
    """
    Bộ đệm In-memory mô phỏng cơ sở dữ liệu PostgreSQL và MinIO Cache trong môi trường test.
    Giúp các ca kiểm thử chạy độc lập, tốc độ mili-giây và không phụ thuộc dịch vụ ngoài.
    """
    in_memory_db = {}
    in_memory_minio = {}

    def mock_create_job(request_id: str, owner_id: str = None):
        in_memory_db[request_id] = {
            "request_id": request_id,
            "status": app.JobStatus.PROCESSING,
            "result": None,
            "error": None,
            "owner_id": owner_id,
        }

    def mock_get_job(request_id: str):
        return in_memory_db.get(request_id)

    def mock_update_job_status(request_id: str, status, result=None, error=None):
        status_enum = status if isinstance(status, app.JobStatus) else app.JobStatus(status)
        sanitized_error = app.GENERIC_ERROR_MESSAGE if (error is not None or status_enum == app.JobStatus.FAILED) else None
        if request_id not in in_memory_db:
            in_memory_db[request_id] = {
                "request_id": request_id,
            }
        in_memory_db[request_id]["status"] = status_enum
        in_memory_db[request_id]["result"] = result
        in_memory_db[request_id]["error"] = sanitized_error

    def mock_sync_get_json(key, bucket_name=None):
        return in_memory_minio.get(key)

    def mock_sync_write_json(key, data, bucket_name=None):
        in_memory_minio[key] = data

    monkeypatch.setattr(app, "create_job", mock_create_job)
    monkeypatch.setattr(app, "get_job", mock_get_job)
    monkeypatch.setattr(app, "update_job_status", mock_update_job_status)
    monkeypatch.setattr(app, "sync_get_json_from_minio", mock_sync_get_json)
    monkeypatch.setattr(app, "sync_write_json_to_minio_bucket", mock_sync_write_json)
    monkeypatch.setattr(app, "save_logs_to_minio", MagicMock())
    monkeypatch.setattr(app, "_dispatch_single_flight", MagicMock())

    return {
        "db": in_memory_db,
        "minio": in_memory_minio,
    }


@pytest.fixture
def client():
    """Khởi tạo TestClient của FastAPI với header xác thực API Key mặc định."""
    api_key = os.environ.get("SERVICE_API_KEY", "test-secret-api-key-8077")
    with TestClient(app.app, headers={"X-API-Key": api_key}) as c:
        yield c


@pytest.fixture
def unauthenticated_client():
    """Khởi tạo TestClient không có header xác thực phục vụ kiểm thử bảo mật."""
    with TestClient(app.app) as c:
        yield c


@pytest.fixture
def sample_valid_pdf_bytes():
    """Nội dung file PDF mẫu hợp lệ (Minimal valid PDF structure)."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f\n0000000009 00000 n\n0000000052 00000 n\n0000000101 00000 n\n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF\n"
    )


@pytest.fixture
def sample_oversized_pdf_bytes():
    """Dữ liệu vượt ngưỡng 30MB (30MB + 1KB)."""
    return b"%PDF-1.4\n" + b"0" * (30 * 1024 * 1024 + 1024)


@pytest.fixture
def sample_kien_nghi_tong_hop():
    """Đối tượng kết quả trích xuất hoàn chỉnh tuân thủ 100% schema_2608.json."""
    return {
        "status": "success",
        "error": None,
        "metadata": {
            "ky_hop": "Kỳ họp thứ 7",
            "quoc_hoi_khoa": "XV",
            "hinh_thuc": "Trước kỳ họp",
            "document_number": "145/ĐĐB-VP",
            "issue_date": "10/05/2024",
            "signer": "Nguyễn Văn A",
            "ten_doan_db": "Đoàn Đại biểu Quốc hội tỉnh Quảng Ninh"
        },
        "kien_nghi": [
            {
                "stt": 1,
                "don_vi_nhan": [
                    {
                        "ten_don_vi": "Bộ Giao thông vận tải",
                        "muc_do_tin_cay": "high",
                        "phuong_phap": "rule_based",
                        "is_mapped": True
                    },
                    {
                        "ten_don_vi": "Bộ Nội Vụ",
                        "muc_do_tin_cay": "low",
                        "phuong_phap": "llm",
                        "is_mapped": False
                    },
                    {
                        "ten_don_vi": "Văn Phòng Chính Phủ",
                        "muc_do_tin_cay": "low",
                        "phuong_phap": "llm",
                        "is_mapped": False
                    }
                ],
                "so_thu_tu_kien_nghi": 1,
                "noi_dung": "Cử tri kiến nghị Bộ Giao thông vận tải sớm đầu tư nâng cấp Quốc lộ 18A.",
                "headers": ["I. LĨNH VỰC GIAO THÔNG"]
            }
        ],
        "tong_so_kien_nghi": 1,
        "cac_don_vi_nhan": [
            "Bộ Giao thông vận tải",
            "Bộ Nội Vụ",
            "Văn Phòng Chính Phủ"
        ]
    }
