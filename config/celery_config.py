import os
from config.env_loader import load_project_env

load_project_env()

# RabbitMQ Broker URL (đọc trực tiếp từ .env)
broker_url = os.environ.get("CELERY_BROKER_URL")

# Redis Result Backend URL (đọc trực tiếp từ .env)
result_backend = os.environ.get("CELERY_RESULT_BACKEND")
redis_retry_on_timeout = True
redis_socket_timeout = 10
redis_socket_connect_timeout = 10
result_backend_transport_options = {
    "global_keyprefix": "x2:dannguyen:uc1:",
    "retry_policy": {
        "timeout": 10.0,
        "max_retries": 5,
        "interval_start": 0.2,
        "interval_step": 0.2,
        "interval_max": 1.0,
    }
}

# Serializer settings
task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
timezone = "Asia/Ho_Chi_Minh"
enable_utc = True

# Worker pool execution model ('threads' for multi-threaded non-blocking I/O execution)
worker_pool = os.environ.get("CELERY_WORKER_POOL", "threads")
worker_concurrency = int(os.environ.get("CELERY_WORKER_CONCURRENCY", "10"))

# Disable prefetching to ensure fair task distribution across workers
worker_prefetch_multiplier = 1
task_acks_late = True

from kombu import Queue

# Priority Queue configuration (0-10, 10 is highest priority)
broker_transport_options = {"max_priority": 10}
task_queue_max_priority = 10
task_default_priority = 5

# Worker dedicated queues routing (requests are assigned to worker queues via Round-Robin)
task_default_queue = "x2.dannguyen.uc1.worker1_queue"
task_routes = None

NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "8"))

task_queues = [
    Queue(f"x2.dannguyen.uc1.worker{i}_queue", max_priority=10) for i in range(1, NUM_WORKERS + 1)
]


