# RAG Backend

A production-style FastAPI backend combining JWT authentication with a full Retrieval-Augmented Generation (RAG) pipeline: document ingestion → chunking → embeddings → vector similarity search → LLM-grounded answers.

Built for a take-home backend assignment; every design decision below is documented with its rationale, and every core requirement has been verified live against a running Docker stack (see [Verification Evidence](#10-verification-evidence)).

<!-- 📸 PLACEHOLDER: docs/images/swagger-overview.png
     Screenshot: the full Swagger UI (/docs) landing page showing all route groups (health, auth, documents, chat) -->
![Swagger UI overview](docs/images/swagger-overview.png)

---

## Table of Contents

1. [Tech stack](#1-tech-stack)
2. [Project structure](#2-project-structure)
3. [Data model & indexing choices](#3-data-model--indexing-choices)
4. [API overview](#4-api-overview)
5. [Setup](#5-setup)
6. [Design notes](#6-design-notes)
7. [Tests](#7-tests)
8. ["Good to Have" checklist](#8-what-is-implemented-from-good-to-have)
9. [Known limitations / next steps](#9-known-limitations--next-steps)
10. [Verification evidence](#10-verification-evidence)

---

## 1. Tech stack

| Concern              | Choice                                                             |
|-----------------------|---------------------------------------------------------------------|
| Web framework         | FastAPI + Uvicorn                                                  |
| Database              | PostgreSQL 16 + [pgvector](https://github.com/pgvector/pgvector)    |
| ORM                    | SQLAlchemy 2.0 (async, `asyncpg` driver)                            |
| Auth                   | `python-jose` (JWT) + `passlib[bcrypt]` (password hashing)          |
| Embeddings / LLM       | Google Gemini API (`gemini-embedding-001` for embeddings, `gemini-flash-latest` for chat) |
| Background jobs        | FastAPI `BackgroundTasks` (ingestion runs off the request path)     |
| Caching (optional)     | Redis (wired up in Compose; extension point, disabled by default)   |
| Tests                  | Pytest + `httpx.AsyncClient`, run against a real Postgres+pgvector instance |
| Containerization       | Docker + Docker Compose                                            |

**Why PostgreSQL + pgvector over MongoDB:** the data is inherently relational (users → documents → chunks, with foreign keys and cascading deletes), and pgvector provides production-grade approximate-nearest-neighbour vector search without needing a separate vector database — everything stays transactional and queryable with plain SQL.

**Why Gemini over OpenAI:** Gemini's free tier requires no credit card, keeping the project runnable end-to-end without a paid signup. `CHAT_MODEL` is set to the Google-maintained alias `gemini-flash-latest` rather than a pinned version string, because Google rotates model availability frequently for new API keys — during development, a hardcoded `gemini-2.5-flash` began returning `404 This model ... is no longer available to new users` within days. The alias always resolves to Google's current Flash release, so the app self-heals across that kind of rotation instead of silently breaking.

---

## 2. Project structure

```
app/
├── main.py                    # FastAPI app, middleware wiring, router registration
├── core/
│   ├── config.py               # Pydantic Settings (reads .env)
│   ├── security.py             # password hashing, JWT encode/decode
│   └── logging_config.py       # stdout logging setup
├── db/
│   ├── base_class.py           # SQLAlchemy declarative base
│   ├── session.py               # async engine + session factory
│   └── init_db.py               # creates pgvector extension, tables, indexes
├── models/                     # SQLAlchemy ORM models (User, Document, Chunk, ErrorLog)
├── schemas/                    # Pydantic request/response schemas
├── api/
│   ├── deps.py                  # get_db, get_current_user dependencies
│   └── routes/                  # auth.py, documents.py, chat.py, health.py
├── services/
│   ├── chunking.py              # pure-function text chunker (unit tested)
│   ├── embeddings.py            # Gemini embeddings client wrapper
│   ├── llm.py                   # Gemini chat completion, incl. streaming
│   ├── retrieval.py             # pgvector cosine-similarity search
│   └── ingestion.py             # orchestrates chunk -> embed -> persist (background job)
├── middleware/
│   └── error_logging.py         # catches unhandled exceptions -> Postgres + JSON response
└── tests/                      # pytest suite (17 tests: unit + API-level)
scripts/
└── e2e_check.py                 # standalone end-to-end verification script, Gemini calls mocked
```

---

## 3. Data model & indexing choices

| Table         | Purpose                                   | Indexes & rationale |
|---------------|--------------------------------------------|----------------------|
| `users`       | Auth accounts                              | Unique index on `email` — the lookup key on every `/auth/login` call; uniqueness enforced at the DB level, not just in application code. |
| `documents`   | Raw ingested text + ingestion status       | Index on `owner_id` (every list/detail query filters by the current user); index on `status` (used for ops/debugging — "show me all failed ingestions"). |
| `chunks`      | Chunked text + embedding vector            | Index on `document_id` (cascading delete / lookup by parent doc); **denormalized, indexed `owner_id`** so `/chat` can filter a user's own chunks directly in the similarity-search query without a join against `documents`; **IVFFlat ANN index on `embedding`** using `vector_cosine_ops`, matching the cosine-distance operator used in the retrieval query (`services/retrieval.py`). |
| `error_logs`  | Middleware-captured unhandled exceptions   | Index on `timestamp` (natural access pattern is "most recent errors" / "errors in the last N hours"); index on `user_id`, nullable — lets you pull "all errors for user X" while still logging errors from unauthenticated requests. |

All foreign keys cascade on delete (deleting a user removes their documents and chunks; deleting a document removes its chunks).

Schema is bootstrapped on app startup (`db/init_db.py`, run from the FastAPI `lifespan` handler) via `Base.metadata.create_all`, which is sufficient for this assignment's scope. Alembic was deliberately **not** included as a dependency — it was never wired up to an actual migration setup, and an unused dependency is worse than no dependency. In a longer-lived project this would move to proper Alembic migrations for schema versioning.

<!-- 📸 PLACEHOLDER: docs/images/db-schema.png
     Screenshot or diagram: psql \d output for chunks table showing the vector(768) column and ivfflat index -->
![Database schema](docs/images/db-schema.png)

---

## 4. API overview

| Method & path            | Auth required | Description |
|---------------------------|:--:|--------------|
| `POST /auth/signup`       |  | Create an account. Password is bcrypt-hashed before storage. |
| `POST /auth/login`        |  | OAuth2 password flow (`username`=email, `password`). Returns a JWT. |
| `GET /auth/me`            | ✅ | Current authenticated user. |
| `POST /documents/`        | ✅ | Ingest a text document. Returns immediately (`202`, `status=pending`); chunking + embedding happens in a background task. |
| `GET /documents/`         | ✅ | List the current user's documents + ingestion status. |
| `GET /documents/{id}`     | ✅ | Document detail, including `chunk_count`. |
| `DELETE /documents/{id}`  | ✅ | Delete a document and its chunks. |
| `POST /chat/`             | ✅ | RAG query. Body: `{"query": "...", "top_k": 4, "stream": false}`. Retrieval is scoped to the current user's own documents and filtered by a minimum similarity threshold (`MIN_SIMILARITY`, default `0.5`) so unrelated documents in the same account aren't surfaced as sources just to fill out `top_k`. Set `"stream": true` for an incrementally-streamed answer. |
| `GET /health`             |  | Liveness check. |
| `GET /debug/error`        |  | Only registered when `DEBUG=true`. Deliberately raises, to exercise the error-logging middleware. Hidden from `/docs` (`include_in_schema=False`) since it's a dev-only tool — call it directly. |

Full interactive docs are available at `/docs` once the server is running.

### Example: end-to-end curl walkthrough

```bash
# 1. Sign up
curl -X POST http://localhost:8000/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "supersecret1", "full_name": "Alice"}'

# 2. Log in
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -d "username=alice@example.com&password=supersecret1" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 3. Ingest a document
curl -X POST http://localhost:8000/documents/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Agentic AI Overview", "content": "Agentic AI refers to..."}'

# 4. Poll status (swap in the id returned above)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/documents/<document_id>

# 5. Ask a question
curl -X POST http://localhost:8000/chat/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "What are the core components of an agentic AI system?", "top_k": 3}'

# 6. Same question, streamed
curl -N -X POST http://localhost:8000/chat/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "What are the core components of an agentic AI system?", "stream": true}'
```

<!-- 📸 PLACEHOLDER: docs/images/signup-response.png
     Screenshot: Swagger UI POST /auth/signup with 201 response body -->
![Signup response](docs/images/signup-response.png)

<!-- 📸 PLACEHOLDER: docs/images/login-and-authorize.png
     Screenshot: Swagger UI POST /auth/login response + the Authorize modal with Bearer token pasted -->
![Login and Authorize](docs/images/login-and-authorize.png)

<!-- 📸 PLACEHOLDER: docs/images/ingest-and-status.png
     Screenshot: POST /documents/ 202 response, followed by GET /documents/{id} showing status: "completed" -->
![Document ingestion and status polling](docs/images/ingest-and-status.png)

<!-- 📸 PLACEHOLDER: docs/images/chat-response.png
     Screenshot: POST /chat/ 200 response showing the answer and sources array with similarity scores -->
![Chat / RAG response](docs/images/chat-response.png)

---

## 5. Setup

### Option A — Docker Compose (recommended)

Requires Docker Desktop.

```bash
cp .env.example .env
```

Edit `.env` and set:
```
GEMINI_API_KEY=<your key — free, no card, at https://aistudio.google.com/apikey>
SECRET_KEY=<any long random string>
```

Then:
```bash
docker compose up --build
```

This starts Postgres (pgvector baked into the image), Redis, and the API. Tables/extension/indexes are created automatically on startup.

- API: http://localhost:8000
- Swagger docs: http://localhost:8000/docs

<!-- 📸 PLACEHOLDER: docs/images/docker-compose-up.png
     Screenshot: terminal output of `docker compose up --build` showing all three containers healthy and
     "Uvicorn running on http://0.0.0.0:8000" -->
![docker compose up output](docs/images/docker-compose-up.png)

### Option B — Local Python environment

Requires a local PostgreSQL 16+ with the pgvector extension installed (`CREATE EXTENSION vector;`).

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env
# edit .env: DATABASE_URL, SECRET_KEY, GEMINI_API_KEY

uvicorn app.main:app --reload
```

### Required environment variables

See `.env.example` for the full list with comments. At minimum:

- `DATABASE_URL` — async Postgres connection string (`postgresql+asyncpg://...`)
- `SECRET_KEY` — JWT signing secret (generate with `openssl rand -hex 32`, or in PowerShell: `-join ((48..57)+(97..122)|Get-Random -Count 40 |%{[char]$_})`)
- `GEMINI_API_KEY` — required for document ingestion and `/chat`. Get a free key with no credit card at https://aistudio.google.com/apikey

RAG tuning knobs, all with sane defaults: `CHUNK_SIZE`, `CHUNK_OVERLAP`, `TOP_K`, `MIN_SIMILARITY`.

---

## 6. Design notes

**Background ingestion.** `POST /documents/` returns `202 Accepted` immediately with `status: "pending"`; chunking, embedding, and persistence happen in a FastAPI `BackgroundTasks` job (`services/ingestion.py`), which updates the document's `status` to `processing` → `completed`/`failed` as it runs. Clients poll `GET /documents/{id}` to know when a document is ready to be queried. This keeps the ingest endpoint fast and avoids blocking on Gemini API latency inside the request/response cycle. (For heavier production workloads, this is the natural place to swap in Celery/RQ + Redis or a Kafka consumer without touching the API surface — `process_document()` is already a self-contained, queueable unit of work.)

**Auth-scoped, relevance-filtered retrieval.** Every chunk row carries an `owner_id`. The `/chat` similarity search filters on it directly (`WHERE owner_id = :current_user`), so one user's documents are never visible to another user's queries. Matches are further filtered against `MIN_SIMILARITY` (default `0.5`) before being used as LLM context or returned in `sources` — without this, a user with multiple unrelated documents would see off-topic chunks surfaced just because `top_k` needed filling. Both properties were explicitly verified during development (see [Verification Evidence](#10-verification-evidence)).

**Error-logging middleware.** Implemented as `BaseHTTPMiddleware` around the whole app. It wraps `call_next`, and on any exception that wasn't already turned into an HTTP response further down the stack, it: (1) logs to stdout, (2) best-effort extracts the user id from the request's bearer token without raising if that fails, (3) writes a row to `error_logs` (timestamp, endpoint, method, message, full stack trace, user id, a generated `request_id`), and (4) returns a generic `500` JSON body — the caller never sees a raw traceback. If the DB write itself fails, it falls back to stdout logging rather than crashing the request cycle. `GET /debug/error` (registered only when `DEBUG=true`) exists specifically to exercise this path on demand.

**Streaming.** `/chat` with `"stream": true` returns a `StreamingResponse` that yields LLM tokens as they arrive (`services/llm.py:generate_answer_stream`), rather than waiting for the full completion.

---

## 7. Tests

```bash
pip install -r requirements.txt
pytest
```

- `test_chunking.py`, `test_security.py` — pure unit tests (chunking algorithm, password hashing, JWT roundtrip/tamper detection). No database required.
- `test_auth_api.py`, `test_error_middleware.py` — API-level tests over the real HTTP layer, run against a real Postgres+pgvector database because the `Chunk` model's vector column can't be faithfully emulated by SQLite. Automatically **skipped** if no test database is reachable.

To run the full suite including the DB-backed tests:

```bash
docker compose up -d db
docker compose exec db psql -U rag_user -c "CREATE DATABASE rag_test_db;"
export TEST_DATABASE_URL=postgresql+asyncpg://rag_user:rag_password@localhost:5432/rag_test_db
pytest -v
```

All 17 tests (11 unit + 6 API-level) pass against a real Postgres 16 + pgvector instance.

`scripts/e2e_check.py` is a standalone script that walks the full user journey — signup, login, document ingestion, background job completion, RAG chat, and cross-user data isolation — against a real database, with Gemini calls swapped for deterministic fakes so it runs without an API key:

```bash
python scripts/e2e_check.py
```

<!-- 📸 PLACEHOLDER: docs/images/pytest-output.png
     Screenshot: terminal output of `pytest -v` showing all 17 tests passing -->
![pytest output](docs/images/pytest-output.png)

---

## 8. What is implemented from "Good to Have"

- ✅ Redis — wired into Docker Compose and configuration (`REDIS_URL`, `ENABLE_REDIS_CACHE`); left as a clearly-labeled extension point rather than adding speculative caching logic.
- ⬜ Kafka / message broker — not implemented; `BackgroundTasks` was chosen instead to keep the assignment's infra footprint minimal.
- ✅ Docker / Docker Compose — full stack (Postgres+pgvector, Redis, API) via `docker compose up`.
- ✅ Background jobs for ingestion.
- ✅ Streaming LLM responses.
- ✅ Unit tests — 17 tests covering auth, security, chunking, and the error middleware.

---

## 9. Known limitations / next steps

- Schema is created via `Base.metadata.create_all` on startup rather than Alembic migrations — fine for this assignment's scope, but real schema evolution needs proper migrations.
- No rate limiting on `/auth/login` or `/chat` (a brute-force / cost-control concern for a public deployment).
- The IVFFlat index is created before any data exists, so its initial `lists` parameter isn't tuned to actual data distribution — worth revisiting once there's a realistic volume of chunks.
- Redis is provisioned but not yet used for anything (e.g. caching repeated chat queries, rate limiting) — a deliberate scope cut for this assignment, not an oversight.
- `MIN_SIMILARITY` is a fixed global threshold rather than adaptive per-query — reasonable for a single-topic-per-account use case, but a production system might tune it per collection.

---

## 10. Verification evidence

Every core requirement below was tested live against a running Docker stack with a real Gemini API key, not just exercised in automated tests.

| Requirement | Verified how |
|---|---|
| JWT auth, bcrypt hashing | Signup → login → Authorize → protected route (`/auth/me`) round-tripped successfully |
| Document ingestion, chunking, embeddings | `POST /documents/` → `202 pending` → background job → `GET /documents/{id}` → `completed`, `chunk_count > 0` |
| `/chat` RAG retrieval | Real Gemini-generated answer grounded in ingested content, with `sources` array showing chunk-level similarity scores |
| Per-user data isolation | A second user querying after the first user's ingestion returns zero sources for unrelated content |
| Relevance filtering (`MIN_SIMILARITY`) | Confirmed a low-similarity chunk (~0.47) from an unrelated document was excluded from `sources` after the filter was added |
| Error-logging middleware | `GET /debug/error` returned clean JSON (`500`, no raw traceback) with a `request_id`; that exact `request_id` was confirmed present in the `error_logs` Postgres table on the next query, along with a separate row showing a populated `user_id` for an authenticated request that failed |

<!-- 📸 PLACEHOLDER: docs/images/error-log-proof.png
     Screenshot: terminal showing curl http://localhost:8000/debug/error output side-by-side with
     the psql SELECT query result showing the matching request_id row in error_logs -->
![Error logging middleware proof](docs/images/error-log-proof.png)