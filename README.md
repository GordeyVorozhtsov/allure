# Allure ❤️

Хранилище Allure-отчётов с полноценным бэкендом на FastAPI. Каждый прогон собирается
официальным CLI — **Allure 2** (через Java) и **Allure 3** (через Node) — поэтому
графики, шаги и вложения такие же, как у обычного `allure generate`. Сверху —
вход через Keycloak, CI-токены, история тренда, стабильные ссылки и badge.

Фронтенд — лёгкий одностраничный интерфейс (`design/index.html`), который работает
с живыми данными бэкенда по API. Никаких заглушек: загрузка, выпуск токенов,
гостевые ссылки, переименование и удаление — всё настоящие запросы к бэкенду.

## Возможности

- **Проекты** — создание, переименование, удаление; у каждого проекта свои прогоны и CI-токены.
- **Сборка отчётов** — каждый прогон собирается дважды:
  - **Allure 2** — `/reports/{проект}/{прогон}/index.html`
  - **Allure 3** — `/reports/{проект}/{прогон}/v3/index.html`
  - Если третья версия не собралась, прогон остаётся доступным — просто без ссылки на v3.
  - Из прошлого отчёта копируется `history`, чтобы в отчёте был тренд по тем же тестам.
- **Загрузка результатов** — zip или отдельные `*-result.json`, с прогрессом и обработкой ошибок.
- **Токены для CI** — один токен на проект, отправка в заголовке `X-API-Token` (или `Authorization: Bearer`).
- **Гостевые ссылки** — `/s/{token}` открывает отчёт без входа; срок 24 часа, 7 дней или бессрочно, при необходимости отзывается.
- **Badge** — `/projects/{проект}/badge.svg`, отдаётся без входа.
- **API** — `GET /api/projects` (список для интерфейса) и `GET /api/projects/{проект}` (детали).
- **Ветки** — прогоны группируются по `execution_branch`, в проекте можно фильтровать по ветке.

## Быстрый старт

### Docker Compose

```bash
docker compose up -d --build
```

Поднимутся два контейнера:

| Сервис   | Адрес                 | Назначение                                       |
|----------|-----------------------|--------------------------------------------------|
| `design` | http://localhost:8090 | Интерфейс + бэкенд Allure                        |
| `keycloak` | http://localhost:8088 | Identity Provider (realm `allure`, клиент `allure` |

Вход — через Keycloak. По умолчанию: логин `admin`, пароль `admin` (админин Keycloak
контейнера, задан через `KC_BOOTSTRAP_ADMIN_USERNAME` / `KC_BOOTSTRAP_ADMIN_PASSWORD`).

### Запуск без Docker (для разработки)

