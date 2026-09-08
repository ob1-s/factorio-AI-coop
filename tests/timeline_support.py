"""Adapters for the persistence API that Workstream E expects from production."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any


class ProductionTimelineApiGap(RuntimeError):
    """Raised when the production tree has no durable timeline API yet."""


def open_persistent_session(db_path: Path) -> Any:
    """Open a production session manager backed by ``db_path``.

    The WIP currently exposes only ``SessionManager()``.  This adapter accepts
    the small set of natural constructor shapes so tests remain useful while a
    SQLite implementation is integrated, but it never silently falls back to
    the in-memory manager: that would make a restart test meaningless.
    """

    from bridge.session import SessionManager

    path = str(db_path)
    signature = inspect.signature(SessionManager)
    parameters = signature.parameters
    for keyword in ("db_path", "database_path", "db", "database"):
        if keyword in parameters:
            try:
                return SessionManager(**{keyword: path})
            except TypeError:
                pass

    # Also accept a store-injected manager if production chose to keep storage
    # in a separate module, without taking a dependency on that module here.
    try:
        store_module = importlib.import_module("bridge.store")
    except ModuleNotFoundError:
        store_module = None
    if store_module is not None:
        store_classes = (
            "SQLiteStore",
            "TimelineStore",
            "ConversationStore",
            "Store",
        )
        for class_name in store_classes:
            store_class = getattr(store_module, class_name, None)
            if store_class is None:
                continue
            for keyword in ("db_path", "database_path", "path", "filename"):
                try:
                    store = store_class(**{keyword: path})
                except (TypeError, ValueError):
                    continue
                try:
                    return SessionManager(store=store)
                except TypeError:
                    continue

    raise ProductionTimelineApiGap(
        "production timeline has no durable constructor: expected "
        "SessionManager(db_path=...), SessionManager(database_path=...), or "
        "SessionManager(store=SQLiteStore(...))"
    )


def close_session(session: Any) -> None:
    """Close whichever lifecycle method a production store exposes."""

    for method_name in ("close", "shutdown", "stop"):
        method = getattr(session, method_name, None)
        if method is not None:
            method()
            return
    store = getattr(session, "store", None)
    for method_name in ("close", "shutdown", "stop"):
        method = getattr(store, method_name, None)
        if method is not None:
            method()
            return
