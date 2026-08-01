import pytest

from app.services.chunking import chunk_text


def test_chunk_text_empty_string_returns_no_chunks():
    assert chunk_text("", chunk_size=10, overlap=2) == []


def test_chunk_text_shorter_than_chunk_size_returns_single_chunk():
    text = "one two three"
    chunks = chunk_text(text, chunk_size=10, overlap=2)
    assert chunks == ["one two three"]


def test_chunk_text_splits_with_overlap():
    words = [f"w{i}" for i in range(1, 11)]  # w1..w10
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=4, overlap=1)

    # step = chunk_size - overlap = 3
    assert chunks[0] == "w1 w2 w3 w4"
    assert chunks[1] == "w4 w5 w6 w7"
    assert chunks[2] == "w7 w8 w9 w10"

    # Overlap of 1 word between consecutive chunks
    assert chunks[0].split()[-1] == chunks[1].split()[0]


def test_chunk_text_covers_all_words():
    words = [f"w{i}" for i in range(1, 23)]
    text = " ".join(words)
    chunks = chunk_text(text, chunk_size=5, overlap=2)

    all_words_seen = set()
    for c in chunks:
        all_words_seen.update(c.split())
    assert all_words_seen == set(words)


def test_chunk_text_invalid_overlap_raises():
    with pytest.raises(ValueError):
        chunk_text("some text here", chunk_size=5, overlap=5)


def test_chunk_text_invalid_chunk_size_raises():
    with pytest.raises(ValueError):
        chunk_text("some text here", chunk_size=0, overlap=0)
