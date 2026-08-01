import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.document import Document


async def search_similar_chunks(
    db: AsyncSession,
    owner_id: uuid.UUID,
    query_embedding: list[float],
    top_k: int,
) -> list[tuple[Chunk, str, float]]:
    """
    Find the top_k chunks (scoped to the given owner) most similar to the
    query embedding, using cosine distance via pgvector.

    Returns a list of (Chunk, document_title, similarity_score) tuples,
    ordered by descending similarity. similarity_score = 1 - cosine_distance.
    """
    distance = Chunk.embedding.cosine_distance(query_embedding)

    stmt = (
        select(Chunk, Document.title, distance.label("distance"))
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.owner_id == owner_id)
        .order_by(distance.asc())
        .limit(top_k)
    )

    result = await db.execute(stmt)
    rows = result.all()

    return [(chunk, title, 1 - dist) for chunk, title, dist in rows]
