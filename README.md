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
- Метрики: `total_clicks`, `unique_clicks`, `bot_clicks`, `bot_ratio`, `top_referrers`, `clicks_by_day`; `recent_clicks` доступны по явному флагу.
- Валидация slug, destination URL и `expires_at`.
- Список, обновление, отключение ссылок и CSV-импорт.
- UTM-builder, preview endpoint и SVG share-code для шаринга ссылки.
- Простые in-memory rate limits для API и редиректов.
- A/B routing с весами destination URL.
- Расширенная аналитика по устройствам, браузерам, ОС, странам и вариантам A/B.
- Фильтры, поиск и пагинация списка ссылок.
- API-ключи в БД со scopes и workspace-контекстом.
- Webhooks для событий кликов через очередь доставок и простой debug HTML listing.
- Safety hints для подозрительных destination URL.

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
- `SHORTENER_BASE_URL` — базовый публичный URL для генерации `short_url` (опционально)
- `SHORTENER_WORKSPACE_ID` — workspace по умолчанию для legacy API-ключа (по умолчанию `default`)
- `SHORTENER_WEBHOOK_TIMEOUT` — timeout доставки webhook в секундах (по умолчанию `2`)

Пример запуска с кастомными переменными:

```bash
SHORTENER_API_KEY=my-super-key SHORTENER_DB_PATH=./data/shortener.db SHORTENER_BASE_URL=https://go.example.com PORT=9000 python src/server.py
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
  "bot_clicks": 4,
  "bot_ratio": 0.0952,
  "top_referrers": [
    {"referrer":"https://example.com","clicks":20}
  ],
  "clicks_by_day": [
    {"date":"2026-06-28","clicks":42}
  ],
  "devices": [],
  "browsers": [],
  "operating_systems": [],
  "countries": [],
  "variants": []
}
```


### Список ссылок

```bash
curl http://127.0.0.1:8080/api/links \
  -H 'X-API-Key: dev-secret-key'
```

### Обновление ссылки

```bash
curl -X PATCH http://127.0.0.1:8080/api/links/launch2026 \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-secret-key' \
  -d '{
    "is_active": false,
    "expires_at": "2026-12-31T23:59:59+00:00"
  }'
```

### Отключение ссылки

```bash
curl -X DELETE http://127.0.0.1:8080/api/links/launch2026 \
  -H 'X-API-Key: dev-secret-key'
```

### CSV-импорт

CSV должен содержать минимум колонки `destination_url` и, опционально, `slug`, `expires_at`, `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`.

```bash
curl -X POST http://127.0.0.1:8080/api/links/import \
  -H 'X-API-Key: dev-secret-key' \
  --data-binary @links.csv
```

### Preview и SVG share-code для шаринга

```bash
curl http://127.0.0.1:8080/preview/launch2026
curl http://127.0.0.1:8080/qr/launch2026 > launch2026.svg
```


### A/B routing

```bash
curl -X POST http://127.0.0.1:8080/api/links \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-secret-key' \
  -d '{
    "destination_url":"https://example.com/control",
    "slug":"experiment",
    "destinations":[
      {"label":"a","url":"https://example.com/a","weight":50},
      {"label":"b","url":"https://example.com/b","weight":50}
    ]
  }'
```

При редиректе сервис выбирает destination URL с учётом веса, стабильно привязывает вариант к visitor hash и сохраняет `variant_label` в аналитике.

### Фильтры списка ссылок

```bash
curl 'http://127.0.0.1:8080/api/links?q=launch&limit=20&offset=0&is_active=true' \
  -H 'X-API-Key: dev-secret-key'
```

Ответ содержит `links` и блок `pagination` с `limit`, `offset` и `total`.

### API-ключи со scopes

```bash
curl -X POST http://127.0.0.1:8080/api/keys \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-secret-key' \
  -d '{"name":"readonly","scopes":["links:read","stats:read"]}'
```

Созданный ключ возвращается только один раз; в SQLite хранится SHA-256 hash.

### Webhooks

```bash
curl -X POST http://127.0.0.1:8080/api/webhooks \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-secret-key' \
  -d '{"url":"https://example.com/hook","events":["click.created"]}'
```

Событие клика сначала попадает в очередь `webhook_deliveries`; отдельный endpoint обработки подписывает payload HMAC SHA-256 в заголовке `X-Shortener-Signature` и выполняет delivery.

### Debug listing

```bash
curl http://127.0.0.1:8080/admin \
  -H 'X-API-Key: dev-secret-key'
```

Debug listing показывает последние 50 ссылок, статус активности и safety hint; это не полноценная админ-панель.

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


### Известные ограничения MVP

- `/qr/<slug>` пока отдаёт stdlib-only SVG share-code, а не стандартизированный сканируемый QR-код. Endpoint сохранён для обратной совместимости, но production-версии стоит подключить полноценный QR encoder.
- In-memory rate limit подходит для локального запуска; для нескольких процессов/инстансов нужен Redis-backed implementation за тем же интерфейсом rate limiter.
- `/admin` — это debug listing, а не полноценный dashboard с графиками и формами управления.
- Webhook delivery вынесен из redirect path в очередь, но production-воркер/retry scheduler ещё нужно запускать отдельным процессом.
- Raw `user_agent` не возвращается в stats по умолчанию; его можно запросить только явно через `include_recent=true&include_user_agent=true`.

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
