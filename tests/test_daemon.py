"""Wire-level daemon tests with a fake Factorio peer and fake LLM."""

from __future__ import annotations

import time
import unittest

from bridge.daemon import DeltaBatcher
from tests.harness import (
    CosmeticStream,
    DaemonProcess,
    FakeLlmServer,
    FakeResponse,
    HarnessError,
    UdpPeer,
)


class DeltaBatcherTests(unittest.TestCase):
    def test_provider_chunks_are_coalesced_bounded_and_lossless(self) -> None:
        emitted: list[str] = []
        batcher = DeltaBatcher(emitted.append, interval=10.0, max_chars=4)

        batcher.add("ab")
        batcher.add("cdefgh")
        batcher.flush()

        self.assertEqual(emitted, ["abcd", "efgh"])
        self.assertEqual("".join(emitted), "abcdefgh")
        self.assertTrue(all(0 < len(chunk) <= 4 for chunk in emitted))

    def test_idle_interval_flushes_before_appending_the_next_chunk(self) -> None:
        emitted: list[str] = []
        now = [0.0]
        batcher = DeltaBatcher(
            emitted.append,
            interval=1.0,
            max_chars=100,
            clock=lambda: now[0],
        )

        batcher.add("first")
        now[0] = 1.1
        batcher.add("second")
        batcher.flush()

        self.assertEqual(emitted, ["first", "second"])


class DaemonAvailabilityTests(unittest.TestCase):
    def test_production_udp_daemon_entrypoint_exists(self) -> None:
        self.assertTrue(
            DaemonProcess.available(),
            "bridge.daemon is missing; the UDP daemon is required by the product contract",
        )


