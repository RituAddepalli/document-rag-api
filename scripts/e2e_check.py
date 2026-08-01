"""
One-off manual verification script (not part of the pytest suite) that
exercises the full user journey: signup -> login -> ingest document ->
background chunking/embedding -> chat retrieval -> LLM answer.

Gemini calls are monkeypatched with deterministic fakes so this can run
without a real API key, while still exercising all the real DB/pgvector/
retrieval code paths.
"""
import asyncio
import os
import sys

os.environ["DATABASE_URL"] = "postgresql+asyncpg://rag_user:rag_password@127.0.0.1:5432/rag_e2e_db"
os.environ["GEMINI_API_KEY"] = "fake-key-for-e2e-script"

sys.path.insert(0, "/home/claude/rag-backend")

import random

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base_class import Base
import app.services.embeddings as embeddings_module
import app.services.llm as llm_module


def fake_vector(seed_text: str) -> list[float]:
    rnd = random.Random(seed_text)
    return [rnd.uniform(-1, 1) for _ in range(768)]


async def fake_embed_text(text_: str) -> list[float]:
    return fake_vector(text_)


async def fake_embed_batch(texts: list[str]) -> list[list[float]]:
    return [fake_vector(t) for t in texts]


async def fake_generate_answer(query: str, context_chunks: list[str]) -> str:
    joined = " | ".join(c[:40] for c in context_chunks)
    return f"[FAKE ANSWER] Based on {len(context_chunks)} chunks: {joined}"


async def main():
    # Patch before app.main import triggers any caching.
    embeddings_module.embed_text = fake_embed_text
    embeddings_module.embed_batch = fake_embed_batch
    llm_module.generate_answer = fake_generate_answer

    from app.api.routes import chat as chat_route
    from app.services import ingestion as ingestion_module

    # ingestion.py imported embed_batch by reference already, patch there too
    ingestion_module.embed_batch = fake_embed_batch
    chat_route.embed_text = fake_embed_text
    chat_route.generate_answer = fake_generate_answer

    from app.main import app

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Signup
        r = await client.post(
            "/auth/signup",
            json={"email": "e2e@example.com", "password": "supersecret1", "full_name": "E2E Tester"},
        )
        assert r.status_code == 201, r.text
        print("[OK] signup:", r.json()["email"])

        # 2. Login
        r = await client.post(
            "/auth/login", data={"username": "e2e@example.com", "password": "supersecret1"}
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print("[OK] login: got JWT")

        # 3. Ingest a document
        content = (
            "The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. "
            "It was designed by Gustave Eiffel's company and completed in 1889 as the entrance to the "
            "1889 World's Fair. The tower is 330 metres tall and was the tallest man-made structure in "
            "the world for 41 years. Millions of people visit the Eiffel Tower every year, making it one "
            "of the most visited paid monuments in the world. The tower has three levels for visitors, "
            "with restaurants on the first and second levels."
        )
        r = await client.post(
            "/documents/", json={"title": "Eiffel Tower Facts", "content": content}, headers=headers
        )
        assert r.status_code == 202, r.text
        doc_id = r.json()["id"]
        print("[OK] document ingested, id =", doc_id, "status =", r.json()["status"])

        # 4. Poll until background job finishes (BackgroundTasks runs after
        # the response is sent, so give it a beat)
        for _ in range(20):
            r = await client.get(f"/documents/{doc_id}", headers=headers)
            status = r.json()["status"]
            if status == "completed":
                break
            await asyncio.sleep(0.25)
        print("[OK] document status after processing:", status, "chunk_count =", r.json()["chunk_count"])
        assert status == "completed", r.json()

        # 5. Chat / RAG retrieval
        r = await client.post(
            "/chat/", json={"query": "How tall is the Eiffel Tower?", "top_k": 2}, headers=headers
        )
        assert r.status_code == 200, r.text
        body = r.json()
        print("[OK] chat answer:", body["answer"])
        print("[OK] sources returned:", len(body["sources"]))
        for s in body["sources"]:
            print("    -", s["document_title"], "| similarity =", s["similarity"], "|", s["content"][:60])
        assert len(body["sources"]) > 0

        # 6. Auth isolation check: a second user shouldn't see first user's chunks
        r = await client.post(
            "/auth/signup", json={"email": "other@example.com", "password": "supersecret1"}
        )
        r = await client.post(
            "/auth/login", data={"username": "other@example.com", "password": "supersecret1"}
        )
        other_token = r.json()["access_token"]
        r = await client.post(
            "/chat/",
            json={"query": "How tall is the Eiffel Tower?", "top_k": 2},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert r.status_code == 200
        assert len(r.json()["sources"]) == 0, "Data isolation violated!"
        print("[OK] per-user data isolation verified (other user sees 0 sources)")

    print("\nALL END-TO-END CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
