"""
Simple word-count based text chunking with overlap.

Kept dependency-free and deterministic so it's easy to unit test and reason
about. For production use this could be swapped for a token-aware splitter
(e.g. tiktoken-based) without changing the calling code.
"""


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """
    Split `text` into overlapping chunks of roughly `chunk_size` words.

    Args:
        text: Raw input text.
        chunk_size: Target number of words per chunk.
        overlap: Number of words repeated between consecutive chunks, to
            preserve context across chunk boundaries.

    Returns:
        List of non-empty chunk strings, in order.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and < chunk_size")

    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    step = chunk_size - overlap
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk_words = words[start:end]
        chunk = " ".join(chunk_words).strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(words):
            break
        start += step

    return chunks
