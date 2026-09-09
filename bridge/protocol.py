"""Framing and validation for the Factorio Companion UDP protocol (v1).

The protocol deliberately keeps the envelope small and boring. UDP input is
untrusted, though, so parsing never lets a malformed datagram escape as an
exception from :func:`parse_packet`.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Tuple


PROTOCOL_VERSION = 1
# The mod's protocol specification targets datagrams below the local MTU. A
# daemon may choose a smaller value, but accepting arbitrarily large input here
# would make it too easy for a bad local process to consume unbounded memory.
# Keep packets below the local MTU and match the Factorio-side contract. The
# daemon's response prompt is intentionally short and DeltaBatcher keeps
# cosmetic traffic well below this ceiling.
MAX_DATAGRAM_BYTES = 4_096
RECOMMENDED_DATAGRAM_BYTES = MAX_DATAGRAM_BYTES
MAX_TYPE_LENGTH = 64
MAX_EXCHANGE_ID_LENGTH = 256
MAX_ID_LENGTH = 256
MAX_TEXT_LENGTH = 20_000

MESSAGE_TYPES = frozenset(
    {
        "hello",
        "hello_ack",
        "heartbeat",
        "user_message",
        "assistant_start",
        "assistant_delta",
        "assistant_end",
        "error",
    }
)


class ProtocolError(ValueError):
    """Raised by packet construction or strict packet validation."""


def _is_int(value: Any) -> bool:
    # bool is an int subclass, but it is not a valid wire integer.
    return isinstance(value, int) and not isinstance(value, bool)


def _string_field(
    value: Any,
    name: str,
    *,
    required: bool = False,
    allow_empty: bool = True,
    max_length: Optional[int] = None,
) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise ProtocolError(f"{name} must not be empty")
    if max_length is not None and len(value) > max_length:
        raise ProtocolError(f"{name} is too long")
    return value


def _validate_payload(msg_type: str, exchange_id: str, payload: Mapping[str, Any]) -> None:
    """Validate fields whose meaning is defined by a v1 message type."""

    if msg_type == "hello":
        if exchange_id:
            raise ProtocolError("hello must not have an exchange_id")
        # A brand-new Factorio save is intentionally unbound. The daemon
        # assigns its durable world ID during the first hello and the mod then
        # persists that ID in the save. If a world ID is supplied, validate it
        # strictly rather than silently replacing malformed input.
        if "world_id" in payload:
            _string_field(
                payload.get("world_id"),
                "payload.world_id",
                required=True,
                allow_empty=False,
                max_length=MAX_ID_LENGTH,
            )
        _string_field(
            payload.get("conversation_head"),
            "payload.conversation_head",
            required=True,
            allow_empty=False,
            max_length=MAX_ID_LENGTH,
        )
        capabilities = payload.get("capabilities", [])
        if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
            raise ProtocolError("payload.capabilities must be a list of strings")
        if any(len(item) > MAX_ID_LENGTH for item in capabilities):
            raise ProtocolError("payload.capabilities contains an item that is too long")
        for field in ("mod_version", "factorio_version", "player_name"):
            if field in payload:
                _string_field(payload[field], f"payload.{field}")
        for field in ("client_session_id",):
            if field in payload:
                _string_field(payload[field], f"payload.{field}", max_length=MAX_ID_LENGTH)

    elif msg_type in {"hello_ack", "heartbeat"}:
        if exchange_id:
            raise ProtocolError(f"{msg_type} must not have an exchange_id")
        for field in ("world_id", "active_world_id", "client_session_id", "daemon_session_id"):
            if field in payload:
                _string_field(payload[field], f"payload.{field}", max_length=MAX_ID_LENGTH)
        if "state" in payload:
            _string_field(payload["state"], "payload.state", max_length=64)
        if "hello_seq" in payload:
            hello_seq = payload["hello_seq"]
            if not _is_int(hello_seq) or hello_seq < 0:
                raise ProtocolError("payload.hello_seq must be a non-negative integer")

    elif msg_type == "user_message":
        if not exchange_id:
            raise ProtocolError("user_message requires an exchange_id")
        _string_field(
            payload.get("turn_id"),
            "payload.turn_id",
            required=True,
            allow_empty=False,
            max_length=MAX_ID_LENGTH,
        )
        parent = payload.get("parent_turn_id")
        if parent is not None:
            _string_field(parent, "payload.parent_turn_id", allow_empty=True, max_length=MAX_ID_LENGTH)
        _string_field(
            payload.get("world_id"),
            "payload.world_id",
            required=True,
            allow_empty=False,
            max_length=MAX_ID_LENGTH,
        )
        text = _string_field(
            payload.get("text"),
            "payload.text",
            required=True,
            allow_empty=False,
            max_length=MAX_TEXT_LENGTH,
        )
        if text is not None and not text.strip():
            raise ProtocolError("payload.text must not be blank")
        context = payload.get("context", {})
        if not isinstance(context, dict):
            raise ProtocolError("payload.context must be an object")
        for field in ("request_id", "client_session_id", "client_turn_id"):
            if field in payload:
                _string_field(payload[field], f"payload.{field}", max_length=MAX_ID_LENGTH)

    elif msg_type == "assistant_start":
        if not exchange_id:
            raise ProtocolError("assistant_start requires an exchange_id")
        _string_field(
            payload.get("world_id"),
            "payload.world_id",
            required=True,
            allow_empty=False,
            max_length=MAX_ID_LENGTH,
        )
        if "model" in payload:
            _string_field(payload["model"], "payload.model")
        for field in ("request_id", "request_turn_id", "client_session_id", "daemon_session_id"):
            if field in payload:
                _string_field(payload[field], f"payload.{field}", max_length=MAX_ID_LENGTH)

    elif msg_type == "assistant_delta":
        if not exchange_id:
            raise ProtocolError("assistant_delta requires an exchange_id")
        _string_field(
            payload.get("world_id"),
            "payload.world_id",
            required=True,
            allow_empty=False,
            max_length=MAX_ID_LENGTH,
        )
        _string_field(payload.get("delta"), "payload.delta", required=True, max_length=MAX_TEXT_LENGTH)
        for field in ("request_id", "request_turn_id", "client_session_id", "daemon_session_id"):
            if field in payload:
                _string_field(payload[field], f"payload.{field}", max_length=MAX_ID_LENGTH)

    elif msg_type == "assistant_end":
        if not exchange_id:
            raise ProtocolError("assistant_end requires an exchange_id")
        _string_field(
            payload.get("world_id"),
            "payload.world_id",
            required=True,
            allow_empty=False,
            max_length=MAX_ID_LENGTH,
        )
        _string_field(
            payload.get("turn_id"),
            "payload.turn_id",
            required=True,
            allow_empty=False,
            max_length=MAX_ID_LENGTH,
        )
        _string_field(
            payload.get("full_text"),
            "payload.full_text",
            required=True,
            allow_empty=False,
            max_length=MAX_TEXT_LENGTH,
        )
        for field in ("request_id", "request_turn_id", "client_turn_id", "parent_turn_id", "client_session_id", "daemon_session_id"):
            if field in payload:
                _string_field(payload[field], f"payload.{field}", max_length=MAX_ID_LENGTH)

    elif msg_type == "error":
        # An error may be session-scoped, for example if a request cannot be
        # associated with a world. code/message are still typed when present.
        for field in ("code", "message"):
            if field in payload:
                _string_field(payload[field], f"payload.{field}", max_length=1000)


def validate_packet(packet: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize an already-decoded packet.

    ``ProtocolError`` is raised for callers that are constructing or testing a
    packet. Network-facing code should normally use :func:`parse_packet`,
    which converts the same failures into an error string.
    """

    if not isinstance(packet, Mapping):
        raise ProtocolError("packet must be a json object")

    version = packet.get("v")
    if not _is_int(version) or version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported version: {version}")

    msg_type = packet.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        raise ProtocolError("missing type field")
    if len(msg_type) > MAX_TYPE_LENGTH:
        raise ProtocolError("type is too long")
    if msg_type not in MESSAGE_TYPES:
        raise ProtocolError(f"unsupported message type: {msg_type}")

    exchange_id = packet.get("exchange_id", "")
    if not isinstance(exchange_id, str):
        raise ProtocolError("exchange_id must be a string")
    if len(exchange_id) > MAX_EXCHANGE_ID_LENGTH:
        raise ProtocolError("exchange_id is too long")

    seq = packet.get("seq", 0)
    if not _is_int(seq) or seq < 0:
        raise ProtocolError("seq must be a non-negative integer")

    tick = packet.get("tick", 0)
    if not _is_int(tick) or tick < 0:
        raise ProtocolError("tick must be a non-negative integer")

    payload = packet.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")

    return {
        "v": PROTOCOL_VERSION,
        "type": msg_type,
        "exchange_id": exchange_id,
        "seq": seq,
        "tick": tick,
        "payload": payload,
    }


