import base64
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from collections import defaultdict
from urllib import error as urlerror
from urllib import parse, request as urlrequest
from urllib.parse import quote
from zipfile import ZipFile

from starlette.background import BackgroundTask

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from server import db, ingest, settings
from server.auth import Actor, actor_from_request, hash_password, kc_token, kc_user_of, verify_password
from server.i18n import messages

log = logging.getLogger("allure3")

KEYCLOAK_ISSUER = os.environ.get("KEYCLOAK_ISSUER", "http://127.0.0.1:8088/realms/allure")
KEYCLOAK_INTERNAL_ISSUER = os.environ.get("KEYCLOAK_INTERNAL_ISSUER", KEYCLOAK_ISSUER)
KEYCLOAK_CLIENT = os.environ.get("KEYCLOAK_CLIENT", "allure")
KEYCLOAK_SECRET = os.environ.get("KEYCLOAK_SECRET", "allure-dev-secret")
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8090").rstrip("/")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 60


def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        return True
    attempts.append(now)
    return False


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


templates = Jinja2Templates(directory=str(settings.ROOT / "templates"))
app = FastAPI(title="Allure<3", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(settings.ROOT / "static")), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET,
    session_cookie="allure3",
    max_age=60 * 60 * 24 * 14,
    same_site="lax",
    https_only=False,
)


@app.on_event("startup")
def startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for slug, run_id in db.init():
        ingest.remove_run_files(slug, run_id)
    if settings.SECRET == "dev-only-secret":
        log.warning("SECRET_KEY не задан, для стенда это нормально")
    log.info("Allure<3 слушает порт %s, данные в %s", settings.PORT, settings.DATA_DIR)


def text(value: str) -> str:
    """Браузер шлёт UTF-8, разбор формы иногда читает это как latin-1."""
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def spark(values: list[int]) -> dict | None:
    if not values:
        return None
    if len(values) == 1:
        values = [values[0], values[0]]
    width, height, pad = 112, 32, 2
    low, high = min(values) - 2, max(values) + 2
    if high <= low:
        high = low + 1
    points = []
    for index, value in enumerate(values):
        x = pad + index / (len(values) - 1) * (width - pad * 2)
        y = height - pad - (value - low) / (high - low) * (height - pad * 2)
        points.append((round(x, 1), round(y, 1)))
    line = " ".join(f"{x},{y}" for x, y in points)
    area = f"{points[0][0]},{height} {line} {points[-1][0]},{height}"
    return {"line": line, "area": area}


def format_when(value: str | None) -> str:
    if not value:
        return ""
    try:
        day, time = value.split("T")
        year, month, day_n = day.split("-")
        hour, minute, _second = time.replace("Z", "").split(":")
        return f"{day_n}.{month}.{year} {hour}:{minute}"
    except ValueError:
        return value


def set_flash(request: Request, kind: str, text: str, link: str = "") -> None:
    request.session["flash"] = {"kind": kind, "text": text, "link": link}


def safe_next(value: str) -> str:
    if value.startswith("/") and not value.startswith("//") and not value.startswith("/\\"):
        return value
    return "/"


def lang_of(request: Request) -> str:
    lang = request.cookies.get("lang", "ru")
    return lang if lang in {"ru", "en"} else "ru"


def theme_of(request: Request) -> str:
    theme = request.cookies.get("theme", "light")
    return theme if theme in {"light", "dark"} else "light"


def msg(request: Request, key: str, **kwargs) -> str:
    text = messages(lang_of(request)).get(key, key)
    return text.format(**kwargs) if kwargs else text


def render(request: Request, name: str, status_code: int = 200, **ctx):
    flash = request.session.pop("flash", None)
    new_token = request.session.pop("new_token", None)
    context = {
        "actor": actor_from_request(request),
        "flash": flash,
        "new_token": new_token,
        "format_when": format_when,
        "m": messages(lang_of(request)),
        "lang": lang_of(request),
        "theme": theme_of(request),
        **ctx,
    }
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def require_page(request: Request) -> Actor | RedirectResponse:
    actor = actor_from_request(request)
    if actor is None:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)
    return actor


def require_api(request: Request) -> Actor:
    actor = actor_from_request(request)
    if actor is None:
        raise HTTPException(status_code=401, detail="нужен вход или заголовок X-API-Token")
    return actor


