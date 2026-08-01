import logging

from sqlalchemy import text

from app.db.base_class import Base
from app.db.session import engine

# Import all models so they are registered on Base.metadata before create_all.
from app.models import chunk, document, error_log, user  # noqa: F401

logger = logging.getLogger(__name__)


async def init_db() -> None:
    """
    Create the pgvector extension, all tables, and any indexes that
    SQLAlchemy's Column(index=True) doesn't cover (e.g. the ANN vector index).

    In a production setting this would be replaced by Alembic migrations;
    for this assignment we bootstrap the schema on startup for simplicity.
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

        # Approximate-nearest-neighbour index for cosine similarity search
        # over chunk embeddings. IVFFlat requires the table to have data to
        # train on for best results, but creating it up front is fine for
        # small/medium datasets and keeps setup simple for this assignment.
        await conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_chunks_embedding_cosine
                ON chunks
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
                """
            )
        )
    logger.info("Database initialized: extension, tables, and indexes are ready.")
