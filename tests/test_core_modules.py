import pytest
import asyncio
from modules.pipeline import file_hash
from modules.minio_utils import normalize_minio_key, get_content_type
from modules.postprocessor import should_exclude_by_headers, filter_and_reindex_kien_nghi
from app import SingleFlightManager
from tests.assertions import check_equal, check_not_equal, check_true


class TestCoreModules:
    """Bộ kiểm thử cho các module cốt lõi, tiện ích tính toán và xử lý trung gian."""

    def test_file_hash_consistency(self):
        """Kiểm tra tính nhất quán và bất biến của hàm băm tài liệu file_hash."""
        content1 = b"PDF Content Document 1"
        content2 = b"PDF Content Document 2"

        h1_a = file_hash(content1)
        h1_b = file_hash(content1)
        h2 = file_hash(content2)

        # Tính nhất quán: cùng nội dung ra cùng mã hash
        check_equal(h1_a, h1_b)
        check_equal(len(h1_a), 64)  # SHA256 / Blake3 hex length
        # Tính phân biệt: khác nội dung ra mã hash khác nhau
        check_not_equal(h1_a, h2)

    @pytest.mark.parametrize("raw_path,expected_key", [
        ("logs/2026-10-14_09-00-00/document.pdf", "2026-10-14_09-00-00/document.pdf"),
        ("/logs/2026-10-14_09-00-00/1.canonical_ocr.json", "2026-10-14_09-00-00/1.canonical_ocr.json"),
        ("x2-dannguyen-uc1-logs/2026-10-14_09-00-00/3.metadata.json", "2026-10-14_09-00-00/3.metadata.json"),
        ("/x2-dannguyen-uc1-logs/sub/artifact.txt", "sub/artifact.txt"),
        ("already_normalized_file.json", "already_normalized_file.json"),
        ("logs/execution_times.json", "execution_times.json"),
    ])
    def test_normalize_minio_key(self, raw_path, expected_key):
        """
        Kiểm tra hàm normalize_minio_key:
        - Đảm bảo loại bỏ chính xác tiền tố logs/, tên bucket và dấu gạch chéo đầu dòng.
        """
        check_equal(normalize_minio_key(raw_path), expected_key)

    @pytest.mark.parametrize("filename,expected_content_type", [
        ("document.pdf", "application/pdf"),
        ("data.json", "application/json; charset=utf-8"),
        ("report.txt", "text/plain; charset=utf-8"),
        ("readme.md", "text/markdown; charset=utf-8"),
        ("summary.csv", "text/csv; charset=utf-8"),
    ])
    def test_get_content_type(self, filename, expected_content_type):
        """Kiểm tra xác định Content-Type chuẩn cho từng loại tệp lưu trữ trên MinIO."""
        check_equal(get_content_type(filename), expected_content_type)

    @pytest.mark.parametrize("headers,expected_exclude", [
        (["I. KIẾN NGHỊ CHUNG"], True),
        (["CÁC KIẾN NGHỊ CHUNG CỦA CỬ TRI"], True),
        (["TÌNH HÌNH CHUNG VÀ KINH TẾ XÃ HỘI"], True),
        (["LĨNH VỰC NÔNG NGHIỆP VÀ PHÁT TRIỂN NÔNG THÔN"], False),
        (["II. KIẾN NGHỊ CỤ THỂ"], False),
        ([], False),
        (None, False),
    ])
    def test_should_exclude_by_headers(self, headers, expected_exclude):
        """Kiểm tra bộ lọc loại trừ các đề mục chung (General complaints exclusion)."""
        check_equal(should_exclude_by_headers(headers), expected_exclude)

    def test_filter_and_reindex_kien_nghi(self):
        """Kiểm tra hàm lọc kiến nghị chung và đánh số lại STT liên tục (1..N)."""
        raw_items = [
            {
                "stt": 1,
                "noi_dung": "Kiến nghị chung số 1",
                "headers": ["KIẾN NGHỊ CHUNG"],
                "don_vi_nhan": [{"ten_don_vi": "Bộ Nội Vụ"}]
            },
            {
                "stt": 2,
                "noi_dung": "Kiến nghị giao thông",
                "headers": ["GIAO THÔNG VẬN TẢI"],
                "don_vi_nhan": [{"ten_don_vi": "Bộ Giao thông vận tải"}]
            },
            {
                "stt": 3,
                "noi_dung": "Kiến nghị tình hình chung",
                "headers": ["TÌNH HÌNH CHUNG"],
                "don_vi_nhan": [{"ten_don_vi": "Văn Phòng Chính Phủ"}]
            },
            {
                "stt": 4,
                "noi_dung": "Kiến nghị y tế",
                "headers": ["Y TẾ VÀ XÃ HỘI"],
                "don_vi_nhan": [{"ten_don_vi": "Bộ Y tế"}]
            },
        ]

        filtered = filter_and_reindex_kien_nghi(raw_items)
        check_equal(len(filtered), 2)
        # Đảm bảo 2 kiến nghị chung bị lọc bỏ, còn 2 kiến nghị cụ thể
        check_equal(filtered[0]["noi_dung"], "Kiến nghị giao thông")
        check_equal(filtered[1]["noi_dung"], "Kiến nghị y tế")
        # Đảm bảo STT được đánh số lại liên tục từ 1
        check_equal(filtered[0]["stt"], 1)
        check_equal(filtered[1]["stt"], 2)
        # Đảm bảo so_thu_tu_kien_nghi tính theo từng cơ quan chính
        check_equal(filtered[0]["so_thu_tu_kien_nghi"], 1)
        check_equal(filtered[1]["so_thu_tu_kien_nghi"], 1)

    def test_single_flight_coalescing_manager(self):
        """
        Kiểm tra cơ chế điều phối Single-Flight Request Coalescing:
        - Request đầu tiên cùng hash nhận vai trò LEADER.
        - Request thứ hai cùng hash nhận vai trò FOLLOWER và chờ LEADER hoàn tất.
        """
        async def _run():
            mgr = SingleFlightManager()
            pdf_hash = "testhash123456789"

            # Request 1 đăng ký
            role1, flight1 = await mgr.acquire_or_join(pdf_hash, force_reprocess=False, request_id="req-1")
            check_equal(role1, "LEADER")
            check_equal(flight1.leader_request_id, "req-1")

            # Request 2 đến trong khi Request 1 đang xử lý
            role2, flight2 = await mgr.acquire_or_join(pdf_hash, force_reprocess=False, request_id="req-2")
            check_equal(role2, "FOLLOWER")
            check_equal(flight2.leader_request_id, "req-1")
            check_true(flight1 is flight2)

            # Leader hoàn thành xử lý
            mock_result = {"status": "success", "data": "test"}
            await mgr.complete(pdf_hash, force_reprocess=False, result=mock_result, error=None)

            check_true(flight2.event.is_set())
            check_equal(flight2.result, mock_result)

        asyncio.run(_run())
