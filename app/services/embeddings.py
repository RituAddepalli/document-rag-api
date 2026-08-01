import logging

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to your .env file to use "
                "document ingestion or /chat."
            )
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


@retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
async def embed_text(text: str) -> list[float]:
    """Generate an embedding vector for a single piece of text."""
    client = get_openai_client()
    response = await client.embeddings.create(model=settings.EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


@retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts in a single API call."""
    if not texts:
        return []
    client = get_openai_client()
    response = await client.embeddings.create(model=settings.EMBEDDING_MODEL, input=texts)
    # OpenAI preserves input order in the response.
    return [item.embedding for item in response.data]
