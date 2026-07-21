"""
Shared Anthropic plumbing for the LLM-backed features (``*_agent`` resolution and
scenario codegen).

Centralizes the two pieces both callers repeat verbatim: building the client with
a raised retry ceiling, and issuing a structured-output (``json_schema``) request
with a prompt-cacheable system block. Each caller keeps its own JSON parsing and
error policy around :func:`structured_json_text`.
"""

from __future__ import annotations

import anthropic

# Single source for the default Claude model used by both LLM-backed features
# (``*_agent`` resolution and scenario codegen), so they can't drift apart.
DEFAULT_MODEL = "claude-sonnet-4-6"


def build_client(api_key: str | None) -> anthropic.Anthropic:
    """Anthropic client with ``max_retries`` above the SDK default of 2, so
    transient overloads (429 / 500 / 529) during a spike are absorbed with
    backoff rather than aborting the workflow mid-run."""
    kwargs: dict = {"max_retries": 5}
    if api_key:
        kwargs["api_key"] = api_key
    return anthropic.Anthropic(**kwargs)


def structured_json_text(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    schema: dict,
    *,
    max_tokens: int,
) -> str | None:
    """Issue a structured-output request and return the model's text block.

    The *system* prompt is sent as a prompt-cacheable ephemeral block and the
    response is constrained to *schema*. Returns ``None`` if the model produced
    no text block; the caller decides how to parse/validate the JSON.
    """
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    return next((b.text for b in resp.content if b.type == "text"), None)
