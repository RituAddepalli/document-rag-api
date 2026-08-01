import logging

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def get_genai_client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to your .env file to use "
                "document ingestion or /chat. Get a free key at "
                "https://aistudio.google.com/apikey"
            )
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


@retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
async def embed_text(text: str) -> list[float]:
    """Generate an embedding vector for a single piece of text."""
    client = get_genai_client()
    response = await client.aio.models.embed_content(
        model=settings.EMBEDDING_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            output_dimensionality=settings.EMBEDDING_DIM,
            task_type="RETRIEVAL_QUERY",
        ),
    )
    return response.embeddings[0].values


@retry(wait=wait_exponential(multiplier=1, min=1, max=10), stop=stop_after_attempt(3))
async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a batch of texts (used for document chunks)."""
    if not texts:
        return []
    client = get_genai_client()
    response = await client.aio.models.embed_content(
        model=settings.EMBEDDING_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(
            output_dimensionality=settings.EMBEDDING_DIM,
            task_type="RETRIEVAL_DOCUMENT",
        ),
    )
    # Gemini preserves input order in the response.
    return [e.values for e in response.embeddings]
