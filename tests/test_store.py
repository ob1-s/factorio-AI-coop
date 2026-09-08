"""Direct SQLite invariants behind the public session/timeline facade."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bridge.store import (
    CrossWorldTurnError,
    IncompleteTurnError,
    SQLiteStore,
    TurnIdCollision,
)


def text_history(store: SQLiteStore, world_id: str, head: str | None = None):
    return [
        (turn.user_text, turn.assistant_text)
        for turn in store.linear_turns(world_id, head_turn_id=head)
    ]


class SQLiteStoreTests(unittest.TestCase):
    def test_completed_turns_persist_and_pending_turns_do_not_enter_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="companion-store-") as directory:
            path = Path(directory) / "timeline.sqlite3"
            first = SQLiteStore(path)
            first.ensure_world("world-a")
            completed = first.record_completed_turn(
                "world-a", "A", "A!", client_turn_id="factorio-1"
            )
            pending = first.begin_turn(
                "world-a", "unfinished", parent_turn_id=completed.turn_id
            )
            self.assertEqual(text_history(first, "world-a", pending.turn_id), [("A", "A!")])
            first.close()

            second = SQLiteStore(path)
            try:
                self.assertEqual(
                    text_history(second, "world-a", completed.turn_id),
                    [("A", "A!")],
                )
                self.assertEqual(second.head_turn_id("world-a"), completed.turn_id)
                self.assertEqual(second.pending_turns("world-a")[0].turn_id, pending.turn_id)
            finally:
                second.close()

    def test_save_rollback_and_fork_are_parent_linked_not_destructive(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.ensure_world("world-a")
            a = store.record_completed_turn("world-a", "A", "A!")
            b = store.record_completed_turn("world-a", "B", "B!", parent_turn_id=a.turn_id)
            c = store.record_completed_turn("world-a", "C", "C!", parent_turn_id=b.turn_id)
            b_prime = store.record_completed_turn(
                "world-a", "B'", "B'!", parent_turn_id=a.turn_id
            )
            c_prime = store.record_completed_turn(
                "world-a", "C'", "C'!", parent_turn_id=b_prime.turn_id
            )

            self.assertEqual(text_history(store, "world-a", c.turn_id), [("A", "A!"), ("B", "B!"), ("C", "C!")])
            self.assertEqual(text_history(store, "world-a", c_prime.turn_id), [("A", "A!"), ("B'", "B'!"), ("C'", "C'!")])
            store.sync_head("world-a", a.turn_id)
            self.assertEqual(store.head_turn_id("world-a"), a.turn_id)
            self.assertEqual(text_history(store, "world-a"), [("A", "A!")])
            self.assertEqual(text_history(store, "world-a", c.turn_id), [("A", "A!"), ("B", "B!"), ("C", "C!")])
        finally:
            store.close()

    def test_reused_client_turn_id_gets_distinct_canonical_ids(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.ensure_world("world-a")
            first = store.record_completed_turn(
                "world-a", "future", "future!", client_turn_id="turn_2"
            )
            second = store.record_completed_turn(
                "world-a", "branch", "branch!", parent_turn_id="turn_0", client_turn_id="turn_2"
            )
            self.assertNotEqual(first.turn_id, second.turn_id)
            self.assertEqual(first.client_turn_id, second.client_turn_id)
            self.assertEqual(text_history(store, "world-a", first.turn_id), [("future", "future!")])
            self.assertEqual(text_history(store, "world-a", second.turn_id), [("branch", "branch!")])
        finally:
            store.close()

    def test_id_factory_collision_is_not_an_overwrite(self) -> None:
        candidates = iter(["same", "same", "different"])
        store = SQLiteStore(":memory:", id_factory=lambda: next(candidates))
        try:
            store.ensure_world("world-a")
            first = store.record_completed_turn("world-a", "A", "A!")
            second = store.record_completed_turn("world-a", "B", "B!")
            self.assertNotEqual(first.turn_id, second.turn_id)
        finally:
            store.close()

    def test_cross_world_parent_is_rejected(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.ensure_world("world-a")
            store.ensure_world("world-b")
            first = store.record_completed_turn("world-a", "A", "A!")
            with self.assertRaises(CrossWorldTurnError):
                store.record_completed_turn(
                    "world-b", "B", "B!", parent_turn_id=first.turn_id
                )
        finally:
            store.close()

    def test_incomplete_parent_cannot_become_a_child(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.ensure_world("world-a")
            pending = store.begin_turn("world-a", "pending")
            with self.assertRaises(IncompleteTurnError):
                store.record_completed_turn(
                    "world-a", "child", "child!", parent_turn_id=pending.turn_id
                )
            self.assertEqual(store.head_turn_id("world-a"), "turn_0")
        finally:
            store.close()

    def test_failed_empty_completion_does_not_move_head(self) -> None:
        store = SQLiteStore(":memory:")
        try:
            store.ensure_world("world-a")
            pending = store.begin_turn("world-a", "pending")
            # A safe implementation may either reject the empty result or
            # atomically mark it aborted.  Both outcomes preserve the old head.
            try:
                result = store.complete_turn("world-a", pending.turn_id, "")
            except (ValueError, IncompleteTurnError, TurnIdCollision):
                result = None
            if result is not None:
                self.assertEqual(result.status, "aborted")
            self.assertEqual(store.head_turn_id("world-a"), "turn_0")
            self.assertEqual(text_history(store, "world-a"), [])
        finally:
            store.close()
