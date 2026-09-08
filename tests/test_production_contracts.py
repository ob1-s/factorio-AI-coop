"""Read-only audits for production paths that have no Factorio runtime in CI."""

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class ProductionContractTests(unittest.TestCase):
    def test_udp_send_failure_is_checked_before_thinking_state(self) -> None:
        text = source("mod/FactorioCompanion/scripts/companion/transport_udp.lua")
        match = re.search(
            r"function transport\.send_user_message\(.*?\nend",
            text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group(0)
        self.assertRegex(
            body,
            r"local\s+\w+\s*,\s*\w+\s*=\s*transport\.send_raw",
            "send_user_message must retain send_raw success/error semantics",
        )
        self.assertNotRegex(
            body,
            r"transport\.send_raw\(pkt,\s*player\)\s*\n\s*transport\.set_status\(\"thinking\"",
            "a failed UDP send must not leave the UI thinking",
        )
    def test_capability_change_does_not_raise_a_built_in_event_as_custom(self) -> None:
        text = source("mod/FactorioCompanion/scripts/companion/campaign.lua")
        self.assertNotRegex(
            text,
            r"script\.raise_event\(\s*defines\.events\.",
            "Factorio built-in event IDs cannot stand in for a custom event",
        )

    def test_normal_product_entrypoint_is_not_the_legacy_rcon_agent(self) -> None:
        text = source("bridge/agent.py")
        self.assertNotIn(
            "import gameapi",
            text,
            "the normal companion path must not remain the RCON polling agent",
        )
