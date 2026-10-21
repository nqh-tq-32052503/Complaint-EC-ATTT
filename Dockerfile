# ==============================================================================
# Dockerfile cho Complaint-EC (FastAPI API Server & Celery Workers)
# ==============================================================================
FROM python:3.10-slim

# Cấu hình môi trường tối ưu cho Python trong container
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Cài đặt các thư viện hệ thống cần thiết (curl cho healthcheck, poppler cho PDF, procps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    poppler-utils \
    procps \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài đặt Python dependencies từ requirements.txt (tận dụng Docker layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Copy các thành phần mã nguồn cần thiết của dự án vào /app (tuân thủ docker:S6470)
COPY app.py entrypoint.sh test_connections.py ./
COPY config ./config
COPY modules ./modules
COPY tasks ./tasks
COPY docs ./docs

# Tạo non-root user và phân quyền thực thi
RUN groupadd -r appuser && useradd -r -g appuser -u 1000 -d /app appuser && \
    chmod +x entrypoint.sh test_connections.py || true && \
    chown -R appuser:appuser /app

# Expose cổng 8077 cho FastAPI Web API
EXPOSE 8077

# Kiểm tra sức khỏe container định kỳ
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8077/health || exit 1

# Chuyển sang user không đặc quyền
USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]