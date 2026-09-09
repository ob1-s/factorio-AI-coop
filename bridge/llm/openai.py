"""Small dependency-free OpenAI-compatible streaming client.

Only the provider boundary lives here. The daemon owns conversation history,
UDP framing, and retry/concurrency policy; this module turns an OpenAI
``/chat/completions`` SSE response into text deltas.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Iterator, List, Mapping, Optional


DEFAULT_BASE_URL = os.environ.get("COMPANION_BASE_URL", "http://127.0.0.1:8645/v1")
DEFAULT_API_KEY = os.environ.get("COMPANION_API_KEY", "hermes-local")
DEFAULT_MODEL = os.environ.get(
    "COMPANION_MODEL", "inclusionai/ling-3.0-flash-sante:free"
)


class LLMError(RuntimeError):
    """A provider failure safe to report at the daemon protocol boundary."""

    def __init__(self, message: str, *, code: str = "MODEL_ERROR") -> None:
        super().__init__(message)
        self.code = code


def _as_text(value: Any) -> str:
    """Extract text from the content shapes used by OpenAI-compatible APIs."""

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces: List[str] = []
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "".join(pieces)
    return ""


def iter_sse_data(lines: Iterable[Any]) -> Iterator[str]:
    """Yield SSE ``data`` event bodies from a response line iterator.

    OpenAI normally emits one JSON data line per event, but honoring blank-line
    event boundaries and multiline ``data:`` fields makes this work with
    proxies and test servers too.
    """

    event_data: List[str] = []
    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", "replace")
        else:
            line = str(raw_line)
        line = line.rstrip("\r\n")

        if line == "":
            if event_data:
                yield "\n".join(event_data)
                event_data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            value = line[5:]
            event_data.append(value[1:] if value.startswith(" ") else value)
        # event/id/retry and provider-specific non-SSE lines are intentionally
        # ignored. They carry no model text.

    if event_data:
        yield "\n".join(event_data)


def _provider_error(detail: Any) -> str:
    """Return a short, single-line provider error without request metadata."""

    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", "replace")
    if not isinstance(detail, str):
        detail = str(detail)
    detail = detail.strip()
    try:
        obj = json.loads(detail)
    except (TypeError, ValueError):
        obj = None
    if isinstance(obj, Mapping):
        error = obj.get("error", obj)
        if isinstance(error, Mapping):
            detail = str(error.get("message") or error.get("detail") or error)
        elif error:
            detail = str(error)
    detail = " ".join(detail.split())
    return detail[:500] or "provider returned no error details"


class OpenAICompatibleClient:
    """OpenAI chat-completions client with strict SSE streaming."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
        *,
        temperature: float = 0.7,
        user_agent: str = "factorio-companion-daemon/0.1",
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.api_key = DEFAULT_API_KEY if api_key is None else api_key
        self.timeout = float(timeout if timeout is not None else _env_timeout())
        self.temperature = temperature
        self.user_agent = user_agent

        if not self.base_url:
            raise ValueError("base_url must not be empty")
        if not self.model:
            raise ValueError("model must not be empty")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    @staticmethod
    def normalize_messages(messages: Iterable[Mapping[str, Any]], system: Optional[str] = None) -> List[dict]:
        """Normalize daemon messages and the older Gemini-style message shape."""

        normalized: List[dict] = []
        if system:
            normalized.append({"role": "system", "content": system})

        for message in messages:
            if not isinstance(message, Mapping):
                raise LLMError("conversation contains an invalid message", code="MODEL_INPUT")
            raw_role = message.get("role", "user")
            role = "assistant" if isinstance(raw_role, str) and raw_role in {"model", "assistant"} else str(raw_role)
            if role not in {"system", "user", "assistant", "tool"}:
                raise LLMError("conversation contains an invalid role", code="MODEL_INPUT")

            content = message.get("content")
            if content is None:
                parts = message.get("parts") or []
                if isinstance(parts, list):
                    content = "".join(
                        part.get("text", "")
                        for part in parts
                        if isinstance(part, Mapping) and isinstance(part.get("text", ""), str)
                    )
                else:
                    content = ""
            if not isinstance(content, (str, list)):
                raise LLMError("conversation contains non-text content", code="MODEL_INPUT")
            normalized.append({"role": role, "content": content})
        return normalized

    def _request(self, payload: Mapping[str, Any], timeout: float):
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=timeout)

    @staticmethod
    def _delta_from_event(event: Mapping[str, Any]) -> str:
        if event.get("error"):
            raise LLMError(_provider_error(event), code="MODEL_ERROR")

        pieces: List[str] = []
        choices = event.get("choices")
        if not isinstance(choices, list):
            return ""
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                pieces.append(_as_text(delta.get("content")))
                # A few OpenAI-compatible endpoints use delta.text.
                pieces.append(_as_text(delta.get("text")))
            else:
                # This also accepts a provider that wraps a complete message
                # in a streamed event.
                message = choice.get("message")
                if isinstance(message, Mapping):
                    pieces.append(_as_text(message.get("content")))
                pieces.append(_as_text(choice.get("text")))
        return "".join(pieces)

    def stream(
        self,
        messages: Iterable[Mapping[str, Any]],
        *,
        system: Optional[str] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate text and call ``on_delta`` for each provider text chunk.

        A stream interrupted after partial output is still an error. The daemon
        must not commit or advertise an incomplete assistant turn as complete.
        """

        request_timeout = float(timeout if timeout is not None else self.timeout)
        if request_timeout <= 0:
            raise LLMError("model timeout must be greater than zero", code="MODEL_TIMEOUT")
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": self.normalize_messages(messages, system),
            "stream": True,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        parts: List[str] = []
        saw_done = False
        try:
            with self._request(payload, request_timeout) as response:
                for data in iter_sse_data(response):
                    if data.strip() == "[DONE]":
                        saw_done = True
                        break
                    try:
                        event = json.loads(data)
                    except (TypeError, ValueError) as exc:
                        raise LLMError(
                            f"provider sent invalid streaming data: {exc}",
                            code="MODEL_PROTOCOL",
                        ) from exc
                    if not isinstance(event, Mapping):
                        raise LLMError("provider sent a non-object streaming event", code="MODEL_PROTOCOL")
                    text = self._delta_from_event(event)
                    if text:
                        parts.append(text)
                        if on_delta:
                            on_delta(text)
                if not saw_done:
                    raise LLMError(
                        "model stream ended before completion",
                        code="MODEL_STREAM",
                    )
        except LLMError:
            raise
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read()
            except Exception:
                detail = ""
            raise LLMError(
                f"model HTTP {exc.code}: {_provider_error(detail)}",
                code="MODEL_HTTP",
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMError(f"model request timed out: {_provider_error(exc)}", code="MODEL_TIMEOUT") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"model request failed: {_provider_error(exc)}", code="MODEL_UNAVAILABLE") from exc
        except Exception as exc:
            raise LLMError(f"model stream failed: {_provider_error(exc)}", code="MODEL_ERROR") from exc

        text = "".join(parts)
        if not text:
            raise LLMError("model returned an empty response", code="MODEL_EMPTY")
        return text

    # Friendly aliases for callers migrating from the old bridge client.
    stream_text = stream


def _env_timeout() -> float:
    raw = os.environ.get("COMPANION_LLM_TIMEOUT", os.environ.get("COMPANION_TIMEOUT", "120"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 120.0
    return value if value > 0 else 120.0


OpenAIClient = OpenAICompatibleClient
Llm = OpenAICompatibleClient
LlmError = LLMError
