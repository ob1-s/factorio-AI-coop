# Factorio Companion

Factorio Companion is a local, single-player AI chat companion for Factorio
2.0. The pre-v0 substrate is deliberately small: the mod sends a grounded
context snapshot over native localhost UDP, the daemon streams an
OpenAI-compatible response, and the mod reconciles the final authoritative
reply into its chat window.

## Quick start

1. Start an OpenAI-compatible `/v1/chat/completions` service. The default
   development endpoint is `http://127.0.0.1:8645/v1` and the default model is
   `inclusionai/ling-3.0-flash-sante:free`.

2. From this repository, start the daemon:

   ```sh
   python -m bridge.daemon \
     --base-url http://127.0.0.1:8645/v1 \
     --model inclusionai/ling-3.0-flash-sante:free \
     --api-key hermes-local
   ```

   The daemon listens on UDP `127.0.0.1:34200` and stores completed dialogue
   in `~/.local/share/factorio-companion/conversation.sqlite3` by default.
   Use `--db-path /path/to/conversation.sqlite3` for an explicit location.

3. Deploy the mod to the Factorio `mods` directory:

   ```sh
   ./bin/deploy-mod.sh /path/to/Factorio/mods
   ```

   The script creates the versioned mod directory and preserves/enables the
   companion entry in `mod-list.json`.

4. Add this Factorio launch option and start a single-player save:

   ```text
   --enable-lua-udp=34199
   ```

   The game receives daemon replies on the UDP socket enabled by this flag;
   the daemon replies to the source address and port of each packet. No RCON
   server, hosted game, or dedicated-server launch is needed for the product
   path.

## Configuration

Command-line flags are the clearest way to configure a one-off daemon. The
same settings are available through environment variables:

| Setting | Flag | Environment variable | Default |
| --- | --- | --- | --- |
| Daemon bind address | `--host` | `COMPANION_UDP_HOST` | `127.0.0.1` |
| Daemon UDP port | `--port` | `COMPANION_UDP_PORT` | `34200` |
| Factorio UDP flag value | `--factorio-port` | `COMPANION_FACTORIO_PORT` | `34199` |
| API base URL | `--base-url` | `COMPANION_BASE_URL` | `http://127.0.0.1:8645/v1` |
| Model | `--model` | `COMPANION_MODEL` | `inclusionai/ling-3.0-flash-sante:free` |
| API key | `--api-key` | `COMPANION_API_KEY` | `hermes-local` |
| SQLite database | `--db-path` | `COMPANION_DB_PATH` | platform data path |
| Provider timeout | `--timeout` | `COMPANION_LLM_TIMEOUT` | `120` seconds |

The daemon requires a streaming provider response. It accepts normal
OpenAI-compatible SSE events and never commits an empty, interrupted, or
oversized completion.

## What is persisted

The Factorio save owns its `world_id` and `conversation_head`. The daemon
stores completed turns in a SQLite DAG keyed by globally unique daemon-issued
IDs. Factorio-generated turn IDs are retained only as client aliases, so a
save rollback cannot overwrite a previous branch. A reload moves the active
head to the save's ancestor without deleting later branches; pending and
aborted generations are excluded from model history.

The normal exchange is one request per world at a time. UDP heartbeats continue
while the provider is generating. Deltas are cosmetic; `assistant_end.full_text`
is authoritative and repairs dropped or reordered deltas.

## Grounding and scope

The context sent to the model is capability-gated. Dynamic entities and alerts
must be both charted and currently visible. Charted knowledge is limited to
static sampled map bounds; the mod does not serialize hidden entities,
resources, inventories, production internals, or enemy evolution state.

The v0 target is single-player, localhost Factorio 2.0. Campaign content,
multiplayer synchronization, remote network peers, and autonomous game actions
are outside this substrate. The old RCON modules remain only as compatibility
or diagnostic code; `python -m bridge.daemon` and native UDP are the product
entrypoint and transport.

## Verification

Run the dependency-free suite from the repository root:

```sh
python -m unittest discover -s tests -t . -v
```

It starts the production daemon entrypoint against a local fake SSE provider
and covers malformed packets, streaming, reconnects, heartbeats, provider
errors, durable restart, rollback/forks, capability contracts, and Lua syntax.
See [tests/README.md](tests/README.md) and the exact setup/runbook in
[docs/setup_guide.md](docs/setup_guide.md).

The wire details are in [docs/protocol_spec.md](docs/protocol_spec.md), and
the architectural decisions are recorded in
[docs/adr-001-udp-bridge-and-substrate.md](docs/adr-001-udp-bridge-and-substrate.md).