def can_see_run(request: Request, project_id: int, run_id: str) -> bool:
    actor = actor_from_request(request)
    if actor is not None and actor.can_read(project_id):
        return True
    return run_id in (request.session.get("share_runs") or [])


def slugify(name: str) -> str:
    chars: list[str] = []
    for char in name.strip().lower():
        if char in CYR:
            chars.append(CYR[char])
        elif char.isascii() and char.isalnum():
            chars.append(char)
        else:
            chars.append("-")
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:40].strip("-") or "project"


def unique_slug(base: str) -> str:
    slug = base
    number = 2
    while db.slug_taken(slug):
        suffix = f"-{number}"
        slug = f"{base[:40 - len(suffix)]}{suffix}"
        number += 1
    return slug


async def read_blobs(form) -> list[tuple[str, bytes]]:
    uploads = []
    for key in ("files", "allureResults", "file"):
        uploads.extend(form.getlist(key))
    blobs: list[tuple[str, bytes]] = []
    remaining = settings.MAX_UPLOAD
    for item in uploads:
        filename = getattr(item, "filename", None)
        if not filename:
            continue
        data = await item.read()
        if len(data) > remaining:
            raise ValueError(f"загрузка больше {settings.MAX_UPLOAD // (1024 * 1024)} МБ")
        remaining -= len(data)
        blobs.append((filename, data))
    if not blobs:
        raise ValueError("приложите zip или JSON-результаты")
    return blobs


def meta_from_form(form) -> dict:
    return {
        "name": text(str(form.get("execution_name") or "").strip())[:120],
        "url": text(str(form.get("execution_url") or "").strip())[:500],
        "type": text(str(form.get("execution_type") or "").strip())[:40],
        "branch": text(str(form.get("execution_branch") or "").strip())[:160],
        "environment": text(str(form.get("environment") or ""))[:4000],
        "source": text(str(form.get("source") or "").strip())[:80],
    }


def badge_svg(passed: int, failed: int, broken: int, total: int) -> str:
    problems = failed + broken
    if total == 0:
        right, color = "пусто", "#6e655b"
    elif problems:
        right, color = f"{problems} упало", "#9d3419"
    else:
        right, color = f"{passed} ок", "#1d6b45"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="132" height="20" role="img" aria-label="Allure<3: {right}">
