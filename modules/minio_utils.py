import os
import json
import logging
import mimetypes
from io import BytesIO
from minio import Minio

logger = logging.getLogger(__name__)

# MinIO Configuration loaded solely from .env
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ROOT_USER", "")
MINIO_SECRET_KEY = os.environ.get("MINIO_ROOT_PASSWORD", "")
MINIO_BUCKET_NAME = os.environ.get("MINIO_BUCKET_NAME", "x2-dannguyen-uc1-logs")
MINIO_CACHE_BUCKET = os.environ.get("MINIO_CACHE_BUCKET", "x2-dannguyen-uc1-cache")
MINIO_KB_BUCKET = os.environ.get("MINIO_KB_BUCKET", "x2-dannguyen-uc1-config")
KNOWLEDGE_BASE_JSON_FILENAME = "knowledge_base.json"
MINIO_KB_OBJECT = os.environ.get("MINIO_KB_OBJECT", KNOWLEDGE_BASE_JSON_FILENAME)
MINIO_SPELLING_BUCKET = os.environ.get("MINIO_SPELLING_BUCKET", "x2-dannguyen-uc1-config")
SPELLING_ERROR_JSON_FILENAME = "spelling_error.json"
MINIO_SPELLING_OBJECT = os.environ.get("MINIO_SPELLING_OBJECT", SPELLING_ERROR_JSON_FILENAME)
MINIO_SECURE = os.environ.get("MINIO_SECURE", "false").lower() == "true"
JSON_UTF8_CONTENT_TYPE = "application/json; charset=utf-8"

import urllib3
from minio import Minio

_minio_client = None

def get_minio_client() -> Minio:
    global _minio_client
    if _minio_client is None:
        http_client = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=3.0, read=30.0),
            retries=urllib3.Retry(total=2, backoff_factor=0.2),
            maxsize=20
        )
        _minio_client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=MINIO_SECURE,
            http_client=http_client
        )
        ensure_bucket_exists(_minio_client, MINIO_BUCKET_NAME)
    return _minio_client

