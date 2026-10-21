"""
Celery tasks package for Complaint-EC pipeline.
"""
import logging

try:
    from config.env_loader import load_project_env
    load_project_env()
except Exception as exc:
    logging.getLogger(__name__).debug("Không nạp được cấu hình môi trường từ config.env_loader: %s", exc)
