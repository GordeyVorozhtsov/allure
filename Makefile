.PHONY: up down logs dev test seed

# Поднять стенд
up:
	docker compose up -d --build

# Остановить стенд
down:
	docker compose down

# Логи сервиса backend
logs:
	docker compose logs -f allure-hub

# Локальный запуск без Docker (нужны allure, allure3, .venv).
dev:
	cd backend && \
	ALLURE_BIN=allure ALLURE3_BIN=allure3 PORT=8090 ../.venv/bin/python -m server

# Прогнать тесты
test:
	cd backend && ../.venv-test/bin/python -m pytest tests/

# Наполнить стенд обезличенными демо-данными (4 проекта, ветки).
# Требует поднятый стенд: make up либо docker compose up -d 
seed:
	python3 scripts/seed.py