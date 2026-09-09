# Workstream E test suite

The suite is written with `unittest`, so it runs in the base Python
environment without installing third-party packages. The same test cases are
also collected by pytest when pytest is available.

Run from the repository root:

```text
python -m unittest discover -s tests -t . -v
```

The current baseline result is `Ran 61 tests` / `OK` on Python 3.14.7.

Pytest is optional. With `uv` available, run it without changing the project
environment:

```text
uvx --from pytest pytest -q
```

The current result is `61 passed, 60 subtests passed`.

## Static validation

The Lua source test automatically uses `luac -p` (or `lua loadfile` as a
fallback) for every `mod/FactorioCompanion/**/*.lua` file:

```text
python -m unittest tests.test_lua_static -v
```

The observed toolchain is Lua 5.5.1 with `/usr/bin/luac`. Python production
modules used by the daemon/store tests can also be syntax-checked without
pytest:

```text
python -m py_compile bridge/protocol.py bridge/session.py bridge/store.py bridge/daemon.py bridge/llm/openai.py
```

## Factorio runtime validation

The installed executable reports:

```text
WINEPREFIX=/home/ob1/Games/umu/umu-default wine 'C:\Games\Factorio\bin\x64\factorio.exe' --version
Version: 2.0.72 (build 84292, win64, steam)
Version: 64
Map input version: 1.0.0-0
Map output version: 2.0.72-0
```

A headless mod-load probe was run with Factorio 2.0.72 and the current
`mod/FactorioCompanion` copy. Its log showed the mod loading, the generated
world ID, and `Opening socket at (IP ADDR:({127.0.0.1:34299}))` for the Lua
UDP endpoint. It was intentionally a separate runtime probe, not part of the
dependency-free unit suite. The probe had no daemon attached, so the log's
`heartbeat_timeout` messages are an expected offline result, not a passing
daemon round trip.

The recorded headless launch arguments were:

```text
WINEPREFIX=/home/ob1/Games/umu/umu-default wine 'C:\Games\Factorio\bin\x64\factorio.exe' --verbose --start-server /home/ob1/Projects/factory-companion/.runtime-test/test-save-current.zip --mod-directory /home/ob1/Projects/factory-companion/.runtime-test/mods --enable-lua-udp 34299 --port 34297 --rcon-port 35997 --rcon-password workstreamd --server-settings bridge/server-settings.json --disable-audio
```

The corresponding Factorio log is
`/home/ob1/Games/umu/umu-default/drive_c/users/steamuser/AppData/Roaming/Factorio/factorio-current.log`.

For a full manual runtime check, start a daemon using the same UDP port as the
Factorio process, then inspect the Factorio log or call the existing diagnostic
remote interface's `copilot.selftest()` over RCON. The product conversational
path remains UDP; RCON is only a diagnostic invocation for the Lua selftest.

The automated suite does cover the daemon's complete UDP lifecycle against a
fake LLM, including reconnect, heartbeat during inference, rapid messages, and
durable restart. It does not start or stop a Factorio GUI/server as part of a
default test run, and it cannot certify a live in-game UDP round trip when no
daemon is configured for the running Factorio instance.

## Scope and known gaps

`tests/harness.py` launches `python -m bridge.daemon`; it never substitutes a
fake daemon, so a missing or crashing production entry point fails loudly.
The fake LLM is local and OpenAI-compatible, with SSE chunks, Unicode,
delays, and HTTP failures.

The suite does not change bridge or Factorio production files. Any failures in
those files are reported as production failures; test-only compatibility
adapters raise an explicit `ProductionTimelineApiGap` instead of silently
falling back to an in-memory timeline.

On the base `unittest` run, Python 3.14 may also print a non-fatal
`ResourceWarning` while collecting the expected HTTP 503 from
`bridge.llm.openai`: the production HTTPError cleanup leaves its temporary
response for the interpreter to collect. Assertions still pass; the optional
pytest run is clean under its default warning filter.