<rect width="132" height="20" rx="3" fill="#1c1712"/>
<rect x="48" width="84" height="20" rx="3" fill="{color}"/>
<rect x="48" width="8" height="20" fill="{color}"/>
<text x="24" y="14" text-anchor="middle" font-family="Georgia,serif" font-size="11" fill="#f7f1e4">Allure<3</text>
<text x="90" y="14" text-anchor="middle" font-family="Georgia,serif" font-size="11" fill="#fffaf1">{right}</text>
</svg>"""


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "ошибка"
    accepts_html = "text/html" in request.headers.get("accept", "")
    asset = request.url.path.startswith("/reports/") and not request.url.path.endswith(
        ("index.html", "/latest")
    )
    if accepts_html and not asset and exc.status_code in {403, 404}:
        title = "Не найдено" if exc.status_code == 404 else "Нет доступа"
        return render(request, "error.html", status_code=exc.status_code, title=title, message=detail)
    return JSONResponse({"detail": detail}, status_code=exc.status_code)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if actor_from_request(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    state = secrets.token_urlsafe(18)
    redirect_uri = request_url_base(request) + "/auth/callback"
    query = parse.urlencode(
        {
            "client_id": KEYCLOAK_CLIENT,
            "response_type": "code",
            "scope": "openid",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    response = RedirectResponse(KEYCLOAK_ISSUER + "/protocol/openid-connect/auth?" + query, status_code=303)
    response.set_cookie("kc_state", state, httponly=True, max_age=600, samesite="lax")
    return response


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    if not code:
        raise HTTPException(400, "state")
    if state and request.cookies.get("kc_state") and state != request.cookies.get("kc_state"):
        raise HTTPException(400, "state")
    form = parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": KEYCLOAK_CLIENT,
            "client_secret": KEYCLOAK_SECRET,
            "redirect_uri": request_url_base(request) + "/auth/callback",
        }
    ).encode()
    try:
        with urlrequest.urlopen(
            urlrequest.Request(KEYCLOAK_INTERNAL_ISSUER + "/protocol/openid-connect/token", data=form)
        ) as response:
            payload = json.loads(response.read())
    except urlerror.HTTPError as exc:
        return Response(exc.read().decode()[:200], status_code=502)
    access = payload.get("access_token", "")
    name = "user"
    try:
        part = access.split(".")[1]
        part += "=" * (-len(part) % 4)
        name = json.loads(base64.urlsafe_b64decode(part)).get("preferred_username") or name
    except (IndexError, ValueError, json.JSONDecodeError):
        pass
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("allure_session", kc_token(name), httponly=True, max_age=14 * 24 * 3600, samesite="lax")
    return response


@app.post("/login")
async def login(request: Request):
    # Вспомогательный локальный вход (session); основной браузерный вход — Keycloak (GET /login).
    if _rate_limited(_client_ip(request)):
        return render(
            request,
            "login.html",
            status_code=429,
            next="/",
            error="Слишком много попыток. Подождите минуту.",
        )
    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    nxt = safe_next(str(form.get("next") or "/"))
    user = db.user_by_name(username.strip())
    if user is None or not verify_password(password, user["password_hash"]):
        return render(request, "login.html", status_code=401, next=nxt, error=msg(request, "bad_login"))
    request.session["uid"] = user["id"]
    return RedirectResponse(nxt, status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("allure_session", path="/")
    return response


@app.get("/me")
def me(request: Request):
    actor = actor_from_request(request)
    if actor is None:
        return JSONResponse({"name": ""}, status_code=401)
    return JSONResponse({"name": actor.username})


def request_url_base(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-proto", "")
    proto = (fwd or request.url.scheme) or "http"
    host = request.headers.get("x-forwarded-host") or request.url.netloc or "127.0.0.1:8090"
    return f"{proto}://{host}"


@app.get("/")
def home(request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    return FileResponse(settings.ROOT.parent / "web" / "index.html")


@app.post("/projects")
async def create_project(request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    form = await request.form()
    if not actor.is_admin:
        set_flash(request, "err", msg(request, "need_admin_project"))
        return RedirectResponse("/", status_code=303)
    name = text(str(form.get("name") or "").strip())
    slug = slugify(name) if name else ""
    if not name or len(name) > 80 or not SLUG_RE.fullmatch(slug):
        set_flash(request, "err", msg(request, "need_name"))
        return RedirectResponse("/", status_code=303)
    if db.slug_taken(slug):
        set_flash(request, "err", msg(request, "name_taken"))
        return RedirectResponse("/", status_code=303)
    db.create_project(slug, name, settings.KEEP_DEFAULT)
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/projects/{slug}/rename")
async def rename_project(slug: str, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    project = db.get_project(slug)
    form = await request.form()
    nxt = safe_next(str(form.get("next") or "/"))
    if project is None:
        set_flash(request, "err", msg(request, "need_name"))
        return RedirectResponse("/", status_code=303)
    if not actor.is_admin:
        set_flash(request, "err", msg(request, "need_admin_project"))
        return RedirectResponse(nxt, status_code=303)
    name = text(str(form.get("name") or "").strip())
    if not name or len(name) > 80:
        set_flash(request, "err", msg(request, "need_name"))
        return RedirectResponse(nxt, status_code=303)
    db.rename_project(slug, name)
    set_flash(request, "ok", msg(request, "renamed", name=name))
    return RedirectResponse(nxt, status_code=303)


@app.post("/users")
async def create_user(request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    if not actor.is_admin:
        set_flash(request, "err", "Пользователей создаёт администратор")
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    username = text(str(form.get("username") or "").strip())
    password = str(form.get("password") or "")
    role = str(form.get("role") or "guest")
    if not USER_RE.fullmatch(username):
        set_flash(request, "err", "Имя: 2–32 символа, латиница, цифры, точка, дефис")
        return RedirectResponse("/", status_code=303)
    if len(password) < 6:
        set_flash(request, "err", "Пароль не короче 6 символов")
        return RedirectResponse("/", status_code=303)
    if role not in {"admin", "guest"}:
        role = "guest"
    try:
        db.create_user(username, hash_password(password), role)
    except sqlite3.IntegrityError:
        set_flash(request, "err", "Такое имя уже есть")
        return RedirectResponse("/", status_code=303)
    set_flash(request, "ok", f"Пользователь {username} создан")
    return RedirectResponse("/", status_code=303)


@app.post("/users/{user_id}/delete")
def remove_user(user_id: int, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    if not actor.is_admin:
        set_flash(request, "err", "Удаляет администратор")
        return RedirectResponse("/", status_code=303)
    if actor.user_id == user_id:
        set_flash(request, "err", "Свой вход удалять нельзя")
        return RedirectResponse("/", status_code=303)
    try:
        db.delete_user(user_id)
    except ValueError as exc:
        set_flash(request, "err", str(exc))
    else:
        set_flash(request, "ok", "Пользователь удалён")
    return RedirectResponse("/", status_code=303)


def as_dict(row) -> dict:
    return dict(row) if not isinstance(row, dict) else row


def shelf_rows(runs, parts_map: dict, selected: str, slug: str):
    seen: list[str] = []
    for run in runs:
        for part in parts_map.get(run["id"], []):
            if part["source"] not in seen:
                seen.append(part["source"])
    seen.sort(key=str.lower)
    if selected not in seen:
        selected = ""
    rows = []
    for run in runs:
        parts = [as_dict(part) for part in parts_map.get(run["id"], [])]
        if selected:
            matched = next((part for part in parts if part["source"] == selected), None)
            if matched is None:
                continue
            counts = matched
        else:
            counts = as_dict(run)
        rows.append({
            "run": run,
            "counts": counts,
            "parts": parts,
            "report3": ingest.report3_ready(slug, run["id"]),
        })
    latest = next((row for row in rows if row["run"]["status"] == "ready"), None)
    return seen, selected, rows, latest


def prepared_runs(slug: str, project_id: int):
    runs = db.list_runs(project_id)
    ingest.backfill_parts(slug, runs)
    return runs, db.parts_for_project(project_id)


@app.get("/lang/{code}")
def choose_lang(code: str, request: Request):
    if code not in {"ru", "en"}:
        code = "ru"
    referer = request.headers.get("referer") or "/"
    base = str(request.base_url).rstrip("/")
    target = referer if referer.startswith("/") or referer.startswith(base) else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("lang", code, max_age=60 * 60 * 24 * 365, samesite="lax")
    return response


@app.post("/api/peek")
async def peek_archive(request: Request):
    if actor_from_request(request) is None:
        raise HTTPException(status_code=401, detail="нужен вход")
    form = await request.form()
    upload = form.get("file")
    filename = getattr(upload, "filename", None)
    if not filename:
        return {"name": "", "sources": []}
    data = await upload.read()
    if len(data) > settings.MAX_UPLOAD:
        raise HTTPException(status_code=400, detail="файл слишком большой")
    if filename.lower().endswith(".zip"):
        return ingest.detect_archive(data)
    return {"name": "", "sources": []}


@app.get("/projects/{slug}")
def old_project_page(slug: str, request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/project/{slug}{query}", status_code=302)


@app.get("/project/{slug}")
def project_page(slug: str, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="проект не найден")
    if not actor.can_read(project["id"]):
        raise HTTPException(status_code=403, detail="нет доступа к проекту")
    runs, parts_map = prepared_runs(slug, project["id"])
    sources, selected, rows, latest = shelf_rows(runs, parts_map, request.query_params.get("source", ""), slug)
    return render(
        request,
        "project.html",
        project=project,
        rows=rows,
        sources=sources,
        selected=selected,
        latest=latest,
        tokens=db.list_tokens(project["id"]),
        base_url=str(request.base_url).rstrip("/"),
    )


@app.post("/projects/{slug}/settings")
async def project_settings(slug: str, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="проект не найден")
    if not actor.is_admin:
        set_flash(request, "err", "Срок хранения меняет администратор")
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    form = await request.form()
    try:
        keep = int(str(form.get("keep_reports") or ""))
    except ValueError:
        keep = 0
    if not 1 <= keep <= 100:
        set_flash(request, "err", "Хранить можно от 1 до 100 отчётов")
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    db.update_keep(slug, keep)
    project = db.get_project(slug)
    for old_slug, old_id in db.prune(project["id"], keep):
        ingest.remove_run_files(old_slug, old_id)
    set_flash(request, "ok", f"Храним последние {keep}")
    return RedirectResponse(f"/projects/{slug}", status_code=303)


@app.post("/projects/{slug}/delete")
def remove_project(slug: str, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    if not actor.is_admin:
        set_flash(request, "err", "Проект удаляет администратор")
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    if db.delete_project(slug):
        shutil.rmtree(ingest.project_dir(slug), ignore_errors=True)
        set_flash(request, "ok", msg(request, "project_deleted", name=slug))
    return RedirectResponse("/", status_code=303)


@app.post("/projects/{slug}/results")
async def upload_results(slug: str, request: Request):
    form = await request.form()
    as_html = str(form.get("response") or "") == "html"
    actor = actor_from_request(request)
    project = db.get_project(slug)

    def fail(status: int, message: str):
        if as_html:
            set_flash(request, "err", message)
            target = f"/projects/{slug}" if project is not None else "/"
            return RedirectResponse(target, status_code=303)
        return JSONResponse({"detail": message}, status_code=status)

    if actor is None:
        if as_html:
            return RedirectResponse("/login?next=" + quote(f"/projects/{slug}", safe=""), status_code=303)
        return fail(401, "нужен вход или заголовок X-API-Token")
    if project is None:
        return fail(404, "проект не найден")
    if not actor.can_upload(project["id"]):
        return fail(403, "нет права загружать в этот проект")
    try:
        blobs = await read_blobs(form)
        result = ingest.build_run(project, blobs, meta_from_form(form))
    except ValueError as exc:
        return fail(400, str(exc))
    except RuntimeError as exc:
        log.warning("сборка %s: %s", slug, exc)
        return fail(500, str(exc))
    if as_html:
        set_flash(request, "ok", msg(request, "run_ready", n=result["build_order"]), result["url"])
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    return result


@app.post("/projects/{slug}/tokens")
async def create_token(slug: str, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="проект не найден")
    if not actor.is_admin:
        set_flash(request, "err", "Токен выпускает администратор")
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    form = await request.form()
    name = text(str(form.get("name") or "").strip())[:40]
    if not name:
        set_flash(request, "err", "Нужно имя токена, например gitlab")
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    plain, digest, prefix = db.new_token()
    db.create_token(project["id"], name, digest, prefix)
    request.session["new_token"] = plain
    set_flash(request, "ok", "Токен показан один раз. Дальше его не прочитать.")
    return RedirectResponse(f"/projects/{slug}", status_code=303)


@app.post("/api/projects/{slug}/token")
async def api_issue_token(slug: str, request: Request):
    """JSON-выпуск CI-токена для SPA-фронта (возвращает token напрямую)."""
    actor = require_api(request)
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="проект не найден")
    if not actor.is_admin:
        raise HTTPException(status_code=403, detail="токен выпускает администратор")
    form = await request.form()
    name = text(str(form.get("name") or "").strip())[:40] or "gitlab"
    plain, digest, prefix = db.new_token()
    db.create_token(project["id"], name, digest, prefix)
    return {"token": plain, "name": name, "prefix": prefix}


@app.post("/projects/{slug}/tokens/{token_id}/delete")
def remove_token(slug: str, token_id: int, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="проект не найден")
    if not actor.is_admin:
        set_flash(request, "err", "Токен удаляет администратор")
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    db.delete_token(token_id, project["id"])
    set_flash(request, "ok", "Токен удалён")
    return RedirectResponse(f"/projects/{slug}", status_code=303)


def owned_run(slug: str, run_id: str):
    run = db.get_run(run_id)
    if run is None or run["slug"] != slug:
        raise HTTPException(status_code=404, detail="прогон не найден")
    return run


@app.post("/projects/{slug}/runs/{run_id}/delete")
def remove_run(slug: str, run_id: str, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    if not actor.is_admin:
        set_flash(request, "err", "Прогон удаляет администратор")
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    owned_run(slug, run_id)
    db.delete_run(run_id)
    ingest.remove_run_files(slug, run_id)
    set_flash(request, "ok", "Прогон удалён")
    return RedirectResponse(f"/projects/{slug}", status_code=303)


@app.post("/projects/{slug}/runs/{run_id}/share")
async def share_run(slug: str, run_id: str, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    run = owned_run(slug, run_id)
    if not actor.can_read(run["project_id"]):
        raise HTTPException(status_code=403, detail="нет доступа к проекту")
    if run["status"] != "ready":
        set_flash(request, "err", "Гостевая ссылка есть только у собранного отчёта")
        return RedirectResponse(f"/projects/{slug}", status_code=303)
    # Срок действия ссылки: 24 часа или 7 дней.
    form = await request.form()
    try:
        ttl = int(str(form.get("ttl_hours") or "").strip() or "24")
    except (TypeError, ValueError):
        ttl = 24
    if ttl not in {24, 168}:
        ttl = 24
    token = db.set_share(run_id, ttl_hours=ttl)
    share_url = f"/s/{token}"
    if request.headers.get("accept", "").find("application/json") != -1:
        return JSONResponse({"url": share_url, "ttl_hours": ttl})
    set_flash(request, "ok", "Гостевая ссылка открывает отчёт без входа", share_url)
    return RedirectResponse(f"/projects/{slug}", status_code=303)


@app.post("/projects/{slug}/runs/{run_id}/unshare")
def unshare_run(slug: str, run_id: str, request: Request):
    actor = require_page(request)
    if isinstance(actor, RedirectResponse):
        return actor
    run = owned_run(slug, run_id)
    if not actor.can_read(run["project_id"]):
        raise HTTPException(status_code=403, detail="нет доступа к проекту")
    db.delete_share(run_id)
    set_flash(request, "ok", "Гостевая ссылка выключена")
    return RedirectResponse(f"/projects/{slug}", status_code=303)


@app.get("/projects/{slug}/runs/{run_id}/download")
def download_run(slug: str, run_id: str, request: Request):
    run = owned_run(slug, run_id)
    project = db.get_project(slug)
    if run["status"] != "ready" or not can_see_run(request, project["id"], run_id):
        raise HTTPException(status_code=404, detail="отчёт не найден")
    report = ingest.report_dir(slug, run_id)
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    handle.close()
    with ZipFile(handle.name, "w") as archive:
        for path in report.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(report).as_posix())
    return FileResponse(
        handle.name,
        filename=f"{slug}-{run['build_order']}.zip",
        media_type="application/zip",
        background=BackgroundTask(os.unlink, handle.name),
    )


@app.get("/projects/{slug}/badge.svg")
def badge(slug: str):
    project = db.get_project(slug)
    if project is None:
        return Response(status_code=404)
    latest = db.latest_ready(project["id"])
    svg = badge_svg(
        int(latest["passed"]) if latest else 0,
        int(latest["failed"]) if latest else 0,
        int(latest["broken"]) if latest else 0,
        int(latest["total"]) if latest else 0,
    )
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/api/projects/{slug}")
def api_project(slug: str, request: Request):
    actor = require_api(request)
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="проект не найден")
    if not actor.can_read(project["id"]):
        raise HTTPException(status_code=403, detail="нет доступа к проекту")
    runs, parts_map = prepared_runs(slug, project["id"])
    payload = []
    for run in runs:
        parts = parts_map.get(run["id"], [])
        payload.append(
            {
                "id": run["id"],
                "build_order": run["build_order"],
                "status": run["status"],
                "created_at": run["created_at"],
                "executor_name": run["executor_name"],
                "executor_url": run["executor_url"],
                "branch": run["branch"],
                "passed": run["passed"],
                "failed": run["failed"],
                "broken": run["broken"],
                "skipped": run["skipped"],
                "unknown": run["unknown"],
                "total": run["total"],
                "sources": [
                    {
                        "source": part["source"],
                        "passed": part["passed"],
                        "failed": part["failed"],
                        "broken": part["broken"],
                        "skipped": part["skipped"],
                        "total": part["total"],
                    }
                    for part in parts
                ],
                "url": f"/reports/{slug}/{run['id']}/index.html" if run["status"] == "ready" else "",
                "url3": f"/reports/{slug}/{run['id']}/v3/index.html" if ingest.report3_ready(slug, run["id"]) else "",
                "error": run["error"] or "",
            }
        )
    return {
        "slug": project["slug"],
        "name": project["name"],
        "latest": f"/reports/{slug}/latest",
        "badge": f"/projects/{slug}/badge.svg",
        "runs": payload,
    }


@app.get("/api/projects")
def api_projects(request: Request):
    """Список проектов в форме, совместимой с SPA-фронтом web/index.html."""
    actor = require_api(request)
    result = []
    for project in db.list_projects(None if actor.is_admin else actor.project_id):
        if not actor.can_read(project["id"]):
            continue
        runs, _parts = prepared_runs(project["slug"], project["id"])
        trend = db.pass_trend(project["id"], limit=8)
        run_payload = []
        for run in runs:
            title = run["executor_name"] or f"#{run['build_order']}"
            ready = run["status"] == "ready"
            run_payload.append(
                {
                    "id": run["id"],
                    "status": run["status"],
                    "when": format_when(run["created_at"]),
                    "duration": "",
                    "title": title,
                    "branch": run["branch"] or "",
                    "commit": "",
                    "passed": run["passed"] or 0,
                    "failed": run["failed"] or 0,
                    "broken": run["broken"] or 0,
                    "skipped": run["skipped"] or 0,
                    "unknown": run["unknown"] or 0,
                    "total": run["total"] or 0,
                    "url": f"/reports/{project['slug']}/{run['id']}/index.html" if ready else "",
                    "url3": f"/reports/{project['slug']}/{run['id']}/v3/index.html"
                    if ready and ingest.report3_ready(project["slug"], run["id"]) else "",
                }
            )
        result.append(
            {
                "slug": project["slug"],
                "name": project["name"],
                "trend": trend,
                "runs": run_payload,
            }
        )
    return result


@app.get("/reports/{slug}/latest")
def latest_report(slug: str, request: Request):
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="проект не найден")
    run = db.latest_ready(project["id"])
    if run is None:
        raise HTTPException(status_code=404, detail="готовых отчётов ещё нет")
    if not can_see_run(request, project["id"], run["id"]):
        return RedirectResponse("/login?next=" + quote(f"/reports/{slug}/latest", safe=""), status_code=303)
    return RedirectResponse(f"/reports/{slug}/{run['id']}/index.html", status_code=302)


@app.get("/s/{token}")
def open_share(token: str, request: Request):
    row = db.share_by_token(token)
    if row is None or row["status"] != "ready":
        raise HTTPException(status_code=404, detail="ссылка не найдена")
    if row["expires_at"] and row["expires_at"] < db.now():
        db.delete_share(row["run_id"])
        raise HTTPException(status_code=404, detail="ссылка истекла")
    granted = list(request.session.get("share_runs") or [])
    if row["run_id"] not in granted:
        granted.append(row["run_id"])
        request.session["share_runs"] = granted[-50:]
    return RedirectResponse(f"/reports/{row['slug']}/{row['run_id']}/index.html", status_code=303)


def send_report(slug: str, run_id: str, path: str, request: Request, engine: str = "2"):
    run = db.get_run(run_id)
    if run is None or run["slug"] != slug or run["status"] != "ready":
        raise HTTPException(status_code=404, detail="отчёт не найден")
    if engine == "3":
        base = ingest.report3_dir(slug, run_id).resolve()
        public = f"/reports/{slug}/{run_id}/v3/index.html"
        if not (base / "index.html").is_file():
            raise HTTPException(status_code=404, detail="отчёт Allure 3 не найден")
    else:
        base = ingest.report_dir(slug, run_id).resolve()
        public = f"/reports/{slug}/{run_id}/index.html"
    if not can_see_run(request, run["project_id"], run_id):
        accepts_html = "text/html" in request.headers.get("accept", "")
        if accepts_html and path in {"", "index.html"}:
            return RedirectResponse("/login?next=" + quote(public, safe=""), status_code=303)
        raise HTTPException(status_code=401, detail="нужен вход")
    relative = path or "index.html"
    target = (base / relative).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(status_code=404, detail="файл отчёта не найден")
    return FileResponse(target)


@app.get("/reports/{slug}/{run_id}")
def report_directory(slug: str, run_id: str):
    return RedirectResponse(f"/reports/{slug}/{run_id}/index.html", status_code=302)


@app.get("/reports/{slug}/{run_id}/{path:path}")
def report_file(slug: str, run_id: str, path: str, request: Request):
    if path == "v3" or path.startswith("v3/"):
        rest = "" if path == "v3" else path[3:].lstrip("/")
        return send_report(slug, run_id, rest, request, engine="3")
    return send_report(slug, run_id, path, request)
