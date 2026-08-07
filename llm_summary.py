"""
LLM-generated summary over retrieved search results, via Databricks
Foundation Model APIs (pay-per-token serving endpoints).

Why Databricks Foundation Model APIs over OpenAI/Anthropic directly: it
reuses the already-authenticated WorkspaceClient (same auth path as
lakebase.py) - no new API key/secret to manage.

Unlike lakebase.py, WorkspaceClient() is instantiated INSIDE summarize()
(not at module import time) - so importing this module doesn't require
live Databricks credentials, only actually calling summarize() does.

Why this exists: raw top-K similarity search can't tell the difference
between "a real match" and "the least-bad option available" - see
DECISIONS.md Phase 4 (the Seattle/Chicago mislabeling saga) and the
low_confidence flag already on /weather/search. An LLM reading the
actual retrieved text can say "none of this is really about what you
asked" in a way a similarity score alone can't - this was the user's
explicit ask after watching search confidently return the wrong city.
"""
from __future__ import annotations

import os

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

# Adjust to whatever pay-per-token Foundation Model endpoint is actually
# available in your workspace - verify via `databricks serving-endpoints
# list` or the Serving UI. This default was current as of when this was
# written but Databricks periodically changes which models are offered.
FM_ENDPOINT_NAME = os.environ.get("WEATHER_FM_ENDPOINT", "databricks-meta-llama-3-3-70b-instruct")

MAX_SUMMARY_TOKENS = int(os.environ.get("WEATHER_SUMMARY_MAX_TOKENS", 300))

_SYSTEM_PROMPT = (
    "You are a weather assistant. Answer the user's question using ONLY "
    "the provided weather alert/forecast excerpts. Be concise (2-4 "
    "sentences). If the excerpts don't actually answer the question, say "
    "so plainly instead of guessing - do not invent weather conditions "
    "that aren't in the provided text."
)


def build_context_block(results: list[dict]) -> str:
    """Format retrieved chunks into a numbered, labeled context block for
    the prompt. Each result needs "location", "headline", "chunk_text"
    keys - matches the shape /weather/search already returns."""
    if not results:
        return "(no relevant documents found)"
    lines = []
    for i, r in enumerate(results, start=1):
        lines.append(
            f"[{i}] {r.get('location', '?')} - {r.get('headline', '')}\n{r.get('chunk_text', '')}"
        )
    return "\n\n".join(lines)


def summarize(query: str, results: list[dict], low_confidence: bool) -> str:
    """
    Ask a Databricks-hosted LLM to answer `query` using only `results` as
    context, and return its response text.

    `low_confidence` (from /weather/search's MIN_SIMILARITY check) is
    passed through into the prompt explicitly, not left for the model to
    infer from the excerpts alone - when nothing scored as a strong
    match, the model is told so directly, so it's more likely to say
    "nothing relevant tracked" instead of stretching a weak match into a
    confident-sounding answer.
    """
    context = build_context_block(results)
    confidence_note = (
        "\n\n(Note: none of these excerpts scored as a strong match for "
        "the question - treat them as weak/uncertain evidence, and say so "
        "if they don't really answer the question.)"
        if low_confidence
        else ""
    )
    user_prompt = f"Question: {query}\n\nExcerpts:\n{context}{confidence_note}"

    w = WorkspaceClient()
    response = w.serving_endpoints.query(
        name=FM_ENDPOINT_NAME,
        messages=[
            ChatMessage(role=ChatMessageRole.SYSTEM, content=_SYSTEM_PROMPT),
            ChatMessage(role=ChatMessageRole.USER, content=user_prompt),
        ],
        max_tokens=MAX_SUMMARY_TOKENS,
    )
    return response.choices[0].message.content.strip()
