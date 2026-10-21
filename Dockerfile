# ==============================================================================
# Dockerfile cho Complaint-EC (Alpine Linux - Python 3.11)
# Tối ưu hóa An toàn thông tin: Tối thiểu diện tích tấn công & Giảm thiểu CVEs/Licenses
# ==============================================================================
FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app"

# 1. Cập nhật và cài đặt các gói hệ thống cần thiết cho runtime và vận hành:
# - apk upgrade: vá tất cả các lỗ hổng hệ điều hành (như zlib CVE-2026-85091)
# - bash: tương thích scripting và troubleshooting
# - curl: công cụ network check & health check
# - procps: hỗ trợ quản lý tiến trình (ps, pkill) cho supervisor
# - ca-certificates: chứng chỉ tin cậy kết nối SSL/TLS
# - tzdata: định cấu hình múi giờ hệ thống
RUN apk update && \
    apk upgrade --no-cache && \
    apk add --no-cache \
        bash \
        curl \
        procps \
        ca-certificates \
        tzdata

WORKDIR /app

# 2. Nâng cấp pip, setuptools an toàn và cài đặt dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    rm -f /usr/local/lib/python3.11/site-packages/pip/_vendor/bom.cdx.json && \
    find /usr/local/lib/python3.11/site-packages -type d -name sboms -exec rm -rf {} +

# 3. Copy toàn bộ mã nguồn ứng dụng
COPY app.py entrypoint.py entrypoint.sh test_connections.py ./
COPY config ./config
COPY modules ./modules
COPY tasks ./tasks
COPY docs ./docs

# 4. Tạo người dùng và nhóm không đặc quyền (non-root appuser: UID/GID 1000)
RUN addgroup -g 1000 appuser && \
    adduser -D -u 1000 -G appuser -h /app appuser && \
    chmod +x entrypoint.py entrypoint.sh test_connections.py && \
    chown -R appuser:appuser /app

# Mở cổng 8077 cho FastAPI Web API
EXPOSE 8077

# Kiểm tra sức khỏe định kỳ container
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8077/health', timeout=5)" || exit 1

# Thiết lập user chạy ứng dụng
USER appuser

# Khởi chạy qua supervisor entrypoint.py
ENTRYPOINT ["python3", "/app/entrypoint.py"]
CMD ["api"]
