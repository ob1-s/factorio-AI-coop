#!/usr/bin/env python3
"""Compatibility launcher for the native UDP companion daemon.

The product conversation path is the UDP daemon. Keeping this tiny shim avoids
making an old ``python bridge/agent.py`` command silently start a second
RCON/outbox architecture; new installations should use
``python -m bridge.daemon``.
"""

from __future__ import annotations

import pathlib
import sys

if __package__:
    from .daemon import main
else:  # direct execution from a checkout, retained for a friendly migration path
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from bridge.daemon import main


if __name__ == "__main__":
    raise SystemExit(main())
