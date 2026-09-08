"""Rollback-safe conversation timeline tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bridge.session import SessionManager, WorldTimeline
from tests.timeline_support import (
    ProductionTimelineApiGap,
    close_session,
    open_persistent_session,
)


def add_completed(
    world: WorldTimeline,
    turn_id: str,
    parent_turn_id: str | None,
    user_text: str,
    assistant_text: str,
):
    node = world.record_turn(turn_id, parent_turn_id, user_text, {"tick": len(user_text)})
    completed = world.complete_turn(turn_id, assistant_text)
    if completed is None:
        raise AssertionError(f"production refused to complete {turn_id}")
    return node


def history_text(history):
    return [(message["role"], message["content"]) for message in history]


class TimelineTests(unittest.TestCase):
    def test_normal_linear_history(self) -> None:
        manager = SessionManager()
        world = manager.get_or_create_world("world-a")
        add_completed(world, "a", "turn_0", "A", "a-reply")
        add_completed(world, "b", "a", "B", "b-reply")

        self.assertEqual(
            history_text(world.get_linear_history()),
            [
                ("user", "A"),
                ("assistant", "a-reply"),
                ("user", "B"),
                ("assistant", "b-reply"),
            ],
        )

    def test_build_llm_messages_appends_grounded_context_without_losing_unicode(self) -> None:
        manager = SessionManager()
        world = manager.get_or_create_world("world-a")
        add_completed(world, "a", "turn_0", "previous", "réponse 🚀")

        messages = manager.build_llm_messages(
            "world-a",
            "a",
            "現在の研究は？",
            {"research": {"current": "steel-processing"}, "player": "工場長"},
        )

        self.assertEqual(messages[-1]["role"], "user")
        self.assertIn("現在の研究は？", messages[-1]["content"])
        self.assertIn("steel-processing", messages[-1]["content"])
        self.assertIn("工場長", messages[-1]["content"])

    def test_fork_from_ancestor_keeps_both_histories(self) -> None:
        manager = SessionManager()
        world = manager.get_or_create_world("world-a")
        add_completed(world, "a", "turn_0", "A", "A!")
        add_completed(world, "b", "a", "B", "B!")
        add_completed(world, "c", "b", "C", "C!")
        add_completed(world, "b-prime", "a", "B-prime", "B-prime!")
        add_completed(world, "c-prime", "b-prime", "C-prime", "C-prime!")

        self.assertEqual(
            history_text(world.get_linear_history("c")),
            [("user", "A"), ("assistant", "A!"), ("user", "B"), ("assistant", "B!"), ("user", "C"), ("assistant", "C!")],
        )
        self.assertEqual(
            history_text(world.get_linear_history("c-prime")),
            [
                ("user", "A"),
                ("assistant", "A!"),
                ("user", "B-prime"),
                ("assistant", "B-prime!"),
                ("user", "C-prime"),
                ("assistant", "C-prime!"),
            ],
        )

    def test_save_rollback_excludes_abandoned_future(self) -> None:
        manager = SessionManager()
        world = manager.get_or_create_world("world-a")
        add_completed(world, "a", "turn_0", "A", "A!")
        add_completed(world, "b", "a", "B", "B!")
        add_completed(world, "c", "b", "C", "C!")

        # Loading the save containing A rewinds only the authoritative head;
        # B/C may remain in the DAG but must not enter the next prompt.
        manager.sync_save("world-a", "a")
        add_completed(world, "b-branch", "a", "B'", "B'!")

        branch = history_text(world.get_linear_history("b-branch"))
        self.assertEqual(branch, [("user", "A"), ("assistant", "A!"), ("user", "B'"), ("assistant", "B'!")])
        self.assertNotIn(("user", "B"), branch)
        self.assertNotIn(("user", "C"), branch)

    def test_worlds_are_isolated_even_when_factorio_turn_ids_repeat(self) -> None:
        manager = SessionManager()
        first = manager.get_or_create_world("world-a")
        second = manager.get_or_create_world("world-b")
        add_completed(first, "turn_1", "turn_0", "A only", "A reply")
        add_completed(second, "turn_1", "turn_0", "B only", "B reply")

        self.assertEqual(history_text(first.get_linear_history()), [("user", "A only"), ("assistant", "A reply")])
        self.assertEqual(history_text(second.get_linear_history()), [("user", "B only"), ("assistant", "B reply")])
        self.assertIsNot(first, second)

    def test_incomplete_turn_never_enters_or_moves_canonical_history(self) -> None:
        manager = SessionManager()
        world = manager.get_or_create_world("world-a")
        add_completed(world, "a", "turn_0", "A", "A!")
        world.record_turn("pending", "a", "still generating", {"tick": 99})

        self.assertEqual(
            history_text(world.get_linear_history("pending")),
            [("user", "A"), ("assistant", "A!")],
        )

        # Empty/failed completion must not become the canonical head.
        world.complete_turn("pending", "")
        self.assertNotEqual(world.head_turn_id, "pending")

    def test_rewound_factorio_id_cannot_overwrite_an_existing_branch(self) -> None:
        manager = SessionManager()
        world = manager.get_or_create_world("world-a")
        add_completed(world, "a", "turn_0", "A", "A!")
        add_completed(world, "factorio-turn-2", "a", "future B", "future B!")

        # Factorio can reuse its local turn ID after loading an old save.  A
        # safe implementation either rejects the duplicate or allocates a new
        # globally unique daemon ID.  Silent replacement is data loss.
        try:
            replacement = world.record_turn(
                "factorio-turn-2", "a", "branched B", {"tick": 12}
            )
        except Exception:
            self.assertEqual(
                history_text(world.get_linear_history("factorio-turn-2")),
                [("user", "A"), ("assistant", "A!"), ("user", "future B"), ("assistant", "future B!")],
            )
            return

        self.assertNotEqual(
            replacement.turn_id,
            "factorio-turn-2",
            "record_turn silently reused a branch identity",
        )

    def _open_persistent_or_fail(self, db_path: Path):
        try:
            return open_persistent_session(db_path)
        except ProductionTimelineApiGap as exc:
            self.fail(str(exc))

    def test_completed_history_survives_daemon_restart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="companion-timeline-") as directory:
            db_path = Path(directory) / "timeline.sqlite3"
            first = self._open_persistent_or_fail(db_path)
            try:
                world = first.get_or_create_world("world-a")
                add_completed(world, "a", "turn_0", "A", "A!")
                add_completed(world, "b", "a", "B", "B!")
            finally:
                close_session(first)

            second = self._open_persistent_or_fail(db_path)
            try:
                world = second.get_or_create_world("world-a")
                self.assertEqual(
                    history_text(world.get_linear_history("b")),
                    [("user", "A"), ("assistant", "A!"), ("user", "B"), ("assistant", "B!")],
                )
            finally:
                close_session(second)
    def test_incomplete_generation_is_not_promoted_after_restart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="companion-timeline-") as directory:
            db_path = Path(directory) / "timeline.sqlite3"
            first = self._open_persistent_or_fail(db_path)
            try:
                world = first.get_or_create_world("world-a")
                add_completed(world, "a", "turn_0", "A", "A!")
                world.record_turn("pending", "a", "unfinished", {"tick": 10})
            finally:
                close_session(first)

            second = self._open_persistent_or_fail(db_path)
            try:
                world = second.get_or_create_world("world-a")
                self.assertEqual(
                    history_text(world.get_linear_history("pending")),
                    [("user", "A"), ("assistant", "A!")],
                )
                self.assertNotEqual(world.head_turn_id, "pending")
            finally:
                close_session(second)

    def test_persistent_world_ids_do_not_cross_contaminate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="companion-timeline-") as directory:
            db_path = Path(directory) / "timeline.sqlite3"
            first = self._open_persistent_or_fail(db_path)
            try:
                add_completed(first.get_or_create_world("world-a"), "turn_1", "turn_0", "A", "A!")
                add_completed(first.get_or_create_world("world-b"), "turn_1", "turn_0", "B", "B!")
            finally:
                close_session(first)

            second = self._open_persistent_or_fail(db_path)
            try:
                self.assertEqual(
                    history_text(second.get_or_create_world("world-a").get_linear_history("turn_1")),
                    [("user", "A"), ("assistant", "A!")],
                )
                self.assertEqual(
                    history_text(second.get_or_create_world("world-b").get_linear_history("turn_1")),
                    [("user", "B"), ("assistant", "B!")],
                )
            finally:
                close_session(second)
