# RAG Backend — FastAPI + JWT Auth + Retrieval-Augmented Generation

A backend service that combines:

- JWT-based authentication (signup/login) with bcrypt-hashed passwords
- A document ingestion pipeline that chunks text, generates embeddings, and stores them in PostgreSQL via **pgvector**
- A `/chat` endpoint implementing Retrieval-Augmented Generation (RAG): embed the query → similarity search → LLM answer grounded in retrieved chunks
- Custom middleware that catches unhandled exceptions, logs them to Postgres, and returns a clean JSON error response
- Background jobs for ingestion, streaming LLM responses, Docker Compose, and a small test suite

---

## 1. Tech stack

| Concern              | Choice                                                             |
|-----------------------|---------------------------------------------------------------------|
| Web framework         | FastAPI + Uvicorn                                                  |
| Database              | PostgreSQL 16 + [pgvector](https://github.com/pgvector/pgvector)    |
| ORM                    | SQLAlchemy 2.0 (async, `asyncpg` driver)                            |
| Auth                   | `python-jose` (JWT) + `passlib[bcrypt]` (password hashing)          |
| Embeddings / LLM       | OpenAI API (`text-embedding-3-small`, `gpt-4o-mini`, both configurable) |
| Background jobs        | FastAPI `BackgroundTasks` (ingestion runs off the request path)     |
| Caching (optional)     | Redis (wired up in Compose; hook point provided, disabled by default)|
| Tests                  | Pytest + `httpx.AsyncClient`                                       |
| Containerization       | Docker + Docker Compose                                            |

Why PostgreSQL + pgvector over MongoDB: the data is inherently relational (users → documents → chunks, all with foreign keys and cascading deletes), and pgvector gives production-grade ANN vector search without needing a separate vector database, while keeping everything transactional and queryable with plain SQL.

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
│   ├── embeddings.py            # OpenAI embeddings client wrapper
│   ├── llm.py                   # OpenAI chat completion, incl. streaming
│   ├── retrieval.py             # pgvector cosine-similarity search
│   └── ingestion.py             # orchestrates chunk -> embed -> persist (background job)
├── middleware/
│   └── error_logging.py         # catches unhandled exceptions -> Postgres + JSON response
└── tests/                      # pytest suite
scripts/
└── e2e_check.py                 # manual end-to-end script (signup -> ingest -> chat), OpenAI calls mocked
```

---

## 3. Data model & indexing choices

| Table         | Purpose                                   | Indexes & rationale |
|---------------|--------------------------------------------|----------------------|
| `users`       | Auth accounts                              | Unique index on `email` — it's the lookup key on every `/auth/login` call, and uniqueness must be enforced at the DB level, not just in application code. |
| `documents`   | Raw ingested text + ingestion status       | Index on `owner_id` (every list/detail query filters by the current user); index on `status` (used for ops/debugging — "show me all failed ingestions"). |
| `chunks`      | Chunked text + embedding vector            | Index on `document_id` (cascading delete / lookup by parent doc); **denormalized, indexed `owner_id`** so `/chat` can filter a user's own chunks directly in the similarity-search query without a join against `documents` — this matters once the table has many users' chunks; **IVFFlat ANN index on `embedding`** using `vector_cosine_ops`, matching the cosine-distance operator used in the retrieval query (`services/retrieval.py`). IVFFlat was chosen over a plain sequential scan because similarity search should stay fast as chunk volume grows; it was chosen over HNSW (also available in pgvector) for lower build/memory cost, which is the more sensible default at small-to-medium scale — HNSW is a reasonable upgrade if recall/query-latency at large scale becomes the priority. |
| `error_logs`  | Middleware-captured unhandled exceptions   | Index on `timestamp` (natural access pattern is "most recent errors" / "errors in the last N hours"); index on `user_id`, nullable (lets you pull "all errors for user X" while still logging errors from unauthenticated requests). |

All foreign keys cascade on delete (deleting a user removes their documents and chunks; deleting a document removes its chunks).

Schema is bootstrapped on app startup (`db/init_db.py`, run from the FastAPI `lifespan` handler) rather than via Alembic, to keep setup to a single `docker compose up`. In a longer-lived project this would move to Alembic migrations — the `DATABASE_URL_SYNC` env var is already reserved for that.

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
| `POST /chat/`             | ✅ | RAG query. Body: `{"query": "...", "top_k": 4, "stream": false}`. Retrieval is scoped to the current user's own documents. Set `"stream": true` to receive the answer as an incrementally-streamed text response instead of one JSON blob. |
| `GET /health`             |  | Liveness check. |

Full interactive docs are available at `/docs` (Swagger UI) once the server is running.

### Example: end-to-end curl walkthrough

```bash
# 1. Sign up
curl -X POST http://localhost:8000/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "supersecret1", "full_name": "Alice"}'

# 2. Log in (note: this is a form-encoded OAuth2 request, not JSON)
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -d "username=alice@example.com&password=supersecret1" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 3. Ingest a document
curl -X POST http://localhost:8000/documents/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Eiffel Tower Facts", "content": "The Eiffel Tower is 330 metres tall and was completed in 1889..."}'

# 4. Poll status (swap in the id returned above)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/documents/<document_id>

# 5. Ask a question
curl -X POST http://localhost:8000/chat/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "How tall is the Eiffel Tower?", "top_k": 3}'

# 6. Same question, streamed
curl -N -X POST http://localhost:8000/chat/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "How tall is the Eiffel Tower?", "stream": true}'
```

---

## 5. Setup

### Option A — Docker Compose (recommended)

Requires Docker + Docker Compose.

```bash
cp .env.example .env
# then edit .env and set OPENAI_API_KEY

docker compose up --build
```

This starts Postgres (with pgvector baked into the image), Redis, and the API. The API creates its own tables/extension/indexes on startup. Once running:

- API: http://localhost:8000
- Swagger docs: http://localhost:8000/docs

### Option B — Local Python environment

Requires a local PostgreSQL 16+ with the pgvector extension installed (`CREATE EXTENSION vector;`), and Redis if you want to exercise that path.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: DATABASE_URL, SECRET_KEY, OPENAI_API_KEY

uvicorn app.main:app --reload
```

### Required environment variables

See `.env.example` for the full list with comments. At minimum you need:

- `DATABASE_URL` — async Postgres connection string (`postgresql+asyncpg://...`)
- `SECRET_KEY` — JWT signing secret (generate with `openssl rand -hex 32` for real deployments)
- `OPENAI_API_KEY` — required for document ingestion and `/chat` (embeddings + LLM calls)

Everything else has sane defaults.

---

## 6. Design notes

**Background ingestion.** `POST /documents/` returns `202 Accepted` immediately with `status: "pending"`; chunking, embedding, and persistence happen in a FastAPI `BackgroundTasks` job (`services/ingestion.py`), which updates the document's `status` to `processing` → `completed`/`failed` as it runs. Clients poll `GET /documents/{id}` to know when a document is ready to be queried. This keeps the ingest endpoint fast and avoids blocking on OpenAI API latency inside the request/response cycle. (For heavier production workloads, this is the natural place to swap in Celery/RQ + Redis or a Kafka consumer without touching the API surface — `process_document()` is already a self-contained, queueable unit of work.)

**Auth-scoped retrieval.** Every chunk row carries an `owner_id`. The `/chat` similarity search filters on it directly (`WHERE owner_id = :current_user`), so one user's documents are never visible to another user's queries — verified explicitly in `scripts/e2e_check.py`.

**Error-logging middleware.** Implemented as `BaseHTTPMiddleware` around the whole app. It wraps `call_next`, and on any exception that wasn't already turned into an HTTP response further down the stack (i.e. anything that isn't a handled `HTTPException`), it: (1) logs to stdout, (2) best-effort extracts the user id from the request's bearer token without raising if that fails, (3) writes a row to `error_logs` (timestamp, endpoint, method, message, full stack trace, user id, a generated `request_id`), and (4) returns a generic `500` JSON body — the caller never sees a raw traceback. If the DB write itself fails (e.g. DB is down), it falls back to stdout logging rather than crashing the request cycle. There's a `GET /debug/error` route (only registered when `DEBUG=true`) for manually triggering this path.

