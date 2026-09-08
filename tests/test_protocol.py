"""Protocol contract tests for the Python side of the UDP bridge."""

from __future__ import annotations

import json
import unittest

from bridge import protocol
from tests.harness import CosmeticStream


def parse_without_crashing(raw: bytes | str):
    """Call the production parser; an exception is itself a test failure."""

    packet, error = protocol.parse_packet(raw)
    assert packet is None or error is None
    return packet, error


class ProtocolTests(unittest.TestCase):
    def test_valid_packet_round_trips_as_utf8_bytes(self) -> None:
        payload = {
            "text": "Olá, фабрика 🚀 — こんにちは",
            "nested": {"answer": True, "count": 3},
            "turn_id": "turn-1",
            "world_id": "world-a",
            "context": {"capabilities": ["telemetry"]},
        }
        raw = protocol.create_packet(
            "user_message",
            exchange_id="ex_world-µ_7",
            seq=4,
            tick=123,
            payload=payload,
        )

        packet, error = parse_without_crashing(raw.encode("utf-8"))

        self.assertIsNone(error)
        self.assertEqual(
            packet,
            {
                "v": protocol.PROTOCOL_VERSION,
                "type": "user_message",
                "exchange_id": "ex_world-µ_7",
                "seq": 4,
                "tick": 123,
                "payload": payload,
            },
        )

    def test_long_unicode_text_is_not_truncated_or_reencoded(self) -> None:
        text = ("工場🚀 café — " * 120).rstrip()
        raw = protocol.create_packet(
            "user_message",
            exchange_id="ex_long",
            payload={
                "turn_id": "turn-long",
                "world_id": "world-a",
                "text": text,
                "context": {"capabilities": ["telemetry"]},
            },
        )

        packet, error = parse_without_crashing(raw)

        self.assertIsNone(error)
        self.assertIsNotNone(packet)
        assert packet is not None
        self.assertEqual(packet["payload"]["text"], text)
        self.assertGreater(len(text), 1_000)
        self.assertLess(len(raw.encode("utf-8")), protocol.MAX_DATAGRAM_BYTES)

    def test_oversized_datagram_is_rejected_without_truncation(self) -> None:
        raw = protocol.create_packet(
            "user_message",
            exchange_id="ex_too-large",
            payload={
                "turn_id": "turn-large",
                "world_id": "world-a",
                "text": "x" * 4_200,
                "context": {"capabilities": ["telemetry"]},
            },
        )
        self.assertGreater(len(raw.encode("utf-8")), protocol.MAX_DATAGRAM_BYTES)

        packet, error = parse_without_crashing(raw)

        self.assertIsNone(packet)
        self.assertTrue(error and "large" in error)

    def test_packet_bytes_enforces_the_outgoing_datagram_budget(self) -> None:
        small = protocol.create_packet("heartbeat", payload={"state": "ready"})
        self.assertEqual(protocol.packet_bytes(small), small.encode("utf-8"))

        large = protocol.create_packet(
            "assistant_delta",
            exchange_id="ex_large",
            payload={"delta": "🚀" * 1_500},
        )
        self.assertGreater(len(large.encode("utf-8")), protocol.MAX_DATAGRAM_BYTES)
        with self.assertRaises(protocol.ProtocolError):
            protocol.packet_bytes(large)

    def test_malformed_packets_are_rejected_without_raising(self) -> None:
        malformed = [
            "",
            "   \n\t",
            b"\xff\xfe",
            "{not-json}",
            "[]",
            "null",
            '"a string"',
            '{"v": 1, "type": "heartbeat",',
        ]
        for raw in malformed:
            with self.subTest(raw=raw):
                packet, error = parse_without_crashing(raw)
                self.assertIsNone(packet)
                self.assertTrue(error)

    def test_invalid_protocol_versions_are_rejected(self) -> None:
        for version in [0, 2, "1", None, True]:
            with self.subTest(version=version):
                raw = json.dumps({"v": version, "type": "heartbeat", "payload": {}})
                packet, error = parse_without_crashing(raw)
                self.assertIsNone(packet)
                self.assertTrue(error and "version" in error)

    def test_invalid_message_type_values_are_rejected(self) -> None:
        for msg_type in [None, "", 1, [], {}, True]:
            with self.subTest(msg_type=msg_type):
                raw = json.dumps({"v": 1, "type": msg_type, "payload": {}})
                packet, error = parse_without_crashing(raw)
                self.assertIsNone(packet)
                self.assertTrue(error)

    def test_invalid_envelope_field_types_are_rejected(self) -> None:
        invalid_fields = [
            ("exchange_id", 7),
            ("exchange_id", []),
            ("seq", "not-an-integer"),
            ("seq", 1.5),
            ("seq", True),
            ("tick", "not-an-integer"),
            ("tick", 1.5),
            ("tick", None),
            ("payload", []),
            ("payload", "text"),
            ("payload", None),
        ]
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                obj = {
                    "v": 1,
                    "type": "heartbeat",
                    "exchange_id": "",
                    "seq": 0,
                    "tick": 0,
                    "payload": {},
                }
                obj[field] = value
                packet, error = parse_without_crashing(json.dumps(obj))
                self.assertIsNone(packet, f"accepted invalid {field}={value!r}: {packet!r}")
                self.assertTrue(error)

    def test_sequence_and_tick_are_unsigned(self) -> None:
        for field in ["seq", "tick"]:
            with self.subTest(field=field):
                obj = {"v": 1, "type": "heartbeat", "seq": 0, "tick": 0, "payload": {}}
                obj[field] = -1
                packet, error = parse_without_crashing(json.dumps(obj))
                self.assertIsNone(packet)
                self.assertTrue(error)

    def test_unknown_message_types_are_not_silently_accepted(self) -> None:
        raw = json.dumps({"v": 1, "type": "future_typo", "payload": {}})
        packet, error = parse_without_crashing(raw)
        self.assertIsNone(packet)
        self.assertTrue(error)

    def test_message_specific_required_fields_and_types_are_strictly_rejected(self) -> None:
        cases = [
            {
                "v": 1,
                "type": "hello",
                "payload": {"world_id": "world-a", "conversation_head": "turn_0", "capabilities": [1]},
            },
            {
                "v": 1,
                "type": "hello",
                "exchange_id": "unexpected",
                "payload": {"world_id": "world-a", "conversation_head": "turn_0"},
            },
            {
                "v": 1,
                "type": "user_message",
                "exchange_id": "ex-user",
                "payload": {
                    "world_id": "world-a",
                    "turn_id": "turn-1",
                    "text": 42,
                    "context": {},
                },
            },
            {
                "v": 1,
                "type": "user_message",
                "exchange_id": "ex-user",
                "payload": {
                    "world_id": "world-a",
                    "turn_id": "turn-1",
                    "text": "   \n",
                    "context": {},
                },
            },
            {
                "v": 1,
                "type": "assistant_delta",
                "exchange_id": "ex-assistant",
                "payload": {"delta": 99},
            },
            {
                "v": 1,
                "type": "assistant_end",
                "exchange_id": "ex-assistant",
                "payload": {"turn_id": "turn-1"},
            },
            {
                "v": 1,
                "type": "error",
                "exchange_id": "ex-error",
                "payload": {"code": ["not", "a", "string"]},
            },
        ]
        for obj in cases:
            with self.subTest(message=obj["type"]):
                packet, error = parse_without_crashing(json.dumps(obj))
                self.assertIsNone(error)
                self.assertIsNotNone(packet)
                assert packet is not None
                with self.assertRaises(protocol.ProtocolError):
                    protocol.validate_message(packet)

    def test_duplicate_and_out_of_order_deltas_are_cosmetic_only(self) -> None:
        exchange = "ex_world-a_turn-1"
        stream = CosmeticStream(exchange)
        packets = [
            protocol.parse_packet(
                protocol.create_packet("assistant_start", exchange, seq=1, payload={"model": "fake"})
            )[0],
            protocol.parse_packet(
                protocol.create_packet("assistant_delta", exchange, seq=2, payload={"delta": "hel"})
            )[0],
            protocol.parse_packet(
                protocol.create_packet("assistant_delta", exchange, seq=2, payload={"delta": "hel"})
            )[0],
            protocol.parse_packet(
                protocol.create_packet("assistant_delta", exchange, seq=1, payload={"delta": "late"})
            )[0],
            # Sequence 4 is absent: the authoritative end repairs the cosmetic
            # stream, as required by the UDP design.
            protocol.parse_packet(
                protocol.create_packet("assistant_delta", exchange, seq=3, payload={"delta": "o"})
            )[0],
            protocol.parse_packet(
                protocol.create_packet(
                    "assistant_end",
                    exchange,
                    seq=5,
                    payload={"turn_id": "turn-1", "full_text": "hello"},
                )
            )[0],
        ]

        for packet in packets:
            self.assertIsNotNone(packet)
            assert packet is not None
            stream.consume(packet)

        self.assertTrue(stream.started)
        self.assertEqual(stream.ignored_sequences, [2, 1])
        self.assertEqual(stream.final_text, "hello")
        self.assertEqual(stream.cosmetic_text, "hello")
