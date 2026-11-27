#!/usr/bin/env python3
"""Наполнение стенда обезличенными демо-данными с одной команды.

Генерирует фейковые Allure-результаты (*-result.json), упаковывает их в zip и
загружает через реальный API загрузки (POST /projects/{slug}/results) с веткой
(execution_branch). Отчёты собирает сам бэкенд (Allure CLI в контейнере).

Использование:
    python3 scripts/seed.py [BASE_URL]

По умолчанию BASE_URL=http://127.0.0.1:8090, вход admin/admin (см. .env).
Сеть проектов: 4 проекта, у web-shop несколько веток, у mobile-app — две.
"""

import io
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin")

# Транслитерация для slug (как в backend/server/main.py slugify).
_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(name: str) -> str:
    chars = []
    for char in name.strip().lower():
        if char in _CYR:
            chars.append(_CYR[char])
        elif char.isascii() and char.isalnum():
            chars.append(char)
        else:
            chars.append("-")
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:40].strip("-") or "project"

# Обезличенные имена — выдуманные, без реальных данных.
PROJECTS = [
    {
        "name": "Web shop",
        "branches": [
            {"branch": "main", "suite": "web.cart"},
            {"branch": "release/1.2", "suite": "web.checkout"},
            {"branch": "feature/payment", "suite": "web.payment"},
        ],
    },
    {
        "name": "Mobile app",
        "branches": [
            {"branch": "develop", "suite": "mobile.auth"},
            {"branch": "main", "suite": "mobile.profile"},
        ],
    },
    {
        "name": "Billing",
        "branches": [
            {"branch": "main", "suite": "billing.api"},
        ],
    },
    {
        "name": "Search service",
        "branches": [
            {"branch": "main", "suite": "search.api"},
        ],
    },
]

FRAMEWORKS = ["pytest", "junit5", "karate"]
LANGUAGES = ["python", "java"]
SEVERITY = ["normal", "minor", "critical", "blocker", "trivial"]
STATUSES = ["passed", "passed", "passed", "passed", "failed", "skipped", "broken"]
TEST_NAMES = [
    "add_item_to_cart", "remove_item", "apply_promo", "checkout_free_delivery",
    "payment_card_declined", "switch_currency", "restore_session", "badge_counter",
    "save_shipping_address", "email_notification", "validate_phone", "bulk_order",
    "report_generation",
]


