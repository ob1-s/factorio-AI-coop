"""Durable SQLite storage for the companion conversation timeline.

The Factorio save contains the authoritative conversation *head*, but the
daemon owns the conversation graph.  This module stores that graph durably so
that a daemon restart does not erase completed dialogue and a save reload can
move a world's head back to an ancestor without deleting the abandoned
future.

Only rows with ``status = 'completed'`` are part of canonical conversation
history.  A pending row is useful for recovering/diagnosing an interrupted
generation, but it can never become model context unless it is completed by
the explicit completion transaction.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional


ROOT_TURN_ID = "turn_0"
SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """Base class for conversation-store failures."""


class UnknownWorldError(StoreError):
    """Raised when an operation references a world that does not exist."""


class UnknownTurnError(StoreError):
    """Raised when a turn cannot be found in the requested world."""


class CrossWorldTurnError(StoreError):
    """Raised when a turn from another world is used as a parent or head."""


class IncompleteTurnError(StoreError):
    """Raised when an incomplete turn is used as a canonical parent/head."""


class TurnAlreadyCompletedError(StoreError):
    """Raised when a completed turn is completed again with different data."""


class AmbiguousTurnAliasError(StoreError):
    """Raised when a legacy client turn ID identifies multiple branches."""


class TurnIdCollision(StoreError):
    """Raised if the ID generator cannot produce a fresh canonical ID."""


@dataclass(frozen=True)
class WorldRecord:
    world_id: str
    head_turn_id: Optional[str]
    created_at: float
    updated_at: float
    metadata: dict[str, Any]

    @property
    def public_head_turn_id(self) -> str:
        return self.head_turn_id or ROOT_TURN_ID


@dataclass(frozen=True)
class TurnRecord:
    """A database-backed turn snapshot.

    ``turn_id`` is allocated by the daemon and is globally unique in the
    database.  ``client_turn_id`` is the optional ID supplied by an older
    Factorio client; it is deliberately not used as identity.
    """

    world_id: str
    turn_id: str
    parent_turn_id: Optional[str]
    user_text: str
    assistant_text: str
    context: dict[str, Any]
    created_at: float
    completed_at: Optional[float]
    model: Optional[str]
    status: str
    client_turn_id: Optional[str] = None
    request_id: Optional[str] = None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed" and self.completed_at is not None


def default_database_path() -> Path:
    """Return the persistent database path used when none is supplied.

    ``COMPANION_DB_PATH`` (or its ``COMPANION_DATABASE_PATH`` compatibility
    spelling) is intentionally the most direct override for daemon
    deployments and tests.  The data-directory override is useful for keeping
    all daemon state together without changing the database filename.
    """

    configured_path = os.environ.get("COMPANION_DB_PATH") or os.environ.get(
        "COMPANION_DATABASE_PATH"
    )
    if configured_path:
        return Path(configured_path).expanduser()

    configured_dir = os.environ.get("COMPANION_DATA_DIR")
    if configured_dir:
        return Path(configured_dir).expanduser() / "conversation.sqlite3"

    return Path.home() / ".local" / "share" / "factorio-companion" / "conversation.sqlite3"


def _now() -> float:
    return time.time()


def _json_object(value: Optional[Mapping[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Validate and encode a JSON object, returning its normalized mapping."""

    normalized: dict[str, Any] = dict(value or {})
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encoded, normalized


def _decode_object(encoded: Optional[str]) -> dict[str, Any]:
    if not encoded:
        return {}
    try:
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise StoreError("stored JSON object is invalid") from exc
    if not isinstance(decoded, dict):
        raise StoreError("stored JSON value is not an object")
    return decoded


