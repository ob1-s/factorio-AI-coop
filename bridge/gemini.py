"""Minimal dependency-free Google Gemini REST client with SSE streaming."""
import json
import os
import urllib.error
import urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-flash-latest"


class GeminiError(Exception):
    pass


def api_key():
    for var in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GOOGLE_API_KEY"):
        value = os.environ.get(var)
        if value:
            return value
    raise GeminiError("no API key found: set GEMINI_API_KEY")


def _post(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _payload(contents, system, temperature):
    payload = {"contents": contents, "generationConfig": {"temperature": temperature}}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    return payload


def stream_text(model, contents, system=None, on_delta=None, temperature=0.7, timeout=120):
    """Stream a completion over SSE. Returns (text, mode).

    Calls on_delta(delta) for each text chunk. If the stream dies after some
    text was already delivered, returns the partial text with mode="partial";
    if it dies before any text, raises GeminiError (caller may fall back).
    """
    url = f"{BASE}/models/{model}:streamGenerateContent?alt=sse&key={api_key()}"
    parts = []
    try:
        resp = _post(url, _payload(contents, system, temperature), timeout)
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if not blob or blob == "[DONE]":
                    continue
                try:
                    obj = json.loads(blob)
                except ValueError:
                    continue
                if obj.get("error"):
                    raise GeminiError(obj["error"].get("message", "api error"))
                candidates = obj.get("candidates") or []
                if not candidates:
                    continue
                for part in (candidates[0].get("content") or {}).get("parts") or []:
                    text = part.get("text")
                    if text:
                        parts.append(text)
                        if on_delta:
                            on_delta(text)
    except GeminiError:
        raise
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if parts:
            return "".join(parts), "partial"
        raise GeminiError(f"HTTP {exc.code}: {detail}")
    except Exception as exc:
        if parts:
            return "".join(parts), "partial"
        raise GeminiError(f"stream failed: {exc}")
    text = "".join(parts)
    if not text:
        raise GeminiError("empty stream response")
    return text, "stream"


def generate_text(model, contents, system=None, temperature=0.7, timeout=60):
    """Non-streaming fallback."""
    url = f"{BASE}/models/{model}:generateContent?key={api_key()}"
    try:
        with _post(url, _payload(contents, system, temperature), timeout) as resp:
            out = json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise GeminiError(f"HTTP {exc.code}: {detail}")
    if out.get("error"):
        raise GeminiError(out["error"].get("message", "api error"))
    candidates = out.get("candidates") or []
    if not candidates:
        raise GeminiError(f"no candidates: {str(out)[:200]}")
    pieces = []
    for part in (candidates[0].get("content") or {}).get("parts") or []:
        if part.get("text"):
            pieces.append(part["text"])
    text = "".join(pieces).strip()
    if not text:
        raise GeminiError("empty response")
    return text


def ask(model, contents, system=None, on_delta=None, temperature=0.7):
    """Stream if possible, fall back to non-streaming. Returns (text, mode)."""
    try:
        return stream_text(model, contents, system, on_delta, temperature)
    except GeminiError:
        text = generate_text(model, contents, system, temperature)
        if on_delta:
            on_delta(text)
        return text, "generate"
