"""
Configuration package for Complaint-EC project.
Tự động nạp biến môi trường từ file .env cho toàn bộ hệ thống.
"""
from config.env_loader import load_project_env

load_project_env()
