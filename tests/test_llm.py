"""OpenAI-compatible backend tests using only a local fake HTTP server."""

from __future__ import annotations

import unittest

from bridge.openai import Llm, LlmError
from bridge.llm.openai import LLMError, OpenAICompatibleClient
from tests.harness import FakeLlmServer, FakeResponse


class OpenAiCompatibleTests(unittest.TestCase):
    def test_sse_stream_preserves_unicode_and_reports_each_delta(self) -> None:
        with FakeLlmServer(
            [
                FakeResponse(
                    text="Olá 世界 🚀",
                    chunks=("Olá ", "世界 ", "🚀"),
                )
            ]
        ) as fake:
            deltas: list[str] = []
            client = Llm(base_url=fake.base_url, api_key="test-key")
            text, mode = client.stream(
                [
                    {"role": "user", "parts": [{"text": "previous"}]},
                    {"role": "model", "parts": [{"text": "earlier"}]},
                ],
                system="You are a test companion.",
                on_delta=deltas.append,
                model="fake-model",
            )
            request = fake.get_request()

        self.assertEqual((text, mode), ("Olá 世界 🚀", "stream"))
        self.assertEqual(deltas, ["Olá ", "世界 ", "🚀"])
        self.assertEqual(request["model"], "fake-model")
        self.assertTrue(request["stream"])
        self.assertEqual(
            request["messages"],
            [
                {"role": "system", "content": "You are a test companion."},
                {"role": "user", "content": "previous"},
                {"role": "assistant", "content": "earlier"},
            ],
        )

    def test_http_model_failure_is_reported_without_fabricating_text(self) -> None:
        with FakeLlmServer([FakeResponse(status=503)]) as fake:
            client = Llm(base_url=fake.base_url, api_key="test-key")
            with self.assertRaises(LlmError) as raised:
                client.stream([], model="fake-model")

        self.assertIn("HTTP 503", str(raised.exception))

    def test_non_streaming_completion_is_supported_for_fallbacks(self) -> None:
        with FakeLlmServer(
            [FakeResponse(text="  final answer  ", stream=False)]
        ) as fake:
            client = Llm(base_url=fake.base_url, api_key="test-key")
            text = client.complete([], model="fake-model")
            request = fake.get_request()

        self.assertEqual(text, "final answer")
        self.assertFalse(request["stream"])


class CurrentDaemonClientTests(unittest.TestCase):
    def test_daemon_client_stream_preserves_unicode_and_normalizes_messages(self) -> None:
        answer = "Olá, fábrica 🚀"
        with FakeLlmServer(
            [FakeResponse(text=answer, chunks=("Olá, ", "fábrica ", "🚀"))]
        ) as fake:
            deltas: list[str] = []
            client = OpenAICompatibleClient(
                base_url=fake.base_url,
                model="fake-model",
                api_key="test-key",
            )
            text = client.stream(
                [
                    {"role": "user", "content": "previous"},
                    {"role": "model", "parts": [{"text": "earlier"}]},
                ],
                system="You are a test companion.",
                on_delta=deltas.append,
            )
            request = fake.get_request()

        self.assertEqual(text, answer)
        self.assertEqual(deltas, ["Olá, ", "fábrica ", "🚀"])
        self.assertEqual(request["model"], "fake-model")
        self.assertTrue(request["stream"])
        self.assertEqual(
            request["messages"],
            [
                {"role": "system", "content": "You are a test companion."},
                {"role": "user", "content": "previous"},
                {"role": "assistant", "content": "earlier"},
            ],
        )

    def test_daemon_client_exposes_provider_http_failure_code(self) -> None:
        with FakeLlmServer([FakeResponse(status=503)]) as fake:
            client = OpenAICompatibleClient(base_url=fake.base_url, model="fake-model")
            with self.assertRaises(LLMError) as raised:
                client.stream([], model="fake-model")

        self.assertEqual(raised.exception.code, "MODEL_HTTP")
        self.assertIn("503", str(raised.exception))