One implementation detail worth calling out: `BaseHTTPMiddleware` runs outside FastAPI's dependency-injection graph, so it can't reuse the request-scoped DB session from `get_db()` — it opens its own session via the module-level `AsyncSessionLocal`. This is fine in production (single configured database) and is accounted for in the test fixtures (see below).

**Streaming.** `/chat` with `"stream": true` returns a `StreamingResponse` that yields LLM tokens as they arrive (`services/llm.py:generate_answer_stream`), rather than waiting for the full completion.

---

## 7. Tests

```bash
pip install -r requirements.txt
pytest
```

- `test_chunking.py`, `test_security.py` — pure unit tests (chunking algorithm, password hashing, JWT roundtrip/tamper detection). No database required.
- `test_auth_api.py`, `test_error_middleware.py` — API-level tests over the real HTTP layer (signup/login/me, unhandled-exception logging), run against a real Postgres+pgvector database because the `Chunk` model's vector column can't be faithfully faked with SQLite. These are automatically **skipped** if no test database is reachable, so `pytest` still runs cleanly without Docker/Postgres present.

To run the full suite including the DB-backed tests:

```bash
docker compose up -d db
export TEST_DATABASE_URL=postgresql+asyncpg://rag_user:rag_password@localhost:5432/rag_test_db
# create the test DB once: docker compose exec db psql -U rag_user -c "CREATE DATABASE rag_test_db;"
pytest -v
```

All 17 tests (11 unit + 6 API-level) pass against a real Postgres 16 + pgvector instance as of this submission.

There's also `scripts/e2e_check.py`, a standalone script that walks the full user journey — signup, login, document ingestion, background job completion, RAG chat, and cross-user data isolation — against a real database, with OpenAI calls swapped for deterministic fakes so it runs without an API key. Useful for a from-scratch sanity check:

```bash
python scripts/e2e_check.py
```

---

## 8. What's implemented from "Good to Have"

- ✅ Redis — wired into Docker Compose and configuration (`REDIS_URL`, `ENABLE_REDIS_CACHE`); not used to cache responses by default, left as a clearly-labeled extension point rather than adding speculative caching logic that's hard to verify.
- ⬜ Kafka / message broker — not implemented; `BackgroundTasks` was chosen instead to keep the assignment's infra footprint minimal (see note above on where a broker would slot in).
- ✅ Docker / Docker Compose — full stack (Postgres+pgvector, Redis, API) via `docker compose up`.
- ✅ Background jobs for ingestion — see section 6.
- ✅ Streaming LLM responses — see section 6.
- ✅ Unit tests — 17 tests covering auth, security, chunking, and the error middleware; see section 7.

---

## 9. Known limitations / things I'd do next with more time

- Schema is created via `Base.metadata.create_all` on startup rather than Alembic migrations — fine for this assignment, but real schema evolution needs migrations.
- No rate limiting on `/auth/login` or `/chat` (brute-force / cost-control concern for a public deployment).
- The IVFFlat index is created before any data exists, so its initial "lists" parameter isn't tuned to actual data distribution — worth revisiting (`REINDEX`) once there's a realistic volume of chunks.
- Redis is provisioned but not yet used for anything (e.g. caching repeated chat queries, rate limiting) — noted as a deliberate scope cut above.
