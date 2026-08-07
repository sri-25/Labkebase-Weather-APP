"""
Unit tests for llm_summary.py - the Databricks Foundation Model API call
that turns retrieved search results into a natural-language answer.

Unlike lakebase.py, WorkspaceClient() is instantiated INSIDE summarize()
(not at module import time), so importing this module doesn't require
live Databricks credentials - only actually calling summarize() does,
and that's exactly what's mocked here (no sys.modules injection needed,
unlike test_app.py/test_embed_pipeline.py).
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm_summary


def test_build_context_block_empty_results():
    assert llm_summary.build_context_block([]) == "(no relevant documents found)"


def test_build_context_block_formats_location_headline_and_text():
    results = [{"location": "Denver, CO", "headline": "Heat Advisory", "chunk_text": "Hot today."}]
    block = llm_summary.build_context_block(results)
    assert "Denver, CO" in block
    assert "Heat Advisory" in block
    assert "Hot today." in block


def test_build_context_block_numbers_multiple_results():
    results = [
        {"location": "A", "headline": "H1", "chunk_text": "t1"},
        {"location": "B", "headline": "H2", "chunk_text": "t2"},
    ]
    block = llm_summary.build_context_block(results)
    assert "[1]" in block
    assert "[2]" in block


def _fake_client_returning(text: str) -> MagicMock:
    fake_response = MagicMock()
    fake_response.choices = [MagicMock(message=MagicMock(content=text))]
    fake_client = MagicMock()
    fake_client.serving_endpoints.query.return_value = fake_response
    return fake_client


def test_summarize_calls_serving_endpoints_query_and_returns_stripped_content(monkeypatch):
    fake_client = _fake_client_returning("  It's hot in Denver today.  ")
    monkeypatch.setattr(llm_summary, "WorkspaceClient", lambda: fake_client)

    result = llm_summary.summarize(
        "how's the weather",
        [{"location": "Denver, CO", "headline": "H", "chunk_text": "t"}],
        False,
    )

    assert result == "It's hot in Denver today."
    assert fake_client.serving_endpoints.query.called


def test_summarize_uses_configured_endpoint_name(monkeypatch):
    fake_client = _fake_client_returning("answer")
    monkeypatch.setattr(llm_summary, "WorkspaceClient", lambda: fake_client)
    monkeypatch.setattr(llm_summary, "FM_ENDPOINT_NAME", "some-other-endpoint")

    llm_summary.summarize("q", [], False)

    call_kwargs = fake_client.serving_endpoints.query.call_args.kwargs
    assert call_kwargs["name"] == "some-other-endpoint"


def test_summarize_includes_low_confidence_note_in_prompt(monkeypatch):
    fake_client = _fake_client_returning("answer")
    monkeypatch.setattr(llm_summary, "WorkspaceClient", lambda: fake_client)

    llm_summary.summarize("query", [], True)

    call_kwargs = fake_client.serving_endpoints.query.call_args.kwargs
    user_message = call_kwargs["messages"][1].content
    assert "weak/uncertain" in user_message


def test_summarize_omits_low_confidence_note_when_confident(monkeypatch):
    fake_client = _fake_client_returning("answer")
    monkeypatch.setattr(llm_summary, "WorkspaceClient", lambda: fake_client)

    llm_summary.summarize("query", [{"location": "A", "headline": "H", "chunk_text": "t"}], False)

    call_kwargs = fake_client.serving_endpoints.query.call_args.kwargs
    user_message = call_kwargs["messages"][1].content
    assert "weak/uncertain" not in user_message


def test_summarize_system_prompt_instructs_no_invention(monkeypatch):
    """The whole point of this feature is honesty about what's actually in
    the retrieved text - guard against a future edit accidentally dropping
    that instruction from the system prompt."""
    fake_client = _fake_client_returning("answer")
    monkeypatch.setattr(llm_summary, "WorkspaceClient", lambda: fake_client)

    llm_summary.summarize("query", [], False)

    call_kwargs = fake_client.serving_endpoints.query.call_args.kwargs
    system_message = call_kwargs["messages"][0].content
    assert "do not invent" in system_message.lower()
