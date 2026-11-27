import hashlib
import hmac
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from server import migrations, settings

_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path():
    return settings.DATA_DIR / "shelf.db"


@contextmanager
def connect():
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(db_path())
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init() -> list[tuple[str, str]]:
    with connect() as conn:
        # Схема и последующие миграции — отдельными файлами в server/migrations/.
        migrations.apply(conn, now)
        stuck = conn.execute(
            """
            SELECT runs.id AS id, projects.slug AS slug
            FROM runs JOIN projects ON projects.id = runs.project_id
            WHERE runs.status = 'building'
            """
        ).fetchall()
        if stuck:
            conn.execute(
                "UPDATE runs SET status = 'failed', error = 'сборка прервана' WHERE status = 'building'"
            )
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if count == 0:
            from server.auth import hash_password

            conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)",
                (
                    settings.ADMIN_USERNAME,
                    hash_password(settings.ADMIN_PASSWORD),
                    "admin",
                    now(),
                ),
            )
    return [(row["slug"], row["id"]) for row in stuck]


def user_by_name(username: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def user_by_id(user_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users():
    with connect() as conn:
        return conn.execute("SELECT id, username, role, created_at FROM users ORDER BY username").fetchall()


def create_user(username: str, password_hash: str, role: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)",
            (username, password_hash, role, now()),
        )


def delete_user(user_id: int) -> None:
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise ValueError("пользователь не найден")
        if user["role"] == "admin":
            admins = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"]
            if admins <= 1:
                raise ValueError("нельзя удалить единственного администратора")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def list_projects(project_id: int | None = None):
    query = """
        SELECT p.*,
               r.id AS run_id,
               r.passed AS passed,
               r.failed AS failed,
               r.broken AS broken,
               r.skipped AS skipped,
               r.unknown AS unknown,
               r.total AS total,
               r.build_order AS build_order,
               r.created_at AS run_created,
               r.executor_name AS executor_name,
               r.status AS run_status
        FROM projects p
        LEFT JOIN runs r ON r.id = (
            SELECT id FROM runs
            WHERE project_id = p.id AND status = 'ready'
            ORDER BY build_order DESC LIMIT 1
        )
    """
    args: tuple = ()
    if project_id is not None:
        query += " WHERE p.id = ?"
        args = (project_id,)
    query += " ORDER BY p.name"
    with connect() as conn:
        return conn.execute(query, args).fetchall()


def get_project(slug: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()


def slug_taken(slug: str) -> bool:
    return get_project(slug) is not None


def create_project(slug: str, name: str, keep_reports: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects(slug, name, keep_reports, created_at) VALUES (?,?,?,?)",
            (slug, name, keep_reports, now()),
        )


def update_keep(slug: str, keep_reports: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE projects SET keep_reports = ? WHERE slug = ?", (keep_reports, slug))


def rename_project(slug: str, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE projects SET name = ? WHERE slug = ?", (name, slug))


def pass_trend(project_id: int, limit: int = 8) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT passed, total FROM runs
            WHERE project_id = ? AND status = 'ready' AND total > 0
            ORDER BY build_order DESC LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    return [round(100 * row["passed"] / row["total"]) for row in reversed(rows)]


def delete_project(slug: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM projects WHERE slug = ?", (slug,))
        return cur.rowcount > 0


def latest_ready(project_id: int):
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM runs
            WHERE project_id = ? AND status = 'ready'
            ORDER BY build_order DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()


def begin_run(project_id: int, run_id: str, executor_name: str, executor_url: str, executor_type: str, branch: str = "") -> int:
    with connect() as conn:
        order = conn.execute(
            "SELECT COALESCE(MAX(build_order), 0) + 1 AS n FROM runs WHERE project_id = ?",
            (project_id,),
        ).fetchone()["n"]
        conn.execute(
            """
            INSERT INTO runs(
                id, project_id, build_order, created_at, status,
                executor_name, executor_url, executor_type, branch
            ) VALUES (?,?,?,?, 'building', ?,?,?,?)
            """,
            (run_id, project_id, order, now(), executor_name, executor_url, executor_type, branch or None),
        )
        return order


def finish_run(run_id: str, counts: dict) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = 'ready', error = NULL,
                passed = ?, failed = ?, broken = ?, skipped = ?, unknown = ?, total = ?
            WHERE id = ?
            """,
            (
                counts["passed"],
                counts["failed"],
                counts["broken"],
                counts["skipped"],
                counts["unknown"],
                counts["total"],
                run_id,
            ),
        )


def save_parts(run_id: str, parts: list[dict]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM run_parts WHERE run_id = ?", (run_id,))
        for part in parts:
            conn.execute(
                """
                INSERT INTO run_parts(run_id, source, passed, failed, broken, skipped, unknown, total)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    part["source"],
                    part["passed"],
                    part["failed"],
                    part["broken"],
                    part["skipped"],
                    part["unknown"],
                    part["total"],
                ),
            )


def parts_for_project(project_id: int) -> dict[str, list]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT run_parts.*
            FROM run_parts
            JOIN runs ON runs.id = run_parts.run_id
            WHERE runs.project_id = ?
            ORDER BY run_parts.source
            """,
            (project_id,),
        ).fetchall()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["run_id"], []).append(row)
    return grouped


def fail_run(run_id: str, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET status = 'failed', error = ? WHERE id = ?",
            (error[:1500], run_id),
        )


def list_runs(project_id: int):
    with connect() as conn:
        return conn.execute(
            """
            SELECT runs.*,
                   CASE WHEN shares.expires_at IS NULL OR shares.expires_at >= ? THEN shares.token ELSE NULL END AS share_token
            FROM runs
            LEFT JOIN shares ON shares.run_id = runs.id
            WHERE runs.project_id = ?
            ORDER BY runs.build_order DESC
            """,
            (now(), project_id,),
        ).fetchall()


def get_run(run_id: str):
    with connect() as conn:
        return conn.execute(
            """
            SELECT runs.*, projects.slug AS slug, projects.name AS project_name
            FROM runs JOIN projects ON projects.id = runs.project_id
            WHERE runs.id = ?
            """,
            (run_id,),
        ).fetchone()


def delete_run(run_id: str):
    with connect() as conn:
        row = conn.execute(
            """
            SELECT projects.slug AS slug
            FROM runs JOIN projects ON projects.id = runs.project_id
            WHERE runs.id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return row["slug"]


def prune(project_id: int, keep: int) -> list[tuple[str, str]]:
    with connect() as conn:
        project = conn.execute("SELECT slug FROM projects WHERE id = ?", (project_id,)).fetchone()
        rows = conn.execute(
            "SELECT id FROM runs WHERE project_id = ? ORDER BY build_order DESC",
            (project_id,),
        ).fetchall()
        extra = [row["id"] for row in rows[keep:]]
        for run_id in extra:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return [(project["slug"], run_id) for run_id in extra]


def create_token(project_id: int, name: str, token_hash: str, token_prefix: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tokens(project_id, name, token_hash, token_prefix, created_at)
            VALUES (?,?,?,?,?)
            """,
            (project_id, name, token_hash, token_prefix, now()),
        )


def list_tokens(project_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT id, name, token_prefix, created_at FROM tokens WHERE project_id = ? ORDER BY id DESC",
            (project_id,),
        ).fetchall()


def delete_token(token_id: int, project_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM tokens WHERE id = ? AND project_id = ?", (token_id, project_id))


def token_by_hash(token_hash: str):
    with connect() as conn:
        return conn.execute(
            """
            SELECT tokens.name AS name, tokens.project_id AS project_id, projects.slug AS slug
            FROM tokens JOIN projects ON projects.id = tokens.project_id
            WHERE tokens.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()


def set_share(run_id: str, ttl_hours: int = 0) -> str:
    token = secrets.token_urlsafe(18)
    expires_at = None
    if ttl_hours and ttl_hours > 0:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        conn.execute("DELETE FROM shares WHERE run_id = ?", (run_id,))
        conn.execute(
            "INSERT INTO shares(token, run_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, run_id, now(), expires_at),
        )
    return token


def share_for_run(run_id: str):
    with connect() as conn:
        return conn.execute("SELECT * FROM shares WHERE run_id = ?", (run_id,)).fetchone()


def delete_share(run_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM shares WHERE run_id = ?", (run_id,))


def share_by_token(token: str):
    with connect() as conn:
        return conn.execute(
            """
            SELECT shares.token AS token, shares.expires_at AS expires_at,
                   runs.id AS run_id, projects.slug AS slug, runs.status AS status
            FROM shares
            JOIN runs ON runs.id = shares.run_id
            JOIN projects ON projects.id = runs.project_id
            WHERE shares.token = ?
            """,
            (token,),
        ).fetchone()


def delete_expired_shares() -> None:
    """Удаляет гостевые ссылки, у которых срок действия уже наступил."""
    with connect() as conn:
        conn.execute("DELETE FROM shares WHERE expires_at IS NOT NULL AND expires_at < ?", (now(),))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str, str]:
    plain = "pat_" + secrets.token_urlsafe(24)
    return plain, hash_token(plain), plain[:12]
