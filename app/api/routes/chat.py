from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatResponse, SourceChunk
from app.services.embeddings import embed_text
from app.services.llm import generate_answer, generate_answer_stream
from app.services.retrieval import search_similar_chunks

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/", response_model=None)
async def chat(
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    RAG chat endpoint: embeds the query, retrieves the most relevant chunks
    the current user owns, and asks the LLM to answer using only that
    context. Set `stream: true` to receive the answer as a text/event
    stream instead of a single JSON payload.
    """
    top_k = payload.top_k or settings.TOP_K

  
    query_embedding = await embed_text(payload.query)
    raw_matches = await search_similar_chunks(db, current_user.id, query_embedding, top_k)
    matches = [m for m in raw_matches if m[2] >= settings.MIN_SIMILARITY]
    context_texts = [chunk.content for chunk, _title, _score in matches]

    sources = [
        SourceChunk(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            document_title=title,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            similarity=round(float(score), 4),
        )
        for chunk, title, score in matches
    ]

    if payload.stream:
        async def event_stream():
            async for token in generate_answer_stream(payload.query, context_texts):
                yield token

        return StreamingResponse(event_stream(), media_type="text/plain")

    answer = await generate_answer(payload.query, context_texts)
    return ChatResponse(answer=answer, sources=sources)
