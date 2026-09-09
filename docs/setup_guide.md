# Factorio Companion setup guide

This guide sets up the pre-v0 localhost substrate. It assumes a Factorio 2.0
single-player installation and a local service exposing the OpenAI chat
completions API with server-sent events (SSE).

## 1. Prepare the model endpoint

The daemon posts to:

```text
<base-url>/chat/completions
```

The service must accept `model`, `messages`, `stream: true`, and
`temperature`, then emit standard SSE `data:` events containing OpenAI-style
`choices[].delta.content` text and a final `data: [DONE]`. The endpoint and
model are configurable; no provider-specific SDK is required.

For a local development service, the defaults are:

```text
COMPANION_BASE_URL=http://127.0.0.1:8645/v1
COMPANION_MODEL=inclusionai/ling-3.0-flash-sante:free
COMPANION_API_KEY=hermes-local
```

The API key is sent as a Bearer token when non-empty. It is a provider-side
credential, not a Factorio credential.

## 2. Start the daemon

From the repository root:

```sh
export COMPANION_BASE_URL="${COMPANION_BASE_URL:-http://127.0.0.1:8645/v1}"
export COMPANION_MODEL="${COMPANION_MODEL:-inclusionai/ling-3.0-flash-sante:free}"
export COMPANION_API_KEY="${COMPANION_API_KEY:-hermes-local}"
python -m bridge.daemon \
  --base-url "$COMPANION_BASE_URL" \
  --model "$COMPANION_MODEL" \
  --api-key "$COMPANION_API_KEY" \
  --db-path "$PWD/conversation.sqlite3"
```

Omit `--db-path` to use the default per-user data directory. The explicit path
above is useful for a disposable development save. The daemon should log a
line saying it is listening on UDP `127.0.0.1:34200`.

Useful checks:

```sh
python -m bridge.daemon --help
python -m unittest discover -s tests -t . -v
```

Do not run the old RCON polling agent for normal conversation. `bridge.agent`
is only a compatibility launcher for the UDP daemon.

## 3. Install and enable the mod

Factorio loads mods from its user-data `mods` directory. Deploy the current
source with:

```sh
./bin/deploy-mod.sh /path/to/Factorio/mods
```

The destination may be a custom directory used with Factorio's
`--mod-directory` option. The script removes only old
`FactorioCompanion_*` directories in that explicitly supplied destination; it
preserves other mods and the existing mod-list entries.

Enable the mod in the Factorio mod manager if necessary. The generated folder
is versioned, for example `FactorioCompanion_0.2.0`.

## 4. Enable native Lua UDP

Add the following Factorio launch option:

```text
--enable-lua-udp=34199
```

The companion mod sends packets to the daemon's port `34200`. Factorio's Lua
UDP receive event is enabled by the launch option and the mod polls the native
receive helper every tick. The two ports have different roles:

```text
Factorio helpers.send_udp -> 127.0.0.1:34200 -> daemon
daemon reply              -> Factorio's packet source port (34199)
```

If the daemon is configured on a different local port, change the mod's
`companion-daemon-port` runtime-global setting to match. Keep both endpoints
on loopback for this release.

On Steam, put the flag in the game's launch options. On a command-line or Wine
launch, append it to the Factorio executable command. The flag is required for
the mod to report `ready`; the game can otherwise run normally with the
companion shown as offline.

## 5. Verify the connection

Open a save with the mod enabled. On player join, the mod sends a `hello`
packet. A healthy connection shows `ready` in the companion header. Send a
short message and expect this sequence:

```text
user_message -> assistant_start -> zero or more assistant_delta -> assistant_end
```

The final packet contains the complete reply. If a provider call fails, the
daemon sends an `error` packet and the UI returns to a retryable state. If the
daemon is restarted, the mod reconnects through heartbeat/hello generation
identities and drops late packets from the old generation.

For an in-game diagnostic check, run `/copilot-selftest`. It exercises the
protocol parser, capability gates, fog-of-war boundary, chat lifecycle, marker
and task UI, and authoritative stream completion. RCON may be used to invoke
that diagnostic remote interface in a server test, but RCON is not used for
conversation, context, delivery, or streaming.

## 6. Save rollback behavior

The save persists the current `world_id` and completed `conversation_head`.
The SQLite database persists every completed daemon turn and its parent. When
an older save is loaded, the daemon accepts that ancestor as the active head
and creates future turns as a new branch. It does not delete the newer branch
and it never adds a pending/aborted generation to model history.

Keep the database with the save family if you want continuity. If a save is
copied as a genuinely separate world, its persisted `world_id` keeps the
conversation namespaces isolated.

## Troubleshooting

`offline` after launching Factorio:

- Confirm the daemon is running and listening on UDP `34200`.
- Confirm the game launch includes `--enable-lua-udp=34199`.
- Confirm the mod's `companion-daemon-port` setting matches `--port`.
- Check Factorio's log for `FactorioCompanion` and UDP helper errors.

`connecting…` indefinitely:

- The daemon may be bound to a different address/port than the mod expects.
- A second daemon or stale process may own the configured port.
- Restart the Factorio save to force a fresh hello after correcting settings.

`error` after a message:

- Check the daemon log for the provider status, timeout, or SSE parse error.
- Verify the provider URL ends at its API root, normally `/v1`, not a browser
  page.
- Try a shorter prompt if the context is unusually large; the protocol has a
  hard 4096-byte UTF-8 datagram limit.

No reply with a running daemon:

- Run the dependency-free test suite to check the daemon/provider boundary.
- Run `/copilot-selftest` to check the in-game Lua boundary.
- Inspect the SQLite path only for diagnostics; editing it is not part of the
  normal setup flow.

## Current scope

This release is a substrate, not a campaign. It supports one local player,
native UDP transport, grounded read-only context, streamed chat UI, and durable
conversation branching. Multiplayer, remote UDP peers, autonomous actions,
and campaign progression are intentionally deferred.