def create_packet(
    msg_type: str,
    exchange_id: str = "",
    seq: int = 0,
    payload: Optional[Mapping[str, Any]] = None,
    tick: int = 0,
) -> str:
    """Serialize a v1 packet to compact UTF-8 JSON.

    Construction validates the common envelope. Network-facing handlers call
    :func:`validate_message` when required fields for a specific message type
    must also be enforced. ``tick`` is supplied by callers because the daemon
    does not know the Factorio simulation tick.
    """

    packet: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": msg_type,
        "exchange_id": exchange_id or "",
        "seq": seq,
        "tick": tick,
        "payload": dict(payload) if isinstance(payload, Mapping) else {},
    }
    # Keep the generic envelope constructor usable for diagnostics and for
    # forward-compatible payloads.  Network handlers call
    # ``validate_message`` when they need required fields for a specific
    # message type.
    normalized = validate_packet(packet)
    try:
        raw = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"packet is not JSON serializable: {exc}") from exc
    return raw


def packet_bytes(packet: str) -> bytes:
    """Encode a serialized packet and enforce the v1 datagram budget."""

    if not isinstance(packet, str):
        raise ProtocolError("packet must be a string")
    raw = packet.encode("utf-8")
    if len(raw) > MAX_DATAGRAM_BYTES:
        raise ProtocolError(
            f"packet is {len(raw)} bytes; maximum is {MAX_DATAGRAM_BYTES}"
        )
    return raw


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def validate_message(packet: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a decoded packet including its v1 message-specific fields."""

    normalized = validate_packet(packet)
    _validate_payload(
        normalized["type"],
        normalized["exchange_id"],
        normalized["payload"],
    )
    return normalized


def parse_packet(data: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse and validate a raw UDP datagram.

    Returns ``(packet, None)`` on success and ``(None, reason)`` on every
    malformed-input path. In particular, invalid integer fields and invalid
    UTF-8 never propagate ``ValueError``/``UnicodeDecodeError`` to the daemon.
    """

    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        if not raw:
            return None, "empty packet"
        if len(raw) > MAX_DATAGRAM_BYTES:
            return None, f"packet too large: {len(raw)} bytes"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, f"invalid utf-8: {exc}"
    elif isinstance(data, str):
        if not data.strip():
            return None, "empty packet"
        try:
            if len(data.encode("utf-8")) > MAX_DATAGRAM_BYTES:
                return None, "packet too large"
        except UnicodeEncodeError as exc:
            return None, f"invalid unicode: {exc}"
        text = data
    else:
        return None, "packet must be bytes or string"

    try:
        obj = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        return None, f"json decode error: {exc}"

    if not isinstance(obj, dict):
        return None, "packet must be a json object"

    try:
        return validate_packet(obj), None
    except ProtocolError as exc:
        return None, str(exc)