@unittest.skipUnless(
    DaemonProcess.available(),
    "production bridge.daemon has not landed; see DaemonAvailabilityTests",
)
class DaemonIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.llm = FakeLlmServer()
        self.peer = UdpPeer()
        self.daemon = DaemonProcess(
            # Let the OS allocate the port.  A preflight bind/close has a
            # race with unrelated local UDP services and made the harness
            # flaky on development machines.
            port=0,
            llm_base_url=self.llm.base_url,
            db_path=self._db_path(),
        )
        try:
            self.daemon.start()
        except Exception:
            self.peer.close()
            self.llm.close()
            raise

    def _db_path(self):
        # unittest does not provide pytest's tmp_path fixture.  The daemon
        # accepts a per-test temporary path through the standard tempfile API.
        import tempfile
        from pathlib import Path

        self._temporary_directory = tempfile.TemporaryDirectory(prefix="companion-daemon-")
        return Path(self._temporary_directory.name) / "timeline.sqlite3"

    def tearDown(self) -> None:
        self.daemon.stop()
        self.peer.close()
        self.llm.close()
        temporary_directory = getattr(self, "_temporary_directory", None)
        if temporary_directory is not None:
            temporary_directory.cleanup()

    def handshake(
        self,
        *,
        peer: UdpPeer | None = None,
        world_id: str = "world-a",
        conversation_head: str = "turn_0",
    ):
        peer = peer or self.peer
        peer.hello(
            self.daemon.address,
            world_id=world_id,
            conversation_head=conversation_head,
        )
        return peer.wait_for_type("hello_ack")

    def test_hello_is_acknowledged_with_ready_state(self) -> None:
        ack = self.handshake()
        payload = ack["payload"]
        self.assertEqual(ack["type"], "hello_ack")
        self.assertEqual(payload.get("active_world_id"), "world-a")
        self.assertEqual(payload.get("client_session_id"), "client-test-session")
        self.assertTrue(payload.get("daemon_session_id"))
        self.assertEqual(payload.get("status"), "ready")

    def test_user_message_produces_start_deltas_and_authoritative_end(self) -> None:
        answer = "Olá, fábrica 🚀"
        self.llm.add_response(
            FakeResponse(text=answer, chunks=("Olá, ", "fábrica ", "🚀"))
        )
        self.handshake()
        exchange = self.peer.user_message(
            self.daemon.address,
            turn_id="turn-1",
            text="What is happening?",
            context={"capabilities": ["telemetry"], "player": {"name": "engineer"}},
        )
        self.peer.wait_for_type("assistant_end", exchange_id=exchange)

        exchange_packets = [
            packet
            for packet in self.peer.received
            if packet.get("exchange_id") == exchange
        ]
        types = [packet["type"] for packet in exchange_packets]
        self.assertIn("assistant_start", types)
        self.assertIn("assistant_delta", types)
        self.assertEqual(types[-1], "assistant_end")
        self.assertLess(types.index("assistant_start"), types.index("assistant_end"))

        start = next(packet for packet in exchange_packets if packet["type"] == "assistant_start")
        end = exchange_packets[-1]
        self.assertEqual(start["payload"].get("client_session_id"), "client-test-session")
        self.assertTrue(start["payload"].get("daemon_session_id"))
        self.assertEqual(end["payload"].get("client_session_id"), "client-test-session")
        self.assertTrue(end["payload"].get("daemon_session_id"))

        stream = CosmeticStream(exchange)
        for packet in exchange_packets:
            stream.consume(packet)
        self.assertTrue(stream.started)
        self.assertEqual(stream.final_text, answer)
        self.assertEqual(stream.cosmetic_text, answer)
        self.assertEqual(stream.ignored_sequences, [])

        request = self.llm.get_request()
        self.assertTrue(request["stream"])
        self.assertIn("What is happening?", request["messages"][-1]["content"])
        self.assertIn("engineer", request["messages"][-1]["content"])

    def test_model_failure_returns_error_and_no_successful_end(self) -> None:
        self.llm.add_response(FakeResponse(status=503))
        self.handshake()
        exchange = self.peer.user_message(
            self.daemon.address,
            turn_id="turn-error",
            text="fail this request",
        )
        error = self.peer.wait_for_type("error", exchange_id=exchange)

        self.assertTrue(error["payload"].get("message") or error["payload"].get("code"))
        self.assertFalse(
            any(
                packet.get("type") == "assistant_end"
                and packet.get("exchange_id") == exchange
                for packet in self.peer.received
            )
        )
        self.daemon.assert_running()

    def test_malformed_packets_do_not_kill_daemon_or_block_reconnect(self) -> None:
        self.peer.send_raw(self.daemon.address, b"{ definitely not json")
        self.peer.send_raw(
            self.daemon.address,
            '{"v":999,"type":"hello","payload":{',
        )
        self.daemon.assert_running()
        ack = self.handshake()
        self.assertEqual(ack["type"], "hello_ack")
        self.daemon.assert_running()

    def test_factorio_peer_can_disappear_and_reconnect(self) -> None:
        self.handshake()
        self.peer.close()
        replacement = UdpPeer()
        self.addCleanup(replacement.close)

        replacement.hello(
            self.daemon.address,
            world_id="world-a",
            conversation_head="turn_0",
        )
        ack = replacement.wait_for_type("hello_ack")
        self.assertEqual(ack["payload"].get("active_world_id"), "world-a")
        self.daemon.assert_running()
        self.peer = replacement

    def test_heartbeat_is_served_during_long_inference(self) -> None:
        self.llm.add_response(
            FakeResponse(
                text="long answer",
                chunks=("long ", "answer"),
                delay_before=1.4,
            )
        )
        self.handshake()
        exchange = self.peer.user_message(
            self.daemon.address,
            turn_id="turn-long",
            text="take your time",
        )

        deadline = time.monotonic() + 1.0
        heartbeat_replies = 0
        sequence = 1
        while time.monotonic() < deadline:
            self.peer.heartbeat(self.daemon.address, seq=sequence, tick=sequence)
            sequence += 1
            try:
                self.peer.wait_for_type("heartbeat", timeout=0.35)
            except HarnessError:
                continue
            heartbeat_replies += 1
            break

        self.assertGreaterEqual(
            heartbeat_replies,
            1,
            "daemon stopped servicing UDP liveness while model inference was running",
        )
        end = self.peer.wait_for_type("assistant_end", exchange_id=exchange, timeout=3.0)
        self.assertEqual(end["payload"]["full_text"], "long answer")
        self.daemon.assert_running()

    def test_restart_reloads_completed_history(self) -> None:
        self.llm.add_response(FakeResponse(text="first answer", chunks=("first answer",)))
        self.handshake()
        first_exchange = self.peer.user_message(
            self.daemon.address,
            turn_id="factorio-turn-1",
            text="first question",
        )
        first_end = self.peer.wait_for_type("assistant_end", exchange_id=first_exchange)
        first_turn_id = first_end["payload"]["turn_id"]
        self.llm.get_request()

        self.daemon.stop()
        self.peer.close()
        self.peer = UdpPeer()
        self.daemon = DaemonProcess(
            port=self.daemon.port,
            llm_base_url=self.llm.base_url,
            db_path=self.daemon.db_path,
        )
        self.daemon.start()
        ack = self.handshake(conversation_head=first_turn_id)
        self.assertEqual(ack["payload"].get("active_turn_id"), first_turn_id)

        self.llm.add_response(FakeResponse(text="second answer", chunks=("second answer",)))
        second_exchange = self.peer.user_message(
            self.daemon.address,
            turn_id="factorio-turn-2",
            parent_turn_id=first_turn_id,
            text="second question",
        )
        self.peer.wait_for_type("assistant_end", exchange_id=second_exchange)
        second_request = self.llm.get_request()
        contents = "\n".join(message.get("content", "") for message in second_request["messages"])
        self.assertIn("first question", contents)
        self.assertIn("first answer", contents)
        self.assertIn("second question", contents)

    def test_rapid_messages_do_not_interleave_generations(self) -> None:
        self.llm.add_response(
            FakeResponse(text="one", chunks=("o", "ne"), delay_between=0.1)
        )
        self.llm.add_response(FakeResponse(text="two", chunks=("two",)))
        self.handshake()
        first = self.peer.user_message(
            self.daemon.address,
            turn_id="turn-rapid-1",
            text="first rapid message",
        )
        second = self.peer.user_message(
            self.daemon.address,
            turn_id="turn-rapid-2",
            text="second rapid message",
        )

        terminal: dict[str, dict] = {}
        stream_events: list[dict] = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(terminal) < 2:
            packet = self.peer.receive(0.1)
            if packet is None:
                continue
            exchange = packet.get("exchange_id")
            if exchange not in {first, second}:
                continue
            stream_events.append(packet)
            if packet["type"] in {"assistant_end", "error"}:
                terminal[exchange] = packet

        self.assertEqual(set(terminal), {first, second})
        active: str | None = None
        for packet in stream_events:
            exchange = packet["exchange_id"]
            if packet["type"] == "assistant_start":
                self.assertIsNone(
                    active,
                    f"generation {exchange} started while {active} was still active",
                )
                active = exchange
            elif packet["type"] == "assistant_delta":
                self.assertEqual(active, exchange)
            elif packet["type"] == "assistant_end":
                self.assertEqual(active, exchange)
                active = None
            elif packet["type"] == "error" and active == exchange:
                active = None
        self.assertIsNone(active)

        successful_turn_ids = [
            packet["payload"].get("turn_id")
            for packet in terminal.values()
            if packet["type"] == "assistant_end"
        ]
        self.assertEqual(len(successful_turn_ids), len(set(successful_turn_ids)))
