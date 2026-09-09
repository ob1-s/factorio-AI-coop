"""The Factorio Companion UDP daemon.

The daemon has one product path:

    Factorio UDP -> validated packet -> conversation/session -> LLM SSE
        -> batched UDP deltas -> Factorio UDP

The receive loop never waits for the model. Model calls run in daemon threads,
so heartbeats continue while a response is being generated.

Run it from the repository root with::

    python -m bridge.daemon

The old RCON bridge is intentionally not imported here. It remains available
only for legacy diagnostics while this module owns the product entrypoint.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import select
import socket
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from . import protocol
from .llm import LLMError, OpenAICompatibleClient
from .session import SessionManager
from .store import default_database_path


LOG = logging.getLogger("companion.daemon")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 34200
DEFAULT_FACTORIO_PORT = 34199
DEFAULT_HEARTBEAT_INTERVAL = 2.0
DEFAULT_DELTA_INTERVAL = 0.10
DEFAULT_DELTA_CHARS = 32
DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_CACHED_EXCHANGES = 256

SYSTEM_PROMPT = """You are Companion, a friendly AI copilot living inside the player's Factorio game through the FactorioCompanion mod. The player talks to you through a small in-game chat window and you reply in that window.

Style rules:
- Keep replies short and conversational: 1-4 sentences. This is a chat bubble, not a report.
- Plain text only: no markdown headers, no long bullet lists, no code fences.
- You are given live game context JSON (player state, research, sometimes more). Ground your answers in it and do not invent items, entities or counts that are not in the context.
- You know Factorio (Space Age / 2.0). If unsure about something the context does not show, say so briefly and give your best general advice.
- Light personality is welcome: you are the factory's helpful companion, not a corporate bot."""

Address = Tuple[str, int]


