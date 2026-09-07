"""OpenAI-compatible backend pointed at the local Hermes proxy (no real key needed).

Provides the same ask() interface as gemini.py so agent.py can swap backends.
Targets http://127.0.0.1:8645/v1 (Hermes). Free models like
'<org>/<model>:free' do not require a valid API key.
"""
import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("COMPANION_BASE_URL", "http://127.0.0.1:8645/v1")
API_KEY = os.environ.get("COMPANION_API_KEY", "hermes-local")
DEFAULT_MODEL = os.environ.get(
    "COMPANION_MODEL", "inclusionai/ling-3.0-flash-sante:free"
)


class LlmError(Exception):
    pass


class Llm:
    """Tiny OpenAI chat-completions client with SSE streaming."""
    def __init__(self, base_url=BASE_URL, api_key=API_KEY):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _messages(self, contents, system):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        for part in contents:
            role = "assistant" if part.get("role") == "model" else "user"
            text = "".join(p.get("text", "") for p in part.get("parts") or [])
            msgs.append({"role": role, "content": text})
        return msgs

    def _post(self, payload, timeout):
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}",
                     # the proxy's auth layer rejects urllib's default UA
                     "User-Agent": "companion-agent/0.1.3"},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=timeout)

    def stream(self, contents, system=None, on_delta=None, temperature=0.7, timeout=120, model=None):
        payload = {
            "model": model or DEFAULT_MODEL,
            "messages": self._messages(contents, system),
            "temperature": temperature,
            "stream": True,
        }
        full_parts = []
        try:
            with self._post(payload, timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        if "OPENROUTER PROCESSING" in line:
                            continue
                    blob = line[5:].strip()
                    if not blob or blob == "[DONE]":
                        continue
                    try:
                        obj = json.loads(blob)
                    except ValueError:
                        continue
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            full_parts.append(text)
                            if on_delta:
                                on_delta(text)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if full_parts:
                return "".join(full_parts), "partial"
            raise LlmError(f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            if full_parts:
                return "".join(full_parts), "partial"
            raise LlmError(f"stream failed: {exc}")
        text = "".join(full_parts)
        if not text:
            raise LlmError("empty stream response")
        return text, "stream"

    def complete(self, contents, system=None, temperature=0.7, timeout=90, model=None):
        payload = {
            "model": model or DEFAULT_MODEL,
            "messages": self._messages(contents, system),
            "temperature": temperature,
            "stream": False,
        }
        try:
            with self._post(payload, timeout) as resp:
                out = json.load(resp)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LlmError(f"HTTP {exc.code}: {detail}")
        choices = out.get("choices") or []
        if not choices:
            raise LlmError(f"no choices: {str(out)[:200]}")
        content = (choices[0].get("message") or {}).get("content")
        if not content:
            raise LlmError("empty response")
        return content.strip()


def ask(model, contents, system=None, on_delta=None, temperature=0.7):
    """Stream via the local proxy, fall back to non-streaming."""
    client = Llm()
    try:
        return client.stream(contents, system, on_delta, temperature, model=model)
    except LlmError:
        text = client.complete(contents, system, temperature, model=model)
        if on_delta:
            on_delta(text)
        return text, "complete"