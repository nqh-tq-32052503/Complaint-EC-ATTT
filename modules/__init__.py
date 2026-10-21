import os
from pathlib import Path

import logging

# Đảm bảo biến môi trường từ .env luôn được nạp trước khi bất kỳ module nào khởi tạo
try:
    from config.env_loader import load_project_env
    load_project_env()
except Exception as exc:
    logging.getLogger(__name__).debug("Không nạp được cấu hình môi trường từ config.env_loader: %s", exc)