class SQLiteStore:
    """Thread-safe, transactional SQLite conversation store.

    One connection is kept open for the lifetime of the store.  SQLite's WAL
    journal and full synchronous mode make the small completion transaction
    resilient to a process disappearing during a write.  The database
    connection is safe to use from the daemon's worker threads; each public
    operation serializes its own transaction with an in-process re-entrant
    lock, while SQLite's busy timeout handles other daemon processes sharing
    the same database.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str | None = None,
        *,
        db_path: os.PathLike[str] | str | None = None,
        database_path: os.PathLike[str] | str | None = None,
        timeout: float = 30.0,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        supplied_paths = [value for value in (path, db_path, database_path) if value is not None]
        if len(supplied_paths) > 1:
            raise ValueError("pass only one of path, db_path, or database_path")
        selected_path = supplied_paths[0] if supplied_paths else default_database_path()
        self.path = os.fspath(selected_path)
        self._lock = threading.RLock()
        self._closed = False
        self._id_factory = id_factory or (lambda: secrets.token_hex(16))

        if self.path not in {":memory:", ""} and not self.path.startswith("file:"):
            self.path = os.fspath(Path(self.path).expanduser())
            parent = Path(self.path).parent
            parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            self.path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
            uri=self.path.startswith("file:"),
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_connection()
        self._initialize_schema()

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for diagnostics and read-only tooling."""

        self._ensure_open()
        return self._connection

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreError("conversation store is closed")

    def _configure_connection(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 30000")
            # WAL is durable for file-backed databases and harmlessly falls
            # back to an in-memory journal for :memory: connections.
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")

    def _initialize_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS worlds (
                    world_id TEXT PRIMARY KEY,
                    head_turn_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    world_id TEXT NOT NULL,
                    parent_turn_id TEXT,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    model TEXT,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'aborted')),
                    client_turn_id TEXT,
                    request_id TEXT,
                    UNIQUE (world_id, turn_id),
                    FOREIGN KEY (world_id) REFERENCES worlds(world_id) ON DELETE CASCADE,
                    FOREIGN KEY (world_id, parent_turn_id)
                        REFERENCES turns(world_id, turn_id)
                );

                CREATE INDEX IF NOT EXISTS idx_turns_world_parent
                    ON turns(world_id, parent_turn_id);
                CREATE INDEX IF NOT EXISTS idx_turns_world_status_created
                    ON turns(world_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_turns_world_client
                    ON turns(world_id, client_turn_id, created_at);

                INSERT INTO schema_metadata(key, value)
                VALUES ('schema_version', '1')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """
            )

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self._ensure_open()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                try:
                    self._connection.commit()
                except BaseException:
                    # A failed commit must not leave this connection inside a
                    # half-open transaction on the next daemon request.
                    self._connection.rollback()
                    raise

    @staticmethod
    def _normalize_world_id(world_id: Optional[str]) -> str:
        normalized = str(world_id or "").strip()
        return normalized or "default_world"

    @staticmethod
    def _normalize_turn_id(turn_id: Optional[str]) -> Optional[str]:
        if turn_id is None:
            return None
        normalized = str(turn_id).strip()
        return normalized or None

    @classmethod
    def _is_root_turn_id(cls, turn_id: Optional[str]) -> bool:
        normalized = cls._normalize_turn_id(turn_id)
        return normalized is None or normalized == ROOT_TURN_ID

    @staticmethod
    def _row_to_turn(row: sqlite3.Row) -> TurnRecord:
        return TurnRecord(
            world_id=str(row["world_id"]),
            turn_id=str(row["turn_id"]),
            parent_turn_id=row["parent_turn_id"],
            user_text=str(row["user_text"]),
            assistant_text=str(row["assistant_text"] or ""),
            context=_decode_object(row["context_json"]),
            created_at=float(row["created_at"]),
            completed_at=(float(row["completed_at"]) if row["completed_at"] is not None else None),
            model=(str(row["model"]) if row["model"] is not None else None),
            status=str(row["status"]),
            client_turn_id=(str(row["client_turn_id"]) if row["client_turn_id"] is not None else None),
            request_id=(str(row["request_id"]) if row["request_id"] is not None else None),
        )

    @staticmethod
    def _row_to_world(row: sqlite3.Row) -> WorldRecord:
        return WorldRecord(
            world_id=str(row["world_id"]),
            head_turn_id=(str(row["head_turn_id"]) if row["head_turn_id"] is not None else None),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=_decode_object(row["metadata_json"]),
        )

    def _ensure_world_locked(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> sqlite3.Row:
        encoded_metadata, _ = _json_object(metadata)
        now = _now()
        connection.execute(
            """
            INSERT INTO worlds(world_id, head_turn_id, created_at, updated_at, metadata_json)
            VALUES (?, NULL, ?, ?, ?)
            ON CONFLICT(world_id) DO NOTHING
            """,
            (world_id, now, now, encoded_metadata),
        )
        row = connection.execute(
            "SELECT world_id, head_turn_id, created_at, updated_at, metadata_json "
            "FROM worlds WHERE world_id = ?",
            (world_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - protected by the INSERT above
            raise UnknownWorldError(world_id)
        return row

    def create_world(
        self,
        world_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> WorldRecord:
        normalized_world_id = self._normalize_world_id(world_id)
        with self._transaction(immediate=True) as connection:
            row = self._ensure_world_locked(connection, normalized_world_id, metadata)
        return self._row_to_world(row)

    def get_world(self, world_id: str) -> Optional[WorldRecord]:
        normalized_world_id = self._normalize_world_id(world_id)
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT world_id, head_turn_id, created_at, updated_at, metadata_json "
                "FROM worlds WHERE world_id = ?",
                (normalized_world_id,),
            ).fetchone()
        return self._row_to_world(row) if row is not None else None

    def ensure_world(
        self,
        world_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> WorldRecord:
        return self.create_world(world_id, metadata)

    def _require_world_locked(self, connection: sqlite3.Connection, world_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT world_id, head_turn_id, created_at, updated_at, metadata_json "
            "FROM worlds WHERE world_id = ?",
            (world_id,),
        ).fetchone()
        if row is None:
            raise UnknownWorldError(world_id)
        return row

    def _get_turn_by_id_locked(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            """
            SELECT turn_id, world_id, parent_turn_id, user_text, assistant_text,
                   context_json, created_at, completed_at, model, status,
                   client_turn_id, request_id
            FROM turns WHERE turn_id = ?
            """,
            (turn_id,),
        ).fetchone()

    def _get_alias_candidates_locked(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        client_turn_id: str,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT turn_id, world_id, parent_turn_id, user_text, assistant_text,
                       context_json, created_at, completed_at, model, status,
                       client_turn_id, request_id
                FROM turns
                WHERE world_id = ? AND client_turn_id = ?
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                         created_at DESC, rowid DESC
                """,
                (world_id, client_turn_id),
            ).fetchall()
        )

    def _resolve_turn_locked(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        turn_id: Optional[str],
        *,
        allow_alias: bool = True,
        prefer_pending: bool = False,
        require_unambiguous_alias: bool = False,
    ) -> Optional[sqlite3.Row]:
        normalized_turn_id = self._normalize_turn_id(turn_id)
        if self._is_root_turn_id(normalized_turn_id):
            return None

        exact = self._get_turn_by_id_locked(connection, normalized_turn_id)  # type: ignore[arg-type]
        if exact is not None:
            if str(exact["world_id"]) != world_id:
                raise CrossWorldTurnError(
                    f"turn {normalized_turn_id!r} belongs to world {exact['world_id']!r}, "
                    f"not {world_id!r}"
                )
            return exact

        if not allow_alias:
            return None

        candidates = self._get_alias_candidates_locked(connection, world_id, normalized_turn_id)  # type: ignore[arg-type]
        if not candidates:
            return None

        if prefer_pending:
            pending = [row for row in candidates if row["status"] == "pending"]
            if pending:
                return pending[0]

        if require_unambiguous_alias and len(candidates) != 1:
            raise AmbiguousTurnAliasError(
                f"client turn ID {normalized_turn_id!r} identifies {len(candidates)} turns "
                f"in world {world_id!r}; use the daemon-issued canonical ID"
            )
        return candidates[0]

    def resolve_turn_id(
        self,
        world_id: str,
        turn_id: Optional[str],
        *,
        allow_alias: bool = True,
        prefer_pending: bool = False,
        require_unambiguous_alias: bool = False,
    ) -> Optional[str]:
        """Resolve a canonical or legacy client turn ID for ``world_id``.

        The virtual root is returned as ``None`` because it has no database
        row.  Unknown IDs also return ``None``; callers that need strictness
        should use ``get_turn`` or ``sync_head``.
        """

        normalized_world_id = self._normalize_world_id(world_id)
        normalized_turn_id = self._normalize_turn_id(turn_id)
        if self._is_root_turn_id(normalized_turn_id):
            return None
        self._ensure_open()
        with self._lock:
            self._require_world_locked(self._connection, normalized_world_id)
            row = self._resolve_turn_locked(
                self._connection,
                normalized_world_id,
                normalized_turn_id,
                allow_alias=allow_alias,
                prefer_pending=prefer_pending,
                require_unambiguous_alias=require_unambiguous_alias,
            )
        return str(row["turn_id"]) if row is not None else None

    def _validate_parent_locked(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        parent_turn_id: Optional[str],
    ) -> Optional[str]:
        normalized_parent_id = self._normalize_turn_id(parent_turn_id)
        if self._is_root_turn_id(normalized_parent_id):
            return None

        row = self._resolve_turn_locked(
            connection,
            world_id,
            normalized_parent_id,
            prefer_pending=False,
            require_unambiguous_alias=True,
        )
        if row is None:
            global_row = self._get_turn_by_id_locked(connection, normalized_parent_id)  # type: ignore[arg-type]
            if global_row is not None and str(global_row["world_id"]) != world_id:
                raise CrossWorldTurnError(
                    f"turn {normalized_parent_id!r} belongs to world {global_row['world_id']!r}, "
                    f"not {world_id!r}"
                )
            raise UnknownTurnError(
                f"parent turn {normalized_parent_id!r} does not exist in world {world_id!r}"
            )

        if row["status"] != "completed" or row["completed_at"] is None:
            raise IncompleteTurnError(
                f"parent turn {row['turn_id']!r} is not a completed turn"
            )
        return str(row["turn_id"])

    def _new_canonical_turn_id(self) -> str:
        candidate = str(self._id_factory()).strip()
        if not candidate:
            raise TurnIdCollision("turn ID factory returned an empty ID")
        # Keep the daemon-issued namespace visually distinct from the old
        # Factorio counter while still returning a compact ID in protocol
        # payloads.
        return f"turn_{candidate}"

    def begin_turn(
        self,
        world_id: str,
        user_text: str,
        *,
        parent_turn_id: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
        client_turn_id: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnRecord:
        """Persist an incomplete turn and return its daemon-issued ID.

        This row is intentionally not visible through canonical history until
        :meth:`complete_turn` commits successfully.
        """

        normalized_world_id = self._normalize_world_id(world_id)
        user_text = str(user_text)
        context_json, _ = _json_object(context)
        normalized_client_id = self._normalize_turn_id(client_turn_id)
        normalized_request_id = self._normalize_turn_id(request_id)

        with self._transaction(immediate=True) as connection:
            world_row = self._ensure_world_locked(connection, normalized_world_id)
            effective_parent_id = parent_turn_id
            if effective_parent_id is None or str(effective_parent_id).strip() == "":
                effective_parent_id = world_row["head_turn_id"] or ROOT_TURN_ID
            canonical_parent_id = self._validate_parent_locked(
                connection,
                normalized_world_id,
                effective_parent_id,
            )

            for _attempt in range(16):
                allocated_turn_id = (
                    self._normalize_turn_id(canonical_turn_id)
                    if canonical_turn_id is not None
                    else self._new_canonical_turn_id()
                )
                if self._is_root_turn_id(allocated_turn_id):
                    raise TurnIdCollision("canonical turn ID must not be the virtual root")
                try:
                    now = _now()
                    connection.execute(
                        """
                        INSERT INTO turns(
                            turn_id, world_id, parent_turn_id, user_text,
                            assistant_text, context_json, created_at,
                            completed_at, model, status, client_turn_id, request_id
                        ) VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, 'pending', ?, ?)
                        """,
                        (
                            allocated_turn_id,
                            normalized_world_id,
                            canonical_parent_id,
                            user_text,
                            context_json,
                            now,
                            (str(model) if model is not None else None),
                            normalized_client_id,
                            normalized_request_id,
                        ),
                    )
                    row = self._get_turn_by_id_locked(connection, allocated_turn_id)
                    if row is None:  # pragma: no cover - protected by INSERT
                        raise StoreError("turn disappeared after insertion")
                    return self._row_to_turn(row)
                except sqlite3.IntegrityError as exc:
                    # The only expected insert conflict is an astronomically
                    # unlikely generated-ID collision.  Retrying keeps the
                    # uniqueness guarantee explicit and testable.
                    if "turns.turn_id" not in str(exc) and "UNIQUE constraint failed: turns.turn_id" not in str(exc):
                        raise
                    if canonical_turn_id is not None:
                        raise TurnIdCollision(
                            f"canonical turn ID {allocated_turn_id!r} is already in use"
                        ) from exc
            raise TurnIdCollision("could not allocate a globally unique turn ID")

    # Alias used by callers that think in terms of an exchange lifecycle.
    start_turn = begin_turn

    def record_turn(
        self,
        world_id: str,
        turn_id: Optional[str],
        parent_turn_id: Optional[str],
        user_text: str,
        context: Optional[Mapping[str, Any]] = None,
        *,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnRecord:
        """Compatibility wrapper for the old provisional-ID API."""

        return self.begin_turn(
            world_id,
            user_text,
            parent_turn_id=parent_turn_id,
            context=context,
            model=model,
            client_turn_id=turn_id,
            request_id=request_id,
            canonical_turn_id=canonical_turn_id,
        )

    def _get_turn_locked(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        turn_id: Optional[str],
        *,
        prefer_pending: bool = False,
        require_unambiguous_alias: bool = False,
    ) -> Optional[sqlite3.Row]:
        return self._resolve_turn_locked(
            connection,
            world_id,
            turn_id,
            prefer_pending=prefer_pending,
            require_unambiguous_alias=require_unambiguous_alias,
        )

    def get_turn(
        self,
        world_id: str,
        turn_id: Optional[str],
        *,
        include_incomplete: bool = True,
    ) -> Optional[TurnRecord]:
        normalized_world_id = self._normalize_world_id(world_id)
        normalized_turn_id = self._normalize_turn_id(turn_id)
        if self._is_root_turn_id(normalized_turn_id):
            return None
        self._ensure_open()
        with self._lock:
            self._require_world_locked(self._connection, normalized_world_id)
            row = self._get_turn_locked(
                self._connection,
                normalized_world_id,
                normalized_turn_id,
                require_unambiguous_alias=True,
            )
        if row is None or (not include_incomplete and row["status"] != "completed"):
            return None
        return self._row_to_turn(row)

    def _set_head_locked(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        canonical_head_id: Optional[str],
    ) -> sqlite3.Row:
        now = _now()
        connection.execute(
            "UPDATE worlds SET head_turn_id = ?, updated_at = ? WHERE world_id = ?",
            (canonical_head_id, now, world_id),
        )
        row = connection.execute(
            "SELECT world_id, head_turn_id, created_at, updated_at, metadata_json "
            "FROM worlds WHERE world_id = ?",
            (world_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - protected by require/foreign key
            raise UnknownWorldError(world_id)
        return row

    def sync_head(self, world_id: str, conversation_head: Optional[str]) -> WorldRecord:
        """Sync a save's head without deleting descendants.

        A non-root head must identify a completed turn in the same world.  A
        legacy provisional ID is accepted only when it resolves unambiguously;
        once a client has branched with a repeated provisional ID, callers
        must use the daemon-issued canonical ID returned at completion.
        """

        normalized_world_id = self._normalize_world_id(world_id)
        normalized_head_id = self._normalize_turn_id(conversation_head)
        with self._transaction(immediate=True) as connection:
            self._ensure_world_locked(connection, normalized_world_id)
            if self._is_root_turn_id(normalized_head_id):
                canonical_head_id = None
            else:
                row = self._get_turn_locked(
                    connection,
                    normalized_world_id,
                    normalized_head_id,
                    require_unambiguous_alias=True,
                )
                if row is None:
                    global_row = self._get_turn_by_id_locked(connection, normalized_head_id)  # type: ignore[arg-type]
                    if global_row is not None and str(global_row["world_id"]) != normalized_world_id:
                        raise CrossWorldTurnError(
                            f"head turn {normalized_head_id!r} belongs to world "
                            f"{global_row['world_id']!r}, not {normalized_world_id!r}"
                        )
                    raise UnknownTurnError(
                        f"head turn {normalized_head_id!r} does not exist in world "
                        f"{normalized_world_id!r}"
                    )
                if row["status"] != "completed" or row["completed_at"] is None:
                    raise IncompleteTurnError(
                        f"head turn {row['turn_id']!r} is not a completed turn"
                    )
                canonical_head_id = str(row["turn_id"])
            world_row = self._set_head_locked(
                connection,
                normalized_world_id,
                canonical_head_id,
            )
        return self._row_to_world(world_row)

    # More descriptive aliases for daemon code.
    sync_save = sync_head
    set_head = sync_head

    def complete_turn(
        self,
        world_id: str,
        turn_id: str,
        assistant_text: str,
        *,
        model: Optional[str] = None,
    ) -> TurnRecord:
        """Atomically mark a turn complete and advance its world's head."""

        normalized_world_id = self._normalize_world_id(world_id)
        normalized_turn_id = self._normalize_turn_id(turn_id)
        if self._is_root_turn_id(normalized_turn_id):
            raise UnknownTurnError("the virtual root cannot be completed")

        with self._transaction(immediate=True) as connection:
            self._require_world_locked(connection, normalized_world_id)
            row = self._get_turn_locked(
                connection,
                normalized_world_id,
                normalized_turn_id,
                prefer_pending=True,
                require_unambiguous_alias=True,
            )
            if row is None:
                global_row = self._get_turn_by_id_locked(connection, normalized_turn_id)  # type: ignore[arg-type]
                if global_row is not None and str(global_row["world_id"]) != normalized_world_id:
                    raise CrossWorldTurnError(
                        f"turn {normalized_turn_id!r} belongs to world {global_row['world_id']!r}, "
                        f"not {normalized_world_id!r}"
                    )
                raise UnknownTurnError(
                    f"turn {normalized_turn_id!r} does not exist in world {normalized_world_id!r}"
                )

            if row["status"] == "completed":
                existing_text = str(row["assistant_text"] or "")
                if existing_text != str(assistant_text):
                    raise TurnAlreadyCompletedError(
                        f"turn {row['turn_id']!r} is already completed with different text"
                    )
                return self._row_to_turn(row)
            if row["status"] != "pending":
                raise IncompleteTurnError(
                    f"turn {row['turn_id']!r} has status {row['status']!r}"
                )

            # An empty provider result is not a completed conversational turn.
            # Mark it aborted in the same transaction and leave the previous
            # canonical head untouched.  Returning the aborted snapshot keeps
            # the old ``complete_turn`` call shape non-throwing for callers
            # that use an empty string to signal a failed generation.
            if not str(assistant_text).strip():
                connection.execute(
                    "UPDATE turns SET status = 'aborted' "
                    "WHERE turn_id = ? AND world_id = ? AND status = 'pending'",
                    (str(row["turn_id"]), normalized_world_id),
                )
                aborted_row = self._get_turn_by_id_locked(connection, str(row["turn_id"]))
                if aborted_row is None:  # pragma: no cover - protected by UPDATE
                    raise StoreError("aborted turn disappeared before commit")
                return self._row_to_turn(aborted_row)

            completed_at = _now()
            connection.execute(
                """
                UPDATE turns
                SET assistant_text = ?, completed_at = ?,
                    model = COALESCE(?, model), status = 'completed'
                WHERE turn_id = ? AND world_id = ? AND status = 'pending'
                """,
                (
                    str(assistant_text),
                    completed_at,
                    (str(model) if model is not None else None),
                    str(row["turn_id"]),
                    normalized_world_id,
                ),
            )
            # This is in the same transaction as the status update.  A crash
            # before commit leaves both the turn and head unchanged.
            self._set_head_locked(connection, normalized_world_id, str(row["turn_id"]))
            completed_row = self._get_turn_by_id_locked(connection, str(row["turn_id"]))
            if completed_row is None:  # pragma: no cover - protected by UPDATE
                raise StoreError("completed turn disappeared before commit")
            return self._row_to_turn(completed_row)

    def record_completed_turn(
        self,
        world_id: str,
        user_text: str,
        assistant_text: str,
        *,
        parent_turn_id: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
        model: Optional[str] = None,
        client_turn_id: Optional[str] = None,
        request_id: Optional[str] = None,
        canonical_turn_id: Optional[str] = None,
    ) -> TurnRecord:
        """Insert a completed turn and advance the head atomically.

        This is useful for non-streaming callers and for tests.  Streaming
        callers should use :meth:`begin_turn` followed by
        :meth:`complete_turn`; the latter still performs the canonical atomic
        commit.
        """

        normalized_world_id = self._normalize_world_id(world_id)
        if not str(assistant_text).strip():
            raise ValueError("assistant_text must contain non-whitespace text")
        context_json, _ = _json_object(context)
        normalized_client_id = self._normalize_turn_id(client_turn_id)
        normalized_request_id = self._normalize_turn_id(request_id)
        with self._transaction(immediate=True) as connection:
            world_row = self._ensure_world_locked(connection, normalized_world_id)
            effective_parent_id = parent_turn_id
            if effective_parent_id is None or str(effective_parent_id).strip() == "":
                effective_parent_id = world_row["head_turn_id"] or ROOT_TURN_ID
            canonical_parent_id = self._validate_parent_locked(
                connection,
                normalized_world_id,
                effective_parent_id,
            )

            for _attempt in range(16):
                allocated_turn_id = (
                    self._normalize_turn_id(canonical_turn_id)
                    if canonical_turn_id is not None
                    else self._new_canonical_turn_id()
                )
                if self._is_root_turn_id(allocated_turn_id):
                    raise TurnIdCollision("canonical turn ID must not be the virtual root")
                try:
                    created_at = _now()
                    completed_at = _now()
                    connection.execute(
                        """
                        INSERT INTO turns(
                            turn_id, world_id, parent_turn_id, user_text,
                            assistant_text, context_json, created_at,
                            completed_at, model, status, client_turn_id, request_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                        """,
                        (
                            allocated_turn_id,
                            normalized_world_id,
                            canonical_parent_id,
                            str(user_text),
                            str(assistant_text),
                            context_json,
                            created_at,
                            completed_at,
                            (str(model) if model is not None else None),
                            normalized_client_id,
                            normalized_request_id,
                        ),
                    )
                    self._set_head_locked(connection, normalized_world_id, allocated_turn_id)
                    row = self._get_turn_by_id_locked(connection, allocated_turn_id)
                    if row is None:  # pragma: no cover - protected by INSERT
                        raise StoreError("turn disappeared after insertion")
                    return self._row_to_turn(row)
                except sqlite3.IntegrityError as exc:
                    if "turns.turn_id" not in str(exc) and "UNIQUE constraint failed: turns.turn_id" not in str(exc):
                        raise
                    if canonical_turn_id is not None:
                        raise TurnIdCollision(
                            f"canonical turn ID {allocated_turn_id!r} is already in use"
                        ) from exc
            raise TurnIdCollision("could not allocate a globally unique turn ID")

    commit_turn = record_completed_turn

    def abort_turn(self, world_id: str, turn_id: str) -> Optional[TurnRecord]:
        """Mark a pending generation aborted without changing the head."""

        normalized_world_id = self._normalize_world_id(world_id)
        normalized_turn_id = self._normalize_turn_id(turn_id)
        if self._is_root_turn_id(normalized_turn_id):
            return None
        with self._transaction(immediate=True) as connection:
            self._require_world_locked(connection, normalized_world_id)
            row = self._get_turn_locked(
                connection,
                normalized_world_id,
                normalized_turn_id,
                prefer_pending=True,
                require_unambiguous_alias=True,
            )
            if row is None:
                return None
            if row["status"] == "pending":
                connection.execute(
                    "UPDATE turns SET status = 'aborted' WHERE turn_id = ? AND status = 'pending'",
                    (str(row["turn_id"]),),
                )
                row = self._get_turn_by_id_locked(connection, str(row["turn_id"]))
            return self._row_to_turn(row) if row is not None else None

    def head_turn_id(self, world_id: str) -> str:
        world = self.get_world(world_id)
        if world is None:
            raise UnknownWorldError(self._normalize_world_id(world_id))
        return world.public_head_turn_id

    def linear_turns(
        self,
        world_id: str,
        head_turn_id: Optional[str] = None,
        *,
        max_turns: Optional[int] = 12,
    ) -> list[TurnRecord]:
        """Return completed ancestors in chronological order.

        The walk is deliberately performed in Python rather than by trusting
        a recursive SQL query: it can stop safely at a missing or incomplete
        row and has an explicit cycle guard if a database is manually edited.
        """

        normalized_world_id = self._normalize_world_id(world_id)
        with self._lock:
            world_row = self._require_world_locked(self._connection, normalized_world_id)
            effective_head = head_turn_id
            if effective_head is None or str(effective_head).strip() == "":
                effective_head = world_row["head_turn_id"] or ROOT_TURN_ID
            row = self._resolve_turn_locked(
                self._connection,
                normalized_world_id,
                effective_head,
                prefer_pending=False,
                require_unambiguous_alias=True,
            )

            path: list[TurnRecord] = []
            visited: set[str] = set()
            while row is not None:
                canonical_id = str(row["turn_id"])
                if canonical_id in visited:
                    break
                visited.add(canonical_id)
                if row["status"] == "completed" and row["completed_at"] is not None:
                    path.append(self._row_to_turn(row))
                parent_id = row["parent_turn_id"]
                if self._is_root_turn_id(parent_id):
                    break
                row = self._resolve_turn_locked(
                    self._connection,
                    normalized_world_id,
                    parent_id,
                    allow_alias=False,
                )

        path.reverse()
        if max_turns is not None:
            if max_turns < 0:
                raise ValueError("max_turns must be non-negative or None")
            if len(path) > max_turns:
                path = path[-max_turns:] if max_turns else []
        return path

    def history(
        self,
        world_id: str,
        head_turn_id: Optional[str] = None,
        *,
        max_turns: Optional[int] = 12,
    ) -> list[TurnRecord]:
        return self.linear_turns(world_id, head_turn_id, max_turns=max_turns)

    def list_turns(
        self,
        world_id: str,
        *,
        include_incomplete: bool = True,
    ) -> list[TurnRecord]:
        normalized_world_id = self._normalize_world_id(world_id)
        self._ensure_open()
        with self._lock:
            self._require_world_locked(self._connection, normalized_world_id)
            query = """
                SELECT turn_id, world_id, parent_turn_id, user_text, assistant_text,
                       context_json, created_at, completed_at, model, status,
                       client_turn_id, request_id
                FROM turns WHERE world_id = ?
            """
            params: list[Any] = [normalized_world_id]
            if not include_incomplete:
                query += " AND status = 'completed' AND completed_at IS NOT NULL"
            query += " ORDER BY created_at, rowid"
            rows = self._connection.execute(query, params).fetchall()
        return [self._row_to_turn(row) for row in rows]

    def pending_turns(self, world_id: Optional[str] = None) -> list[TurnRecord]:
        self._ensure_open()
        with self._lock:
            if world_id is None:
                rows = self._connection.execute(
                    """
                    SELECT turn_id, world_id, parent_turn_id, user_text, assistant_text,
                           context_json, created_at, completed_at, model, status,
                           client_turn_id, request_id
                    FROM turns WHERE status = 'pending' ORDER BY created_at, rowid
                    """
                ).fetchall()
            else:
                normalized_world_id = self._normalize_world_id(world_id)
                self._require_world_locked(self._connection, normalized_world_id)
                rows = self._connection.execute(
                    """
                    SELECT turn_id, world_id, parent_turn_id, user_text, assistant_text,
                           context_json, created_at, completed_at, model, status,
                           client_turn_id, request_id
                    FROM turns WHERE world_id = ? AND status = 'pending'
                    ORDER BY created_at, rowid
                    """,
                    (normalized_world_id,),
                ).fetchall()
        return [self._row_to_turn(row) for row in rows]

    def discard_incomplete(self, world_id: Optional[str] = None) -> int:
        """Mark pending rows aborted; completed history is never touched."""

        with self._transaction(immediate=True) as connection:
            if world_id is None:
                cursor = connection.execute(
                    "UPDATE turns SET status = 'aborted' WHERE status = 'pending'"
                )
            else:
                normalized_world_id = self._normalize_world_id(world_id)
                self._require_world_locked(connection, normalized_world_id)
                cursor = connection.execute(
                    "UPDATE turns SET status = 'aborted' "
                    "WHERE world_id = ? AND status = 'pending'",
                    (normalized_world_id,),
                )
        return int(cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __del__(self) -> None:
        # Library callers are encouraged to use ``close``/the context manager,
        # but short-lived sessions should not leave SQLite descriptors behind
        # when their manager is simply discarded.
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "SQLiteStore":
        self._ensure_open()
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.close()


# Friendly names for callers that do not need to care about the implementation
# choice.  SQLiteStore remains the explicit name used in diagnostics/tests.
ConversationStore = SQLiteStore
TimelineStore = SQLiteStore
Store = SQLiteStore


__all__ = [
    "AmbiguousTurnAliasError",
    "ConversationStore",
    "CrossWorldTurnError",
    "IncompleteTurnError",
    "ROOT_TURN_ID",
    "SCHEMA_VERSION",
    "SQLiteStore",
    "Store",
    "StoreError",
    "TimelineStore",
    "TurnAlreadyCompletedError",
    "TurnIdCollision",
    "TurnRecord",
    "UnknownTurnError",
    "UnknownWorldError",
    "WorldRecord",
    "default_database_path",
]
