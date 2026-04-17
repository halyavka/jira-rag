# Jira RAG

RAG-індексер над Jira (задачі, коментарі, мердж-реквести, статуси, історія) з
семантичним пошуком для інших агентів.

- **Qdrant** — векторний пошук по summary + description / коментарях / MR-описах.
- **Supabase (Postgres)** — джерело правди: сирі дані, статус-історія, курсори синку.
- **Sync**: інкрементальний cron + real-time webhook з Jira.

## Архітектура

```
 Jira Cloud ──► JiraClient ─┬─► Supabase (issues, comments, merge_requests,
                            │                status_history, sync_state)
                            └─► Qdrant (jira_issues / jira_comments / jira_merge_requests)
                                         ▲
                                         │ find_tasks_by_functionality()
                          інші агенти ───┘
```

Точки входу:
- CLI: `jira-rag {init,sync,search,status,serve}`
- Python API: `from jira_rag.search import create_searcher`
- HTTP: `GET /search`, `GET /issues/{key}`, `POST /webhook/jira/{secret}`

---

## 1. Що потрібно

- Python 3.11+
- Docker (для Qdrant) — або зовнішній Qdrant
- Supabase проект (або будь-який Postgres із `gen_random_uuid()`, `pgcrypto`)
- Jira Cloud API token: https://id.atlassian.com/manage-profile/security/api-tokens
- Адмін-доступ до Jira для налаштування webhook (опційно)

## 2. Встановлення

```bash
git clone <repo> jira-rag && cd jira-rag
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## 3. Конфіг

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

Заповни `.env`:
```env
JIRA_API_TOKEN=ATATT3xFfGF...
JIRA_WEBHOOK_SECRET=<згенеруй: openssl rand -hex 32>
SUPABASE_DATABASE_URL=postgresql://postgres.<project>:<pwd>@aws-0-<region>.pooler.supabase.com:6543/postgres
QDRANT_HOST=localhost
```

У [config.yaml](config.yaml) виправ:
- `jira.url`, `jira.email`
- `jira.projects` — список ключів проектів які індексуємо
- `embeddings.provider` — `fastembed` (локально, без ключа) або `voyage`
- `webhook.enabled: true` — якщо плануєш webhook

## 4. Запусти Qdrant

```bash
docker compose up -d
```

## 5. Міграції + колекції

```bash
python scripts/migrate.py        # створює схему jira.* в Supabase
python scripts/init_qdrant.py    # створює 3 колекції в Qdrant
```

Перевірка:
```bash
python scripts/init_qdrant.py --status
jira-rag status
```

## 6. Перший синк

```bash
jira-rag sync
```

Повний ресинк (ігнорує курсори):
```bash
jira-rag sync --full
```

Один проект:
```bash
jira-rag sync --project PROJ
```

## 7. Пошук

CLI:
```bash
jira-rag search "user can reset password via SMS"
jira-rag search "push notifications" --project PROJ --top-k 10
jira-rag search "onboarding flow" --json   # для скриптів
```

Python (для інших агентів):
```python
from jira_rag.config import load_config
from jira_rag.search import create_searcher

searcher = create_searcher(load_config("config.yaml"))
hits = searcher.find_tasks_by_functionality(
    "скидання пароля через SMS",
    project_keys=["PROJ"],
    top_k=5,
)
for hit in hits:
    print(hit.issue_key, hit.score, hit.summary)
    print(hit.context.description_text[:500])
    print("статус:", hit.context.status, f"({hit.context.progress_percent}%)")
    print("коментів:", len(hit.context.comments), "MR:", len(hit.context.merge_requests))
```

HTTP (коли запущений `jira-rag serve`):
```bash
curl "http://localhost:8100/search?q=reset+password&project=PROJ&top_k=5"
curl http://localhost:8100/issues/PROJ-123
```

## 8. Auto-sync: webhook + cron

### 8.1. HTTP-сервер

```bash
jira-rag serve
# або прод:
sudo cp scripts/jira-rag.service /etc/systemd/system/
sudo systemctl enable --now jira-rag
```

Сервер тримає і `/search`, і `/webhook/jira/{secret}` якщо `webhook.enabled: true`.

### 8.2. Jira webhook (real-time)

В Jira: **Settings → System → Webhooks → Create**.
- URL: `https://<твій-публічний-хост>/webhook/jira/<JIRA_WEBHOOK_SECRET>`
- Events: `issue_created`, `issue_updated`, `issue_deleted`,
  `comment_created`, `comment_updated`, `comment_deleted`, `worklog_updated`
