import pytest
import sqlite3

from server import db
from server.auth import hash_password


class TestInit:
    def test_creates_admin(self, data_dir):
        db.init()
        users = db.list_users()
        assert len(users) == 1
        assert users[0]["username"] == "admin"
        assert users[0]["role"] == "admin"

    def test_idempotent(self, data_dir):
        db.init()
        db.init()
        users = db.list_users()
        assert len(users) == 1  # не создаёт второго админа

    def test_marks_stuck_runs(self, data_dir):
        db.init()
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        order = db.begin_run(project["id"], "run-1", "ci", "", "")
        # повторный init должен пометить 'building' -> 'failed'
        stuck = db.init()
        assert any(slug == "proj" for slug, _ in stuck)
        run = db.get_run("run-1")
        assert run["status"] == "failed"
        assert "прервана" in (run["error"] or "")


class TestUsers:
    def test_crud(self, data_dir, db_init):
        db.create_user("testuser", hash_password("secret123"), "guest")
        user = db.user_by_name("testuser")
        assert user is not None
        assert user["role"] == "guest"

        by_id = db.user_by_id(user["id"])
        assert by_id["username"] == "testuser"

        db.delete_user(user["id"])
        assert db.user_by_name("testuser") is None

    def test_duplicate_username(self, data_dir, db_init):
        db.create_user("dup", hash_password("secret123"), "guest")
        with pytest.raises(sqlite3.IntegrityError):
            db.create_user("dup", hash_password("other123"), "guest")

    def test_cannot_delete_last_admin(self, data_dir, db_init):
        admin = db.user_by_name("admin")
        with pytest.raises(ValueError):
            db.delete_user(admin["id"])

    def test_can_delete_one_of_two_admins(self, data_dir, db_init):
        db.create_user("admin2", hash_password("secret123"), "admin")
        user = db.user_by_name("admin2")
        db.delete_user(user["id"])  # не падает, остался первый админ


