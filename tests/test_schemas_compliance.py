import pytest
from pydantic import ValidationError

from app import (
    KienNghiTongHop,
    KienNghiItem,
    DonViNhan,
    Metadata,
    ErrorInfo,
    ExtractResponse,
    JobStatus,
    create_table_error_response,
)
from tests.assertions import check_equal, check_in, check_is_none, check_true


class TestSchemasCompliance:
    """
    Bộ kiểm thử kiểm định tính tuân thủ Schema dữ liệu (Data Schema Compliance):
    - Đảm bảo toàn bộ cấu trúc dữ liệu trả về của hệ thống tuân thủ nghiêm ngặt theo
      JSON Schema draft 2020-12 (docs/schema_2608.json).
    - Kiểm tra các ràng buộc: extra="forbid", uniqueItems, minItems/maxItems, consistency checks.
    """

    def test_valid_schema_instantiation(self, sample_kien_nghi_tong_hop):
        """Khởi tạo thành công KienNghiTongHop với dữ liệu mẫu chuẩn."""
        model = KienNghiTongHop(**sample_kien_nghi_tong_hop)
        check_equal(model.tong_so_kien_nghi, 1)
        check_equal(len(model.kien_nghi), 1)
        check_equal(model.metadata.signer, "Nguyễn Văn A")
        check_equal(model.kien_nghi[0].don_vi_nhan[0].muc_do_tin_cay, "high")

    @pytest.mark.parametrize("target_class,payload", [
        (KienNghiTongHop, {"extra_unauthorized_key": "injected"}),
        (KienNghiItem, {"stt": 1, "noi_dung": "test", "so_thu_tu_kien_nghi": 1, "don_vi_nhan": [], "headers": [], "unknown_field": 123}),
        (DonViNhan, {"ten_don_vi": "Bộ Y tế", "muc_do_tin_cay": "high", "phuong_phap": "llm", "is_mapped": True, "extra_col": True}),
        (Metadata, {"ky_hop": "Kỳ họp 1", "hack": "attack"}),
        (ErrorInfo, {"code": "TABLE_CONTENT_UNSUPPORTED", "message": "error", "stacktrace": "leak"}),
        (ExtractResponse, {"request_id": "123", "status": JobStatus.PROCESSING, "malicious_field": "test"}),
    ])
    def test_extra_fields_forbidden(self, target_class, payload):
        """
        Kiểm tra cấu hình extra='forbid' (tương đương additionalProperties: false trong JSON Schema):
        - Bất kỳ trường lạ nào không nằm trong schema đều phải bị từ chối bằng ValidationError.
        """
        with pytest.raises(ValidationError) as exc_info:
            target_class(**payload)
        check_true("extra_forbidden" in str(exc_info.value) or "Extra inputs are not permitted" in str(exc_info.value))

    def test_consistency_check_tong_so_kien_nghi(self, sample_kien_nghi_tong_hop):
        """
        Kiểm tra tính toàn vẹn nghiệp vụ:
        - tong_so_kien_nghi bắt buộc phải bằng độ dài thực tế của danh sách kien_nghi.
        """
        bad_data = dict(sample_kien_nghi_tong_hop)
        bad_data["tong_so_kien_nghi"] = 99  # Sai lệch với len(kien_nghi) == 1

        with pytest.raises(ValidationError) as exc_info:
            KienNghiTongHop(**bad_data)
        check_in("tong_so_kien_nghi", str(exc_info.value))
        check_in("phải bằng số phần tử trong kien_nghi", str(exc_info.value))

    def test_unique_items_cac_don_vi_nhan(self, sample_kien_nghi_tong_hop):
        """
        Kiểm tra ràng buộc uniqueItems: true của trường cac_don_vi_nhan:
        - Không được phép chứa phần tử trùng lặp.
        """
        bad_data = dict(sample_kien_nghi_tong_hop)
        bad_data["cac_don_vi_nhan"] = ["Bộ Y tế", "Bộ Giao thông vận tải", "Bộ Y tế"]  # Trùng Bộ Y tế

        with pytest.raises(ValidationError) as exc_info:
            KienNghiTongHop(**bad_data)
        check_in("uniqueItems", str(exc_info.value))
        check_in("phải chứa các giá trị duy nhất", str(exc_info.value))

    def test_don_vi_nhan_min_max_length(self, sample_kien_nghi_tong_hop):
        """
        Kiểm tra ràng buộc số lượng đơn vị thẩm quyền dự đoán:
        - Tối thiểu 1 đơn vị (minItems: 1).
        - Tối đa 5 đơn vị (maxItems: 5).
        """
        bad_data = dict(sample_kien_nghi_tong_hop)
        bad_item = dict(bad_data["kien_nghi"][0])

        # 1. Test rỗng (0 phần tử)
        bad_item["don_vi_nhan"] = []
        bad_data["kien_nghi"] = [bad_item]
        with pytest.raises(ValidationError):
            KienNghiTongHop(**bad_data)

        # 2. Test vượt quá 5 phần tử (6 phần tử)
        sample_org = {
            "ten_don_vi": "Bộ A",
            "muc_do_tin_cay": "low",
            "phuong_phap": "llm",
            "is_mapped": False
        }
        bad_item["don_vi_nhan"] = [dict(sample_org, ten_don_vi=f"Bộ {i}") for i in range(6)]
        bad_data["kien_nghi"] = [bad_item]
        with pytest.raises(ValidationError):
            KienNghiTongHop(**bad_data)

    @pytest.mark.parametrize("invalid_conf", ["extreme", "urgent", "unknown", ""])
    def test_confidence_literal_enforcement(self, invalid_conf):
        """Kiểm tra ràng buộc enum: ['high', 'medium', 'low'] cho muc_do_tin_cay."""
        with pytest.raises(ValidationError):
            DonViNhan(
                ten_don_vi="Bộ Y tế",
                muc_do_tin_cay=invalid_conf,
                phuong_phap="rule_based",
                is_mapped=True
            )

    def test_table_unsupported_error_response_conformance(self):
        """
        Kiểm tra hàm khởi tạo response lỗi TABLE_CONTENT_UNSUPPORTED:
        - Đảm bảo response tạo ra hoàn toàn hợp lệ với KienNghiTongHop.
        """
        error_resp = create_table_error_response("File PDF chứa bảng biểu không hỗ trợ")
        check_equal(error_resp.status, "failed")
        check_equal(error_resp.error.code, "TABLE_CONTENT_UNSUPPORTED")
        check_equal(error_resp.tong_so_kien_nghi, 0)
        check_equal(error_resp.kien_nghi, [])
        check_equal(error_resp.cac_don_vi_nhan, [])
        check_is_none(error_resp.metadata.signer)