- JQL (опційно): `project in (PROJ, PLAT)` — зменшить трафік
- Exclude body: **не постав** — нам потрібен `issue.key`

Якщо сервер за NAT — cloudflared tunnel або ngrok:
```bash
cloudflared tunnel --url http://localhost:8100
```

Тест:
```bash
curl -X POST "http://localhost:8100/webhook/jira/$JIRA_WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"webhookEvent":"jira:issue_updated","issue":{"key":"PROJ-1"}}'
# → {"accepted":true,...}
```

### 8.3. Cron safety-net

Webhook може пропустити подію (мережа, рестарт). Раз на годину робимо
інкрементальний синк — курсор в `sync_state` гарантує що ми не тягнемо все.

**systemd** (рекомендовано):
```bash
sudo cp scripts/jira-rag-sync.{service,timer} /etc/systemd/system/
sudo systemctl enable --now jira-rag-sync.timer
systemctl list-timers | grep jira-rag
```

**cron**:
```cron
*/15 * * * * cd /opt/jira-rag && ./scripts/run_sync.sh >> /var/log/jira-rag-sync.log 2>&1
```

## 9. Корисні команди

```bash
jira-rag status                          # курсори + кількість задач per project
python scripts/init_qdrant.py --status   # стан колекцій
python scripts/init_qdrant.py --reset    # перестворити колекції (треба --full sync)
```

## 10. Структура

```
jira_rag/
├── config.example.yaml
├── docker-compose.yml               # Qdrant
├── migrations/001_initial.sql       # схема jira.*
├── scripts/
│   ├── migrate.py                   # SQL-міграції
│   ├── init_qdrant.py               # колекції
│   ├── run_sync.sh                  # cron wrapper
│   ├── jira-rag.service             # systemd: HTTP-сервер
│   ├── jira-rag-sync.{service,timer}# systemd: godynnyj cron
└── src/jira_rag/
    ├── config/      # Pydantic-схема + YAML loader (${ENV} interpolation)
    ├── database/    # Postgres pool + 6 repo-класів
    ├── jira_client/ # Jira REST + dev-panel (MR) + ADF→text
    ├── vectordb/    # Qdrant + FastEmbed/Voyage + 3 колекції
    ├── indexer/     # sync_all, sync_project, sync_single_issue, delete_issue
    ├── search/      # find_tasks_by_functionality → hits + full context
    ├── webhook/     # POST /webhook/jira/{secret}
    └── cli.py       # init / sync / search / status / serve
```

## 11. Docker Compose deployment

Окремо від systemd-варіанту (§8). Тут jira-rag, qdrant і cron-sync живуть в одному
compose-стеку; mirelia-agent — в сусідньому compose-стеку на тій самій Docker
мережі `rag-shared`. Сервіси знаходять одне одного по DNS імені сервісу.

### 11.1. First-time setup

```bash
docker network create rag-shared       # одноразово, спільна для всіх стеків

cd /opt/jira-rag
cp config.example.yaml config.yaml     # + заповнити
cp .env.example .env                   # + заповнити
docker compose build
docker compose --profile init run --rm migrate         # створює jira.* в Supabase
docker compose --profile init run --rm init-qdrant     # створює Qdrant колекції
docker compose up -d                                   # запускає serve + sync-cron
docker compose --profile init run --rm sync-once --full  # перший повний синк
```

Перевірка:
```bash
curl http://localhost:8100/health
docker compose logs -f jira-rag
docker compose exec jira-rag jira-rag status
```

### 11.2. Наступні апдейти

```bash
docker compose pull && docker compose up -d --build
# міграції нових версій:
docker compose --profile init run --rm migrate
```

### 11.3. Cron sync всередині docker

Сервіс `sync-cron` робить `jira-rag sync` кожну годину в тому самому образі.
Це заміняє systemd-timer з §8.3 — не треба нічого ставити на хост. Логи:
```bash
docker compose logs -f sync-cron
```

### 11.4. Webhook з docker

`jira-rag` публікує `8100` на хост. Поставити перед ним reverse-proxy
(Traefik/Caddy/Nginx) з TLS і прокинути `POST /webhook/jira/{secret}` —
тоді Jira Cloud достукається ззовні. Для dev — `cloudflared tunnel --url http://localhost:8100`.

### 11.5. Підключення mirelia-agent до стеку

