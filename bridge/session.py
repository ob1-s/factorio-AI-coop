"""Durable conversation sessions with rollback-safe timeline branching.

``SessionManager`` is the daemon-facing facade.  It deliberately keeps the
small API of the original in-memory implementation while delegating all
state to :mod:`bridge.store`:

* completed turns survive daemon restarts;
* every world has an independent head and parent graph;
* a save reload can move a head to any completed ancestor without deleting
  descendants;
* a generation is invisible to model history until its completion transaction
  commits; and
* canonical turn IDs are allocated by the daemon, not copied from Factorio's
  save-rollback-prone counter.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

try:  # Works both as ``bridge.session`` and when bridge/ is on sys.path.
    from .store import (
        ROOT_TURN_ID,
        SQLiteStore,
        StoreError,
        TurnRecord,
    )
except ImportError:  # pragma: no cover - compatibility for legacy launchers
    from store import (  # type: ignore[no-redef]
        ROOT_TURN_ID,
        SQLiteStore,
        StoreError,
        TurnRecord,
    )


@dataclass
class TurnNode:
    """Compatibility view of a persisted turn.

    ``turn_id`` is always the daemon-issued canonical ID.  A provisional ID
    received from Factorio is available as ``client_turn_id`` and is never
    used as the database key.
    """

    turn_id: str
    parent_turn_id: Optional[str]
    user_text: str
    assistant_text: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    model: Optional[str] = None
    status: str = "pending"
    client_turn_id: Optional[str] = None
    request_id: Optional[str] = None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed" and self.completed_at is not None

    @classmethod
    def from_record(cls, record: TurnRecord) -> "TurnNode":
        return cls(
            turn_id=record.turn_id,
            parent_turn_id=record.parent_turn_id or ROOT_TURN_ID,
            user_text=record.user_text,
            assistant_text=record.assistant_text,
            context=dict(record.context),
            timestamp=record.created_at,
            completed_at=record.completed_at,
            model=record.model,
            status=record.status,
            client_turn_id=record.client_turn_id,
            request_id=record.request_id,
        )


class TurnSnapshot(dict[str, TurnNode]):
    """Dictionary-compatible view with non-counting legacy alias lookups."""

    def __init__(
        self,
        canonical_nodes: Dict[str, TurnNode],
        aliases: Dict[str, TurnNode],
    ) -> None:
        super().__init__(canonical_nodes)
        self._aliases = aliases

    def __getitem__(self, key: str) -> TurnNode:
        try:
            return super().__getitem__(key)
        except KeyError:
            return self._aliases[key]

    def get(self, key: str, default: Optional[TurnNode] = None) -> Optional[TurnNode]:
        if key in self:
            return self[key]
        return default

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or key in self._aliases


class WorldTimeline:
    """Persistent view over one world's conversation DAG."""

    def __init__(self, world_id: str, store: Optional[SQLiteStore] = None):
        self.world_id = str(world_id or "default_world").strip() or "default_world"
        if store is not None:
            self.store = store
        elif (
            os.environ.get("COMPANION_DB_PATH")
            or os.environ.get("COMPANION_DATABASE_PATH")
            or os.environ.get("COMPANION_DATA_DIR")
        ):
            self.store = SQLiteStore()
        else:
            self.store = SQLiteStore(":memory:")
        self._owns_store = store is None
        self.store.ensure_world(self.world_id)

    @property
    def head_turn_id(self) -> str:
        return self.store.head_turn_id(self.world_id)

    @head_turn_id.setter
    def head_turn_id(self, turn_id: Optional[str]) -> None:
        self.store.sync_head(self.world_id, turn_id)

    @property
    def turns(self) -> Dict[str, TurnNode]:
        """Return a fresh compatibility snapshot, keyed by canonical IDs.

        A unique legacy client ID is also exposed as a lookup key.  If a
        client reused that ID across branches, the ambiguous alias is omitted
        rather than pointing at the wrong branch.
        """

        records = self.store.list_turns(self.world_id, include_incomplete=True)
        nodes = {record.turn_id: TurnNode.from_record(record) for record in records}
        aliases: Dict[str, TurnNode] = {}
        ambiguous: set[str] = set()
        for node in nodes.values():
            alias = node.client_turn_id
            if not alias:
                continue
            if alias in ambiguous:
                continue
            previous = aliases.get(alias)
            if previous is not None and previous.turn_id != node.turn_id:
                aliases.pop(alias, None)
                ambiguous.add(alias)
            else:
                aliases[alias] = node
        return TurnSnapshot(nodes, aliases)

    def get_turn(self, turn_id: Optional[str], *, include_incomplete: bool = True) -> Optional[TurnNode]:
        record = self.store.get_turn(
            self.world_id,
            turn_id,
            include_incomplete=include_incomplete,
        )
        return TurnNode.from_record(record) if record is not None else None

    def begin_turn(
        self,
        user_text: str,
        *,
        parent_turn_id: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
        client_turn_id: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnNode:
        record = self.store.begin_turn(
            self.world_id,
            user_text,
            parent_turn_id=parent_turn_id,
            context=context,
            model=model,
            client_turn_id=client_turn_id,
            request_id=request_id,
            canonical_turn_id=canonical_turn_id,
        )
        return TurnNode.from_record(record)

    # ``start_turn`` reads naturally in exchange/streaming code.
    start_turn = begin_turn

    def record_turn(
        self,
        turn_id: Optional[str],
        parent_turn_id: Optional[str],
        user_text: str,
        context: Optional[Mapping[str, Any]] = None,
        *,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnNode:
        """Compatibility wrapper for the old Factorio-provisional API."""

        record = self.store.record_turn(
            self.world_id,
            turn_id,
            parent_turn_id,
            user_text,
            context,
            model=model,
            request_id=request_id,
            canonical_turn_id=canonical_turn_id,
        )
        return TurnNode.from_record(record)

    def complete_turn(
        self,
        turn_id: str,
        assistant_text: str,
        *,
        model: Optional[str] = None,
    ) -> TurnNode:
        record = self.store.complete_turn(
            self.world_id,
            turn_id,
            assistant_text,
            model=model,
        )
        return TurnNode.from_record(record)

    def record_completed_turn(
        self,
        user_text: str,
        assistant_text: str,
        *,
        parent_turn_id: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
        client_turn_id: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnNode:
        record = self.store.record_completed_turn(
            self.world_id,
            user_text,
            assistant_text,
            parent_turn_id=parent_turn_id,
            context=context,
            model=model,
            client_turn_id=client_turn_id,
            request_id=request_id,
            canonical_turn_id=canonical_turn_id,
        )
        return TurnNode.from_record(record)

    commit_turn = record_completed_turn

    def abort_turn(self, turn_id: str) -> Optional[TurnNode]:
        record = self.store.abort_turn(self.world_id, turn_id)
        return TurnNode.from_record(record) if record is not None else None

    def sync_save(self, conversation_head: Optional[str]) -> "WorldTimeline":
        self.store.sync_head(self.world_id, conversation_head)
        return self

    sync_head = sync_save

    def get_linear_turns(
        self,
        head_turn_id: Optional[str] = None,
        *,
        max_turns: Optional[int] = 12,
    ) -> List[TurnNode]:
        records = self.store.linear_turns(
            self.world_id,
            head_turn_id=head_turn_id,
            max_turns=max_turns,
        )
        return [TurnNode.from_record(record) for record in records]

    def get_linear_history(
        self,
        head_turn_id: Optional[str] = None,
        max_turns: int = 12,
    ) -> List[Dict[str, str]]:
        """Walk a completed branch and format it for an LLM.

        Only completed records are supplied by the store, so a process death
        during generation cannot leak partial assistant output into context.
        """

        messages: List[Dict[str, str]] = []
        for node in self.get_linear_turns(head_turn_id, max_turns=max_turns):
            messages.append({"role": "user", "content": node.user_text})
            messages.append({"role": "assistant", "content": node.assistant_text})
        return messages

    history = get_linear_history

    def close(self) -> None:
        if self._owns_store:
            self.store.close()


class SessionManager:
    """Daemon-facing session manager backed by :class:`SQLiteStore`.

    Passing ``db_path``/``database_path`` (or configuring
    ``COMPANION_DB_PATH``/``COMPANION_DATABASE_PATH``/``COMPANION_DATA_DIR``)
    selects the durable on-disk
    store used by the daemon.  With no configuration, the manager still uses
    SQLite but keeps the database in memory, which makes short-lived library
    sessions independent by default.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        store: Optional[SQLiteStore] = None,
        database_path: Optional[str] = None,
    ) -> None:
        if db_path is not None and database_path is not None:
            raise ValueError("pass only one of db_path and database_path")
        if store is not None and (db_path is not None or database_path is not None):
            raise ValueError("store and db_path/database_path are mutually exclusive")
        if store is not None:
            self.store = store
        elif db_path is not None or database_path is not None:
            self.store = SQLiteStore(db_path if db_path is not None else database_path)
        elif (
            os.environ.get("COMPANION_DB_PATH")
            or os.environ.get("COMPANION_DATABASE_PATH")
            or os.environ.get("COMPANION_DATA_DIR")
        ):
            self.store = SQLiteStore()
        else:
            self.store = SQLiteStore(":memory:")
        self._owns_store = store is None
        self.worlds: Dict[str, WorldTimeline] = {}
        self.active_world_id: Optional[str] = None

    @staticmethod
    def _normalize_world_id(world_id: Optional[str]) -> str:
        value = str(world_id or "").strip()
        return value or "default_world"

    def get_or_create_world(
        self,
        world_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> WorldTimeline:
        normalized_world_id = self._normalize_world_id(world_id)
        world = self.worlds.get(normalized_world_id)
        if world is None:
            self.store.ensure_world(normalized_world_id, metadata)
            world = WorldTimeline(normalized_world_id, self.store)
            self.worlds[normalized_world_id] = world
        return world

    def sync_save(
        self,
        world_id: str,
        conversation_head: Optional[str],
    ) -> WorldTimeline:
        world = self.get_or_create_world(world_id)
        world.sync_save(conversation_head)
        self.active_world_id = world.world_id
        return world

    sync_head = sync_save

    def build_llm_messages(
        self,
        world_id: str,
        parent_turn_id: Optional[str],
        current_user_text: str,
        context_snapshot: Optional[Mapping[str, Any]] = None,
        *,
        max_turns: int = 12,
    ) -> List[Dict[str, str]]:
        """Build context from exactly one world's completed ancestor chain."""

        world = self.get_or_create_world(world_id)
        history = world.get_linear_history(
            head_turn_id=parent_turn_id,
            max_turns=max_turns,
        )

        current_content = str(current_user_text)
        if context_snapshot:
            ctx_str = json.dumps(context_snapshot, ensure_ascii=False, indent=1)
            current_content = f"{current_content}\n\n[Live Game Context]\n{ctx_str}"

        return history + [{"role": "user", "content": current_content}]

    def begin_turn(
        self,
        world_id: str,
        user_text: str,
        *,
        parent_turn_id: Optional[str] = None,
        context_snapshot: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
        client_turn_id: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnNode:
        world = self.get_or_create_world(world_id)
        return world.begin_turn(
            user_text,
            parent_turn_id=parent_turn_id,
            context=context_snapshot,
            model=model,
            client_turn_id=client_turn_id,
            request_id=request_id,
            canonical_turn_id=canonical_turn_id,
        )

    start_turn = begin_turn

    def record_turn(
        self,
        world_id: str,
        turn_id: Optional[str] = None,
        parent_turn_id: Optional[str] = None,
        user_text: Any = "",
        context_snapshot: Optional[Mapping[str, Any]] = None,
        *,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnNode:
        # The production UDP daemon historically used an active-world
        # shorthand: record_turn(canonical_id, parent_id, text, context).
        # Keep that call shape working while making the world-explicit API the
        # normal and documented form.
        if context_snapshot is None and isinstance(user_text, Mapping):
            active_world_id = self.active_world_id or "default_world"
            world = self.get_or_create_world(active_world_id)
            return world.begin_turn(
                str(parent_turn_id or ""),
                parent_turn_id=turn_id,
                context=user_text,
                model=model,
                request_id=request_id,
                canonical_turn_id=canonical_turn_id or str(world_id),
            )

        world = self.get_or_create_world(world_id)
        return world.record_turn(
            turn_id,
            parent_turn_id,
            user_text,
            context_snapshot,
            model=model,
            request_id=request_id,
            canonical_turn_id=canonical_turn_id,
        )

    def complete_turn(
        self,
        world_id: str,
        turn_id: str,
        assistant_text: Optional[str] = None,
        *,
        model: Optional[str] = None,
    ) -> TurnNode:
        # Compatibility with the daemon's active-world shorthand:
        # complete_turn(canonical_id, assistant_text).
        if assistant_text is None:
            active_world_id = self.active_world_id or "default_world"
            return self.get_or_create_world(active_world_id).complete_turn(
                world_id,
                turn_id,
                model=model,
            )
        world = self.get_or_create_world(world_id)
        return world.complete_turn(turn_id, assistant_text, model=model)

    def record_completed_turn(
        self,
        world_id: str,
        user_text: str,
        assistant_text: str,
        *,
        parent_turn_id: Optional[str] = None,
        context_snapshot: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
        client_turn_id: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnNode:
        world = self.get_or_create_world(world_id)
        return world.record_completed_turn(
            user_text,
            assistant_text,
            parent_turn_id=parent_turn_id,
            context=context_snapshot,
            model=model,
            client_turn_id=client_turn_id,
            request_id=request_id,
            canonical_turn_id=canonical_turn_id,
        )

    commit_turn = record_completed_turn

    def abort_turn(self, world_id: str, turn_id: str) -> Optional[TurnNode]:
        """Mark an interrupted generation non-canonical without moving head."""

        world = self.get_or_create_world(world_id)
        return world.abort_turn(turn_id)

    def get_history(
        self,
        world_id: str,
        head_turn_id: Optional[str] = None,
        *,
        max_turns: int = 12,
    ) -> List[Dict[str, str]]:
        return self.get_or_create_world(world_id).get_linear_history(
            head_turn_id=head_turn_id,
            max_turns=max_turns,
        )

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> "SessionManager":
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "ROOT_TURN_ID",
    "SessionManager",
    "StoreError",
    "TurnNode",
    "TurnSnapshot",
    "WorldTimeline",
]
