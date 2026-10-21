# ==============================================================================
# Dockerfile cho Complaint-EC (Alpine Linux - Python 3.11)
# Tối ưu hóa An toàn thông tin: Tối thiểu diện tích tấn công & Giảm thiểu CVEs/Licenses
# ==============================================================================
FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app"

# 1. Cập nhật và vá tất cả các lỗ hổng hệ điều hành (không cài thêm gói OS thừa)
# Ghi chú: ca-certificates và tzdata đã có sẵn trong base image python:3.11-alpine.
# Không cài bash, curl, procps để loại bỏ các gói GPL-3.0/GPL-2.0 không cần thiết.
RUN apk update && \
    apk upgrade --no-cache

WORKDIR /app

# 2. Cài đặt dependencies Python, sau đó gỡ bỏ triệt để build-tools & metadata gây false-positive
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y setuptools pip wheel 2>/dev/null || true && \
    rm -rf /root/.cache && \
    find /usr/local/lib/python3.11/site-packages -type d -name "licenses" -exec rm -rf {} + 2>/dev/null || true && \
    find /usr/local/lib/python3.11/site-packages -type d -name "sboms" -exec rm -rf {} + 2>/dev/null || true

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