def ensure_bucket_exists(client: Minio, bucket_name: str):
    """Đảm bảo bucket tồn tại và thiết lập policy Public Read nếu có quyền"""
    try:
        if not client.bucket_exists(bucket_name):
            client.make_bucket(bucket_name)
    except Exception as exc:
        # Nếu đã tồn tại hoặc không có quyền tạo bucket thì ghi log debug
        logger.debug("Bucket '%s' check/creation bypassed: %s", bucket_name, exc)

    # Trên cụm shared MinIO, tài khoản client/user thông thường không có quyền set_bucket_policy (chỉ admin có quyền)
    # Thử thiết lập policy một lần, nếu bị AccessDenied thì bỏ qua không in warning
    try:
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{bucket_name}/*"]
                }
            ]
        }
        client.set_bucket_policy(bucket_name, json.dumps(policy))
    except Exception as exc:
        logger.debug("Set bucket policy bypassed for '%s': %s", bucket_name, exc)

def normalize_minio_key(path: str) -> str:
    """Chuẩn hóa đường dẫn file thành object key tương đối trong MinIO bucket logs"""
    path = str(path).replace("\\", "/")
    if f"/{MINIO_BUCKET_NAME}/" in path:
        key = path.split(f"/{MINIO_BUCKET_NAME}/", 1)[1]
    elif path.startswith(f"{MINIO_BUCKET_NAME}/"):
        key = path[len(f"{MINIO_BUCKET_NAME}/"):]
    elif "/logs/" in path:
        key = path.split("/logs/", 1)[1]
    elif path.startswith("logs/"):
        key = path[5:]
    else:
        key = path
    return key.lstrip("/")

def get_content_type(key: str, default: str = "text/plain") -> str:
    """Xác định Content-Type chuẩn cho từng định dạng file"""
    low_key = key.lower()
    if low_key.endswith(".md"):
        return "text/markdown; charset=utf-8"
    elif low_key.endswith(".txt"):
        return "text/plain; charset=utf-8"
    elif low_key.endswith(".json"):
        return JSON_UTF8_CONTENT_TYPE
    elif low_key.endswith(".csv"):
        return "text/csv; charset=utf-8"
    elif low_key.endswith(".pdf"):
        return "application/pdf"
    
    content_type, _ = mimetypes.guess_type(key)
    if content_type:
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        return content_type
    return default

from concurrent.futures import ThreadPoolExecutor

_minio_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="minio_async_log")


def sync_write_to_minio(path: str, data: str | bytes, content_type: str | None = None):
    """Thực thi ghi dữ liệu đồng bộ vào MinIO bucket 'logs'"""
    if not path:
        return
    try:
        client = get_minio_client()
        key = normalize_minio_key(path)

        if isinstance(data, str):
            data_bytes = data.encode('utf-8')
        else:
            data_bytes = data

        if not content_type:
            content_type = get_content_type(key)

        stream = BytesIO(data_bytes)
        client.put_object(
            bucket_name=MINIO_BUCKET_NAME,
            object_name=key,
            data=stream,
            length=len(data_bytes),
            content_type=content_type,
            metadata={"Content-Disposition": "inline"}
        )
        print(f"[MinIO Log] Đã ghi object '{key}' vào bucket '{MINIO_BUCKET_NAME}' (Content-Type: {content_type})")
    except Exception as e:
        print(f"[MinIO Error] Lỗi ghi object '{path}': {e}")


def write_to_minio(path: str, data: str | bytes, content_type: str | None = None):
    """Ghi dữ liệu bất đồng bộ ở background thread (không làm chặn main processing thread)"""
    if not path:
        return
    _minio_executor.submit(sync_write_to_minio, path, data, content_type)


def sync_write_json_to_minio(path: str, content: dict | list):
    """Thực thi ghi dữ liệu JSON đồng bộ vào MinIO"""
    json_str = json.dumps(content, ensure_ascii=False, indent=4)
    sync_write_to_minio(path, json_str, content_type=JSON_UTF8_CONTENT_TYPE)


def write_json_to_minio(path: str, content: dict | list):
    """Ghi JSON bất đồng bộ vào MinIO ở background thread"""
    if not path:
        return
    _minio_executor.submit(sync_write_json_to_minio, path, content)


def sync_append_txt_to_minio(path: str, text_to_append: str):
    """Thực thi nối chuỗi text vào object hiện có trên MinIO đồng bộ"""
    if not path:
        return
    try:
        client = get_minio_client()
        key = normalize_minio_key(path)

        existing_text = ""
        try:
            response = client.get_object(MINIO_BUCKET_NAME, key)
            existing_text = response.read().decode('utf-8')
            response.close()
            response.release_conn()
        except Exception:
            existing_text = ""

        new_text = existing_text + text_to_append
        sync_write_to_minio(path, new_text, content_type="text/plain; charset=utf-8")
    except Exception as e:
        print(f"[MinIO Error] Lỗi append txt vào '{path}': {e}")


def append_txt_to_minio(path: str, text_to_append: str):
    """Nối text bất đồng bộ vào MinIO ở background thread"""
    if not path:
        return
    _minio_executor.submit(sync_append_txt_to_minio, path, text_to_append)


def sync_append_json_to_minio(path: str, new_record: dict | list):
    """Thực thi nối bản ghi vào mảng JSON trên MinIO đồng bộ"""
    if not path:
        return
    try:
        client = get_minio_client()
        key = normalize_minio_key(path)

        data = []
        try:
            response = client.get_object(MINIO_BUCKET_NAME, key)
            raw_content = response.read().decode('utf-8')
            response.close()
            response.release_conn()
            if raw_content.strip():
                parsed = json.loads(raw_content)
                if isinstance(parsed, list):
                    data = parsed
        except Exception:
            data = []

        data.append(new_record)
        sync_write_json_to_minio(path, data)
    except Exception as e:
        print(f"[MinIO Error] Lỗi append json vào '{path}': {e}")


def append_json_to_minio(path: str, new_record: dict | list):
    """Nối bản ghi JSON bất đồng bộ vào MinIO ở background thread"""
    if not path:
        return
    _minio_executor.submit(sync_append_json_to_minio, path, new_record)


def sync_get_json_from_minio(object_name: str, bucket_name: str = MINIO_CACHE_BUCKET):
    """Lấy dữ liệu JSON từ MinIO bucket (mặc định là MINIO_CACHE_BUCKET). Trả về dict/list nếu có, None nếu không có."""
    try:
        client = get_minio_client()
        ensure_bucket_exists(client, bucket_name)
        key = object_name.lstrip("/")
        response = client.get_object(bucket_name, key)
        raw_data = response.read().decode('utf-8')
        response.close()
        response.release_conn()
        return json.loads(raw_data)
    except Exception:
        return None


def sync_write_json_to_minio_bucket(object_name: str, content: dict | list, bucket_name: str = MINIO_CACHE_BUCKET):
    """Ghi trực tiếp dữ liệu JSON vào MinIO bucket chỉ định (mặc định là MINIO_CACHE_BUCKET)."""
    try:
        client = get_minio_client()
        ensure_bucket_exists(client, bucket_name)
        key = object_name.lstrip("/")
        json_bytes = json.dumps(content, ensure_ascii=False, indent=4).encode('utf-8')
        stream = BytesIO(json_bytes)
        client.put_object(
            bucket_name=bucket_name,
            object_name=key,
            data=stream,
            length=len(json_bytes),
            content_type=JSON_UTF8_CONTENT_TYPE,
            metadata={"Content-Disposition": "inline"}
        )
        print(f"[MinIO Cache] Đã lưu cache '{key}' vào bucket '{bucket_name}'")
    except Exception as e:
        print(f"[MinIO Error] Lỗi ghi cache vào bucket '{bucket_name}' object '{object_name}': {e}")


def sync_get_text_from_minio(object_name: str, bucket_name: str = MINIO_CACHE_BUCKET) -> tuple[str | None, dict]:
    """
    Lấy dữ liệu text/markdown và metadata từ MinIO bucket (mặc định là MINIO_CACHE_BUCKET).
    Trả về tuple: (raw_content: str, metadata: dict). Nếu không tồn tại hoặc lỗi, trả về (None, {}).
    """
    try:
        client = get_minio_client()
        ensure_bucket_exists(client, bucket_name)
        key = object_name.lstrip("/")
        response = client.get_object(bucket_name, key)
        raw_data = response.read().decode('utf-8')
        metadata = {}
        if hasattr(response, "headers"):
            for k, v in response.headers.items():
                if k.lower().startswith("x-amz-meta-"):
                    meta_key = k.lower().replace("x-amz-meta-", "").replace("-", "_")
                    metadata[meta_key] = v
        response.close()
        response.release_conn()
        return raw_data, metadata
    except Exception:
        return None, {}


def sync_write_text_to_minio_bucket(
    object_name: str,
    content: str,
    bucket_name: str = MINIO_CACHE_BUCKET,
    content_type: str = "text/markdown; charset=utf-8",
    metadata: dict | None = None,
):
    """
    Ghi trực tiếp dữ liệu text/markdown vào MinIO bucket chỉ định (mặc định là MINIO_CACHE_BUCKET).
    """
    try:
        client = get_minio_client()
        ensure_bucket_exists(client, bucket_name)
        key = object_name.lstrip("/")
        data_bytes = content.encode('utf-8')
        stream = BytesIO(data_bytes)
        meta = {"Content-Disposition": "inline"}
        if metadata:
            for k, v in metadata.items():
                meta[str(k).replace("_", "-")] = str(v)
        client.put_object(
            bucket_name=bucket_name,
            object_name=key,
            data=stream,
            length=len(data_bytes),
            content_type=content_type,
            metadata=meta,
        )
        print(f"[MinIO OCR Cache] Đã lưu cache '{key}' vào bucket '{bucket_name}'")
    except Exception as e:
        print(f"[MinIO Error] Lỗi ghi text vào bucket '{bucket_name}' object '{object_name}': {e}")


def upload_knowledge_base_to_minio(
    local_path: str | None = None,
    bucket_name: str = MINIO_KB_BUCKET,
    object_name: str = MINIO_KB_OBJECT,
    overwrite: bool = False,
) -> bool:
    """Tải file knowledge_base.json cục bộ lên MinIO bucket 'config' nếu chưa tồn tại (hoặc khi overwrite=True)."""
    try:
        client = get_minio_client()
        ensure_bucket_exists(client, bucket_name)

        if not overwrite:
            try:
                client.stat_object(bucket_name, object_name)
                print(f"[MinIO] '{object_name}' đã tồn tại trên bucket '{bucket_name}', giữ nguyên dữ liệu từ MinIO.")
                return True
            except Exception as exc:
                logger.debug("Object '%s' chưa tồn tại trên bucket '%s': %s", object_name, bucket_name, exc)

        if not local_path:
            possible_paths = [
                os.path.abspath(os.path.join(os.path.dirname(__file__), KNOWLEDGE_BASE_JSON_FILENAME)),
                f"modules/{KNOWLEDGE_BASE_JSON_FILENAME}",
                KNOWLEDGE_BASE_JSON_FILENAME,
            ]
            for p in possible_paths:
                if os.path.exists(p):
                    local_path = p
                    break
        if not local_path or not os.path.exists(local_path):
            print("[MinIO Warning] Không tìm thấy file knowledge_base.json cục bộ để upload.")
            return False

        with open(local_path, "r", encoding="utf-8") as f:
            content = f.read()

        data_bytes = content.encode("utf-8")
        stream = BytesIO(data_bytes)
        client.put_object(
            bucket_name=bucket_name,
            object_name=object_name,
            data=stream,
            length=len(data_bytes),
            content_type=JSON_UTF8_CONTENT_TYPE,
            metadata={"Content-Disposition": "inline"},
        )
        print(f"[MinIO] Đã upload knowledge base từ '{local_path}' lên bucket '{bucket_name}/{object_name}'")
        return True
    except Exception as e:
        print(f"[MinIO Error] Lỗi upload knowledge base lên MinIO: {e}")
        return False


def sync_get_knowledge_base_from_minio(
    bucket_name: str = MINIO_KB_BUCKET,
    object_name: str = MINIO_KB_OBJECT,
) -> str | None:
    """Lấy nội dung chuỗi JSON của knowledge_base.json từ MinIO bucket 'config'."""
    try:
        client = get_minio_client()
        ensure_bucket_exists(client, bucket_name)
        response = client.get_object(bucket_name, object_name)
        data = response.read().decode("utf-8")
        response.close()
        response.release_conn()
        return data.strip() if data else None
    except Exception as e:
        print(f"[MinIO Warning] Không thể đọc '{object_name}' từ bucket '{bucket_name}': {e}")
        return None


def upload_spelling_error_to_minio(
    local_path: str | None = None,
    bucket_name: str = MINIO_SPELLING_BUCKET,
    object_name: str = MINIO_SPELLING_OBJECT,
    overwrite: bool = False,
) -> bool:
    """Tải file spelling_error.json cục bộ lên MinIO bucket 'config' nếu chưa tồn tại (hoặc khi overwrite=True)."""
    try:
        client = get_minio_client()
        ensure_bucket_exists(client, bucket_name)

        if not overwrite:
            try:
                client.stat_object(bucket_name, object_name)
                print(f"[MinIO] '{object_name}' đã tồn tại trên bucket '{bucket_name}', giữ nguyên dữ liệu từ MinIO.")
                return True
            except Exception as exc:
                logger.debug("Object '%s' chưa tồn tại trên bucket '%s': %s", object_name, bucket_name, exc)

        if not local_path:
            possible_paths = [
                os.path.abspath(os.path.join(os.path.dirname(__file__), SPELLING_ERROR_JSON_FILENAME)),
                f"modules/{SPELLING_ERROR_JSON_FILENAME}",
                SPELLING_ERROR_JSON_FILENAME,
            ]
            for p in possible_paths:
                if os.path.exists(p):
                    local_path = p
                    break
        if not local_path or not os.path.exists(local_path):
            print("[MinIO Warning] Không tìm thấy file spelling_error.json cục bộ để upload.")
            return False

        with open(local_path, "r", encoding="utf-8") as f:
            content = f.read()

        data_bytes = content.encode("utf-8")
        stream = BytesIO(data_bytes)
        client.put_object(
            bucket_name=bucket_name,
            object_name=object_name,
            data=stream,
            length=len(data_bytes),
            content_type=JSON_UTF8_CONTENT_TYPE,
            metadata={"Content-Disposition": "inline"},
        )
        print(f"[MinIO] Đã upload spelling error dictionary từ '{local_path}' lên bucket '{bucket_name}/{object_name}'")
        return True
    except Exception as e:
        print(f"[MinIO Error] Lỗi upload spelling error dictionary lên MinIO: {e}")
        return False


def sync_get_spelling_error_from_minio(
    bucket_name: str = MINIO_SPELLING_BUCKET,
    object_name: str = MINIO_SPELLING_OBJECT,
) -> str | None:
    """Lấy nội dung chuỗi JSON của spelling_error.json từ MinIO bucket 'config'."""
    try:
        client = get_minio_client()
        ensure_bucket_exists(client, bucket_name)
        response = client.get_object(bucket_name, object_name)
        data = response.read().decode("utf-8")
        response.close()
        response.release_conn()
        return data.strip() if data else None
    except Exception as e:
        print(f"[MinIO Warning] Không thể đọc '{object_name}' từ bucket '{bucket_name}': {e}")
        return None