def _env_int(*names: str, default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        try:
            return int(raw)
        except ValueError:
            LOG.warning("ignoring invalid integer in %s", name)
    return default


def _env_float(*names: str, default: float) -> float:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        try:
            return float(raw)
        except ValueError:
            LOG.warning("ignoring invalid number in %s", name)
    return default


@dataclass
class DaemonConfig:
    """Runtime configuration for the UDP and provider boundaries."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_DAEMON_PORT
    factorio_port: int = DEFAULT_FACTORIO_PORT
    db_path: Optional[str] = None
    base_url: str = "http://127.0.0.1:8645/v1"
    model: str = "inclusionai/ling-3.0-flash-sante:free"
    api_key: str = "hermes-local"
    llm_timeout: float = 120.0
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    delta_interval: float = DEFAULT_DELTA_INTERVAL
    delta_chars: int = DEFAULT_DELTA_CHARS
    max_workers: int = DEFAULT_MAX_WORKERS
    max_cached_exchanges: int = DEFAULT_MAX_CACHED_EXCHANGES
    max_history: int = 12
    daemon_version: str = "0.2.0"
    system_prompt: str = SYSTEM_PROMPT

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        """Build configuration from environment variables.

        ``COMPANION_*`` names are the product names. A couple of legacy aliases
        are accepted for local development, but no RCON setting is consulted.
        """

        from .llm.openai import DEFAULT_API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL

        return cls(
            host=os.environ.get(
                "COMPANION_UDP_HOST",
                os.environ.get(
                    "COMPANION_LISTEN_HOST",
                    os.environ.get(
                        "COMPANION_DAEMON_HOST",
                        os.environ.get("COMPANION_HOST", DEFAULT_HOST),
                    ),
                ),
            ),
            port=_env_int(
                "COMPANION_UDP_PORT",
                "COMPANION_LISTEN_PORT",
                "COMPANION_DAEMON_PORT",
                "COMPANION_PORT",
                default=DEFAULT_DAEMON_PORT,
            ),
            factorio_port=_env_int("COMPANION_FACTORIO_PORT", default=DEFAULT_FACTORIO_PORT),
            db_path=os.environ.get("COMPANION_DB_PATH", os.environ.get("COMPANION_DATABASE_PATH")),
            base_url=os.environ.get(
                "COMPANION_BASE_URL",
                os.environ.get("COMPANION_LLM_BASE_URL", DEFAULT_BASE_URL),
            ),
            model=os.environ.get("COMPANION_MODEL", DEFAULT_MODEL),
            api_key=os.environ.get("COMPANION_API_KEY", DEFAULT_API_KEY),
            llm_timeout=_env_float("COMPANION_LLM_TIMEOUT", "COMPANION_TIMEOUT", default=120.0),
            heartbeat_interval=_env_float(
                "COMPANION_HEARTBEAT_INTERVAL", default=DEFAULT_HEARTBEAT_INTERVAL
            ),
            delta_interval=_env_float("COMPANION_DELTA_INTERVAL", default=DEFAULT_DELTA_INTERVAL),
            delta_chars=_env_int("COMPANION_DELTA_CHARS", default=DEFAULT_DELTA_CHARS),
            max_workers=_env_int("COMPANION_MAX_WORKERS", default=DEFAULT_MAX_WORKERS),
            max_cached_exchanges=_env_int(
                "COMPANION_MAX_CACHED_EXCHANGES", default=DEFAULT_MAX_CACHED_EXCHANGES
            ),
            max_history=_env_int("COMPANION_MAX_HISTORY", default=12),
        )

    @property
    def timeout(self) -> float:
        """Compatibility spelling for callers that call it simply timeout."""

        return self.llm_timeout

    def validate(self) -> None:
        if not self.host:
            raise ValueError("UDP host must not be empty")
        if not 0 <= self.port <= 65535:
            raise ValueError("UDP port must be between 0 and 65535")
        if not 0 <= self.factorio_port <= 65535:
            raise ValueError("Factorio port must be between 0 and 65535")
        if self.llm_timeout <= 0:
            raise ValueError("LLM timeout must be greater than zero")
        if self.heartbeat_interval <= 0:
            raise ValueError("heartbeat interval must be greater than zero")
        if self.delta_interval <= 0:
            raise ValueError("delta interval must be greater than zero")
        if self.delta_chars <= 0:
            raise ValueError("delta character threshold must be greater than zero")
        if self.max_workers <= 0:
            raise ValueError("max_workers must be greater than zero")
        if self.max_cached_exchanges < 0:
            raise ValueError("max_cached_exchanges must not be negative")
        if self.max_history <= 0:
            raise ValueError("max_history must be greater than zero")


# A descriptive alias is convenient for integrations that call this a daemon
# settings object rather than a config object.
DaemonSettings = DaemonConfig


@dataclass
class Peer:
    world_id: str
    address: Address
    generation: int
    conversation_head: str
    capabilities: Tuple[str, ...] = ()
    player_name: str = ""
    client_session_id: str = ""
    last_seen: float = field(default_factory=time.monotonic)
    control_seq: int = 1


@dataclass
class RequestState:
    world_id: str
    exchange_id: str
    requested_turn_id: str
    parent_turn_id: str
    user_text: str
    context: Dict[str, Any]
    canonical_turn_id: str
    request_id: str
    client_session_id: str
    peer_generation: int
    address: Address
    fingerprint: str
    started_at: float
    session_started: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    next_seq: int = 2  # assistant_start is sequence 1

    def take_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq


@dataclass
class CompletedExchange:
    fingerprint: str
    canonical_turn_id: str
    requested_turn_id: str
    request_id: str
    full_text: str
    generation: int
    final_seq: int


class DeltaBatcher:
    """Coalesce provider chunks into bounded, cadence-controlled UDP deltas."""

    def __init__(
        self,
        emit: Callable[[str], None],
        *,
        interval: float = DEFAULT_DELTA_INTERVAL,
        max_chars: int = DEFAULT_DELTA_CHARS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.emit = emit
        self.interval = interval
        self.max_chars = max_chars
        self.clock = clock
        self.parts: List[str] = []
        self.char_count = 0
        self.last_flush = clock()

    def _emit(self) -> None:
        if not self.parts:
            return
        text = "".join(self.parts)
        self.parts.clear()
        self.char_count = 0
        self.last_flush = self.clock()
        self.emit(text)

    def add(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            return
        now = self.clock()
        if self.parts and now - self.last_flush >= self.interval:
            self._emit()

        offset = 0
        while offset < len(text):
            room = self.max_chars - self.char_count
            piece = text[offset : offset + room]
            self.parts.append(piece)
            self.char_count += len(piece)
            offset += len(piece)
            if self.char_count >= self.max_chars:
                self._emit()

    def flush(self) -> None:
        self._emit()


class CompanionDaemon:
    """Non-blocking UDP server with one in-flight request per world."""

    def __init__(
        self,
        config: Optional[DaemonConfig] = None,
        *,
        llm: Optional[Any] = None,
        session_manager: Optional[Any] = None,
        sock: Optional[socket.socket] = None,
        clock: Callable[[], float] = time.monotonic,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config or DaemonConfig.from_env()
        self.config.validate()
        self.llm = llm or OpenAICompatibleClient(
            base_url=self.config.base_url,
            model=self.config.model,
            api_key=self.config.api_key,
            timeout=self.config.llm_timeout,
        )
        self._owns_sessions = session_manager is None
        if session_manager is not None:
            self.sessions = session_manager
        else:
            # The library facade intentionally defaults to in-memory SQLite so
            # short-lived callers do not create files. The executable daemon,
            # however, must preserve completed dialogue across restarts.
            self.sessions = SessionManager(
                db_path=self.config.db_path or str(default_database_path())
            )
        self._sessions_closed = False
        self.sock = sock
        self._owns_socket = sock is None
        self._clock = clock
        self.log = logger or LOG
        # A daemon process is one transport generation. Factorio uses this ID
        # to reject packets left over from a previous daemon instance.
        self.daemon_session_id = uuid.uuid4().hex

        self._state_lock = threading.RLock()
        self._session_lock = threading.RLock()
        self._stop = threading.Event()
        self._started = False
        self._next_heartbeat = 0.0
        self._peers: Dict[str, Peer] = {}
        self._address_world: Dict[Address, str] = {}
        self._active: Dict[Tuple[str, str], RequestState] = {}
        self._completed: "OrderedDict[Tuple[str, str], CompletedExchange]" = OrderedDict()
        self._threads: Set[threading.Thread] = set()

    @property
    def address(self) -> Optional[Tuple[str, int]]:
        if self.sock is None:
            return None
        try:
            value = self.sock.getsockname()
        except OSError:
            return None
        return value[0], value[1]

    @property
    def peers(self) -> Dict[str, Peer]:
        """A snapshot useful to diagnostics and narrow integration tests."""

        with self._state_lock:
            return dict(self._peers)

    @property
    def active_requests(self) -> Dict[Tuple[str, str], RequestState]:
        with self._state_lock:
            return dict(self._active)

    def start(self) -> Tuple[str, int]:
        if self._started and self.sock is not None:
            return self.address or (self.config.host, self.config.port)

        if self.sock is None:
            created = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            created.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            created.bind((self.config.host, self.config.port))
            self.sock = created
            self._owns_socket = True
        self.sock.setblocking(False)
        self._started = True
        self._stop.clear()
        self._next_heartbeat = self._clock() + self.config.heartbeat_interval
        bound = self.address
        if bound is None:
            raise OSError("UDP socket did not expose a bound address")
        self.log.info("UDP daemon listening on %s:%s", bound[0], bound[1])
        return bound

    def stop(self) -> None:
        self._stop.set()
        with self._state_lock:
            requests = list(self._active.values())
            for request in requests:
                request.cancel_event.set()
                with self._session_lock:
                    self._abort_session_turn(request)
            self._active.clear()
            self._started = False
        if self.sock is not None and self._owns_socket:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self._owns_sessions and not self._sessions_closed:
            close = getattr(self.sessions, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    self.log.exception("could not close conversation store")
            self._sessions_closed = True
        # Request threads are daemon threads. They observe cancellation before
        # sending and cannot keep CLI shutdown hostage to a provider timeout.

    def run_forever(self) -> None:
        """Run the receive/heartbeat loop until interrupted or stopped."""

        self.start()
        try:
            while not self._stop.is_set():
                self.run_once(0.20)
        except KeyboardInterrupt:
            self.log.info("daemon stopped")
        finally:
            self.stop()

    def run_once(self, timeout: float = 0.0) -> int:
        """Process available datagrams once and service due heartbeats.

        This small method keeps integration tests independent of a forever
        loop and is also useful to embedders that own their event loop.
        """

        if not self._started:
            self.start()
        if self.sock is None:
            return 0

        processed = 0
        try:
            readable, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            if not self._stop.is_set():
                self.log.exception("UDP select failed")
            return 0
        if readable:
            processed = self._drain_socket()
        self._service_heartbeats()
        return processed

    def _drain_socket(self) -> int:
        if self.sock is None:
            return 0
        count = 0
        while not self._stop.is_set():
            try:
                data, address = self.sock.recvfrom(protocol.MAX_DATAGRAM_BYTES + 1)
            except BlockingIOError:
                break
            except OSError:
                if not self._stop.is_set():
                    self.log.exception("UDP receive failed")
                break
            count += 1
            self.handle_datagram(data, address)
        return count

    def handle_datagram(self, data: Any, address: Any) -> bool:
        """Validate and dispatch one datagram; never raises on bad input."""

        normalized_address = self._normalize_address(address)
        if normalized_address is None:
            self.log.debug("discarding packet from invalid address %r", address)
            return False
        packet, error = protocol.parse_packet(data)
        if packet is None:
            self.log.debug("discarding malformed UDP packet from %s: %s", normalized_address, error)
            return False
        try:
            # ``parse_packet`` validates the common envelope. Dispatchers use
            # the stricter v1 schema so missing world/text/etc. fields are
            # rejected before they reach session or provider code.
            packet = protocol.validate_message(packet)
            msg_type = packet["type"]
            if msg_type == "hello":
                self._handle_hello(packet, normalized_address)
            elif msg_type == "heartbeat":
                self._handle_heartbeat(packet, normalized_address)
            elif msg_type == "user_message":
                self._handle_user_message(packet, normalized_address)
            else:
                # Assistant packets are daemon-to-mod messages. A local peer
                # sending one is harmless but is not a request to process.
                self.log.debug("ignoring inbound daemon-only packet type %s", msg_type)
            return True
        except Exception:
            # A malformed or unexpected payload must not take down the receive
            # loop. Known request failures are handled with protocol errors in
            # their handlers; this catches integration/session edge cases.
            self.log.exception("error handling UDP packet type %s", packet.get("type"))
            exchange_id = packet.get("exchange_id", "")
            if exchange_id:
                if packet.get("type") == "user_message":
                    self._send_user_error(
                        packet,
                        normalized_address,
                        "DAEMON_ERROR",
                        "The companion daemon could not process that request.",
                    )
                else:
                    self._send_error(
                        normalized_address,
                        exchange_id,
                        "DAEMON_ERROR",
                        "The companion daemon could not process that request.",
                    )
            return False

    @staticmethod
    def _normalize_address(address: Any) -> Optional[Address]:
        if not isinstance(address, (tuple, list)) or len(address) < 2:
            return None
        host, port = address[0], address[1]
        if not isinstance(host, str):
            return None
        try:
            ip = ipaddress.ip_address(host)
            port_number = int(port)
        except (ValueError, TypeError):
            return None
        if not ip.is_loopback or not 0 <= port_number <= 65535:
            return None
        return host, port_number

    def _send_packet(
        self,
        msg_type: str,
        exchange_id: str,
        seq: int,
        payload: Mapping[str, Any],
        address: Address,
    ) -> bool:
        if self.sock is None or self._stop.is_set():
            return False
        try:
            raw = protocol.create_packet(msg_type, exchange_id, seq, payload)
            # The Factorio side intentionally enforces the same conservative
            # 4 KiB budget. A successful completion is preflighted before its
            # durable commit, so an oversized final answer cannot advance the
            # saved conversation head without reaching the game.
            encoded = protocol.packet_bytes(raw)
            self.sock.sendto(encoded, address)
            return True
        except (OSError, protocol.ProtocolError, TypeError, ValueError) as exc:
            self.log.debug("could not send %s to %s: %s", msg_type, address, exc)
            return False

    def _send_error(
        self,
        address: Address,
        exchange_id: str,
        code: str,
        message: str,
        *,
        world_id: Optional[str] = None,
        seq: int = 1,
        request_id: Optional[str] = None,
        request_turn_id: Optional[str] = None,
        client_session_id: Optional[str] = None,
        daemon_session_id: Optional[str] = None,
    ) -> bool:
        payload: Dict[str, Any] = {
            "code": _safe_code(code),
            "message": _safe_message(message),
        }
        if isinstance(world_id, str) and world_id and len(world_id) <= protocol.MAX_ID_LENGTH:
            payload["world_id"] = world_id
        for field, value in (
            ("request_id", request_id),
            ("request_turn_id", request_turn_id),
            ("client_session_id", client_session_id),
            ("daemon_session_id", daemon_session_id),
        ):
            if isinstance(value, str) and value and len(value) <= protocol.MAX_ID_LENGTH:
                payload[field] = value
        return self._send_packet("error", exchange_id, seq, payload, address)

    def _send_user_error(
        self,
        packet: Mapping[str, Any],
        address: Address,
        code: str,
        message: str,
    ) -> bool:
        """Send a request-correlated error that the mod can safely accept."""

        payload = packet.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        exchange_id = packet.get("exchange_id")
        if not isinstance(exchange_id, str):
            exchange_id = ""
        world_id = payload.get("world_id")
        return self._send_error(
            address,
            exchange_id,
            code,
            message,
            world_id=world_id if isinstance(world_id, str) else None,
            request_id=(payload.get("request_id") or exchange_id),
            request_turn_id=payload.get("turn_id"),
            client_session_id=payload.get("client_session_id"),
            daemon_session_id=self.daemon_session_id,
        )

    def _handle_hello(self, packet: Mapping[str, Any], address: Address) -> None:
        payload = packet["payload"]
        head = payload.get("conversation_head") or "turn_0"

        # Factorio's runtime Lua state is deterministic. It cannot safely mint
        # a globally unique save identity or a fresh process/session nonce.
        # New saves therefore omit world_id on their first hello; the external
        # daemon owns the entropy boundary and returns the assigned ID.
        announced_world_id = payload.get("world_id")
        if announced_world_id:
            world_id = announced_world_id
        else:
            if head != "turn_0":
                self._send_error(
                    address,
                    "",
                    "HISTORY_ERROR",
                    "An unbound save must begin at the root conversation.",
                )
                return
            world_id = f"world_{uuid.uuid4().hex}"

        # Product Factorio clients omit client_session_id on hello. A supplied
        # value is retained only for backwards compatibility/test peers.
        client_session_id = (
            payload.get("client_session_id") or f"session_{uuid.uuid4().hex}"
        )
        capabilities = tuple(payload.get("capabilities") or ())
        now = self._clock()

        with self._state_lock:
            old_world_for_address = self._address_world.get(address)
            if old_world_for_address and old_world_for_address != world_id:
                self._invalidate_world_locked(old_world_for_address)
                self._peers.pop(old_world_for_address, None)

            old_peer = self._peers.get(world_id)
            if old_peer is not None:
                self._invalidate_world_locked(world_id)
                if old_peer.address != address:
                    # A reconnect from a new ephemeral Factorio source port
                    # replaces the old address mapping as well as the peer.
                    # Leaving the stale mapping would let old heartbeats
                    # update the new generation.
                    self._address_world.pop(old_peer.address, None)
                generation = old_peer.generation + 1
            else:
                generation = 1
            self._address_world[address] = world_id
            peer = Peer(
                world_id=world_id,
                address=address,
                generation=generation,
                conversation_head=head,
                capabilities=capabilities,
                player_name=payload.get("player_name", ""),
                client_session_id=client_session_id,
                last_seen=now,
            )
            self._peers[world_id] = peer

            try:
                with self._session_lock:
                    timeline = self.sessions.sync_save(world_id, head)
                    canonical_head = str(
                        getattr(timeline, "head_turn_id", head) or "turn_0"
                    )
                    peer.conversation_head = canonical_head
                    history_depth = self._history_depth(timeline, canonical_head)
            except Exception:
                self.log.exception("could not synchronize world %s", world_id)
                if self._peers.get(world_id) is peer:
                    self._peers.pop(world_id, None)
                if self._address_world.get(address) == world_id:
                    self._address_world.pop(address, None)
                self._send_error(
                    address,
                    "",
                    "HISTORY_ERROR",
                    "The saved conversation head could not be synchronized.",
                    world_id=world_id,
                )
                return

            model = getattr(self.llm, "model", None) or self.config.model
            ack = {
                "daemon_version": self.config.daemon_version,
                "model": model,
                "world_id": world_id,
                "active_world_id": world_id,
                "active_turn_id": peer.conversation_head,
                "history_depth": history_depth,
                "status": "ready",
                "daemon_session_id": self.daemon_session_id,
            }
            # Echo the initiating hello sequence so the Factorio side can
            # reject a delayed acknowledgement from an older handshake.
            ack["hello_seq"] = int(packet.get("seq", 0))
            if peer.client_session_id:
                ack["client_session_id"] = peer.client_session_id
            self._send_packet("hello_ack", "", 1, ack, address)
        self.log.info(
            "Factorio connected: world=%s head=%s address=%s",
            world_id,
            peer.conversation_head,
            address,
        )

    def _history_depth(self, timeline: Any, head: str) -> int:
        try:
            messages = timeline.get_linear_history(head_turn_id=head, max_turns=self.config.max_history)
            return len(messages) // 2
        except TypeError:
            messages = timeline.get_linear_history(head)
            return len(messages) // 2
        except Exception:
            return 0

    def _handle_heartbeat(self, packet: Mapping[str, Any], address: Address) -> None:
        with self._state_lock:
            world_id = self._address_world.get(address)
            peer = self._peers.get(world_id) if world_id else None
            if peer is not None and peer.address != address:
                self.log.debug("ignoring heartbeat from stale address %s", address)
                return
            payload = packet["payload"]
            announced_world = payload.get("world_id")
            announced_session = payload.get("client_session_id")
            if peer is not None:
                if announced_world is not None and announced_world != peer.world_id:
                    self.log.debug("ignoring heartbeat for another world from %s", address)
                    return
                if peer.client_session_id and announced_session != peer.client_session_id:
                    self.log.debug("ignoring heartbeat for another client session from %s", address)
                    return
            if peer is not None:
                peer.last_seen = self._clock()
                state = "busy" if any(key[0] == peer.world_id for key in self._active) else "ready"
            else:
                state = "ready"
            response = {
                "ping_tick": payload.get("ping_tick", packet.get("tick", 0)),
                "state": state,
                "daemon_session_id": self.daemon_session_id,
            }
            if peer is not None:
                response["world_id"] = peer.world_id
                if peer.client_session_id:
                    response["client_session_id"] = peer.client_session_id
            elif isinstance(announced_world, str) and announced_world:
                # A restarted daemon may hear Factorio's old-session heartbeat
                # before the mod's timeout triggers a fresh hello.
                response["world_id"] = announced_world
                if isinstance(announced_session, str) and announced_session:
                    response["client_session_id"] = announced_session
            self._send_packet("heartbeat", "", max(1, int(packet.get("seq", 1))), response, address)

    def _service_heartbeats(self) -> None:
        now = self._clock()
        if now < self._next_heartbeat:
            return
        self._next_heartbeat = now + self.config.heartbeat_interval
        with self._state_lock:
            peers = list(self._peers.values())
            for peer in peers:
                peer.control_seq += 1
                busy = any(key[0] == peer.world_id for key in self._active)
                self._send_packet(
                    "heartbeat",
                    "",
                    peer.control_seq,
                    {
                        "ping_tick": 0,
                        "state": "busy" if busy else "ready",
                        "world_id": peer.world_id,
                        "client_session_id": peer.client_session_id or None,
                        "daemon_session_id": self.daemon_session_id,
                    },
                    peer.address,
                )

    def _peer_for_address_locked(self, address: Address) -> Optional[Peer]:
        world_id = self._address_world.get(address)
        if world_id is None:
            return None
        peer = self._peers.get(world_id)
        if peer is None or peer.address != address:
            return None
        return peer

    def _handle_user_message(self, packet: Mapping[str, Any], address: Address) -> None:
        payload = packet["payload"]
        exchange_id = packet["exchange_id"]
        world_id = payload["world_id"]
        requested_turn_id = payload["turn_id"]
        parent_turn_id = payload.get("parent_turn_id") or "turn_0"
        text = payload["text"]
        context = payload.get("context") or {}
        request_id = payload.get("request_id") or exchange_id
        client_session_id = payload.get("client_session_id") or ""

        def send_request_error(code: str, message: str) -> None:
            self._send_user_error(packet, address, code, message)

        if not isinstance(context, dict):
            send_request_error("INVALID_CONTEXT", "The game context must be an object.")
            return
        context_world = context.get("world_id")
        if context_world is not None and context_world != world_id:
            send_request_error("WORLD_MISMATCH", "The context belongs to a different game world.")
            return

        fingerprint = _fingerprint(requested_turn_id, parent_turn_id, text, context)

        with self._state_lock:
            peer = self._peer_for_address_locked(address)
            if peer is None:
                # A daemon restart can receive a user's packet while Factorio
                # still believes the old daemon is ready. Bootstrap from the
                # self-identifying user packet; a normal hello will supersede
                # this generation shortly afterwards.
                mapped_world = self._address_world.get(address)
                if mapped_world and mapped_world != world_id:
                    send_request_error("WORLD_MISMATCH", "This UDP session belongs to another game world.")
                    return
                existing = self._peers.get(world_id)
                if existing is not None and existing.address != address:
                    send_request_error("STALE_SESSION", "Reconnect the Factorio UDP session before sending a message.")
                    return
                generation = (existing.generation + 1) if existing is not None else 1
                if existing is not None:
                    self._invalidate_world_locked(world_id)
                peer = Peer(
                    world_id=world_id,
                    address=address,
                    generation=generation,
                    conversation_head=parent_turn_id,
                    capabilities=tuple(context.get("capabilities") or ()),
                    client_session_id=client_session_id,
                    last_seen=self._clock(),
                )
                self._peers[world_id] = peer
                self._address_world[address] = world_id
                try:
                    with self._session_lock:
                        timeline = self.sessions.sync_save(world_id, parent_turn_id)
                        parent_turn_id = str(
                            getattr(timeline, "head_turn_id", parent_turn_id) or "turn_0"
                        )
                        peer.conversation_head = parent_turn_id
                except Exception:
                    self.log.exception("could not bootstrap world %s from user packet", world_id)
                    self._peers.pop(world_id, None)
                    if self._address_world.get(address) == world_id:
                        self._address_world.pop(address, None)
                    self._send_error(
                        address,
                        exchange_id,
                        "HISTORY_ERROR",
                        "The saved conversation head could not be synchronized.",
                        world_id=world_id,
                        request_id=request_id,
                        request_turn_id=requested_turn_id,
                        client_session_id=client_session_id,
                        daemon_session_id=self.daemon_session_id,
                    )
                    return
                self._send_packet(
                    "hello_ack",
                    "",
                    1,
                    self._hello_ack_payload(world_id, parent_turn_id, 0, client_session_id),
                    address,
                )
            else:
                peer.last_seen = self._clock()
                if peer.client_session_id and client_session_id != peer.client_session_id:
                    self._send_error(
                        address,
                        exchange_id,
                        "STALE_SESSION",
                        "This request belongs to an older Factorio UDP session.",
                        world_id=world_id,
                        request_id=request_id,
                        request_turn_id=requested_turn_id,
                        client_session_id=client_session_id,
                        daemon_session_id=self.daemon_session_id,
                    )
                    return

            key = (world_id, exchange_id)
            cached = self._completed.get(key)
            if cached is not None:
                if cached.generation == peer.generation and cached.fingerprint == fingerprint:
                    self._completed.move_to_end(key)
                    self._send_completed_locked(peer, exchange_id, cached)
                    return
                if cached.generation != peer.generation:
                    self._completed.pop(key, None)
                else:
                    send_request_error("DUPLICATE_EXCHANGE", "That request identifier was already used for another message.")
                    return

            active = self._active.get(key)
            if active is not None:
                if active.fingerprint == fingerprint:
                    # Duplicate UDP delivery: the first generation owns the
                    # stream, so do not start a second model call.
                    return
                send_request_error("DUPLICATE_EXCHANGE", "That request is already in progress.")
                return

            if peer.conversation_head != parent_turn_id:
                send_request_error("STALE_HEAD", "The saved conversation head changed; reconnecting the game session is required.")
                return

            world_active = any(key_world == world_id for key_world, _ in self._active)
            if world_active:
                send_request_error("BUSY", "Another companion request is already running for this world.")
                return
            if len(self._active) >= self.config.max_workers:
                send_request_error("BUSY", "The companion daemon is busy with other worlds.")
                return

            session_node = None
            try:
                with self._session_lock:
                    messages = self.sessions.build_llm_messages(
                        world_id,
                        parent_turn_id,
                        text,
                        context,
                        max_turns=self.config.max_history,
                    )
                    session_node = self._begin_session_turn(
                        world_id,
                        requested_turn_id,
                        request_id,
                        parent_turn_id,
                        text,
                        context,
                    )
            except Exception:
                self.log.exception("could not build model conversation for world %s", world_id)
                send_request_error("HISTORY_ERROR", "The conversation history could not be prepared.")
                return

            canonical_turn_id = getattr(session_node, "turn_id", None) or _new_turn_id()

            request = RequestState(
                world_id=world_id,
                exchange_id=exchange_id,
                requested_turn_id=requested_turn_id,
                parent_turn_id=parent_turn_id,
                user_text=text,
                context=dict(context),
                canonical_turn_id=canonical_turn_id,
                request_id=request_id,
                client_session_id=client_session_id,
                peer_generation=peer.generation,
                address=address,
                fingerprint=fingerprint,
                started_at=self._clock(),
                session_started=session_node is not None,
            )
            self._active[key] = request

            # Send start before invoking the provider. If the kernel refuses
            # this send, avoid spending a model call on a peer we cannot reach.
            started = self._send_packet(
                "assistant_start",
                exchange_id,
                1,
                {
                    "model": getattr(self.llm, "model", None) or self.config.model,
                    "world_id": world_id,
                    "request_id": request_id,
                    "request_turn_id": requested_turn_id,
                    "client_session_id": client_session_id or None,
                    "daemon_session_id": self.daemon_session_id,
                },
                address,
            )
            if not started:
                self._active.pop(key, None)
                with self._session_lock:
                    self._abort_session_turn(request)
                return

            worker = threading.Thread(
                target=self._run_request,
                args=(request, messages),
                name=f"companion-{world_id[:12]}-{exchange_id[:12]}",
                daemon=True,
            )
            self._threads.add(worker)
            worker.start()

    def _send_completed_locked(self, peer: Peer, exchange_id: str, cached: CompletedExchange) -> None:
        self._send_packet(
            "assistant_end",
            exchange_id,
            cached.final_seq,
            {
                "world_id": peer.world_id,
                "turn_id": cached.canonical_turn_id,
                "request_turn_id": cached.requested_turn_id,
                "request_id": cached.request_id,
                "full_text": cached.full_text,
                "duration_ms": 0,
                "client_session_id": peer.client_session_id or None,
                "daemon_session_id": self.daemon_session_id,
            },
            peer.address,
        )

    def _configured_model(self) -> str:
        return getattr(self.llm, "model", None) or self.config.model

    def _hello_ack_payload(
        self,
        world_id: str,
        head: str,
        history_depth: int,
        client_session_id: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "daemon_version": self.config.daemon_version,
            "model": self._configured_model(),
            "world_id": world_id,
            "active_world_id": world_id,
            "active_turn_id": head,
            "history_depth": history_depth,
            "status": "ready",
            "daemon_session_id": self.daemon_session_id,
        }
        if client_session_id:
            payload["client_session_id"] = client_session_id
        return payload

    def _begin_session_turn(
        self,
        world_id: str,
        requested_turn_id: str,
        request_id: str,
        parent_turn_id: str,
        text: str,
        context: Mapping[str, Any],
    ) -> Any:
        """Start a pending durable turn when the session facade supports it."""

        begin = getattr(self.sessions, "begin_turn", None)
        if begin is None:
            begin = getattr(self.sessions, "start_turn", None)
        if begin is None:
            return None

        import inspect

        params = inspect.signature(begin).parameters
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in params.values()
        )
        if "world_id" not in params and not accepts_kwargs:
            # This is likely a WorldTimeline-style helper, not the manager
            # facade. The completion fallback below can still support it.
            return None
        kwargs: Dict[str, Any] = {
            "parent_turn_id": parent_turn_id,
            "context_snapshot": context,
            "context": context,
            "model": self._configured_model(),
            "client_turn_id": requested_turn_id,
            "request_id": request_id,
        }
        if not accepts_kwargs:
            if "context_snapshot" in params:
                kwargs.pop("context", None)
            else:
                kwargs.pop("context_snapshot", None)
            kwargs = {name: value for name, value in kwargs.items() if name in params}
        return begin(world_id, text, **kwargs)

    def _request_current_locked(self, request: RequestState) -> bool:
        if request.cancel_event.is_set() or self._stop.is_set():
            return False
        if self._active.get((request.world_id, request.exchange_id)) is not request:
            return False
        peer = self._peers.get(request.world_id)
        return peer is not None and peer.generation == request.peer_generation and peer.address == request.address

    def _emit_delta(self, request: RequestState, delta: str) -> None:
        with self._state_lock:
            if not self._request_current_locked(request):
                return
            peer = self._peers.get(request.world_id)
            if peer is None:
                return
            base_payload: Dict[str, Any] = {
                "world_id": request.world_id,
                "request_id": request.request_id,
                "request_turn_id": request.requested_turn_id,
                "client_session_id": request.client_session_id or None,
                "daemon_session_id": self.daemon_session_id,
            }

            # A provider or config can hand us a chunk larger than the
            # configured cosmetic batch size. Split by the serialized UTF-8
            # packet size so the hard UDP budget remains true for every
            # outgoing delta, including escaped/control-heavy text.
            offset = 0
            while offset < len(delta):
                remaining = delta[offset:]
                low, high = 1, len(remaining)
                best = 0
                while low <= high:
                    middle = (low + high) // 2
                    candidate_payload = dict(base_payload)
                    candidate_payload["delta"] = remaining[:middle]
                    try:
                        protocol.packet_bytes(
                            protocol.create_packet(
                                "assistant_delta",
                                request.exchange_id,
                                request.next_seq,
                                candidate_payload,
                            )
                        )
                    except protocol.ProtocolError:
                        high = middle - 1
                    else:
                        best = middle
                        low = middle + 1
                if best == 0:
                    self.log.warning(
                        "could not fit an assistant delta for exchange %s",
                        request.exchange_id,
                    )
                    return
                candidate_payload = dict(base_payload)
                candidate_payload["delta"] = remaining[:best]
                seq = request.take_seq()
                if not self._send_packet(
                    "assistant_delta",
                    request.exchange_id,
                    seq,
                    candidate_payload,
                    peer.address,
                ):
                    return
                offset += best

    def _run_request(self, request: RequestState, messages: Iterable[Mapping[str, Any]]) -> None:
        try:
            batcher = DeltaBatcher(
                lambda delta: self._emit_delta(request, delta),
                interval=self.config.delta_interval,
                max_chars=self.config.delta_chars,
                clock=self._clock,
            )
            full_text = self._invoke_llm(messages, batcher.add, request.cancel_event)
            batcher.flush()
            if not isinstance(full_text, str) or not full_text.strip():
                raise LLMError("model returned an empty response", code="MODEL_EMPTY")

            duration_ms = max(0, int((self._clock() - request.started_at) * 1000))
            with self._state_lock:
                if not self._request_current_locked(request):
                    return
                final_seq = request.next_seq
                final_payload = {
                    "world_id": request.world_id,
                    "turn_id": request.canonical_turn_id,
                    "request_turn_id": request.requested_turn_id,
                    "request_id": request.request_id,
                    "parent_turn_id": request.parent_turn_id,
                    "full_text": full_text,
                    "duration_ms": duration_ms,
                    "client_session_id": request.client_session_id or None,
                    "daemon_session_id": self.daemon_session_id,
                }
                try:
                    protocol.packet_bytes(
                        protocol.create_packet(
                            "assistant_end",
                            request.exchange_id,
                            final_seq,
                            final_payload,
                        )
                    )
                except protocol.ProtocolError as exc:
                    raise LLMError(
                        "model response is too large for one Factorio UDP completion packet",
                        code="MODEL_TOO_LARGE",
                    ) from exc

                with self._session_lock:
                    self._complete_session_turn(request, full_text)

                peer = self._peers.get(request.world_id)
                if peer is None:
                    return
                sent = self._send_packet(
                    "assistant_end",
                    request.exchange_id,
                    request.take_seq(),
                    final_payload,
                    peer.address,
                )
                if sent:
                    peer.conversation_head = request.canonical_turn_id
                self._active.pop((request.world_id, request.exchange_id), None)
                if self.config.max_cached_exchanges:
                    key = (request.world_id, request.exchange_id)
                    self._completed[key] = CompletedExchange(
                        fingerprint=request.fingerprint,
                        canonical_turn_id=request.canonical_turn_id,
                        requested_turn_id=request.requested_turn_id,
                        request_id=request.request_id,
                        full_text=full_text,
                        generation=request.peer_generation,
                        final_seq=final_seq,
                    )
                    self._completed.move_to_end(key)
                    while len(self._completed) > self.config.max_cached_exchanges:
                        self._completed.popitem(last=False)
            self.log.info("completed exchange %s for world %s (%d chars)", request.exchange_id, request.world_id, len(full_text))
        except LLMError as exc:
            self._finish_error(request, exc.code, str(exc))
        except Exception:
            self.log.exception("unhandled request failure for %s", request.exchange_id)
            self._finish_error(request, "DAEMON_ERROR", "The companion daemon could not complete that request.")
        finally:
            with self._state_lock:
                self._threads.discard(threading.current_thread())

    def _invoke_llm(
        self,
        messages: Iterable[Mapping[str, Any]],
        on_delta: Callable[[str], None],
        cancel_event: threading.Event,
    ) -> str:
        stream = getattr(self.llm, "stream", None)
        if stream is None:
            stream = getattr(self.llm, "stream_text", None)
        if stream is None:
            raise LLMError("the configured LLM client does not support streaming", code="MODEL_CONFIG")

        # Keep the callback boundary provider-neutral. The standard client has
        # all of these keyword parameters; filtering lets a narrow fake client
        # be used for diagnostics without weakening production behavior.
        kwargs = {
            "system": self.config.system_prompt,
            "on_delta": on_delta,
            "model": self.config.model,
            "timeout": self.config.llm_timeout,
        }
        try:
            import inspect

            signature = inspect.signature(stream)
            parameters = signature.parameters
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if not accepts_kwargs:
                kwargs = {name: value for name, value in kwargs.items() if name in parameters}
            result = stream(messages, **kwargs)
        except LLMError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise LLMError(f"model request timed out: {exc}", code="MODEL_TIMEOUT") from exc
        except Exception as exc:
            raise LLMError(f"model stream failed: {exc}", code="MODEL_ERROR") from exc

        if cancel_event.is_set():
            raise LLMError("request superseded by a new Factorio session", code="REQUEST_STALE")
        if isinstance(result, tuple):
            status = result[1] if len(result) > 1 else None
            if status == "partial":
                raise LLMError(
                    "model stream ended before completion",
                    code="MODEL_STREAM",
                )
            result = result[0] if result else ""
        if not isinstance(result, str):
            raise LLMError("model client returned a non-text response", code="MODEL_PROTOCOL")
        return result

    def _complete_session_turn(self, request: RequestState, full_text: str) -> None:
        """Atomically complete the pending turn, with a legacy fallback."""

        if request.session_started:
            complete = getattr(self.sessions, "complete_turn", None)
            if complete is None:
                raise RuntimeError("session manager cannot complete a pending turn")
            import inspect

            params = inspect.signature(complete).parameters
            kwargs: Dict[str, Any] = {"model": self._configured_model()}
            if "world_id" in params:
                complete(request.world_id, request.canonical_turn_id, full_text, **kwargs)
            else:
                complete(request.canonical_turn_id, full_text, **kwargs)
            return

        # Compatibility for an older in-memory WorldTimeline injected by a
        # diagnostic caller. The production SessionManager uses the durable
        # begin/complete pair above.
        timeline = self.sessions.get_or_create_world(request.world_id)
        record = getattr(timeline, "record_turn", None)
        complete = getattr(timeline, "complete_turn", None)
        if record is None or complete is None:
            raise RuntimeError("session manager cannot record a completed turn")
        record(
            request.canonical_turn_id,
            request.parent_turn_id,
            request.user_text,
            request.context,
        )
        complete(request.canonical_turn_id, full_text)

    def _abort_session_turn(self, request: RequestState) -> None:
        """Keep failed/stale pending rows out of the durable pending queue."""

        if not request.session_started:
            return
        abort = getattr(self.sessions, "abort_turn", None)
        if abort is None:
            return
        try:
            import inspect

            params = inspect.signature(abort).parameters
            if "world_id" in params:
                abort(request.world_id, request.canonical_turn_id)
            else:
                abort(request.canonical_turn_id)
        except Exception:
            self.log.exception("could not mark failed turn %s aborted", request.canonical_turn_id)

    def _finish_error(self, request: RequestState, code: str, message: str) -> None:
        with self._state_lock:
            if not self._request_current_locked(request):
                return
            self._active.pop((request.world_id, request.exchange_id), None)
            with self._session_lock:
                self._abort_session_turn(request)
            self._send_packet(
                "error",
                request.exchange_id,
                request.take_seq(),
                {
                    "code": _safe_code(code),
                    "message": _safe_message(message),
                    "world_id": request.world_id,
                    "request_id": request.request_id,
                    "request_turn_id": request.requested_turn_id,
                    "client_session_id": request.client_session_id or None,
                    "daemon_session_id": self.daemon_session_id,
                },
                self._peers[request.world_id].address,
            )
        self.log.warning("exchange %s failed (%s): %s", request.exchange_id, code, _safe_message(message))

    def _invalidate_world_locked(self, world_id: str) -> None:
        for key, request in list(self._active.items()):
            if key[0] == world_id:
                request.cancel_event.set()
                self._active.pop(key, None)
                with self._session_lock:
                    self._abort_session_turn(request)


def _new_turn_id() -> str:
    """Allocate a daemon-owned ID that cannot rewind with a Factorio save."""

    return f"turn_{uuid.uuid4().hex}"


def _fingerprint(turn_id: str, parent_turn_id: str, text: str, context: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            {"turn_id": turn_id, "parent_turn_id": parent_turn_id, "text": text, "context": context},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return repr((turn_id, parent_turn_id, text, context))


def _safe_message(message: Any) -> str:
    text = " ".join(str(message or "The companion model is unavailable.").split())
    return text[:500] or "The companion model is unavailable."


def _safe_code(code: Any) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(code or "DAEMON_ERROR"))
    return text[:64] or "DAEMON_ERROR"


def build_arg_parser() -> argparse.ArgumentParser:
    defaults = DaemonConfig.from_env()
    parser = argparse.ArgumentParser(description="Factorio Companion localhost UDP daemon")
    parser.add_argument("--host", default=defaults.host, help="localhost UDP bind address")
    parser.add_argument("--port", type=int, default=defaults.port, help="daemon UDP port (default: 34200)")
    parser.add_argument("--factorio-port", type=int, default=defaults.factorio_port, help="Factorio UDP port (informational; replies use the hello source port)")
    parser.add_argument(
        "--db-path",
        "--database",
        dest="db_path",
        default=defaults.db_path or str(default_database_path()),
        help="SQLite conversation database path",
    )
    parser.add_argument("--base-url", default=defaults.base_url, help="OpenAI-compatible API base URL")
    parser.add_argument("--model", default=defaults.model, help="model name sent to /chat/completions")
    parser.add_argument("--api-key", default=defaults.api_key, help="provider API key")
    parser.add_argument("--timeout", "--llm-timeout", dest="llm_timeout", type=float, default=defaults.llm_timeout, help="LLM request/read timeout in seconds")
    parser.add_argument("--heartbeat-interval", type=float, default=defaults.heartbeat_interval, help="daemon heartbeat interval in seconds")
    parser.add_argument("--delta-interval", type=float, default=defaults.delta_interval, help="maximum idle time before flushing a delta")
    parser.add_argument("--delta-chars", type=int, default=defaults.delta_chars, help="character threshold for delta batching")
    parser.add_argument("--max-workers", type=int, default=defaults.max_workers, help="maximum simultaneous worlds")
    parser.add_argument("--max-cached-exchanges", type=int, default=defaults.max_cached_exchanges, help="number of completed UDP exchanges cached for duplicate repair")
    parser.add_argument("--max-history", type=int, default=defaults.max_history, help="completed turns included in each model prompt")
    parser.add_argument("--log-level", default=os.environ.get("COMPANION_LOG_LEVEL", "INFO"), help="logging level")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    defaults = DaemonConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = DaemonConfig(
        host=args.host,
        port=args.port,
        factorio_port=args.factorio_port,
        db_path=args.db_path,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        llm_timeout=args.llm_timeout,
        heartbeat_interval=args.heartbeat_interval,
        delta_interval=args.delta_interval,
        delta_chars=args.delta_chars,
        max_workers=args.max_workers,
        max_cached_exchanges=args.max_cached_exchanges,
        max_history=args.max_history,
    )
    try:
        daemon = CompanionDaemon(config)
        daemon.start()
        LOG.info(
            "ready; Factorio should use --enable-lua-udp %s; LLM=%s model=%s",
            config.factorio_port,
            config.base_url,
            config.model,
        )
        daemon.run_forever()
    except OSError as exc:
        LOG.error("could not start UDP daemon: %s", exc)
        return 2
    except ValueError as exc:
        LOG.error("invalid daemon configuration: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
