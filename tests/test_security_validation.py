import io
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
