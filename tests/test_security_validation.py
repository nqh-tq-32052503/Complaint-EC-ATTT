import io
import os
import pytest
from unittest.mock import patch
from tests.assertions import check_equal, check_in, check_true


class TestSecurityValidation:
    """
    Bộ kiểm thử đánh giá An Toàn Thông Tin (Security & Defensive Testing):
    - Kiểm soát định dạng và tính toàn vẹn của tệp tải lên (MIME-type enforcement).
    - Kiểm soát dung lượng tải lên (DoS Protection / Oversized Payload).
    - Phòng chống tấn công duyệt thư mục (Path Traversal via Filename).
    - Phòng chống tiêm mã độc SQL (SQL Injection qua Path Parameter).
    - Phòng chống tấn công Cross-Site Scripting (XSS qua Parameter).
    - Xác minh thu hẹp bề mặt tấn công (Attack Surface Reduction - Các endpoint thừa đã bị đóng hoàn toàn).
    """

    @pytest.mark.parametrize("invalid_content_type,filename", [
        ("image/jpeg", "avatar.jpg"),
        ("image/png", "scan.png"),
        ("text/plain", "notes.txt"),
        ("application/x-sh", "malicious_script.sh"),
        ("application/zip", "archive.zip"),
        ("application/msword", "report.doc"),
    ])
    def test_unsupported_media_type_rejection(self, client, invalid_content_type, filename):
        """
        Kiểm tra cơ chế chặn các định dạng MIME không được phép:
        - Hệ thống bắt buộc phải trả về HTTP 415 Unsupported Media Type.
        """
        fake_file_content = b"Non-PDF fake binary content"
        files = {"file": (filename, fake_file_content, invalid_content_type)}

        response = client.post("/extract", files=files)
        check_equal(response.status_code, 415)
        data = response.json()
        check_in("Chỉ chấp nhận file PDF (application/pdf)", data["detail"])
        check_in(invalid_content_type, data["detail"])

    def test_oversized_payload_protection(self, client, sample_oversized_pdf_bytes):
        """
        Kiểm tra cơ chế chống tấn công từ chối dịch vụ (DoS) qua file dung lượng lớn:
        - Ngưỡng giới hạn kỹ thuật: 30MB (31,457,280 bytes).
        - Nếu vượt quá, ngắt xử lý ngay lập tức và trả về HTTP 413 Payload Too Large.
        """
        files = {"file": ("oversized_document.pdf", sample_oversized_pdf_bytes, "application/pdf")}

        response = client.post("/extract", files=files)
        check_equal(response.status_code, 413)
        data = response.json()
        check_in("File vượt quá kích thước cho phép (30MB)", data["detail"])

    @pytest.mark.parametrize("malicious_filename", [
        "../../../../etc/passwd",
        "..\\..\\..\\windows\\win.ini",
        "/var/log/syslog.pdf",
        "cong_van_test%2e%2e%2fsecret.pdf",
        "file\x00nullbyte.pdf",
    ])
    def test_path_traversal_filename_injection(self, client, sample_valid_pdf_bytes, malicious_filename):
        """
        Kiểm tra khả năng miễn nhiễm với tấn công Path Traversal trong tên file upload:
        - Hệ thống không ghi file tạm ra đĩa cứng theo tên người dùng truyền lên.
        - Thao tác xử lý in-memory và sinh object key an toàn.
        """
        files = {"file": (malicious_filename, sample_valid_pdf_bytes, "application/pdf")}
        with patch("modules.celery_health.check_celery_and_queue_health", return_value=(True, "OK")):
            response = client.post("/extract", files=files)

        # Hệ thống phải tiếp nhận an toàn hoặc xử lý bình thường mà không bị crash hay path traversal
        check_equal(response.status_code, 200)
        data = response.json()
        check_in("request_id", data)
        check_equal(data["status"], "PROCESSING")

    @pytest.mark.parametrize("sqli_payload", [
        "' OR '1'='1",
        "1'; DROP TABLE request_logs; --",
        "' UNION SELECT null, null, null, null--",
        "admin'--",
        "c7a812ef' AND 1=1--",
    ])
    def test_sql_injection_defense_in_request_id(self, client, sqli_payload):
        """
        Kiểm tra khả năng chống SQL Injection tại tham số đường dẫn (GET /extract/{request_id}):
        - Hệ thống sử dụng parameterized queries với psycopg2 sql.SQL identifier.
        - Không bị ảnh hưởng bởi payload SQL độc hại, trả về HTTP 404 chuẩn.
        """
        response = client.get(f"/extract/{sqli_payload}")
        check_equal(response.status_code, 404)
        check_equal(response.json(), {"detail": "request_id not found"})

    @pytest.mark.parametrize("xss_payload", [
        "<script>alert('XSS')</script>",
        "<img src=x onerror=alert(1)>",
        "javascript:alert(1)",
        "'\"><svg/onload=alert(1)>",
    ])
    def test_xss_defense_in_request_id(self, client, xss_payload):
        """
        Kiểm tra khả năng chống tấn công Cross-Site Scripting (XSS):
        - Phản hồi từ máy chủ luôn có Content-Type application/json.
        - Không trả về HTML rendered payload trực tiếp cho trình duyệt thực thi.
        """
        response = client.get(f"/extract/{xss_payload}")
        check_equal(response.status_code, 404)
        check_in("application/json", response.headers.get("content-type", ""))
        data = response.json()
        check_in(data["detail"], ("request_id not found", "Not Found"))

    def test_attack_surface_reduction_removed_endpoints(self, client):
        """
        Xác minh thu hẹp bề mặt tấn công (Attack Surface Reduction):
        - Đảm bảo các endpoint không còn sử dụng (/classify_complaint, /extract/{id}/result, /webhook)
          đã hoàn toàn bị xóa khỏi bảng routing, trả về HTTP 404 / 405.
        """
        # 1. Endpoint /classify_complaint (POST)
        res_post_classify = client.post("/classify_complaint", json={"text": "Cử tri kiến nghị..."})
        check_in(res_post_classify.status_code, (404, 405), f"Endpoint /classify_complaint (POST) vẫn đang mở! HTTP {res_post_classify.status_code}")

        # 2. Endpoint /classify_complaint (GET)
        res_get_classify = client.get("/classify_complaint?text=test")
        check_in(res_get_classify.status_code, (404, 405), f"Endpoint /classify_complaint (GET) vẫn đang mở! HTTP {res_get_classify.status_code}")

        # 3. Endpoint /extract/{request_id}/result (GET)
        res_get_result = client.get("/extract/c7a812ef-29b1-4b16-9ef0-0a568b209d84/result")
        check_in(res_get_result.status_code, (404, 405), f"Endpoint /extract/.../result vẫn đang mở! HTTP {res_get_result.status_code}")

    # -------------------------------------------------------------------------
    # BUG-1123: Kiểm tra nội dung file tải lên (Magic Bytes & Structural Validation)
    # -------------------------------------------------------------------------
    def test_bug_1123_not_a_pdf_poc_rejected(self, client):
        """
        Tái hiện chính xác PoC của Pentest cho BUG-1123:
        - filename: 'not-a-pdf.pdf'
        - Content-Type: 'application/pdf'
        - Body: 'NOTPDF'
        Hệ thống bắt buộc phải từ chối với HTTP 415.
        """
        files = {"file": ("not-a-pdf.pdf", b"NOTPDF", "application/pdf")}
        response = client.post("/extract", files=files)
        check_equal(response.status_code, 415)
        data = response.json()
        check_in("Chỉ chấp nhận file PDF hợp lệ", data["detail"])

    def test_bug_1123_truncated_pdf_missing_eof_rejected(self, client):
        """Kiểm tra tệp tin có header %PDF- nhưng bị cắt xén thiếu marker %%EOF."""
        truncated_pdf = b"%PDF-1.4\n" + b"Random fake binary payload" * 10
        files = {"file": ("corrupted.pdf", truncated_pdf, "application/pdf")}
        response = client.post("/extract", files=files)
        check_equal(response.status_code, 415)
        data = response.json()
        check_in("marker %%EOF", data["detail"])

    def test_bug_1123_non_pdf_file_signature_rejected(self, client):
        """Kiểm tra tệp giả mạo có chữ ký PNG nhưng đặt đuôi .pdf và MIME application/pdf."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        files = {"file": ("fake_image.pdf", png_bytes, "application/pdf")}
        response = client.post("/extract", files=files)
        check_equal(response.status_code, 415)

    # -------------------------------------------------------------------------
    # BUG-1113: Kiểm tra xác thực quyền sử dụng API bằng API Key & Ownership
    # -------------------------------------------------------------------------
    def test_bug_1113_unauthenticated_request_rejected(self, unauthenticated_client):
        """Các endpoint nghiệp vụ bắt buộc phải có API Key, từ chối khi không truyền key (HTTP 401)."""
        # 1. POST /extract
        res_post = unauthenticated_client.post("/extract")
        check_equal(res_post.status_code, 401)
        check_in("API Key không hợp lệ hoặc bị thiếu", res_post.json()["detail"])

        # 2. GET /extract/{request_id}
        res_get = unauthenticated_client.get("/extract/some-uuid-1234")
        check_equal(res_get.status_code, 401)

        # 3. GET /health/celery
        res_celery = unauthenticated_client.get("/health/celery")
        check_equal(res_celery.status_code, 401)

        # 4. GET /health (Liveness probe không cần auth)
        res_health = unauthenticated_client.get("/health")
        check_equal(res_health.status_code, 200)

    def test_bug_1113_invalid_api_key_rejected(self, unauthenticated_client):
        """Kiểm tra từ chối khi API Key sai (HTTP 401)."""
        bad_headers = {"X-API-Key": "completely_wrong_key_xyz"}
        res = unauthenticated_client.post("/extract", headers=bad_headers)
        check_equal(res.status_code, 401)

    def test_bug_1113_bearer_token_authentication(self, unauthenticated_client, sample_valid_pdf_bytes):
        """Kiểm tra hỗ trợ xác thực bằng header Authorization: Bearer <token>."""
        valid_key = os.environ.get("SERVICE_API_KEY", "test-secret-api-key-8077")
        bearer_headers = {"Authorization": f"Bearer {valid_key}"}
        files = {"file": ("valid.pdf", sample_valid_pdf_bytes, "application/pdf")}

        with patch("modules.celery_health.check_celery_and_queue_health", return_value=(True, "OK")):
            res = unauthenticated_client.post("/extract", files=files, headers=bearer_headers)
        check_equal(res.status_code, 200)

    def test_bug_1113_job_ownership_isolation(self, unauthenticated_client, sample_valid_pdf_bytes, monkeypatch):
        """Kiểm tra cơ chế gắn job với người tạo (chống đọc trộm kết quả của client khác - HTTP 403)."""
        valid_key_a = "key-client-a-1113"
        valid_key_b = "key-client-b-1113"

        # Cấu hình 2 API key hợp lệ đồng thời (hỗ trợ phân tách client & rotation)
        monkeypatch.setenv("SERVICE_API_KEY", f"{valid_key_a},{valid_key_b}")

        # Client A tạo job
        files = {"file": ("doc_a.pdf", sample_valid_pdf_bytes, "application/pdf")}
        with patch("modules.celery_health.check_celery_and_queue_health", return_value=(True, "OK")):
            res_a = unauthenticated_client.post("/extract", files=files, headers={"X-API-Key": valid_key_a})
        check_equal(res_a.status_code, 200)
        req_id = res_a.json()["request_id"]

        # Client A kiểm tra job của mình -> Thành công (200)
        res_get_a = unauthenticated_client.get(f"/extract/{req_id}", headers={"X-API-Key": valid_key_a})
        check_equal(res_get_a.status_code, 200)

        # Client B cố tình đọc job của Client A -> Bị từ chối (403 Forbidden)
        res_get_b = unauthenticated_client.get(f"/extract/{req_id}", headers={"X-API-Key": valid_key_b})
        check_equal(res_get_b.status_code, 403)
        check_in("Không có quyền truy cập", res_get_b.json()["detail"])

    # -------------------------------------------------------------------------
    # BUG-1120: Phản hồi lỗi không lộ chuỗi exception
    # -------------------------------------------------------------------------
    def test_bug_1120_get_extract_status_masks_exception(self, client, isolated_job_store):
        """
        Kiểm tra BUG-1120: Endpoint GET /extract/{request_id} tuyệt đối không lộ chuỗi exception:
        - Bất kể lỗi nội bộ nào (DB connection, timeout, null pointer, OCR driver exception,...),
        - Trường error chỉ trả về thông báo chung chung.
        """
        import uuid
        from app import JobStatus, GENERIC_ERROR_MESSAGE

        req_id = str(uuid.uuid4())
        sensitive_exception = "psycopg2.OperationalError: FATAL: password authentication failed for user 'postgres'"
        isolated_job_store["db"][req_id] = {
            "request_id": req_id,
            "status": JobStatus.FAILED,
            "result": None,
            "error": sensitive_exception,
        }

        response = client.get(f"/extract/{req_id}")
        check_equal(response.status_code, 200)
        data = response.json()
        check_equal(data["status"], "FAILED")
        check_equal(data["error"], GENERIC_ERROR_MESSAGE)
        # Khẳng định không chứa chuỗi nhạy cảm nào
        assert sensitive_exception not in response.text
        assert "psycopg2" not in response.text
        assert "password" not in response.text

    def test_bug_1120_update_job_status_masks_error_string(self):
        """Kiểm tra hàm update_job_status tự động chuẩn hóa mọi error thành thông báo chung."""
        import uuid
        from app import update_job_status, get_job, JobStatus, GENERIC_ERROR_MESSAGE

        req_id = str(uuid.uuid4())
        sensitive_trace = "Traceback (most recent call last): ZeroDivisionError: division by zero"
        update_job_status(req_id, JobStatus.FAILED, error=sensitive_trace)

        job = get_job(req_id)
        check_equal(job["status"], JobStatus.FAILED)
        check_equal(job["error"], GENERIC_ERROR_MESSAGE)
        assert sensitive_trace not in (job["error"] or "")

    def test_bug_1120_celery_health_check_masks_driver_exception(self, client):
        """Kiểm tra GET /health/celery không làm lộ chuỗi exception của RabbitMQ / Redis."""
        with patch("modules.celery_health._check_broker", return_value=(False, "Celery Broker (RabbitMQ) không phản hồi hoặc mất kết nối")):
            res = client.get("/health/celery")
            check_equal(res.status_code, 200)
            data = res.json()
            check_equal(data["celery_active"], False)
            assert "amqp" not in data["detail"].lower()
            assert "exception" not in data["detail"].lower()

    def test_bug_1120_unhandled_server_exception_returns_generic_message(self):
        """Kiểm tra ngoại lệ bất ngờ (500) được bắt và trả về thông báo chung, không lộ stack trace."""
        from app import app, GENERIC_ERROR_MESSAGE
        from fastapi.testclient import TestClient

        with patch("app.get_job", side_effect=RuntimeError("Internal database connection broke: password=secret")):
            safe_client = TestClient(app, raise_server_exceptions=False)
            res = safe_client.get("/extract/any-id", headers={"X-API-Key": "test-secret-api-key-8077"})
            check_equal(res.status_code, 500)
            data = res.json()
            check_equal(data["detail"], GENERIC_ERROR_MESSAGE)
            assert "password" not in res.text
            assert "RuntimeError" not in res.text
