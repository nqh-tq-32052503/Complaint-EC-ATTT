#!/bin/bash
set -e

# ==============================================================================
# Entrypoint Script for Complaint-EC Container
# Modes:
#   - api / app        : Runs FastAPI Web API service (default)
#   - worker / workers : Runs Celery pipeline workers with graceful shutdown
#   - all              : Runs both Celery workers and FastAPI in a single container
#   - <custom command> : Runs arbitrary command (e.g. bash, pytest, python)
# ==============================================================================

# Nạp biến môi trường từ .env nếu tồn tại
if [ -f "/app/.env" ]; then
    set -a
    source "/app/.env"
    set +a
elif [ -f ".env" ]; then
    set -a
    source ".env"
    set +a
fi

export OCR_SERVICE_URL="${OCR_SERVICE_URL}"
export NUM_WORKERS="${NUM_WORKERS:-8}"
export PYTHONPATH="/app:${PYTHONPATH}"

# Tự động chuyển đổi localhost/127.0.0.1 sang host.docker.internal trong container
if [[ "$LLM_GATEWAY_URL" =~ localhost|127\.0\.0\.1 ]]; then
    export LLM_GATEWAY_URL=$(echo "$LLM_GATEWAY_URL" | sed -E 's/(localhost|127\.0\.0\.1)/host.docker.internal/g')
    echo "[CONFIG] LLM_GATEWAY_URL converted for Docker: $LLM_GATEWAY_URL"
fi
if [[ "$POSTGRES_HOST" == "localhost" || "$POSTGRES_HOST" == "127.0.0.1" ]]; then
    export POSTGRES_HOST="host.docker.internal"
fi
if [[ "$MINIO_ENDPOINT" =~ ^(localhost|127\.0\.0\.1): ]]; then
    export MINIO_ENDPOINT=$(echo "$MINIO_ENDPOINT" | sed -E 's/^(localhost|127\.0\.0\.1)/host.docker.internal/g')
fi

sync_minio_config() {
    echo "[INIT] Checking MinIO buckets and config objects..."
    python3 -c "from modules.minio_utils import upload_knowledge_base_to_minio, upload_spelling_error_to_minio; upload_knowledge_base_to_minio(); upload_spelling_error_to_minio()" 2>/dev/null || true
}

MODE="${1:-api}"

case "$MODE" in
    api|app)
        echo "========================================================================"
        echo " Starting Complaint-EC FastAPI Service on port 8077"
        echo "========================================================================"
        sync_minio_config
        python3 test_connections.py 2 || {
            echo "[WARNING] One or more diagnostic checks failed. Continuing startup..."
        }
        exec uvicorn app:app --host 0.0.0.0 --port 8077
        ;;

    worker|workers)
        echo "========================================================================"
        echo " Starting Complaint-EC Celery Workers ($NUM_WORKERS worker queues)"
        echo "========================================================================"
        sync_minio_config
        python3 test_connections.py 2 || {
            echo "[WARNING] One or more diagnostic checks failed. Continuing startup..."
        }

        pids=()
        cleanup() {
            echo -e "\n[INFO] Received shutdown signal. Terminating Celery workers gracefully..."
            for pid in "${pids[@]}"; do
                kill -TERM "$pid" 2>/dev/null || true
            done
            wait "${pids[@]}" 2>/dev/null || true
            echo "[INFO] All Celery workers stopped."
            exit 0
        }
        trap cleanup SIGTERM SIGINT

        mkdir -p /app/logs
        for i in $(seq 1 $NUM_WORKERS); do
            WORKER_NAME="x2.dannguyen.uc1.worker${i}_queue"
            echo "[WORKER] Starting $WORKER_NAME listening on queue $WORKER_NAME..."
            celery -A tasks.celery_app worker \
                -Q "$WORKER_NAME" \
                -n "${WORKER_NAME}@%h" \
                --loglevel=info \
                -P solo &
            pids+=($!)
            sleep 0.3
        done

        echo "[SUCCESS] All $NUM_WORKERS workers running. Waiting for tasks..."
        wait "${pids[@]}"
        ;;

    all)
        echo "========================================================================"
        echo " Starting Complaint-EC All-In-One (FastAPI + $NUM_WORKERS Celery Workers)"
        echo "========================================================================"
        sync_minio_config
        python3 test_connections.py 2 || {
            echo "[WARNING] One or more diagnostic checks failed. Continuing startup..."
        }

        for i in $(seq 1 $NUM_WORKERS); do
            WORKER_NAME="x2.dannguyen.uc1.worker${i}_queue"
            celery -A tasks.celery_app worker \
                -Q "$WORKER_NAME" \
                -n "${WORKER_NAME}@%h" \
                --loglevel=info \
                -P solo &
            sleep 0.3
        done

        exec uvicorn app:app --host 0.0.0.0 --port 8077
        ;;

    *)
        exec "$@"
        ;;
esac
