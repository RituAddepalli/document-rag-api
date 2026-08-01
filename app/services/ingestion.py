import logging
import uuid

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.chunk import Chunk
from app.models.document import Document, DocumentStatus
from app.services.chunking import chunk_text
from app.services.embeddings import embed_batch

logger = logging.getLogger(__name__)


async def process_document(document_id: uuid.UUID) -> None:
    """
    Background job: load a document, chunk its content, generate embeddings,
    and persist the resulting Chunk rows. Updates the document's status as
    it progresses so clients can poll GET /documents/{id}.

    Runs in its own DB session since it executes outside the request's
    request-scoped session (FastAPI BackgroundTasks run after the response
    dependency generators have already been torn down).
    """
    async with AsyncSessionLocal() as db:
        document = await db.get(Document, document_id)
        if document is None:
            logger.error("process_document: document %s not found", document_id)
            return

        try:
            document.status = DocumentStatus.PROCESSING
            await db.commit()

            pieces = chunk_text(
                document.content,
                chunk_size=settings.CHUNK_SIZE,
                overlap=settings.CHUNK_OVERLAP,
            )
            if not pieces:
                document.status = DocumentStatus.FAILED
                document.error_message = "Document produced no chunks (empty content)."
                await db.commit()
                return

            embeddings = await embed_batch(pieces)

            for idx, (content, embedding) in enumerate(zip(pieces, embeddings)):
                db.add(
                    Chunk(
                        document_id=document.id,
                        owner_id=document.owner_id,
                        chunk_index=idx,
                        content=content,
                        embedding=embedding,
                    )
                )

            document.status = DocumentStatus.COMPLETED
            document.error_message = None
            await db.commit()
            logger.info("Document %s ingested: %d chunks", document.id, len(pieces))

        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.exception("Failed to process document %s", document_id)
            document = await db.get(Document, document_id)
            if document is not None:
                document.status = DocumentStatus.FAILED
                document.error_message = str(exc)[:2000]
                await db.commit()
