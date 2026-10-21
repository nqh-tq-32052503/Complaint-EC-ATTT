# Complaint-EC

Hệ thống số hóa, trích xuất thông tin hành chính và phân loại thẩm quyền giải quyết **kiến nghị cử tri** từ tài liệu PDF, được đóng gói và vận hành qua **Docker Compose**.

---

## 🚀 Khởi Chạy Nhanh (Docker Compose)

### 1. Cấu hình môi trường
Tạo file cấu hình `.env` từ file mẫu:
```bash
cp .env.example .env
```
*(Kiểm tra và cập nhật các thông số kết nối PostgreSQL, RabbitMQ, Redis, MinIO, OCR Service và LLM Gateway).*

### 2. Khởi chạy hệ thống
Build và khởi chạy toàn bộ dịch vụ ngầm (`app` cổng `8077` và `worker` gồm 8 queues):
```bash
docker compose up -d --build
```

### 3. Kiểm tra trạng thái & Log
* **Xem trạng thái container**:
  ```bash
  docker compose ps
  ```
* **Xem log toàn bộ**:
  ```bash
  docker compose logs -f
  ```
* **Xem riêng log FastAPI App hoặc Celery Worker**:
  ```bash
  docker compose logs -f app
  docker compose logs -f worker
  ```

### 4. Dừng hệ thống
```bash
docker compose down
```

---

## 🌐 Các Endpoint API Chính

* **Swagger UI Documentation**: [http://localhost:8077/docs](http://localhost:8077/docs)
* **POST `/extract`**: Upload file PDF khởi tạo tác vụ trích xuất kiến nghị cử tri (bất đồng bộ).
* **GET `/extract/{request_id}`**: Polling định kỳ kiểm tra trạng thái và lấy kết quả trích xuất JSON.
* **GET `/health` & GET `/health/celery`**: Kiểm tra tình trạng hoạt động của FastAPI server và cụm điều phối Celery workers.

---

## 🧪 Kiểm Thử Hệ Thống & Đánh Giá An Toàn (Testing & Security Evaluation)

Thư mục `tests/` chứa toàn bộ kịch bản kiểm thử tự động được thiết kế phục vụ công tác QA và **Đánh giá An toàn Thông tin (Security Assessment)**. Bộ kiểm thử được xây dựng theo kiến trúc *offline-first* (sử dụng in-memory test fixtures và mock các dịch vụ xử lý nặng như OCR/LLM), cho phép kiểm thử toàn diện mọi luồng hoạt động mà không phụ thuộc vào hạ tầng bên ngoài.

### 1. Cấu trúc bộ test suite

| File kiểm thử | Số test case | Mục đích kiểm thử |
| :--- | :---: | :--- |
| [`tests/test_security_validation.py`](tests/test_security_validation.py) | **23** | **Kiểm thử Bảo mật & Rào chắn ATTT**: Chặn MIME type trái phép (JPEG, PNG, Shell script, ZIP, v.v.), chặn DoS payload quá 30MB, ngăn ngừa Path Traversal trong tên file (`../`, null-byte), chống SQL Injection & XSS qua `request_id`, và xác minh thu hẹp bề mặt tấn công (*Attack Surface Reduction* - đóng hoàn toàn endpoint `/classify_complaint` và Webhook). |
| [`tests/test_api_endpoints.py`](tests/test_api_endpoints.py) | **10** | **Kiểm thử Chức năng API**: Kiểm tra luồng Polling bất đồng bộ (`POST /extract` ➔ `GET /extract/{request_id}`), trạng thái tác vụ (`processing`, `finished`, `failed`, `not_found`), cơ chế tính toán SHA256 Cache Hit và cờ ghi đè `force_reprocess`. |
| [`tests/test_schemas_compliance.py`](tests/test_schemas_compliance.py) | **14** | **Kiểm thử Chuẩn hóa Schema**: Đối soát tính tuân thủ nghiêm ngặt theo `docs/schema_2608.json` (chính sách `extra="forbid"`, logic toàn vẹn `tong_so_kien_nghi == len(kien_nghi)`, kiểm tra danh sách cơ quan nhận `uniqueItems`, định dạng lỗi `TABLE_CONTENT_UNSUPPORTED`). |
| [`tests/test_core_modules.py`](tests/test_core_modules.py) | **21** | **Kiểm thử Module Nghiệp vụ Lõi**: Hàm băm SHA256 (`file_hash`), logic chuẩn hóa key lưu trữ MinIO (`normalize_minio_key`), nhận diện Content-Type, lọc tiêu đề bảng và cơ chế chống nghẽn Single-Flight Coalescing. |

---

### 2. Cài đặt môi trường kiểm thử

Cài đặt các gói phụ trợ kiểm thử (nếu chưa có):
```bash
pip install pytest httpx
```

---

### 3. Hướng dẫn thực thi lệnh kiểm thử

* **Chạy toàn bộ test suite (khuyến nghị cho đội ngũ Security)**:
  ```bash
  pytest -v tests/
  ```

* **Chạy riêng nhóm kiểm tra Bảo mật & Rào chắn dữ liệu đầu vào**:
  ```bash
  pytest -v tests/test_security_validation.py
  ```

* **Chạy riêng nhóm kiểm tra chức năng API & chu trình Polling**:
  ```bash
  pytest -v tests/test_api_endpoints.py
  ```

* **Chạy riêng nhóm kiểm tra tính tương thích JSON Schema**:
  ```bash
  pytest -v tests/test_schemas_compliance.py
  ```

* **Chạy riêng nhóm kiểm tra module lõi & Single-Flight Coalescing**:
  ```bash
  pytest -v tests/test_core_modules.py
  ```

* **Chạy kiểm thử có đo độ phủ mã nguồn (Code Coverage)** *(tuỳ chọn)*:
  ```bash
  pytest --cov=modules --cov=app -v tests/
  ```

---

## 📚 Tài Liệu Kỹ Thuật Chi Tiết

* 📋 [**Tài Liệu Đặc Tả API (API_DOCS.md)**](API_DOCS.md): Hồ sơ kỹ thuật mô tả giao diện API, cơ chế tiếp nhận dữ liệu PDF & lưu trữ MinIO, quy trình tích hợp Polling định kỳ và đặc tả schema dữ liệu chuẩn.
* 📘 [**Tài Liệu Tích Hợp API**](docs/api.md): Đặc tả các endpoint, quy trình Webhook / Polling, cơ chế Retry và mã lỗi.
* 🏛️ [**Kiến Trúc Hệ Thống & AI Pipeline**](docs/architecture.md): Sơ đồ hệ thống, cơ chế Single-Flight Coalescing, 8 hàng đợi Celery và 4 giai đoạn AI pipeline.
* 📋 [**Đặc Tả Chuẩn Dữ Liệu (Schema)**](docs/schema.md): Giải thích chi tiết các trường dữ liệu và ràng buộc validation JSON Schema.
* 🤖 [**Hướng Dẫn Phục Vụ Mô Hình AI (vLLM)**](docs/model_serving.md): Cách triển khai Surya OCR và Gemma-4-26B-A4B-it trên máy chủ GPU.

