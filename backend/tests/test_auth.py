import pytest

from server.auth import Actor, hash_password, verify_password, actor_from_request


class TestPasswordHashing:
    def test_hash_verify_roundtrip(self):
        hashed = hash_password("super-secret")
        assert hashed != "super-secret"
        assert verify_password("super-secret", hashed) is True

    def test_wrong_password(self):
        hashed = hash_password("correct")
        assert verify_password("wrong", hashed) is False

    def test_salt_differs(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # разные соли -> разные хэши

    def test_verify_invalid_format(self):
        assert verify_password("x", "not-a-valid-hash") is False
        assert verify_password("x", "") is False

    def test_verify_wrong_algo(self):
        assert verify_password("x", "md5$1000$abcd$efgh") is False


class TestActor:
    def test_is_admin(self):
        assert Actor("session", "admin", "root", None).is_admin is True
        assert Actor("session", "guest", "x", None).is_admin is False
        assert Actor("session", "uploader", "x", 1).is_admin is False

    def test_can_read_admin(self):
        actor = Actor("session", "admin", "root", None)
        assert actor.can_read(1) is True
        assert actor.can_read(999) is True

    def test_can_read_guest(self):
        actor = Actor("session", "guest", "x", None)
        assert actor.can_read(1) is True
        assert actor.can_read(999) is True

    def test_can_read_uploader(self):
        actor = Actor("token", "uploader", "ci", None, user_id=1)
        actor.project_id = 1
        assert actor.can_read(1) is True
        assert actor.can_read(2) is False

    def test_can_upload_admin(self):
        assert Actor("session", "admin", "root", None).can_upload(1) is True

    def test_can_upload_uploader(self):
        actor = Actor("token", "uploader", "ci", 1)
        assert actor.can_upload(1) is True
        assert actor.can_upload(2) is False

    def test_can_upload_guest(self):
        actor = Actor("session", "guest", "x", None)
        assert actor.can_upload(1) is False


class TestActorFromRequest:
    def test_no_auth(self):
        class FakeRequest:
            session = {}
            headers = {}
        assert actor_from_request(FakeRequest()) is None

    def test_session_actor(self, data_dir, db_init):
        from server import db
        from server.auth import hash_password

        db.create_user("testuser", hash_password("secret123"), "guest")
        user = db.user_by_name("testuser")

        class FakeRequest:
            session = {"uid": user["id"]}
            headers = {}

        actor = actor_from_request(FakeRequest())
        assert actor is not None
        assert actor.kind == "session"
        assert actor.username == "testuser"
        assert actor.role == "guest"
        assert actor.user_id == user["id"]

    def test_token_actor(self, data_dir, db_init):
        from server import db

        db.create_project("myproj", "myproj", 5)
        project = db.get_project("myproj")
        plain, digest, prefix = db.new_token()
        db.create_token(project["id"], "gitlab", digest, prefix)

        class FakeRequest:
            session = {}
            headers = {"x-api-token": plain}

        actor = actor_from_request(FakeRequest())
        assert actor is not None
        assert actor.kind == "token"
        assert actor.role == "uploader"
        assert actor.username == "gitlab"
        assert actor.project_id == project["id"]

    def test_bearer_auth(self, data_dir, db_init):
        from server import db

        db.create_project("another", "another", 5)
        project = db.get_project("another")
        plain, digest, prefix = db.new_token()
        db.create_token(project["id"], "jenkins", digest, prefix)

        class FakeRequest:
            session = {}
            headers = {"authorization": f"Bearer {plain}"}

        actor = actor_from_request(FakeRequest())
        assert actor is not None
        assert actor.role == "uploader"
        assert actor.project_id == project["id"]

    def test_empty_token(self):
        class FakeRequest:
            session = {}
            headers = {"x-api-token": ""}
        assert actor_from_request(FakeRequest()) is None