class TestProjects:
    def test_create_and_get(self, data_dir, db_init):
        db.create_project("myproj", "My Project", 5)
        project = db.get_project("myproj")
        assert project is not None
        assert project["name"] == "My Project"
        assert project["keep_reports"] == 5

    def test_slug_taken(self, data_dir, db_init):
        db.create_project("taken", "Taken", 5)
        assert db.slug_taken("taken") is True
        assert db.slug_taken("free") is False

    def test_delete(self, data_dir, db_init):
        db.create_project("temp", "Temp", 5)
        assert db.delete_project("temp") is True
        assert db.delete_project("temp") is False
        assert db.get_project("temp") is None

    def test_update_keep(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        db.update_keep("proj", 15)
        assert db.get_project("proj")["keep_reports"] == 15


class TestTokens:
    def test_create_list_delete(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")

        plain, digest, prefix = db.new_token()
        db.create_token(project["id"], "gitlab", digest, prefix)

        tokens = db.list_tokens(project["id"])
        assert len(tokens) == 1
        assert tokens[0]["name"] == "gitlab"
        assert tokens[0]["token_prefix"] == prefix

        row = db.token_by_hash(digest)
        assert row is not None
        assert row["project_id"] == project["id"]
        assert row["slug"] == "proj"

        db.delete_token(tokens[0]["id"], project["id"])
        assert len(db.list_tokens(project["id"])) == 0

    def test_new_token_format(self, data_dir, db_init):
        plain, digest, prefix = db.new_token()
        assert plain.startswith("pat_")
        assert len(plain) > 20
        assert len(prefix) == 12
        assert plain[:12] == prefix
        assert len(digest) == 64  # sha256 hex

    def test_token_hash_is_one_way(self, data_dir, db_init):
        plain, digest, _ = db.new_token()
        assert digest != plain
        assert db.hash_token(plain) == digest

    def test_incorrect_token_not_found(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        plain, digest, prefix = db.new_token()
        db.create_token(project["id"], "x", digest, prefix)
        assert db.token_by_hash(db.hash_token("wrong-token")) is None


class TestRuns:
    def test_begin_and_finish(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")

        order = db.begin_run(project["id"], "run-123", "ci", "http://x", "gitlab")
        assert order == 1

        run = db.get_run("run-123")
        assert run["status"] == "building"
        assert run["build_order"] == 1
        assert run["slug"] == "proj"

        counts = {"passed": 5, "failed": 1, "broken": 0, "skipped": 2, "unknown": 0, "total": 8}
        db.finish_run("run-123", counts)
        run = db.get_run("run-123")
        assert run["status"] == "ready"
        assert run["passed"] == 5
        assert run["total"] == 8

    def test_build_order_increments(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        assert db.begin_run(project["id"], "run-1", "", "", "") == 1
        assert db.begin_run(project["id"], "run-2", "", "", "") == 2
        assert db.begin_run(project["id"], "run-3", "", "", "") == 3

    def test_fail_run(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        db.begin_run(project["id"], "run-fail", "", "", "")
        db.fail_run("run-fail", "ошибка сборки")
        run = db.get_run("run-fail")
        assert run["status"] == "failed"
        assert run["error"] == "ошибка сборки"

    def test_list_runs_order(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        db.begin_run(project["id"], "run-1", "", "", "")
        db.begin_run(project["id"], "run-2", "", "", "")
        runs = db.list_runs(project["id"])
        assert len(runs) == 2
        assert runs[0]["build_order"] == 2  # DESC порядок

    def test_delete_run(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        db.begin_run(project["id"], "run-del", "", "", "")
        assert db.delete_run("run-del") == "proj"
        assert db.delete_run("run-del") is None
        assert db.get_run("run-del") is None

    def test_save_parts(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        db.begin_run(project["id"], "run-parts", "", "", "")
        parts = [
            {"source": "unit", "passed": 3, "failed": 0, "broken": 0, "skipped": 0, "unknown": 0, "total": 3},
            {"source": "e2e", "passed": 1, "failed": 1, "broken": 0, "skipped": 1, "unknown": 0, "total": 3},
        ]
        db.save_parts("run-parts", parts)
        grouped = db.parts_for_project(project["id"])
        assert len(grouped["run-parts"]) == 2

    def test_latest_ready(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        db.begin_run(project["id"], "run-old", "", "", "")
        db.finish_run("run-old", {"passed": 1, "failed": 0, "broken": 0, "skipped": 0, "unknown": 0, "total": 1})
        db.begin_run(project["id"], "run-new-failed", "", "", "")
        db.fail_run("run-new-failed", "error")

        latest = db.latest_ready(project["id"])
        assert latest["id"] == "run-old"

    def test_prune(self, data_dir, db_init):
        db.create_project("proj", "Proj", 2)
        project = db.get_project("proj")
        for i in range(1, 5):
            db.begin_run(project["id"], f"run-{i}", "", "", "")
        dumped = db.prune(project["id"], keep=2)
        assert len(dumped) == 2
        runs = db.list_runs(project["id"])
        assert len(runs) == 2
        assert {r["id"] for r in runs} == {"run-3", "run-4"}


class TestShares:
    def test_set_and_get(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        db.begin_run(project["id"], "run-share", "", "", "")
        token = db.set_share("run-share")
        assert len(token) >= 16

        row = db.share_by_token(token)
        assert row is not None
        assert row["run_id"] == "run-share"
        assert row["slug"] == "proj"

    def test_delete_share(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        db.begin_run(project["id"], "run-share2", "", "", "")
        token = db.set_share("run-share2")
        db.delete_share("run-share2")
        assert db.share_by_token(token) is None

    def test_share_for_run(self, data_dir, db_init):
        db.create_project("proj", "Proj", 5)
        project = db.get_project("proj")
        db.begin_run(project["id"], "run-share3", "", "", "")
        db.set_share("run-share3")
        row = db.share_for_run("run-share3")
        assert row is not None

    def test_unknown_token(self, data_dir, db_init):
        assert db.share_by_token("nonexistent") is None