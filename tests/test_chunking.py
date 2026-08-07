"""
Unit tests for chunking.py. Pure logic - no DB, no network, no model.

Run: pytest test_chunking.py -v
"""

from chunking import chunk_text


def test_empty_string_returns_empty_list():
    assert chunk_text("") == []


def test_whitespace_only_returns_empty_list():
    assert chunk_text("   \n\t  ") == []


def test_short_text_returns_single_unsplit_chunk():
    text = "Mostly clear, with a low around 68. Northwest wind around 5 mph."
    result = chunk_text(text, chunk_size=800, chunk_overlap=100)
    assert result == [text]


def test_text_exactly_chunk_size_returns_single_chunk():
    text = "x" * 800
    result = chunk_text(text, chunk_size=800, chunk_overlap=100)
    assert result == [text]


def test_long_text_splits_into_multiple_overlapping_chunks():
    # Build a 1000-char string of unique, position-identifiable content
    # (six-digit zero-padded index every 6 chars) so we can verify exactly
    # which characters ended up in which chunk.
    text = "".join(f"{i:06d}" for i in range(200))  # 200 * 6 = 1200 chars
    result = chunk_text(text, chunk_size=800, chunk_overlap=100)

    assert len(result) == 2
    assert result[0] == text[0:800]
    assert result[1] == text[700:1200]


def test_overlap_region_is_shared_between_consecutive_chunks():
    text = "".join(f"{i:06d}" for i in range(200))  # 1200 chars
    chunks = chunk_text(text, chunk_size=800, chunk_overlap=100)

    # Last 100 chars of chunk 1 should equal first 100 chars of chunk 2's
    # position in the original text (chars 700-800).
    overlap_from_chunk1 = chunks[0][-100:]
    overlap_from_chunk2 = chunks[1][:100]
    assert overlap_from_chunk1 == overlap_from_chunk2 == text[700:800]


def test_three_chunks_for_longer_text():
    text = "".join(f"{i:06d}" for i in range(400))  # 2400 chars
    chunks = chunk_text(text, chunk_size=800, chunk_overlap=100)
    # step = 700; starts at 0, 700, 1400 (1400+800=2200 < 2400, so one more at 2100)
    # starts: 0, 700, 1400, 2100 (2100+800=2900 >= 2400 -> stop after this one)
    assert len(chunks) == 4
    assert chunks[0] == text[0:800]
    assert chunks[1] == text[700:1500]
    assert chunks[2] == text[1400:2200]
    assert chunks[3] == text[2100:2400]


def test_no_chunk_exceeds_chunk_size():
    text = "".join(f"{i:06d}" for i in range(500))  # 3000 chars
    chunks = chunk_text(text, chunk_size=800, chunk_overlap=100)
    assert all(len(c) <= 800 for c in chunks)


def test_chunks_cover_the_entire_original_text():
    """No characters should be skipped/lost between chunk boundaries."""
    text = "".join(f"{i:06d}" for i in range(300))  # 1800 chars
    chunks = chunk_text(text, chunk_size=800, chunk_overlap=100)
    # Reconstruct coverage by checking the last chunk reaches the end.
    assert text.endswith(chunks[-1][-50:])
    assert text.startswith(chunks[0][:50])


def test_invalid_overlap_raises_value_error():
    try:
        chunk_text("some text", chunk_size=100, chunk_overlap=100)
        assert False, "expected ValueError"
    except ValueError:
        pass

    try:
        chunk_text("some text", chunk_size=100, chunk_overlap=150)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_custom_chunk_size_and_overlap():
    text = "a" * 50
    chunks = chunk_text(text, chunk_size=20, chunk_overlap=5)
    # step = 15; starts: 0, 15, 30 (30+20=50 >= 50 -> stop)
    assert len(chunks) == 3
    assert all(len(c) <= 20 for c in chunks)


def test_zero_overlap_works():
    text = "".join(f"{i:03d}" for i in range(100))  # 300 chars
    chunks = chunk_text(text, chunk_size=100, chunk_overlap=0)
    assert len(chunks) == 3
    assert chunks[0] == text[0:100]
    assert chunks[1] == text[100:200]
    assert chunks[2] == text[200:300]