У `integrations/mirelia_agent/`:
- [Dockerfile.example](integrations/mirelia_agent/Dockerfile.example)
- [docker-compose.example.yml](integrations/mirelia_agent/docker-compose.example.yml)

Скопіювати в mirelia-agent repo, перейменувати без суфіксу `.example`, підправити
під свої потреби (Slack-bot режим vs CLI-режим). Ключове вже зашито:
```yaml
networks: [rag-shared, default]         # ← спільна мережа
environment:
  JIRA_RAG_URL: http://jira-rag:8100    # ← service name, не localhost
  QDRANT_HOST: qdrant
```

Клієнт постачається окремим pip-пакетом [jira-rag-client](integrations/jira_rag_client/)
(stdlib-only, типізовані dataclasses). Встановлюється в mirelia:
```bash
pip install "git+https://github.com/halyavka/jira-rag.git#subdirectory=integrations/jira_rag_client"
# або в pyproject.toml / requirements.txt
```
У Dockerfile.example це вже зашито окремим `RUN pip install`. Клієнт читає
`JIRA_RAG_URL` з env — у compose-конфігу вказаний `http://jira-rag:8100`.

### 11.6. Shared secrets

Один файл на обидва стеки — `/opt/rag/shared.env` ([template](integrations/shared.env.example)):
```bash
cp integrations/shared.env.example /opt/rag/shared.env
# заповнити; chmod 600
```
Посилатися з обох compose:
```yaml
env_file:
  - .env                  # локальні для сервісу
  - /opt/rag/shared.env   # JIRA_*, SUPABASE_DATABASE_URL
```

### 11.7. Повна мапа портів/мереж

```
                           rag-shared (docker bridge)
                         ┌──────────────────────────────┐
┌──────────┐             │  qdrant:6333                 │
│ Internet │──443──►TLS──┼──►jira-rag:8100              │
└──────────┘             │      ▲                       │
                         │      │ http://jira-rag:8100  │
                         │   mirelia-bot (Slack bot)    │
                         │   mirelia-cli (on-demand)    │
                         └──────────────────────────────┘
                                    │
                                    ▼ (через інтернет)
                           Supabase  /  Jira Cloud
```

Qdrant і Supabase — доступні обом стекам. Jira і Supabase — зовнішні хмарні
сервіси, контейнерам треба лише вихідний інтернет.

## 12. Інтеграція з mirelia-agent (same-server)

Обидва сервіси на одному VPS — jira-rag крутиться як фоновий сервіс, mirelia
ходить до нього по `http://localhost:8100`.

### 11.1. Shared infrastructure

| Ресурс | Конфігурація |
|---|---|
| Qdrant | **Один контейнер** на `:6333`. Колекції не конфліктують: mirelia = `selector_knowledge` / `test_patterns` / `error_solutions`; jira-rag = `jira_issues` / `jira_comments` / `jira_merge_requests`. |
| Supabase | **Одна БД**, різні schema: mirelia у `api.*`, jira-rag у `jira.*`. Той самий `database_url` в обох конфігах. |
| `.env` | Спільний — `JIRA_*`, `SUPABASE_DATABASE_URL`, `QDRANT_HOST`. |

Нічого в mirelia-agent міняти на рівні БД не треба — jira-rag просто додає нові
таблиці в існуючу Supabase.

### 11.2. Layout на сервері

```
/opt/
├── jira-rag/              # цей репозиторій
│   └── .venv/bin/jira-rag
└── mirelia-agent/         # існуючий агент
```

```bash
# jira-rag як HTTP-сервіс + hourly cron safety-net
sudo cp /opt/jira-rag/scripts/jira-rag.service          /etc/systemd/system/
sudo cp /opt/jira-rag/scripts/jira-rag-sync.{service,timer} /etc/systemd/system/
sudo systemctl enable --now jira-rag jira-rag-sync.timer
```

mirelia продовжує запускатися як раніше — він буде звертатися до
`http://localhost:8100`.

### 11.3. Python-клієнт для mirelia (pip package)

Окремий самодостатній пакет [jira-rag-client](integrations/jira_rag_client/):
- stdlib-only (без `requests`/`httpx` — жодних нових runtime deps)
- типізовані dataclasses (`IssueContext`, `SearchHit`, `Comment`, `MergeRequest`)
- dict-API для backward compat з legacy кодом
- форматери для LLM prompt

