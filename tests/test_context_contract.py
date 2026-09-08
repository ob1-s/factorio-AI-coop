"""Python-testable capability and fog-of-war contract checks."""

from __future__ import annotations

from pathlib import Path
import unittest


CONTEXT_FIELD_BY_CAPABILITY = {
    "telemetry": "player",
    "research": "research",
    "inventory": "inventory",
    "radar_vision": "visible_perception",
    "map_analysis": "charted_knowledge",
}

FORBIDDEN_HIDDEN_KEYS = {"evolution", "evolution_factor", "enemy_evolution_factor"}


class ContextContractError(AssertionError):
    pass


def validate_context_contract(context: dict) -> None:
    """Validate the serialized context boundary, independent of Lua runtime."""

    capabilities = context.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise ContextContractError("context.capabilities must be a list of strings")
    granted = set(capabilities)

    for capability, field in CONTEXT_FIELD_BY_CAPABILITY.items():
        present = field in context
        if capability in granted and not present:
            raise ContextContractError(
                f"unlocked capability {capability!r} has no {field!r} context"
            )
        if capability not in granted and present:
            raise ContextContractError(
                f"locked capability {capability!r} leaked {field!r} context"
            )

    def walk(value, path: str = "context"):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in FORBIDDEN_HIDDEN_KEYS:
                    raise ContextContractError(f"hidden engine field leaked at {path}.{key}")
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(context)


class ContextContractTests(unittest.TestCase):
    def test_locked_capabilities_have_no_corresponding_context_fields(self) -> None:
        context = {
            "capabilities": ["telemetry"],
            "player": {"name": "engineer"},
        }
        validate_context_contract(context)

    def test_unlocked_capabilities_require_their_context_fields(self) -> None:
        context = {
            "capabilities": ["telemetry", "research", "inventory", "radar_vision", "map_analysis"],
            "player": {},
            "research": {},
            "inventory": {},
            "visible_perception": {},
            "charted_knowledge": {},
        }
        validate_context_contract(context)

    def test_a_locked_field_is_a_contract_violation_even_if_prompt_ignores_it(self) -> None:
        context = {
            "capabilities": ["telemetry"],
            "player": {},
            "research": {"current": "secret-research"},
        }
        with self.assertRaises(ContextContractError):
            validate_context_contract(context)

    def test_exact_enemy_evolution_factor_is_not_player_context(self) -> None:
        context = {
            "capabilities": ["telemetry", "map_analysis"],
            "player": {},
            "charted_knowledge": {"bounds": {}, "evolution": 0.42},
        }
        with self.assertRaises(ContextContractError):
            validate_context_contract(context)

    def test_context_lua_has_explicit_capability_gates(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "mod"
            / "FactorioCompanion"
            / "scripts"
            / "companion"
            / "context.lua"
        ).read_text(encoding="utf-8")
        for capability, field in CONTEXT_FIELD_BY_CAPABILITY.items():
            with self.subTest(capability=capability):
                self.assertIn(f'if campaign.has("{capability}") then', source)
                self.assertIn(f"snapshot.{field}", source)

    def test_context_lua_does_not_serialize_hidden_evolution_state(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "mod"
            / "FactorioCompanion"
            / "scripts"
            / "companion"
            / "context.lua"
        ).read_text(encoding="utf-8")
        self.assertNotIn("evolution", source.lower())
