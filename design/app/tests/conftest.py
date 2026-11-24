import os
import sys
import tempfile
from pathlib import Path

import pytest

# Добавляем корень проекта в sys.path, чтобы можно было импортировать server.*
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def data_dir(tmp_path):
    """Временный DATA_DIR для изоляции БД и файлов."""
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["SECRET_KEY"] = "test-secret"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "admin"
    os.environ["KEEP_REPORTS"] = "5"
    os.environ["MAX_UPLOAD_MB"] = "10"
    os.environ["MAX_UNPACKED_MB"] = "50"

    from server import settings
    settings.DATA_DIR = tmp_path

    yield tmp_path

    # Очищаем env-переменные после тестов
    for key in ("DATA_DIR", "SECRET_KEY", "ADMIN_USERNAME", "ADMIN_PASSWORD",
                "KEEP_REPORTS", "MAX_UPLOAD_MB", "MAX_UNPACKED_MB"):
        os.environ.pop(key, None)


@pytest.fixture()
def db_init(data_dir):
    """Инициализирует БД (создаёт схему и админа)."""
    from server import db
    db.init()
    return db


@pytest.fixture()
def client(data_dir, db_init):
    """FastAPI TestClient с инициализированной БД."""
    from fastapi.testclient import TestClient
    from server.main import app

    # Перезагружаем main, чтобы pickle session middleware пересоздался с новым DATA_DIR
    import server.main as main
    for mod in list(sys.modules):
        if mod.startswith("server."):
            del sys.modules[mod]

    # Импортируем заново после очистки
    import importlib
    importlib.import_module("server.main")

    with TestClient(main.app) as test_client:
        yield test_client

    # Очищаем rate-limit словарь между тестами
    main._login_attempts.clear()