Нужны установленные CLI: `allure` (2, требует Java) и `allure3` (3, npm-пакет `allure@3`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r design/app/requirements.txt

cd design/app
ALLURE_BIN=allure ALLURE3_BIN=allure3 PORT=8090 .venv/bin/python -m server
```

Сервис будет слушать `http://localhost:8090`. Вход — через Keycloak (`GET /login`);
для разработки/тестов доступен и локальный логин (`POST /login`).

## Загрузка результатов из CI

Проекту нужен CI-токен. Его выпускает администратор в интерфейсе (кнопка GitLab)
или через API: `POST /api/projects/{проект}/token`.

Пример для GitLab:

```bash
curl -F files=@results.zip \
  -F execution_name=gitlab \
  -F execution_url="$CI_PIPELINE_URL" \
  -F execution_type=gitlab \
  -F execution_branch="$CI_COMMIT_BRANCH" \
  -H "X-API-Token: $ALLURE_TOKEN" \
  http://localhost:8090/projects/library/results
```

Принимаются поля файла `files`, `file` и `allureResults`. Дополнительно
поддерживаются `execution_name`, `execution_url`, `execution_type`,
`execution_branch` и `environment`.

## Конфигурация

Основные переменные окружения (см. `docker-compose.yml`):

| Переменная            | По умолчанию          | Назначение                              |
|-----------------------|-----------------------|-----------------------------------------|
| `PORT`                | `8090`                | Порт HTTP-сервера                       |
| `DATA_DIR`            | `/data`               | Каталог данных (том)                    |
| `ADMIN_USERNAME`      | `admin`               | Логин администратора                    |
| `ADMIN_PASSWORD`      | `admin`               | Пароль администратора                   |
| `SECRET_KEY`          | `dev-only-secret`     | Секрет сессий                           |
| `SESSION_SECRET`      | `dev-only-secret`     | Секрет подписи сессий                   |
| `KEEP_REPORTS`        | `20`                  | Сколько последних отчётов хранить       |
| `MAX_UPLOAD_MB`       | `100`                 | Лимит загрузки, МБ                      |
| `MAX_UNPACKED_MB`     | `500`                 | Лимит распаковки архива, МБ             |
| `KEYCLOAK_ISSUER`     | `http://127.0.0.1:8088/realms/allure` | Issuer для браузера            |
| `KEYCLOAK_INTERNAL_ISSUER` | `http://keycloak:8080/realms/allure` | Issuer внутри docker-сети     |
| `KEYCLOAK_CLIENT`     | `allure`              | Client-ID в Keycloak                    |
| `KEYCLOAK_SECRET`     | `allure-dev-secret`   | Client-secret в Keycloak                |
| `BASE_URL`            | `http://127.0.0.1:8090` | Внешний адрес (для ссылок в curl)     |
| `ALLURE_BIN`          | `/usr/local/bin/allure` | CLI Allure 2                           |
| `ALLURE3_BIN`         | `/usr/local/bin/allure3` | CLI Allure 3                           |

> **Для боевого стенда** задайте собственные `SECRET_KEY`, `SESSION_SECRET` и
> `KEYCLOAK_SECRET`, а также приведите `redirectUris` в
> `deploy/keycloak/allure-realm.json` к актуальному `BASE_URL`.
> Секреты по умолчанию (`dev-only-secret`, `admin`/`admin`) предназначены только
> для локальной разработки.

## Тесты

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install -r requirements-dev.txt

cd design/app
../../.venv-test/bin/python -m pytest tests/
```

Покрыты основные сценарии: вход через Keycloak и локальный логин, выпуск и отзыв
токенов, контракт `/api/projects`, загрузка результатов, сборка Allure 2/3,
гостевые ссылки, badge, управление пользователями и проектами.

Тесты не требуют установки Java/Node: генераторы отчётов подменяются фикстурами,
поэтому прогон стабилен на любой машине.

## О Dockerfile.design

`Dockerfile.design` — это рабочий образ сервиса `design`. В нём собран полный
рантайм для обоих генераторов отчётов, которого нет в лёгком `python:3.12-slim`:

- OpenJDK 17 + Allure 2 (`2.46.1`)
- Node.js + Allure 3 (`allure@3.22.1`, переименован в `allure3`)

Запуск — `python -m server` из `/app/design/app` на порту `8090`. Положение CLI
задаётся через `ALLURE_BIN` / `ALLURE3_BIN`.

## Структура проекта

```
design/
  index.html            # Интерфейс (SPA): список проектов, загрузка, токены, share
  projects.js           # Заглушка состояния: `let projects = []`, данные из /api/projects
  app/                  # Бэкенд (FastAPI)
    server/             #   main.py, auth.py, db.py, ingest.py, settings.py, i18n.py
    static/             #   Статика бэкенда
    templates/          #   Jinja2-шаблоны (резерв/админ-формы)
    tests/              #   Тесты
    requirements.txt
deploy/keycloak/        # Realm для Keycloak (allure-realm.json)
Dockerfile.design       # Образ 8090: Java + Allure 2 + Node + Allure 3
docker-compose.yml      # Сервисы: design (8090), keycloak (8088)
```

## Лицензия

<!-- Укажите лицензию проекта, если она есть. -->