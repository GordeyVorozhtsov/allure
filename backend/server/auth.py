import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass

from server import db

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-secret").encode()
SESSION_WEEK = 14 * 24 * 3600


def kc_token(user: str) -> str:
    payload = f"{user}|{int(time.time()) + SESSION_WEEK}"
    sig = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return payload + "|" + sig


def kc_user_of(raw: str) -> str:
    try:
        name, exp, sig = raw.split("|", 2)
    except ValueError:
        return ""
    if not hmac.compare_digest(
        sig, hmac.new(SESSION_SECRET, f"{name}|{exp}".encode(), hashlib.sha256).hexdigest()
    ):
        return ""
    if int(exp) < time.time():
        return ""
    return name


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        check = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(check.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


@dataclass
class Actor:
    kind: str
    role: str
    username: str
    project_id: int | None
    user_id: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_read(self, project_id: int) -> bool:
        if self.role in ("admin", "guest"):
            return True
        return self.project_id == project_id

    def can_upload(self, project_id: int) -> bool:
        if self.role == "admin":
            return True
        return self.role == "uploader" and self.project_id == project_id


def actor_from_request(request) -> Actor | None:
    kc_cookie = getattr(request, "cookies", {}).get("allure_session")
    if kc_cookie:
        name = kc_user_of(kc_cookie)
        if name:
            return Actor("session", "admin", name, None)
    user_id = request.session.get("uid")
    if user_id:
        user = db.user_by_id(int(user_id))
        if user is not None:
            return Actor("session", user["role"], user["username"], None, user["id"])
    header = request.headers.get("x-api-token", "").strip()
    if not header:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            header = auth[7:].strip()
    if not header:
        return None
    row = db.token_by_hash(db.hash_token(header))
    if row is None:
        return None
    return Actor("token", "uploader", row["name"], row["project_id"])
