# ADR 001: native UDP bridge and rollback-safe companion substrate

## Status

Accepted for pre-v0.

## Context

The companion needs a local path between a normal single-player Factorio 2.0
save and an OpenAI-compatible streaming model. The path must remain responsive
while generation runs, survive daemon restarts, and avoid allowing a save
rollback to expose future conversation or hidden map state.

Factorio provides the native Lua UDP helper/event surface used here:
[`LuaHelpers.send_udp` and `recv_udp`](https://lua-api.factorio.com/latest/classes/LuaHelpers.html),
[`on_udp_packet_received`](https://lua-api.factorio.com/latest/events.html), and
force chart/visibility queries such as
[`LuaForce.is_chunk_charted`](https://lua-api.factorio.com/latest/classes/LuaForce.html).

Earlier versions used an RCON polling agent and a persisted conversational
outbox. That required server-style orchestration, coupled game actions to a
privileged console surface, and could not provide a clean streaming contract
for a standard single-player launch.

## Decisions

### 1. Native localhost UDP is the sole product path

The Factorio mod sends compact UTF-8 JSON datagrams to daemon UDP port `34200`.
Factorio receives replies through the native UDP socket enabled with
`--enable-lua-udp=34199`; the daemon replies to each packet's source address
and port. Both sides enforce a 4096-byte datagram ceiling.

RCON is not used for conversation, context collection, message delivery, or
streaming. The retained remote interface is limited to diagnostics, capability
administration, and UI/test utilities. The old Python RCON modules are not
imported by the product daemon; `bridge.agent` is only a compatibility shim to
`bridge.daemon`.

### 2. The receive boundary is defensive and versioned

The daemon parses bytes as UTF-8, rejects malformed JSON and invalid envelope
types/integers without taking down its receive loop, then applies message-
specific validation before session or provider work. The Lua side applies the
same packet-size/envelope checks and wraps its event handler so a malformed
packet cannot crash the game script.

The protocol does not emulate TCP. There are no per-delta acknowledgements or
retransmissions. `seq` lets the mod discard duplicate/out-of-order assistant
packets, and the final `assistant_end.full_text` repairs any cosmetic streaming
loss.

### 3. Streaming is asynchronous and one exchange is active per world

The daemon receive loop never waits for the model. Each accepted request sends
`assistant_start`, runs the OpenAI-compatible SSE call in a worker, batches
provider text into small `assistant_delta` packets, and ends with one
authoritative `assistant_end`. Heartbeats continue while a worker is running.

Only one generation runs per world. A configurable worker limit allows
independent worlds to make progress without interleaving two requests from one
save. Duplicate requests with the same fingerprint are ignored while active;
recent completed exchanges can replay their final packet.

### 4. Durable timeline uses globally unique daemon turn IDs

SQLite stores `worlds` and parent-linked `turns`. A turn is inserted as
`pending`, becomes visible to model history only when its non-empty assistant
text and the world's head are committed atomically, and is marked `aborted` on
handled failure. SQLite WAL/full-synchronous settings and a serialized store
lock provide the durability boundary for the small local database.

Factorio owns a save-persisted `world_id` and `conversation_head`. Its runtime
request/turn IDs are provisional aliases because their counters can rewind with
a save. The daemon instead issues a globally unique `turn_<random>` primary
key. On reload, a saved ancestor moves the active head without deleting later
descendants, so new dialogue forms a branch and cannot contaminate the active
history with the abandoned future.

### 5. Model context is explicit, capability-gated, and fog-safe

The mod builds the exact context snapshot placed in `user_message`. Capability
keys gate telemetry, research, inventory, radar perception, and charted map
metadata. Dynamic entity/alert snapshots require both charted and currently
visible chunks. Charted knowledge is limited to static sampled bounds; the
context excludes hidden entities/resources, production/power internals,
inventories of world entities, and enemy evolution state.

Transport and timeline metadata remain packet fields and are not appended to the
model context. Alternate Lua callers cannot inject an unchecked context because
the send boundary rebuilds it through the capability-gated builder.

### 6. Pre-v0 scope is single-player and read-only

The substrate supports local chat, grounded read-only context, streamed GUI
display, durable branching, reconnect, and diagnostics. Multiplayer, remote
network peers, autonomous game actions, and campaign/progression content are
explicitly deferred.

## Consequences

Players must start the daemon and add one Factorio launch option. A provider
failure is visible as a request error and leaves the saved head unchanged.
Dropped UDP deltas do not corrupt the completed answer. A daemon restart or
Factorio reload creates a new transport generation and rejects late packets.

The 4096-byte budget means context and final answers must remain compact. The
daemon reports `MODEL_TOO_LARGE` rather than committing a completion that the
mod cannot receive. UDP is intentionally local and best-effort; the heartbeat,
generation checks, duplicate policy, and authoritative final packet cover the
failure modes needed for pre-v0 without adding a second transport protocol.
