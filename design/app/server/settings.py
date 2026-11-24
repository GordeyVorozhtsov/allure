import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()
SECRET = os.environ.get("SECRET_KEY", "dev-only-secret")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8090"))
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "100")) * 1024 * 1024
MAX_UNPACKED = int(os.environ.get("MAX_UNPACKED_MB", "500")) * 1024 * 1024
KEEP_DEFAULT = int(os.environ.get("KEEP_REPORTS", "20"))
ALLURE_BIN = os.environ.get("ALLURE_BIN", "allure")
ALLURE3_BIN = os.environ.get("ALLURE3_BIN", "allure3")
