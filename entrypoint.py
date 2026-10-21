#!/usr/bin/env python3
"""
Entrypoint Supervisor for Complaint-EC Container (Google Distroless / Python Runtime).
Modes:
  - api / app        : Runs FastAPI Web API service (default)
  - worker / workers : Runs Celery pipeline workers with graceful shutdown
  - all              : Runs both Celery workers and FastAPI in a single container
  - <custom command> : Runs arbitrary command (e.g. pytest, python script)
"""

from __future__ import annotations

import os
import sys
import time
import signal
import subprocess
import threading
from typing import List

# 1. Nạp biến môi trường từ .env
from config.env_loader import load_project_env
load_project_env()

# 2. Cấu hình môi trường mạng nội bộ Docker
llm_url = os.environ.get("LLM_GATEWAY_URL", "")
if "localhost" in llm_url or "127.0.0.1" in llm_url:
    os.environ["LLM_GATEWAY_URL"] = llm_url.replace("localhost", "host.docker.internal").replace("127.0.0.1", "host.docker.internal")
    print(f"[CONFIG] LLM_GATEWAY_URL converted for Docker: {os.environ['LLM_GATEWAY_URL']}")

minio_ep = os.environ.get("MINIO_ENDPOINT", "")
if minio_ep.startswith("localhost:") or minio_ep.startswith("127.0.0.1:"):
    os.environ["MINIO_ENDPOINT"] = minio_ep.replace("localhost:", "host.docker.internal:").replace("127.0.0.1:", "host.docker.internal:")
    print(f"[CONFIG] MINIO_ENDPOINT converted for Docker: {os.environ['MINIO_ENDPOINT']}")

app_dir = "/app"
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)


def sync_minio_config():
    """Kiểm tra và khởi tạo các bucket/file cấu hình MinIO."""
    print("[INIT] Checking MinIO buckets and config objects...")
    try:
        from modules.minio_utils import upload_knowledge_base_to_minio, upload_spelling_error_to_minio
        upload_knowledge_base_to_minio()
        upload_spelling_error_to_minio()
    except Exception as exc:
        print(f"[INIT Warning] MinIO sync exception (continuing startup): {exc}")


def run_diagnostics():
    """Chạy kiểm tra chẩn đoán kết nối tới các dịch vụ phụ thuộc."""
    try:
        import test_connections
        test_connections.run_diagnostics(max_retries=2)
    except Exception as exc:
        print(f"[WARNING] Diagnostic checks failed: {exc}. Continuing startup...")


def run_api():
    """Khởi chạy FastAPI Web API service với Uvicorn."""
    print("========================================================================")
    print(" Starting Complaint-EC FastAPI Service on port 8077")
    print("========================================================================")
    sync_minio_config()
    run_diagnostics()

    import uvicorn
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8077"))
    uvicorn.run("app:app", host=host, port=port)


def run_workers():
    """Khởi chạy các Celery Workers và quản lý vòng đời tiến trình graceful shutdown."""
    num_workers = int(os.environ.get("NUM_WORKERS", "8"))
    print("========================================================================")
    print(f" Starting Complaint-EC Celery Workers ({num_workers} worker queues)")
    print("========================================================================")
    sync_minio_config()
    run_diagnostics()

    procs: List[subprocess.Popen] = []
    shutdown_event = threading.Event()

    def cleanup(signum=None, frame=None):
        if shutdown_event.is_set():
            return
        shutdown_event.set()
        print("\n[INFO] Received shutdown signal. Terminating Celery workers gracefully...")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("[INFO] All Celery workers stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    for i in range(1, num_workers + 1):
        worker_name = f"x2.dannguyen.uc1.worker{i}_queue"
        print(f"[WORKER] Starting {worker_name} listening on queue {worker_name}...")
        cmd = [
            sys.executable, "-m", "celery",
            "-A", "tasks.celery_app", "worker",
            "-Q", worker_name,
            "-n", f"{worker_name}@%h",
            "--loglevel=info",
            "-P", "solo",
        ]
        p = subprocess.Popen(cmd)
        procs.append(p)
        time.sleep(0.3)

    print(f"[SUCCESS] All {num_workers} workers running. Waiting for tasks...")
    while not shutdown_event.is_set():
        time.sleep(1)


def run_all():
    """Khởi chạy đồng thời cả Celery Workers và FastAPI Web API."""
    num_workers = int(os.environ.get("NUM_WORKERS", "8"))
    print("========================================================================")
    print(f" Starting Complaint-EC All-In-One (FastAPI + {num_workers} Celery Workers)")
    print("========================================================================")
    sync_minio_config()
    run_diagnostics()

    procs: List[subprocess.Popen] = []

    def cleanup(signum=None, frame=None):
        print("\n[INFO] Shutting down all services...")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("[INFO] All services stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    for i in range(1, num_workers + 1):
        worker_name = f"x2.dannguyen.uc1.worker{i}_queue"
        cmd = [
            sys.executable, "-m", "celery",
            "-A", "tasks.celery_app", "worker",
            "-Q", worker_name,
            "-n", f"{worker_name}@%h",
            "--loglevel=info",
            "-P", "solo",
        ]
        p = subprocess.Popen(cmd)
        procs.append(p)
        time.sleep(0.3)

    import uvicorn
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8077"))
    try:
        uvicorn.run("app:app", host=host, port=port)
    finally:
        cleanup()


def main():
    args = sys.argv[1:]
    mode = args[0].lower() if args else "api"

    if mode in ("api", "app"):
        run_api()
    elif mode in ("worker", "workers"):
        run_workers()
    elif mode == "all":
        run_all()
    else:
        # Chạy lệnh tuỳ ý (e.g. ["-c", "..."], ["pytest"], ["python", "script.py"])
        if args and (args[0].startswith("-") or args[0].endswith(".py")):
            res = subprocess.run([sys.executable, *args])
        else:
            res = subprocess.run(args)
        sys.exit(res.returncode)


if __name__ == "__main__":
    main()
