import os
from pathlib import Path
from typing import Optional

_ENV_LOADED = False

def load_project_env(env_path: Optional[str] = None) -> bool:
    """
    Nạp biến môi trường duy nhất từ file .env vào os.environ.
    Hỗ trợ cả trường hợp chưa cài python-dotenv (bằng parser thuần chuẩn Python),
    đảm bảo tất cả code trong project đều chỉ đọc cấu hình từ .env mà không phụ thuộc
    vào bất kỳ giá trị hardcode nào.
    """
    global _ENV_LOADED

    if env_path is None:
        # Tìm file .env tại thư mục gốc của project (Complaint-EC)
        project_root = Path(__file__).resolve().parent.parent
        env_file = project_root / ".env"
    else:
        env_file = Path(env_path)

    if not env_file.exists():
        return False

    # 1. Thử dùng thư viện python-dotenv nếu có
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=env_file, override=False)
    except ImportError:
        pass

    # 2. Đọc trực tiếp từ file để đảm bảo 100% nạp đủ vào os.environ
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("\"'")
                # Nạp vào os.environ nếu chưa có
                if key and key not in os.environ:
                    os.environ[key] = val
        _ENV_LOADED = True
        return True
    except Exception as exc:
        print(f"[CẢNH BÁO] Không thể đọc file .env tại {env_file}: {exc}")
        return False

# Tự động nạp ngay khi module được import
load_project_env()
