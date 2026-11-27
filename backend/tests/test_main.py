import io
import json
import zipfile

import pytest

from server import db
from server.auth import hash_password


class TestHealth:
    def test_health_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuthFlows:
    def test_login_page(self, client):
        # Основной вход — Keycloak: GET /login редиректит на Identity Provider.
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 303
        assert "/protocol/openid-connect/auth" in response.headers["location"]
        assert "client_id=" in response.headers["location"]

    def test_login_success(self, client):
        response = client.post(
            "/login",
            data={"username": "admin", "password": "admin"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"

    def test_login_failure(self, client):
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 401

    def test_login_redirects_to_next(self, client):
        response = client.post(
            "/login",
            data={"username": "admin", "password": "admin", "next": "/project/test"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/project/test"

    def test_unauthorized_page_redirects_to_login(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    def test_logout(self, client):
        client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
        response = client.post("/logout", follow_redirects=False)
        assert response.status_code == 303


class TestRateLimiting:
    def test_rate_limited_after_5_attempts(self, client):
        for _ in range(5):
            client.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False)
        # 6-я попытка -> 429
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert response.status_code == 429


def login(client) -> None:
    """Логин как админ и следование за редиректом."""
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)


class TestProjectCRUD:
    def test_create_project_as_admin(self, client):
        login(client)
        response = client.post(
            "/projects", data={"name": "My Project"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert db.get_project("my-project") is not None

    def test_create_project_requires_admin(self, client):
        # Создаём гостя
        login(client)
        client.post(
            "/users",
            data={"username": "guest1", "password": "secret123", "role": "guest"},
            follow_redirects=False,
        )
        client.post("/logout", follow_redirects=False)
        client.post(
            "/login",
            data={"username": "guest1", "password": "secret123"},
            follow_redirects=False,
        )

        response = client.post(
            "/projects", data={"name": "Nope"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert db.get_project("nope") is None

    def test_create_project_invalid_name(self, client):
        login(client)
        # Пустое имя — slugify даёт 'project', валидация должна отклонить
        response = client.post(
            "/projects", data={"name": ""}, follow_redirects=False
        )
        assert response.status_code == 303
        assert db.get_project("project") is None


class TestUserManagement:
    def test_create_user(self, client):
        login(client)
        response = client.post(
            "/users",
            data={"username": "tester", "password": "secret123", "role": "guest"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert db.user_by_name("tester") is not None

    def test_create_user_short_password(self, client):
        login(client)
        response = client.post(
            "/users",
            data={"username": "tester", "password": "123", "role": "guest"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert db.user_by_name("tester") is None

    def test_create_user_duplicate(self, client):
        login(client)
        client.post(
            "/users",
            data={"username": "tester", "password": "secret123", "role": "guest"},
            follow_redirects=False,
        )
        response = client.post(
            "/users",
            data={"username": "tester", "password": "other123", "role": "guest"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    def test_delete_user(self, client):
        login(client)
        client.post(
            "/users",
            data={"username": "todelete", "password": "secret123", "role": "guest"},
            follow_redirects=False,
        )
        user = db.user_by_name("todelete")
        response = client.post(f"/users/{user['id']}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert db.user_by_name("todelete") is None

    def test_cannot_delete_self(self, client):
        login(client)
        admin = db.user_by_name("admin")
        response = client.post(f"/users/{admin['id']}/delete", follow_redirects=False)
        assert response.status_code == 303
        assert db.user_by_name("admin") is not None


class TestTokenFlow:
    def test_issue_token(self, client):
        login(client)
        client.post(
            "/projects", data={"name": "tokproj"}, follow_redirects=False
        )
        response = client.post(
            "/projects/tokproj/tokens", data={"name": "gitlab"}, follow_redirects=False
        )
        assert response.status_code == 303
        tokens = db.list_tokens(db.get_project("tokproj")["id"])
        assert len(tokens) == 1
        assert tokens[0]["name"] == "gitlab"

    def test_revoke_token(self, client):
        login(client)
        client.post(
            "/projects", data={"name": "tokproj2"}, follow_redirects=False
        )
        project = db.get_project("tokproj2")
        client.post(
            "/projects/tokproj2/tokens", data={"name": "gitlab"}, follow_redirects=False
        )
        token = db.list_tokens(project["id"])[0]
        response = client.post(
            f"/projects/tokproj2/tokens/{token['id']}/delete",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert len(db.list_tokens(project["id"])) == 0


class TestBadge:
    def test_badge_no_projects(self, client):
        response = client.get("/projects/none/badge.svg")
        assert response.status_code == 404

    def test_badge_ok(self, client):
        login(client)
        client.post(
            "/projects", data={"name": "badgeproj"}, follow_redirects=False
        )
        response = client.get("/projects/badgeproj/badge.svg")
        assert response.status_code == 200
        assert "пусто" in response.text


class TestApiProject:
    def test_api_project_requires_auth(self, client):
        response = client.get("/api/projects/some")
        assert response.status_code == 401

    def test_api_project_not_found(self, client):
        login(client)
        response = client.get("/api/projects/missing")
        assert response.status_code == 404

    def test_api_project_summary(self, client):
        login(client)
        client.post(
            "/projects", data={"name": "apiproj"}, follow_redirects=False
        )
        response = client.get("/api/projects/apiproj")
        assert response.status_code == 200
        data = response.json()
        assert data["slug"] == "apiproj"
        assert data["runs"] == []
        assert data["latest"] == "/reports/apiproj/latest"


class TestUpload:
    def _make_zip_with_results(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("1-result.json", json.dumps({"uuid": "1", "status": "passed"}))
            zf.writestr("2-result.json", json.dumps({"uuid": "2", "status": "failed"}))
        return buf.getvalue()

    def test_upload_html(self, client):
        login(client)
        client.post(
            "/projects", data={"name": "uploadproj"}, follow_redirects=False
        )
        zip_bytes = self._make_zip_with_results()
        response = client.post(
            "/projects/uploadproj/results",
            data={"response": "html"},
            files={"files": ("results.zip", zip_bytes, "application/zip")},
            follow_redirects=False,
        )
        assert response.status_code == 303

    def test_upload_requires_auth(self, client):
        zip_bytes = self._make_zip_with_results()
        response = client.post(
            "/projects/impossible/results",
            files={"files": ("x.zip", zip_bytes, "application/zip")},
        )
        assert response.status_code == 401

    def test_upload_no_files(self, client):
        login(client)
        client.post(
            "/projects", data={"name": "uploadproj2"}, follow_redirects=False
        )
        response = client.post(
            "/projects/uploadproj2/results",
            data={"response": "html"},
            follow_redirects=False,
        )
        assert response.status_code == 303  # flash error, редирект

    def test_upload_bad_zip(self, client):
        login(client)
        client.post(
            "/projects", data={"name": "uploadproj3"}, follow_redirects=False
        )
        response = client.post(
            "/projects/uploadproj3/results",
            data={"response": "html"},
            files={"files": ("bad.zip", b"not zip", "application/zip")},
            follow_redirects=False,
        )
        assert response.status_code == 303  # flash error


class TestApiPeek:
    def test_peek_requires_auth(self, client):
        response = client.post("/api/peek")
        assert response.status_code == 401

    def test_peek_zip(self, client):
        login(client)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("executor.json", json.dumps({"name": "frontend"}))
        zip_bytes = buf.getvalue()
        response = client.post(
            "/api/peek",
            files={"file": ("results.zip", zip_bytes, "application/zip")},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "frontend"


class TestDesignShareReports:
    """Сквозной сценарий: токен → загрузка → готовый отчёт → гостевая ссылка → Allure 2/3.

    В тестах нет Java/Node, поэтому generate/generate3 подменяются на создание
    минимальных отчётов. Это делает прогон "ready" и позволяет проверить весь
    контур, который жалуется пользователь: токен, share, отчёты, контракт /api/projects.
    """

    @pytest.fixture(autouse=True)
    def _fake_report_builds(self, monkeypatch):
        from server import ingest

        def fake_generate(results, output):
            output.mkdir(parents=True, exist_ok=True)
            (output / "index.html").write_text("<html>allure2</html>")
            (output / "app.js").write_text("//v2")

        def fake_generate3(slug, results, output, name, keep):
            output.mkdir(parents=True, exist_ok=True)
            (output / "index.html").write_text("<html>allure3</html>")
            (output / "app.js").write_text("//v3")

        monkeypatch.setattr(ingest, "generate", fake_generate)
        monkeypatch.setattr(ingest, "generate3", fake_generate3)

    def _zip(self, branch: str | None = None) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("1-result.json", json.dumps({"uuid": "1", "name": "login", "status": "passed"}))
            zf.writestr("2-result.json", json.dumps({"uuid": "2", "name": "reg", "status": "failed"}))
        return buf.getvalue()

    def _make_ready_run(self, client, name: str = "ReadyProj", branch: str = "develop"):
        login(client)
        client.post(f"/projects", data={"name": name}, follow_redirects=False)
        slug = name.lower()
        client.post(
            f"/projects/{slug}/results",
            data={"execution_name": "gitlab", "execution_branch": branch, "execution_type": "gitlab"},
            files={"files": ("results.zip", self._zip(branch), "application/zip")},
        )
        return slug

    def test_ready_run_has_url_and_url3_in_list(self, client):
        slug = self._make_ready_run(client)
        projects = client.get("/api/projects").json()
        project = next((p for p in projects if p["slug"] == slug), None)
        assert project is not None
        run = project["runs"][-1]
        assert run["status"] == "ready"
        assert run["url"] == f"/reports/{slug}/{run['id']}/index.html"
        assert run["url3"] == f"/reports/{slug}/{run['id']}/v3/index.html"

    def test_allure2_report_served(self, client):
        slug = self._make_ready_run(client)
        run = self._last_ready_run(client, slug)
        response = client.get(f"/reports/{slug}/{run['id']}/index.html")
        assert response.status_code == 200
        assert "allure2" in response.text

    def test_allure3_report_served(self, client):
        slug = self._make_ready_run(client)
        run = self._last_ready_run(client, slug)
        response = client.get(f"/reports/{slug}/{run['id']}/v3/index.html")
        assert response.status_code == 200
        assert "allure3" in response.text

    def test_guest_share_opens_report_without_login(self, client):
        slug = self._make_ready_run(client)
        run = self._last_ready_run(client, slug)
        response = client.post(
            f"/projects/{slug}/runs/{run['id']}/share",
            headers={"Accept": "application/json"},
            data={"ttl_hours": "24"},
        )
        assert response.status_code == 200
        share_url = response.json()["url"]
        assert share_url.startswith("/s/")
        assert response.json()["ttl_hours"] == 24

        # Гость (свежая сессия без входа) открывает отчёт через /s/{token}.
        client.post("/logout", follow_redirects=False)
        guest = client.get(share_url, follow_redirects=False)
        assert guest.status_code == 303
        report = client.get(guest.headers["location"])
        assert report.status_code == 200

    def test_share_rejects_non_ready_run(self, client):
        slug = self._make_ready_run(client)
        # Прогон без готового отчёта
        login(client)
        client.post("/projects", data={"name": "BrokenProj"}, follow_redirects=False)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("1-result.json", json.dumps({"uuid": "1", "status": "passed"}))
        broken = buf.getvalue()
        # Подменяем generate, чтобы он бросал исключение и прогон становился discarded/failed.
        from server import ingest
        orig = ingest.generate

        def boom(results, output):
            raise RuntimeError("report build failed")

        import server.main as main
        main.ingest.generate = boom
        try:
            client.post(
                "/projects/brokenproj/results",
                files={"files": ("x.zip", broken, "application/zip")},
            )
            run = db.list_runs(db.get_project("brokenproj")["id"])[0]
            response = client.post(
                f"/projects/brokenproj/runs/{run['id']}/share",
                data={"ttl_hours": "24"},
                follow_redirects=False,
            )
            assert response.status_code == 303  # flash: нет гостевой ссылки
        finally:
            main.ingest.generate = orig

    def _last_ready_run(self, client, slug):
        projects = client.get("/api/projects").json()
        project = next((p for p in projects if p["slug"] == slug), None)
        assert project is not None
        ready = [r for r in project["runs"] if r["status"] == "ready"]
        assert ready, "нет готового прогона"
        return ready[-1]


class TestDesignApi:
    """API-контракт для SPA-фронта (web/index.html): список /api/projects."""

    def _zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("1-result.json", json.dumps({"uuid": "1", "status": "passed"}))
            zf.writestr("2-result.json", json.dumps({"uuid": "2", "status": "failed"}))
        return buf.getvalue()

    def test_projects_list_requires_auth(self, client):
        response = client.get("/api/projects")
        assert response.status_code == 401

    def test_projects_list_shape(self, client):
        login(client)
        client.post("/projects", data={"name": "DesignProj"}, follow_redirects=False)
        zip_bytes = self._zip()
        client.post(
            "/projects/designproj/results",
            data={"execution_branch": "main"},
            files={"files": ("results.zip", zip_bytes, "application/zip")},
        )
        response = client.get("/api/projects")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list) and len(data) >= 1
        project = next((p for p in data if p["slug"] == "designproj"), None)
        assert project is not None
        assert project["name"] == "DesignProj"
        assert isinstance(project["trend"], list)
        assert len(project["runs"]) >= 1
        run = project["runs"][0]
        for key in ("id", "when", "title", "branch", "passed", "failed", "skipped"):
            assert key in run
        assert run["branch"] == "main"

    def test_issue_token_json(self, client):
        login(client)
        client.post("/projects", data={"name": "TokProj"}, follow_redirects=False)
        response = client.post("/api/projects/tokproj/token", data={"name": "gitlab"})
        assert response.status_code == 200
        data = response.json()
        assert data["token"].startswith(data["prefix"])

    def test_root_serves_design_index(self, client):
        # Свежий вход отсутствует -> / ведёт в flow входа (redirection на /login/Keycloak).
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (302, 303)
        assert "auth" in response.headers.get("location", "") or "/login" in response.headers.get("location", "")

    def test_design_assets_served(self, client):
        """Дизайн-фронт должен отдаваться без 404 на свои ресурсы (иначе чёрный экран)."""
        login(client)
        # index.html — отдаёт SPA, и в нём больше нет внешнего scripts/projects.js.
        response = client.get("/")
        assert response.status_code == 200
        assert "projects.js" not in response.text
