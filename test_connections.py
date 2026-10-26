#!/usr/bin/env python3
"""
Diagnostic script to test connections to all services required by Complaint-EC:
1. RabbitMQ (Celery Broker)
2. Redis (Celery Result Backend)
3. MinIO (Object Storage)
4. OCR Microservice
5. LLM Gateway (OpenAI-compatible)
"""

import os
import sys
import time
import socket
import urllib.request
import urllib.error
import json
from urllib.parse import urlparse
from config.env_loader import load_project_env

load_project_env()


def print_status(name: str, passed: bool, message: str = ""):
    tag = "[SUCCESS]" if passed else "[FAILED]"
    print(f"  {tag} {name}: {message}")


def test_rabbitmq() -> bool:
    broker_url = os.environ.get("CELERY_BROKER_URL", "")
    print(f"[TEST 1/5] RabbitMQ Broker ({broker_url.split('@')[-1] if '@' in broker_url else broker_url})...")
    try:
        from kombu import Connection
        with Connection(broker_url, connect_timeout=5) as conn:
            conn.connect()
        print_status("RabbitMQ", True, "Successfully established AMQP connection")
        return True
    except Exception as exc:
        print_status("RabbitMQ", False, f"AMQP error: {exc}")
        return False


def test_redis() -> bool:
    redis_url = os.environ.get("CELERY_RESULT_BACKEND", "")
    print(f"[TEST 2/5] Redis Backend ({redis_url.split('@')[-1] if '@' in redis_url else redis_url})...")
    try:
        import redis
        r = redis.Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
        r.ping()
        print_status("Redis", True, "PING responded PONG successfully")
        return True
    except Exception as exc:
        print_status("Redis", False, f"Redis error: {exc}")
        return False


def test_minio() -> bool:
    endpoint = os.environ.get("MINIO_ENDPOINT", "")
    access_key = os.environ.get("MINIO_ROOT_USER", "")
    secret_key = os.environ.get("MINIO_ROOT_PASSWORD", "")
    secure = os.environ.get("MINIO_SECURE", "false").lower() == "true"

    print(f"[TEST 3/5] MinIO Storage ({endpoint})...")
    try:
        from minio import Minio
        client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        buckets = client.list_buckets()
        bucket_names = [b.name for b in buckets]
        print_status("MinIO", True, f"Listed {len(bucket_names)} buckets (e.g. {bucket_names[:3]})")
        return True
    except Exception as exc:
        print_status("MinIO", False, f"MinIO error: {exc}")
        return False


def test_ocr_service() -> bool:
    ocr_url = os.environ.get(
        "OCR_SERVICE_URL",
        "https://8085--main--dev--sinhnq3.coder.vts-ai.space"
    ).rstrip("/")
    check_url = f"{ocr_url}/docs"
    print(f"[TEST 4/5] OCR Service ({check_url})...")
    try:
        import requests
        resp = requests.get(check_url, timeout=10)
        if resp.status_code in (200, 404, 405):
            print_status("OCR Service", True, f"HTTP {resp.status_code} reachable")
            return True
        else:
            print_status("OCR Service", False, f"Unexpected HTTP status {resp.status_code}")
            return False
    except Exception as exc:
        print_status("OCR Service", False, f"HTTP connection error: {exc}")
        return False


def test_llm_gateway() -> bool:
    base_url = os.environ.get("LLM_GATEWAY_URL", "http://localhost:4000/v1").rstrip("/")
    api_key = os.environ.get("LLM_GATEWAY_API_KEY", "")
    model_name = os.environ.get("LLM_MODEL_NAME", "google/gemma-4-26B-A4B-it")
    check_url = f"{base_url}/models"
    print(f"[TEST 5/5] LLM Gateway ({check_url} | model: {model_name})...")
    try:
        import requests
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.get(check_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("id") for m in data.get("data", [])] if isinstance(data, dict) else []
            print_status("LLM Gateway", True, f"HTTP 200 (Available models: {models})")
            return True
        else:
            print_status("LLM Gateway", False, f"HTTP status {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as exc:
        print_status("LLM Gateway", False, f"Connection error: {exc}")
        return False


def run_diagnostics(max_retries: int = 1, retry_delay: int = 3) -> bool:
    for attempt in range(1, max_retries + 1):
        print(f"\n========================================================================")
        print(f"  Complaint-EC Connection Diagnostics (Attempt {attempt}/{max_retries})")
        print(f"========================================================================")
        results = {
            "RabbitMQ": test_rabbitmq(),
            "Redis": test_redis(),
            "MinIO": test_minio(),
            "OCR Service": test_ocr_service(),
            "LLM Gateway": test_llm_gateway(),
        }

        all_ok = all(results.values())
        if all_ok:
            print("========================================================================")
            print("  [ALL PASSED] All 5 dependencies are connected and operational! ")
            print("========================================================================\n")
            return True

        failed_services = [k for k, v in results.items() if not v]
        print("------------------------------------------------------------------------")
        print(f"  [ATTENTION] Failed checks: {', '.join(failed_services)}")
        if attempt < max_retries:
            print(f"  Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)

    return False


if __name__ == "__main__":
    retries = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1
    success = run_diagnostics(max_retries=retries)
    sys.exit(0 if success else 1)
