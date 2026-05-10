# 🚀 URL-Shortener — современный, быстрый и умный сокращатель ссылок

> Не просто `short URL`, а готовая база для будущей SaaS-платформы управления ссылками.

## ✨ О проекте

Этот проект объединяет:

1. **Рабочий MVP-сервис сокращения ссылок** (Python + SQLite + WSGI).
2. **Стратегию развития до production** в `docs/PRODUCT_STRATEGY_RU.md`.

Можно сразу запускать локально, использовать для внутренних задач и постепенно эволюционировать в полноценный продукт.

---

## ✅ Почему этот код хорош (преимущества текущей реализации)

- **Минимум зависимостей**: используется стандартная библиотека Python (WSGI + sqlite3), что упрощает запуск и поддержку.
- **Быстрый старт**: один файл `src/server.py`, понятная структура и низкий порог входа.
- **Готовые базовые бизнес-функции**:
  - создание короткой ссылки;
  - custom slug;
  - редирект;
  - сбор статистики;
  - срок жизни ссылки.
- **Базовая безопасность API**: приватные эндпоинты защищены `X-API-Key`.
- **Приватность в аналитике**: сохраняется хэш `ip+ua`, а не сырой PII.
- **Расширяемость**: архитектура и документация уже ориентированы на переход к Redis/Postgres/ClickHouse/Kafka.

---

## 🔥 Что уже умеет MVP

- `GET /health` — проверка состояния сервиса.
- `POST /api/links` — создание короткой ссылки.
- `GET /<slug>` — редирект по короткой ссылке.
- `GET /api/links/<slug>/stats` — статистика по ссылке.

Дополнительно:

- Автогенерация slug.
- Кастомный slug.
- Поддержка `expires_at`.
- Детекция ботов по User-Agent.
- Метрики: `total_clicks`, `unique_clicks`, `bot_clicks`.

---

## 🧱 Стек

- **Python 3.10+**
- **WSGI (stdlib)**
- **SQLite**

---

## ⚙️ Установка и запуск

### 1) Клонируй репозиторий

```bash
git clone <your-repo-url>
cd URL-Shortener
```

### 2) (Опционально) создай виртуальное окружение

```bash
python -m venv .venv
source .venv/bin/activate
```

### 3) Запусти сервер

```bash
python src/server.py
```

По умолчанию сервис запускается на:

`http://127.0.0.1:8080`

---

## 🧩 Переменные окружения

- `PORT` — порт сервера (по умолчанию `8080`)
- `SHORTENER_DB_PATH` — путь к SQLite БД (по умолчанию `shortener.db`)
- `SHORTENER_API_KEY` — API-ключ (по умолчанию `dev-secret-key`)

Пример запуска с кастомными переменными:

```bash
SHORTENER_API_KEY=my-super-key SHORTENER_DB_PATH=./data/shortener.db PORT=9000 python src/server.py
```

---

## 🛠 Примеры API

### Health-check

```bash
curl http://127.0.0.1:8080/health
```

Ответ:

```json
{"status":"ok"}
```

### Создание короткой ссылки

```bash
curl -X POST http://127.0.0.1:8080/api/links \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-secret-key' \
  -d '{
    "destination_url":"https://example.com/landing",
    "slug":"launch2026"
  }'
```

Пример ответа:

```json
{
  "slug": "launch2026",
  "short_url": "/launch2026"
}
```

### Переход по короткой ссылке

Открой в браузере:

`http://127.0.0.1:8080/launch2026`

### Статистика по ссылке

```bash
curl http://127.0.0.1:8080/api/links/launch2026/stats \
  -H 'X-API-Key: dev-secret-key'
```

Пример ответа:

```json
{
  "slug": "launch2026",
  "total_clicks": 42,
  "unique_clicks": 31,
  "bot_clicks": 4
}
```

### Пример ссылки со сроком действия

```bash
curl -X POST http://127.0.0.1:8080/api/links \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-secret-key' \
  -d '{
    "destination_url":"https://example.com/promo",
    "slug":"promo-24h",
    "expires_at":"2026-12-31T23:59:59+00:00"
  }'
```

После истечения срока действия сервис вернет `410 Gone`.

---

## 🧠 Куда развивать дальше (из MVP в продукт)

В `docs/PRODUCT_STRATEGY_RU.md` описан план развития до production-уровня:

- Redis rate limiting
- Postgres + ClickHouse
- A/B routing
- Geo routing
- Workspaces / роли / multi-tenant
- Anti-fraud и anomaly detection
- AI-помощник для slug/CTR

Это уже готовый roadmap для перехода от прототипа к enterprise-grade платформе.

---

## 📁 Структура проекта

```text
.
├── README.md
├── docs/
│   └── PRODUCT_STRATEGY_RU.md
└── src/
    └── server.py
```

---

## 🤝 Для кого этот проект

- Для разработчиков, которым нужен self-hosted shortener «здесь и сейчас».
- Для MVP/стартапов, которым важны скорость запуска и низкая стоимость владения.
- Для product/marketing команд как база для трекинга кампаний и экспериментов.

---

## 🏁 Итог

Сейчас это **легковесный, понятный и рабочий MVP**.
Следующий шаг — масштабирование по roadmap из `docs/PRODUCT_STRATEGY_RU.md` в полноценную SaaS-платформу.
