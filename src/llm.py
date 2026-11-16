"""The OpenAI-compatible LLM client.

The endpoint is configurable because the lab must not be tied to one vendor:
``LLM_BASE_URL``, ``LLM_API_KEY`` and ``LLM_MODEL`` are read from the
environment (or a ``.env`` file), and any endpoint that speaks the OpenAI
chat-completions protocol works.

Two things this module is careful about:

* **JSON parsing.** The model is asked for a JSON object, but a
  server-side compatible endpoint may return the object wrapped in a
  Markdown fence or surrounded by prose. The parser therefore tries the
  strict decode first and then falls back to locating the outermost braces.
* **Errors are not hidden.** A failed call raises, with the status code and
  the response body, so a run can record a provider error instead of
  reporting a fabricated answer.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


class LLMError(Exception):
    """Raised when the language model cannot be reached or does not comply."""


def load_env() -> None:
    """Load ``.env`` into the environment without overwriting existing values."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is optional
        return
    load_dotenv(override=False)


def build_client():
    """Create the OpenAI client from the environment."""
    load_env()
    from openai import OpenAI

    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")
    if not model:
        raise LLMError(
            "LLM_MODEL chưa được đặt. Ví dụ: LLM_MODEL=gpt-4o-mini trong file .env"
        )
    client = OpenAI(base_url=base_url, api_key=api_key or "not-needed")
    return client, model


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract a JSON object from a model response.

    Tries the strict decode, then a fenced code block, then the outermost
    braces. Two recoveries are applied before giving up, because a model that
    ignores the format should not cost the whole question:

    * a malformed object whose string values contain unescaped quotes is
      repaired, because the action and the query are still readable;
    * a response that reads like a final answer (prose carrying a citation
      id) is wrapped into an ``answer`` action.

    When nothing can be recovered, :class:`LLMError` is raised so that the
    failure is recorded rather than silently turned into a refusal.
    """
    if not text or not text.strip():
        raise LLMError("model trả về nội dung rỗng")

    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        parsed = _try_json(candidate)
        if parsed is not None:
            return parsed

    repaired = _repair_json(text)
    if repaired is not None:
        return repaired

    if _looks_like_answer(text):
        return {"action": "answer", "answer": text.strip(), "citations": []}

    raise LLMError(f"không tìm thấy JSON object trong phản hồi: {text[:300]!r}")


def _try_json(candidate: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


#: A quote that ends a value: not preceded by a backslash, and followed by a
#: comma or a closing brace.
_VALUE_END = re.compile(r'(?<!\\)"(?=[,}])')

#: A key at the start of a value: "name" followed by a colon.
_KEY = re.compile(r'"(\w+)"\s*:\s*')


def _repair_json(text: str) -> dict[str, Any] | None:
    """Repair a JSON object whose string values contain unescaped quotes.

    A model asked for a JSON object sometimes emits a query containing double
    quotes without escaping them, which makes the whole object unparseable.
    The repair scans the ``"name": value`` pairs directly: each value runs
    from after the colon to the next unescaped quote that is followed by a
    comma or a closing brace. Quotes that turn out to be part of the value
    itself are stripped rather than ending it.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    body = text[start : end + 1]

    result: dict[str, Any] = {}
    for key_match in _KEY.finditer(body):
        key = key_match.group(1)
        rest = body[key_match.end() :]
        terminator = _VALUE_END.search(rest)
        raw = (rest[: terminator.start()] if terminator else rest).strip()
        # The value may itself be quoted; a stray quote on either side is
        # part of the damage and is removed.
        if len(raw) > 1 and raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        elif raw.startswith('"'):
            raw = raw[1:]
        elif raw.endswith('"'):
            raw = raw[:-1]
        result[key] = raw.replace('\\"', '"').replace("\\\\", "\\")
    if "action" not in result:
        return None
    # The action must be a bare word. A damaged value such as '" "search"'
    # carries stray quotes around it, so the word itself is taken.
    action = re.search(r"[A-Za-z_]+", result["action"])
    result["action"] = action.group(0) if action else result["action"]
    return result


def _looks_like_answer(text: str) -> bool:
    """True when a non-JSON response reads like a final answer.

    A response that carries a citation id is unmistakably an answer.
    """
    return bool(re.search(r"\[[A-Za-z]:[^\]]+\]", text))


def chat_json(
    client,
    model: str,
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    retries: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the model and return its JSON payload plus a usage record.

    The usage record carries the token counts and the latency, so that the
    cost of a configuration can be reported rather than estimated.
    """
    import time

    last_error: Exception | None = None
    for attempt in range(retries):
        started = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "system", "content": system}, *messages],
                "temperature": temperature,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            response = client.chat.completions.create(**kwargs)
            elapsed_ms = (time.perf_counter() - started) * 1000

            message = response.choices[0].message
            content = message.content or ""
            payload = parse_json_object(content)
            usage = {
                "elapsed_ms": round(elapsed_ms, 1),
                "prompt_tokens": getattr(response.usage, "prompt_tokens", None),
                "completion_tokens": getattr(response.usage, "completion_tokens", None),
                "total_tokens": getattr(response.usage, "total_tokens", None),
            }
            return payload, usage
        except Exception as exc:  # network errors, rate limits, parse failures
            last_error = exc
            if attempt == retries - 1:
                break
            # Linear backoff: enough for a 429 without stalling the run.
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"gọi model thất bại sau {retries} lần thử: {last_error}")
