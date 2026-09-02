"""Shared Anthropic client, structured-output helper, and web-search helper.

The division of labour in this application is strict: the model reads, classifies
and writes; Python calculates. So this module offers exactly two shapes of call —
`complete_json`, which constrains a response to a JSON schema, and `search`,
which runs the server-side web-search tool and returns prose plus the sources it
cited. Neither is ever asked to do arithmetic.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import config


class CredentialsError(RuntimeError):
    """No usable Anthropic credentials. Carries a message meant for the user."""


def _no_credentials_message() -> str:
    return (
        "No Anthropic credentials found.\n"
        f"Add ANTHROPIC_API_KEY to {config.PROJECT_ROOT / '.env'} "
        "(copy .env.example), or set it in the environment:\n"
        '    PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."\n'
        "Get a key at https://console.anthropic.com/settings/keys"
    )


def build_client(client: Any | None = None) -> Any:
    """Return the injected client, or construct one from the environment.

    Without credentials the SDK raises a bare TypeError from inside its
    constructor; catch it here so the CLI reports something actionable. A
    variable set to whitespace is the same situation as an unset one, but the
    SDK would carry it all the way to a 401.
    """
    if client is not None:
        return client

    import anthropic

    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    token = (os.getenv("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if not key and not token:
        raise CredentialsError(_no_credentials_message())

    try:
        return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    except TypeError as error:  # the SDK's unresolved-credentials guard
        if "authentication method" not in str(error):
            raise
        raise CredentialsError(_no_credentials_message()) from error


# --- Structured output ------------------------------------------------------


def complete_json(
    system: str,
    prompt: str,
    schema: dict[str, Any],
    client: Any | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int = 16000,
) -> dict[str, Any]:
    """Run one request constrained to `schema` and return the parsed object.

    Retries once with a stricter instruction if the response will not parse,
    which is the documented contract for a malformed structured response.
    """
    client = build_client(client)
    model = model or config.LLM_MODEL
    effort = effort or config.LLM_EFFORT

    raw = _request(client, model, system, prompt, schema, effort, max_tokens)
    parsed = _parse(raw)
    if parsed is None:
        retry = (
            prompt + "\n\nYour previous response was not valid JSON. Respond with a single "
            "JSON object and nothing else: no prose, no code fences, no commentary."
        )
        parsed = _parse(_request(client, model, system, retry, schema, effort, max_tokens))
    if parsed is None:
        raise ValueError("The model did not return valid JSON after a retry.")
    return parsed


def _request(
    client: Any,
    model: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    effort: str,
    max_tokens: int,
) -> str:
    # Streamed rather than created: a deck-sized prompt at a high max_tokens can run
    # past the SDK's non-streaming request ceiling, which it refuses outright.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise ValueError("The model declined to process this deck.")
    return _text_of(response)


def _text_of(response: Any) -> str:
    return "".join(block.text for block in response.content if block.type == "text")


def _parse(text: str) -> dict[str, Any] | None:
    """Parse a JSON object out of the response, tolerating stray wrapping."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


# --- Web search -------------------------------------------------------------


def search(
    system: str,
    prompt: str,
    client: Any | None = None,
    model: str | None = None,
    effort: str | None = None,
    max_uses: int = 8,
    max_tokens: int = 12000,
    max_restarts: int = 4,
) -> dict[str, Any]:
    """Answer `prompt` with the server-side web-search tool enabled.

    Returns {"text", "sources"}, where each source is {title, url, page_age}
    harvested from the search-result blocks and from any citations attached to
    the answer. A long research turn can stop with `stop_reason: "pause_turn"`;
    the loop below resumes it by sending the paused turn straight back.
    """
    client = build_client(client)
    model = model or config.LLM_MODEL
    effort = effort or config.LLM_EFFORT

    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    tools = [{"type": config.WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_uses}]

    text_parts: list[str] = []
    sources: list[dict[str, str]] = []

    for _ in range(max_restarts + 1):
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            output_config={"effort": effort},
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            raise ValueError("The model declined to run this research query.")

        text_parts.append(_text_of(response))
        sources.extend(_harvest_sources(response))
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})

    return {"text": "\n".join(part for part in text_parts if part), "sources": _dedupe(sources)}


def _harvest_sources(response: Any) -> list[dict[str, str]]:
    """Pull URLs out of search-result blocks and out of citations on the answer."""
    found: list[dict[str, str]] = []
    for block in response.content:
        if block.type == "web_search_tool_result":
            # A failed search returns a single error object here rather than a
            # list of results, so check the shape before iterating it.
            content = getattr(block, "content", None)
            if not isinstance(content, list):
                continue
            for result in content:
                url = getattr(result, "url", "")
                if url:
                    found.append(
                        {
                            "title": getattr(result, "title", "") or "",
                            "url": url,
                            "page_age": getattr(result, "page_age", "") or "",
                        }
                    )
        elif block.type == "text":
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", "")
                if url:
                    found.append(
                        {
                            "title": getattr(citation, "title", "") or "",
                            "url": url,
                            "page_age": "",
                        }
                    )
    return found


def _dedupe(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for source in sources:
        if source["url"] in seen:
            continue
        seen.add(source["url"])
        unique.append(source)
    return unique
