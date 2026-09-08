"""Small, dependency-free test harnesses for the companion wire contract.

The harness deliberately does not implement a daemon.  ``DaemonProcess`` starts
the production ``bridge.daemon`` entry point, while ``FakeLlmServer`` and
``UdpPeer`` stand in for the two external endpoints.  Keeping those roles
separate makes a missing or broken production daemon fail loudly instead of
turning the integration tests into a self-test.
"""

from __future__ import annotations

import importlib.util
import json
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable

from bridge.protocol import create_packet, parse_packet, validate_message


REPO_ROOT = Path(__file__).resolve().parents[1]


class HarnessError(RuntimeError):
    """Raised for a test harness setup or transport error."""


class ProductionDaemonMissing(HarnessError):
    """Raised when the production daemon entry point has not landed yet."""


@dataclass
class FakeResponse:
    """One planned response from the fake OpenAI-compatible server."""

    text: str = "ack"
    chunks: tuple[str, ...] | None = None
    status: int | None = None
    delay_before: float = 0.0
    delay_between: float = 0.0
    stream: bool = True

    def resolved_chunks(self) -> tuple[str, ...]:
        if self.chunks is not None:
            return self.chunks
        return (self.text,)


class _FakeLlmHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, _format: str, *_args: Any) -> None:
        # Test output should contain assertions and production logs, not an
        # access log for every SSE line.
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        fake = self.server.fake_llm  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"invalid json"}}')
            return

        fake.requests.put(request)
        plan = fake.next_response(request)
        if plan.delay_before:
            time.sleep(plan.delay_before)

        if plan.status is not None:
            body = json.dumps(
                {"error": {"message": f"fake LLM status {plan.status}"}},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(plan.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not plan.stream or request.get("stream") is False:
            body = json.dumps(
                {
                    "choices": [{"message": {"content": plan.text}}],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in plan.resolved_chunks():
            event = {
                "choices": [{"delta": {"content": chunk}, "index": 0}],
            }
            line = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode(
                "utf-8"
            )
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if plan.delay_between:
                time.sleep(plan.delay_between)
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


class FakeLlmServer:
    """Threaded local OpenAI-compatible SSE server used by integration tests."""

    def __init__(self, responses: Iterable[FakeResponse] = ()) -> None:
        self._responses: queue.Queue[FakeResponse] = queue.Queue()
        for response in responses:
            self._responses.put(response)
        self.requests: queue.Queue[dict[str, Any]] = queue.Queue()
        self._default = FakeResponse(text="fake response", chunks=("fake response",))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlmHandler)
        self._server.fake_llm = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="fake-llm-server",
            daemon=True,
        )
        self._thread.start()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def base_url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}/v1"

    def add_response(self, response: FakeResponse) -> None:
        self._responses.put(response)

    def next_response(self, _request: dict[str, Any]) -> FakeResponse:
        try:
            return self._responses.get_nowait()
        except queue.Empty:
            return self._default

    def get_request(self, timeout: float = 2.0) -> dict[str, Any]:
        try:
            return self.requests.get(timeout=timeout)
        except queue.Empty as exc:
            raise HarnessError("fake LLM did not receive a request") from exc

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def __enter__(self) -> "FakeLlmServer":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class UdpPeer:
    """A fake Factorio UDP endpoint with packet capture and wait helpers."""

    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.address = ("127.0.0.1", int(self.socket.getsockname()[1]))
        self.received: list[dict[str, Any]] = []
        self.world_id = "world-a"
        self.client_session_id = ""

    @property
    def port(self) -> int:
        return self.address[1]

    def send_raw(self, target: tuple[str, int], raw: bytes | str) -> None:
        wire = raw.encode("utf-8") if isinstance(raw, str) else raw
        self.socket.sendto(wire, target)

    def send_packet(
        self,
        target: tuple[str, int],
        msg_type: str,
        *,
        exchange_id: str = "",
        seq: int = 0,
        payload: dict[str, Any] | None = None,
        tick: int = 0,
    ) -> None:
        self.send_raw(
            target,
            create_packet(
                msg_type,
                exchange_id=exchange_id,
                seq=seq,
                payload=payload,
                tick=tick,
            ),
        )

    def receive(self, timeout: float = 0.25) -> dict[str, Any] | None:
        self.socket.settimeout(max(0.001, timeout))
        try:
            raw, _address = self.socket.recvfrom(256 * 1024)
        except socket.timeout:
            return None
        packet, error = parse_packet(raw)
        if packet is None:
            raise HarnessError(f"production sent invalid packet: {error}")
        try:
            packet = validate_message(packet)
        except Exception as exc:
            raise HarnessError(f"production sent schema-invalid packet: {exc}") from exc
        self.received.append(packet)
        return packet

    def wait_for(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            packet = self.receive(min(0.1, max(0.001, deadline - time.monotonic())))
            if packet is not None and predicate(packet):
                return packet
        raise HarnessError(
            f"timed out waiting for UDP packet; received types="
            f"{[p.get('type') for p in self.received]!r}; packets={self.received!r}"
        )

    def wait_for_type(
        self,
        msg_type: str,
        *,
        exchange_id: str | None = None,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        return self.wait_for(
            lambda packet: packet.get("type") == msg_type
            and (exchange_id is None or packet.get("exchange_id") == exchange_id),
            timeout=timeout,
        )

    def hello(
        self,
        target: tuple[str, int],
        *,
        world_id: str = "world-a",
        conversation_head: str = "turn_0",
        capabilities: list[str] | None = None,
        client_session_id: str | None = "client-test-session",
    ) -> None:
        self.world_id = world_id
        self.client_session_id = client_session_id or ""
        payload = {
            "mod_version": "test-mod",
            "factorio_version": "2.0",
            "world_id": world_id,
            "conversation_head": conversation_head,
            "player_name": "test-engineer",
            "capabilities": capabilities or ["telemetry"],
        }
        if client_session_id is not None:
            payload["client_session_id"] = client_session_id
        self.send_packet(
            target,
            "hello",
            seq=1,
            payload=payload,
        )

    def user_message(
        self,
        target: tuple[str, int],
        *,
        world_id: str = "world-a",
        turn_id: str = "turn_1",
        parent_turn_id: str = "turn_0",
        text: str = "hello",
        context: dict[str, Any] | None = None,
        exchange_id: str | None = None,
        request_id: str | None = None,
        client_session_id: str | None = None,
    ) -> str:
        exchange = exchange_id or f"ex_{world_id}_{turn_id}"
        session_id = self.client_session_id if client_session_id is None else client_session_id
        request = request_id or f"request_{turn_id}"
        payload = {
            "world_id": world_id,
            "turn_id": turn_id,
            "client_turn_id": turn_id,
            "request_id": request,
            "parent_turn_id": parent_turn_id,
            "text": text,
            "context": context or {"capabilities": ["telemetry"]},
        }
        if session_id:
            payload["client_session_id"] = session_id
        self.send_packet(
            target,
            "user_message",
            exchange_id=exchange,
            seq=1,
            payload=payload,
        )
        return exchange

    def heartbeat(
        self,
        target: tuple[str, int],
        *,
        seq: int = 1,
        tick: int = 0,
        world_id: str | None = None,
        client_session_id: str | None = None,
    ) -> None:
        payload = {"ping_tick": tick, "state": "thinking"}
        effective_world_id = self.world_id if world_id is None else world_id
        effective_session_id = (
            self.client_session_id
            if client_session_id is None
            else client_session_id
        )
        if effective_world_id:
            payload["world_id"] = effective_world_id
        if effective_session_id:
            payload["client_session_id"] = effective_session_id
        self.send_packet(
            target,
            "heartbeat",
            seq=seq,
            tick=tick,
            payload=payload,
        )

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> "UdpPeer":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class CosmeticStream:
    """Test-side model of the UI's cosmetic delta and authoritative end rules."""

    def __init__(self, exchange_id: str) -> None:
        self.exchange_id = exchange_id
        self.started = False
        self.last_delta_seq = 0
        self.cosmetic_text = ""
        self.final_text: str | None = None
        self.ignored_sequences: list[int] = []

    def consume(self, packet: dict[str, Any]) -> None:
        if packet.get("exchange_id") != self.exchange_id:
            return
        msg_type = packet.get("type")
        payload = packet.get("payload") or {}
        if msg_type == "assistant_start":
            self.started = True
        elif msg_type == "assistant_delta":
            seq = packet["seq"]
            if seq <= self.last_delta_seq:
                self.ignored_sequences.append(seq)
                return
            self.last_delta_seq = seq
            self.cosmetic_text += payload.get("delta", "")
        elif msg_type == "assistant_end":
            self.final_text = payload.get("full_text", "")
            # The end packet is authoritative and repairs dropped or late
            # cosmetic deltas.
            self.cosmetic_text = self.final_text


class DaemonProcess:
    """Launch the production daemon as a subprocess for wire-level tests."""

    def __init__(
        self,
        *,
        port: int,
        llm_base_url: str,
        db_path: Path,
        model: str = "fake-model",
    ) -> None:
        self.port = port
        self.llm_base_url = llm_base_url
        self.db_path = db_path
        self.model = model
        self.process: subprocess.Popen[str] | None = None
        self.output: list[str] = []
        self._reader: threading.Thread | None = None
        self._bound_port: int | None = None

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("bridge.daemon") is not None

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        # The database is intentionally environment-configured because the
        # product CLI keeps provider/UDP options separate from data location.
        env["COMPANION_DB_PATH"] = str(self.db_path)
        env["COMPANION_API_KEY"] = "test-key"
        env["COMPANION_MODEL"] = self.model
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _read_output(self, pipe: Any) -> None:
        for line in iter(pipe.readline, ""):
            self.output.append(line.rstrip("\n"))

    def start(self) -> "DaemonProcess":
        if not self.available():
            raise ProductionDaemonMissing(
                "bridge.daemon is missing; the UDP daemon is a production prerequisite "
                "for Workstream E integration tests"
            )
        env = self._environment()
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bridge.daemon",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--base-url",
                self.llm_base_url,
                "--model",
                self.model,
                "--api-key",
                "test-key",
                "--timeout",
                "2",
                "--log-level",
                "DEBUG",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert self.process.stdout is not None
        self._reader = threading.Thread(
            target=self._read_output,
            args=(self.process.stdout,),
            name="companion-daemon-output",
            daemon=True,
        )
        self._reader.start()
        # Give an immediately-crashing process a chance to expose its reason;
        # UDP itself is the readiness probe, so do not sleep for a fixed daemon
        # startup interval here.
        time.sleep(0.05)
        self.assert_running()
        if self.port == 0:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and self._bound_port is None:
                for line in self.output:
                    match = re.search(r"UDP daemon listening on [^:]+:(\d+)", line)
                    if match:
                        self._bound_port = int(match.group(1))
                        break
                if self._bound_port is None:
                    time.sleep(0.01)
            if self._bound_port is None:
                raise HarnessError(
                    f"daemon did not report its ephemeral port; output={self.output!r}"
                )
        return self

    @property
    def address(self) -> tuple[str, int]:
        return ("127.0.0.1", self._bound_port or self.port)

    def assert_running(self) -> None:
        if self.process is None:
            raise HarnessError("daemon was not started")
        code = self.process.poll()
        if code is not None:
            raise HarnessError(
                f"bridge.daemon exited with status {code}; output={self.output!r}"
            )

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2.0)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self._reader is not None:
            self._reader.join(timeout=1.0)

    def __enter__(self) -> "DaemonProcess":
        return self.start()

    def __exit__(self, *_args: Any) -> None:
        self.stop()
