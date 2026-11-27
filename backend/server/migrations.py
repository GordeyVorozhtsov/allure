"""Раннер SQL-миграций Allure-стенда.

Миграции лежат отдельными нумерованными файлами в ``server/migrations/`` и
разделяются символом ``;``. Применённые файлы записываются в таблицу
``schema_migrations`` (имя файла + время), поэтому повторный ``init()`` не
применяет уже сделанное — идемпотентно.

Раннер устойчив к тому, что применяемая колонка/объект уже существует в БД со
старой схемой (например ``ALTER TABLE ADD COLUMN`` по колонке, которая была
создана ещё до перехода на файлы-миграции): такая миграция помечается сделанной
и пропускается впредь.
"""

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# SQLite: «duplicate column name: x» — колонка уже есть в таблице.
_DUPLICATE_COLUMN_RE = re.compile(r"duplicate column name", re.IGNORECASE)


def _migration_files() -> list[Path]:
    """Файлы ```NNNN_имя.sql``` по порядку номера."""
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    return files


def _applied(conn) -> set[str]:
    """Имена уже применённых миграций."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    return {row["name"] for row in conn.execute("SELECT name FROM schema_migrations").fetchall()}


def apply(conn, now: callable) -> list[str]:
    """Применить неприменённые миграции. Возвращает имена новых файлов."""
    applied = _applied(conn)
    new_names = []
    for path in _migration_files():
        name = path.name
        if name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
        except Exception as exc:  # noqa: BLE001
            # Колонка уже существует в старой dev-БД — считаем миграцию сделанной.
            if not _DUPLICATE_COLUMN_RE.search(str(exc)):
                raise
        conn.execute(
            "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            (name, now()),
        )
        new_names.append(name)
    return new_names