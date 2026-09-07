"""UDP Protocol framing and envelope validation for Factorio Companion."""
import json
import time

PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    pass


def create_packet(msg_type, exchange_id="", seq=0, payload=None, tick=0):
    """Serialize a protocol packet to JSON string."""
    return json.dumps({
        "v": PROTOCOL_VERSION,
        "type": str(msg_type),
        "exchange_id": str(exchange_id or ""),
        "seq": int(seq),
        "tick": int(tick),
        "payload": payload if isinstance(payload, dict) else {}
    }, ensure_ascii=False)


def parse_packet(data):
    """Parse raw bytes or string into a validated packet dict.

    Returns (packet_dict, None) on success or (None, error_str) on failure.
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, f"invalid utf-8: {exc}"

    if not isinstance(data, str) or not data.strip():
        return None, "empty packet"

    try:
        obj = json.loads(data)
    except json.JSONDecodeError as exc:
        return None, f"json decode error: {exc}"

    if not isinstance(obj, dict):
        return None, "packet must be a json object"

    if obj.get("v") != PROTOCOL_VERSION:
        return None, f"unsupported version: {obj.get('v')}"

    msg_type = obj.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        return None, "missing type field"

    return {
        "v": PROTOCOL_VERSION,
        "type": msg_type,
        "exchange_id": str(obj.get("exchange_id", "")),
        "seq": int(obj.get("seq", 0)),
        "tick": int(obj.get("tick", 0)),
        "payload": obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    }, None
