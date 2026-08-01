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