# ==============================================================================
# Multi-Stage Dockerfile cho Complaint-EC (Google Distroless Python 3)
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Builder (Cài đặt dependencies và chuẩn bị wheels an toàn)
# ------------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Cài đặt / nâng cấp các công cụ đóng gói tối thiểu theo khuyến nghị bảo mật
RUN pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" wheel

# Cài đặt các thư viện Python vào thư mục độc lập
COPY requirements.txt .
RUN pip install --no-cache-dir --target=/build/site-packages -r requirements.txt


# ------------------------------------------------------------------------------
# Stage 2: Runtime (Google Distroless - Tối ưu ATTT, 0 Shell, 0 Package Manager)
# ------------------------------------------------------------------------------
FROM gcr.io/distroless/python3-debian12:latest

# Cấu hình môi trường Python và đường dẫn nạp module
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app:/usr/local/lib/python3.11/dist-packages"

WORKDIR /app

# Copy thư viện đã cài đặt từ builder stage
COPY --chown=nonroot:nonroot --from=builder /build/site-packages /usr/local/lib/python3.11/dist-packages

# Copy mã nguồn dự án vào /app với quyền người dùng không đặc quyền
COPY --chown=nonroot:nonroot app.py entrypoint.py test_connections.py ./
COPY --chown=nonroot:nonroot config ./config
COPY --chown=nonroot:nonroot modules ./modules
COPY --chown=nonroot:nonroot tasks ./tasks
COPY --chown=nonroot:nonroot docs ./docs

# Expose cổng 8077 cho FastAPI Web API
EXPOSE 8077

# Kiểm tra sức khỏe container định kỳ bằng Python native (không phụ thuộc curl / shell)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD ["/usr/bin/python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8077/health', timeout=5)"]

# Chuyển sang user nonroot (UID 65532) của Google Distroless
USER nonroot

# Khởi chạy qua supervisor entrypoint.py
ENTRYPOINT ["/usr/bin/python3", "/app/entrypoint.py"]
CMD ["api"]