Встановлення:
```bash
pip install "git+https://github.com/halyavka/jira-rag.git#subdirectory=integrations/jira_rag_client"
# або з локального checkout:
pip install -e /opt/jira-rag/integrations/jira_rag_client
```

Додати в `mirelia-agent/requirements.txt` (або `pyproject.toml`):
```
jira-rag-client @ git+https://github.com/halyavka/jira-rag.git#subdirectory=integrations/jira_rag_client
```

Два entry points:

```python
from jira_rag_client import JiraRagClient, find_related_tasks

rag = JiraRagClient()   # читає JIRA_RAG_URL з env (http://jira-rag:8100)

# Сценарій 1: знаємо ключ — повний hydration замість прямого Jira REST
issue = rag.get_issue("PID-4275")   # IssueContext dataclass, або None якщо недоступний
if issue:
    print(issue.status, issue.progress_percent)
    for c in issue.comments:
        print(c.author, c.body_text[:80])

# Сценарій 2: не знаємо ключ — семантичний пошук по функціоналу
hits = rag.search(
    "registerUserViaSms email verification",
    project_keys=["PID"],
    top_k=3,
    min_score=0.5,
)
# hits[0].context.description_text, .comments, .merge_requests
```

Dict-API (для старого коду):
```python
from jira_rag_client import get_issue_context, find_related_tasks
issue: dict = get_issue_context("PID-4275")
hits: list[dict] = find_related_tasks("password flow", project_keys=["PID"])
```

За замовчуванням **ніколи не кидає винятки** — повертає `None`/`[]`/`{}` при
будь-якій помилці. Для fix-агента це правильний default: мертвий jira-rag
не повинен валити основний flow.

### 11.4. Куди вкрутити в mirelia

**A. `main.py` → `run_fix()`** — підмінити прямий Jira-виклик на rag-кеш з
fallback. Патч: [main_patch.diff](integrations/mirelia_agent/main_patch.diff).
Суть: jira-rag пріоритетніший, бо має коментарі + MR + повну історію; якщо
задача ще не в індексі — fallback на існуючий `fetch_jira_issue`.

**B. `nodes/fix_existing.py`** — додатково шукати релевантні задачі коли
`--jira` не передали. Snippet: [fix_existing_snippet.py](integrations/mirelia_agent/fix_existing_snippet.py).
Використовує `test_method + page_object + api_service + error_message` як
query — в mirelia це зазвичай осмислені назви типу `registerUserViaSmsAndVerifyEmail`,
які добре матчаться на опис фічі в Jira.

### 11.5. Webhook = завжди актуальний кеш

Без webhook jira-rag оновлюється раз на годину (cron). Це значить що свіжий
коментар девів у Jira з'явиться в mirelia-prompt за ~60 хв. З webhook — за
секунди. Для fix-сценаріїв це критично: якщо дев щойно відписав "поле X
прибрано з API", агент має це бачити до фіксу.

```
Jira → POST https://<your-host>/webhook/jira/<secret> → sync_single_issue
                                                     → jira.issues upsert
                                                     → jira_issues vector upsert
```

Налаштовується один раз в Jira UI (див. §8.2).

### 11.6. Моніторинг інтеграції

```bash
# Чи сервіси живі
systemctl status jira-rag jira-rag-sync.timer

# Що в індексі
jira-rag status

# Перевірити що mirelia дістається сервісу
curl http://localhost:8100/health
curl "http://localhost:8100/search?q=test&project=PID&top_k=1"
```

## 13. Troubleshooting

- **`supabase.database_url is not set`** — Supabase connection string з
  Dashboard → Settings → Database → Connection string → URI. Не забудь пароль.
- **`401 invalid webhook secret`** — секрет у URL не збігається з `webhook.secret`.
  Секрет поза URL не перевіряється (Jira Cloud не підписує).
- **Qdrant dimension mismatch** після зміни моделі — `python scripts/init_qdrant.py --reset`
  + `jira-rag sync --full`.
- **Порожні description після синку** — Jira Cloud повертає ADF JSON; у нас є
  конвертер `adf_to_text`. Якщо воно все одно пусте — `raw` в `jira.issues`
  міститиме оригінал.
- **Дубль вектори** не з'являються: point id = `uuid5("issue", key)` — upsert
  завжди замінює.
- **Повний ребілд без втрати БД** — `python scripts/init_qdrant.py --reset` +
  `UPDATE jira.issues SET embed_hash=''` + `jira-rag sync` (дешевше ніж `--full`).
