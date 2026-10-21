import os
import time
from typing import Tuple, List, Optional, Any

_last_health_check_time: float = 0.0
_last_health_status: Tuple[bool, str] = (True, "Initialized")
HEALTH_CACHE_TTL: float = float(os.environ.get("CELERY_HEALTH_CACHE_TTL", "5.0"))  # 5 seconds default TTL


def _check_broker(celery_app: Any, timeout: float) -> Tuple[bool, Optional[str]]:
    """Kiểm tra kết nối tới Celery Broker (RabbitMQ)."""
    try:
        with celery_app.connection_for_read() as conn:
            conn.ensure_connection(max_retries=1, timeout=timeout)
        return True, None
    except Exception as exc:
        return False, f"Celery Broker (RabbitMQ) không phản hồi hoặc mất kết nối: {exc}"


def _check_backend(celery_app: Any) -> Tuple[bool, Optional[str]]:
    """Kiểm tra kết nối tới Celery Result Backend (Redis)."""
    try:
        res = celery_app.backend.client.ping()
        if not res:
            return False, "Celery Result Backend (Redis) trả về phản hồi không hợp lệ"
        return True, None
    except Exception as exc:
        return False, f"Celery Result Backend (Redis) không phản hồi hoặc mất kết nối: {exc}"


def _filter_active_pipeline_workers(pings: List[Any]) -> List[str]:
    """Lọc danh sách các worker đang phản hồi thuộc hàng đợi pipeline."""
    active = []
    for p in pings:
        if not isinstance(p, dict):
            continue
        for w_name, resp in p.items():
            if isinstance(resp, dict) and resp.get("ok") == "pong" and "x2.dannguyen.uc1.worker" in w_name:
                active.append(w_name)
    return active


def _check_workers(celery_app: Any, timeout: float) -> Tuple[bool, Optional[str], int]:
    """Kiểm tra các worker đang lắng nghe hàng đợi Celery."""
    try:
        pings = celery_app.control.ping(timeout=timeout)
        if not pings:
            return False, "Không có Celery worker nào phản hồi ping (timeout hoặc tất cả worker bị dừng)", 0

        active_workers = _filter_active_pipeline_workers(pings)
        if not active_workers:
            return False, "Không có Celery worker nào thuộc hàng đợi pipeline (x2.dannguyen.uc1.worker*_queue) phản hồi", 0

        return True, None, len(active_workers)
    except Exception as exc:
        return False, f"Lỗi khi kiểm tra trạng thái Celery workers: {exc}", 0


def check_celery_and_queue_health(
    timeout: float = 0.5,
    force_check: bool = False,
) -> Tuple[bool, str]:
    """
    Kiểm tra trạng thái sẵn sàng của Celery Broker, Result Backend và các Worker Queues.
    Sử dụng bộ nhớ cache ngắn hạn (mặc định 5s) để tránh overhead kiểm tra liên tục giữa các request.
    """
    global _last_health_check_time, _last_health_status

    now = time.time()
    if not force_check and (now - _last_health_check_time) < HEALTH_CACHE_TTL:
        return _last_health_status

    try:
        from tasks.celery_app import celery_app
    except Exception as exc:
        _last_health_status = (False, f"Không thể nạp Celery application: {exc}")
        _last_health_check_time = now
        return _last_health_status

    # 1. Kiểm tra Broker
    broker_ok, broker_err = _check_broker(celery_app, timeout)
    if not broker_ok:
        _last_health_status = (False, broker_err or "")
        _last_health_check_time = now
        return _last_health_status

    # 2. Kiểm tra Backend
    backend_ok, backend_err = _check_backend(celery_app)
    if not backend_ok:
        _last_health_status = (False, backend_err or "")
        _last_health_check_time = now
        return _last_health_status

    # 3. Kiểm tra Workers
    workers_ok, worker_err, worker_count = _check_workers(celery_app, timeout)
    if not workers_ok:
        _last_health_status = (False, worker_err or "")
        _last_health_check_time = now
        return _last_health_status

    _last_health_status = (True, f"Celery và {worker_count} pipeline workers đang hoạt động bình thường")
    _last_health_check_time = now
    return _last_health_status
