import io
import re
import uuid
import pytest
from unittest.mock import patch

from app import JobStatus, KienNghiTongHop
from modules.pipeline import file_hash
from tests.assertions import (
    check_equal,
    check_in,
    check_not_in,
    check_is_none,
    check_is_not_none,
    check_true,
)


class TestApiEndpoints:
    """Bộ kiểm thử chức năng cho 4 Endpoint API chính thức của hệ thống."""

    def test_health_check(self, client):
        """Kiểm tra Endpoint Liveness Probe (GET /health) và Middleware X-Process-Time."""
        response = client.get("/health")
        check_equal(response.status_code, 200)
        check_equal(response.json(), {"status": "ok"})
        
        # Kiểm tra sự hiện diện của Custom Response Header do Middleware thêm vào
        process_time_header = response.headers.get("x-process-time")
        check_is_not_none(process_time_header)
        check_true(bool(re.match(r"^\d+\.\d{4}s$", process_time_header)))

    def test_celery_health_check_active(self, client):
        """Kiểm tra Endpoint GET /health/celery khi Broker & Worker Queues hoạt động bình thường."""
        with patch("modules.celery_health.check_celery_and_queue_health") as mock_health:
            mock_health.return_value = (True, "Celery và 8 pipeline workers đang hoạt động bình thường")
            response = client.get("/health/celery")
            check_equal(response.status_code, 200)
            data = response.json()
            check_equal(data["celery_active"], True)
            check_equal(data["mode"], "celery_distributed")
            check_in("8 pipeline workers", data["detail"])

    def test_celery_health_check_fallback(self, client):
        """Kiểm tra Endpoint GET /health/celery khi Broker gián đoạn -> Tự động chuyển In-process Fallback."""
        with patch("modules.celery_health.check_celery_and_queue_health") as mock_health:
            mock_health.return_value = (False, "Không thể kết nối RabbitMQ Broker")
            response = client.get("/health/celery")
            check_equal(response.status_code, 200)
            data = response.json()
            check_equal(data["celery_active"], False)
            check_equal(data["mode"], "in_process_fallback")

    def test_post_extract_valid_pdf_returns_processing(self, client, sample_valid_pdf_bytes, isolated_job_store):
        """
        Kiểm tra luồng khởi tạo trích xuất (POST /extract):
        - Nhận file PDF hợp lệ.
        - Khởi tạo job PROCESSING và phản hồi HTTP 200 ngay lập tức.
        - Kiểm tra cơ chế response_model_exclude_unset=True loại trừ result và error null.
        """
        files = {"file": ("cong_van_test.pdf", sample_valid_pdf_bytes, "application/pdf")}
        data = {"force_reprocess": "false"}

        with patch("modules.celery_health.check_celery_and_queue_health", return_value=(True, "Workers OK")):
            response = client.post("/extract", files=files, data=data)

        check_equal(response.status_code, 200)
        payload = response.json()
        check_in("request_id", payload)
        check_equal(payload["status"], "PROCESSING")

        # Đảm bảo do response_model_exclude_unset=True nên result và error không xuất hiện
        check_not_in("result", payload)
        check_not_in("error", payload)

        # Đảm bảo job được ghi nhận vào kho lưu trữ
        req_id = payload["request_id"]
        job = isolated_job_store["db"].get(req_id)
        check_is_not_none(job)
        check_equal(job["status"], JobStatus.PROCESSING)

    def test_get_extract_status_processing(self, client, isolated_job_store):
        """Kiểm tra Endpoint Polling (GET /extract/{request_id}) khi tác vụ đang xử lý."""
        req_id = str(uuid.uuid4())
        isolated_job_store["db"][req_id] = {
            "request_id": req_id,
            "status": JobStatus.PROCESSING,
            "result": None,
            "error": None,
        }

        response = client.get(f"/extract/{req_id}")
        check_equal(response.status_code, 200)
        data = response.json()
        check_equal(data["request_id"], req_id)
        check_equal(data["status"], "PROCESSING")
        check_is_none(data["result"])
        check_is_none(data["error"])

    def test_get_extract_status_finished(self, client, sample_kien_nghi_tong_hop, isolated_job_store):
        """Kiểm tra Endpoint Polling (GET /extract/{request_id}) khi tác vụ hoàn thành thành công."""
        req_id = str(uuid.uuid4())
        isolated_job_store["db"][req_id] = {
            "request_id": req_id,
            "status": JobStatus.FINISHED,
            "result": sample_kien_nghi_tong_hop,
            "error": None,
        }

        response = client.get(f"/extract/{req_id}")
        check_equal(response.status_code, 200)
        data = response.json()
        check_equal(data["request_id"], req_id)
        check_equal(data["status"], "FINISHED")
        check_equal(data["result"]["status"], "success")
        check_equal(data["result"]["tong_so_kien_nghi"], 1)
        check_equal(len(data["result"]["kien_nghi"]), 1)
        check_equal(data["result"]["metadata"]["document_number"], "145/ĐĐB-VP")
        check_is_none(data["error"])

    def test_get_extract_status_failed(self, client, isolated_job_store):
        """Kiểm tra Endpoint Polling (GET /extract/{request_id}) khi tác vụ thất bại."""
        req_id = str(uuid.uuid4())
        err_msg = "Lỗi xử lý: Kết nối dịch vụ OCR bị timeout sau 300s"
        isolated_job_store["db"][req_id] = {
            "request_id": req_id,
            "status": JobStatus.FAILED,
            "result": None,
            "error": err_msg,
        }

        response = client.get(f"/extract/{req_id}")
        check_equal(response.status_code, 200)
        data = response.json()
        check_equal(data["request_id"], req_id)
        check_equal(data["status"], "FAILED")
        check_is_none(data["result"])
        check_equal(data["error"], err_msg)

    def test_get_extract_status_not_found(self, client):
        """Kiểm tra Endpoint Polling (GET /extract/{request_id}) với mã request_id không tồn tại."""
        random_id = str(uuid.uuid4())
        response = client.get(f"/extract/{random_id}")
        check_equal(response.status_code, 404)
        check_equal(response.json(), {"detail": "request_id not found"})

    def test_post_extract_cache_hit(self, client, sample_valid_pdf_bytes, sample_kien_nghi_tong_hop, isolated_job_store):
        """
        Kiểm tra cơ chế Cache Hit trên MinIO:
        - Nếu mã hash của file PDF đã tồn tại trong bucket cache,
        - Endpoint trả về ngay kết quả FINISHED mà không cần khởi chạy AI Pipeline.
        """
        pdf_hash = file_hash(sample_valid_pdf_bytes)
        cache_key = f"{pdf_hash}.json"

        # Đưa trước kết quả vào MinIO Cache
        isolated_job_store["minio"][cache_key] = sample_kien_nghi_tong_hop

        files = {"file": ("cong_van_cache.pdf", sample_valid_pdf_bytes, "application/pdf")}
        data = {"force_reprocess": "false"}

        response = client.post("/extract", files=files, data=data)
        check_equal(response.status_code, 200)
        res_data = response.json()
        check_equal(res_data["status"], "FINISHED")
        check_equal(res_data["result"]["tong_so_kien_nghi"], 1)
        check_not_in("error", res_data)  # excluded unset

    def test_post_extract_force_reprocess_bypasses_cache(self, client, sample_valid_pdf_bytes, sample_kien_nghi_tong_hop, isolated_job_store):
        """
        Kiểm tra cờ force_reprocess=true:
        - Bỏ qua cache có sẵn, ép buộc hệ thống khởi tạo lại tác vụ PROCESSING mới.
        """
        pdf_hash = file_hash(sample_valid_pdf_bytes)
        cache_key = f"{pdf_hash}.json"
        isolated_job_store["minio"][cache_key] = sample_kien_nghi_tong_hop

        files = {"file": ("cong_van_force.pdf", sample_valid_pdf_bytes, "application/pdf")}
        data = {"force_reprocess": "true"}

        with patch("modules.celery_health.check_celery_and_queue_health", return_value=(True, "OK")):
            response = client.post("/extract", files=files, data=data)

        check_equal(response.status_code, 200)
        res_data = response.json()
        check_equal(res_data["status"], "PROCESSING")