def _make_result(suite: str, idx: int, rng: random.Random) -> dict:
    name = TEST_NAMES[idx % len(TEST_NAMES)]
    full = f"{suite}.{name}"
    status = rng.choice(STATUSES)
    h = hashlib_hex(full)
    start = 1_700_000_000_000 + idx * 120_000_000 + rng.randrange(50_000_000)
    duration = rng.randrange(400_000, 2_400_000)
    item = {
        "uuid": str(uuid.uuid4()),
        "historyId": h,
        "name": name.replace("_", " "),
        "fullName": full,
        "status": status,
        "start": start,
        "stop": start + duration,
        "labels": [
            {"name": "suite", "value": suite},
            {"name": "package", "value": suite + ".api"},
            {"name": "framework", "value": rng.choice(FRAMEWORKS)},
            {"name": "language", "value": rng.choice(LANGUAGES)},
            {"name": "severity", "value": rng.choice(SEVERITY)},
            {"name": "feature", "value": "Демо-сценарий"},
            {"name": "story", "value": f"Сюжет {idx % 4 + 1}"},
        ],
        "steps": [
            {"name": "Открыть приложение", "status": "passed", "start": start, "stop": start + duration // 3},
            {"name": "Выполнить сценарий", "status": "passed" if status != "pending" else "pending",
             "start": start + duration // 3, "stop": start + 2 * duration // 3},
            {"name": "Проверить ожидаемый результат", "status": status,
             "start": start + 2 * duration // 3, "stop": start + duration},
        ],
        "attachments": [],
    }
    if status in ("failed", "broken"):
        item["statusDetails"] = {
            "message": "Результат не совпал с ожидаемым (обезличенные демо-данные).",
            "trace": f"AssertionError: {full}\n  seed.py",
        }
    return item


def make_zip(suite: str, count: int) -> bytes:
    rng = random.Random(int.from_bytes(suite.encode(), "big") % 10**9)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("environment.properties", "Browser=chromium\nOs=ubuntu-22.04 # demo\n")
        z.writestr("executor.json", json.dumps({"name": "CI (demo)", "type": "gitlab"}))
        for idx in range(count):
            item = _make_result(suite, idx, rng)
            z.writestr(f"{idx + 1:02d}-{item['historyId'][:8]}-result.json", json.dumps(item))
    buf.seek(0)
    return buf.read()


def _multipart(fields, files) -> bytes:
    boundary = "----allure-seed-" + uuid.uuid4().hex
    buf = io.BytesIO()
    for key, value in fields:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        buf.write(str(value).encode())
        buf.write(b"\r\n")
    for key, filename, content, ctype in files:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write((f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n').encode())
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(content)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    buf.seek(0)
    return boundary, buf.read()


def _request(opener, method: str, url: str, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with opener.open(req, timeout=90) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except (ValueError, UnicodeDecodeError):
                return resp.status, body.decode(errors="replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except (ValueError, UnicodeDecodeError):
            return e.code, e.read().decode(errors="replace")


def main() -> int:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    opener.addheaders = [("User-Agent", "allure-seed/1.0")]

    form = urllib.parse.urlencode({"username": ADMIN_USER, "password": ADMIN_PASS}).encode()
    st, _ = _request(opener, "POST", BASE + "/login", data=form,
             headers={"Content-Type": "application/x-www-form-urlencoded"})
    if st >= 400:
        print(f"✗ вход не удался (HTTP {st}). Проверьте ADMIN_USERNAME/ADMIN_PASSWORD т бэкенд.")
        return 1

    st, existing = _request(opener, "GET", BASE + "/api/projects")
    existing_projects = existing if isinstance(existing, list) else []
    existing_slugs = {p.get("slug") for p in existing_projects}
    # Уже засеяны (есть готовые прогоны) — чтобы повторный make seed не плодил дубликаты.
    already_seeded = {p.get("slug") for p in existing_projects if any(r.get("status") == "ready" for r in (p.get("runs") or []))}

    created, uploaded, failed = 0, 0, 0
    for proj in PROJECTS:
        slug = slugify(proj["name"])
        if slug in already_seeded:
            print(f"  ~ {proj['name']} ({slug}): уже есть готовые прогоны, пропускаю")
            continue
        if slug not in existing_slugs:
            bound, body = _multipart([("name", proj["name"])], [])
            st, _ = _request(opener, "POST", BASE + "/projects", data=body,
                            headers={"Content-Type": f"multipart/form-data; boundary={bound}"})
            created += 1 if st < 400 else 0
            existing_slugs.add(slug)

        for bi, b in enumerate(proj["branches"]):
            zip_bytes = make_zip(b["suite"], count=8 + bi * 3)
            fields = [
                ("execution_name", b["branch"]),
                ("execution_type", "gitlab"),
                ("execution_branch", b["branch"]),
                ("execution_url", f"https://ci.example.com/pipelines/{bi + 1000}"),
                ("environment", "Browser=chromium"),
            ]
            bound, body = _multipart(fields, [("files", "results.zip", zip_bytes, "application/zip")])
            st, result = _request(opener, "POST", f"{BASE}/projects/{slug}/results", data=body,
                                  headers={"Content-Type": f"multipart/form-data; boundary={bound}"})
            if isinstance(result, dict) and result.get("url"):
                printed = result["url"]
                if result.get("total") is not None:
                    printed += f"  (tests={result['total']})"
                uploaded += 1
                print(f"  ✓ {slug} · {b['branch']} -> {printed}")
            else:
                failed += 1
                print(f"  ✗ {slug} · {b['branch']}: HTTP {st} {result}")

    print()
    print(f"Готово: создано проектов: {created}, загружено прогонов: {uploaded}."
          + (f" Ошибок: {failed}." if failed else ""))
    print("Откройте http://127.0.0.1:8090 и войдите admin/admin.")
    return 1 if failed else 0


def hashlib_hex(s: str) -> str:
    import hashlib
    return hashlib.sha1(s.encode()).hexdigest()


if __name__ == "__main__":
    sys.exit(main())