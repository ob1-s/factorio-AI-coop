# ADR 001: Pure Localhost UDP Transport & Substrate Architecture for Factorio AI Companion

## Status
Accepted

## Context
Factorio 2.0 / Space Age introduced a native UDP scripting interface:
- CLI option: `--enable-lua-udp=<port>`
- Lua API: `helpers.send_udp(port, data, player_index)` and `helpers.recv_udp(player_index)`
- Event: `defines.events.on_udp_packet_received`

Prior prototypes in community tooling used RCON (`/silent-command rcon.print(...)`) to bridge external scripts with Factorio. However:
1. RCON requires running Factorio as a dedicated or hosted multiplayer server (`--start-server` or `--host`), which prevents standard single-player campaign launches from the Steam menu.
2. RCON poll-loops block on round-trips and cannot stream tokens or deltas fluidly into the GUI.
3. RCON operates at high privilege (`/c` console access) whereas native UDP can be cleanly restricted to a dedicated JSON protocol parsed entirely within Lua sandbox event handlers.

## Decisions

### 1. Pure Native UDP as Product Transport
The mod communicates strictly over localhost UDP:
- Factorio listens on port `34199` (configured via launch flag `--enable-lua-udp 34199`).
- Companion Daemon listens on port `34200`.
- All inter-process traffic is single-datagram UTF-8 JSON.
- RCON is eliminated from production architecture. Automated unit tests mock the transport layer.

### 2. Visible vs. Charted Fog-of-War Enforcement
To guarantee that the AI copilot never acts as an unfair radar or cheats the simulation:
- **Charted Knowledge** (`force.is_chunk_charted`): Used strictly for static geography (discovered surface bounds, ore deposit locations as last seen, known structural footprints).
- **Visible Perception** (`force.is_chunk_visible` or `surface.is_chunk_visible`): Mandatory for all real-time dynamic facts: biter positions, current enemy attacks, live health/pollution metrics, vehicle movement, and active production status.
- Non-visible and uncharted entities are mathematically dropped before serialization into the context JSON payload.

### 3. Lightweight Streaming without TCP Emulation
Because received UDP datagrams are integrated into Factorio's input actions:
- We explicitly reject per-delta ACKs and retransmission mechanisms over UDP.
- Instead, the daemon streams grouped chunks (`assistant_delta`, cadence ~80–150ms) for cosmetic GUI typing, followed by an authoritative `assistant_end(full_text)`.
- If a delta is dropped, the final packet immediately repairs the UI without engine overhead.

### 4. Save Rollback & Conversation Tree Branching
To prevent the daemon from "remembering the future" when a player reloads a past save:
- Every save stores a canonical `world_id` and `conversation_head` (turn ID).
- The daemon structures dialogue history as a directed acyclic tree of turns.
- Loading an earlier save points `conversation_head` to an ancestor node, causing the daemon to branch rather than contaminate the conversation timeline with future knowledge.

### 5. Capabilities as Feature Flags
The progression layer is defined by granular capability keys (`"telemetry"`, `"research"`, `"inventory"`, `"radar_vision"`, `"map_analysis"`, `"markers"`, `"tasks"`). Numerical levels (0..4) are mapped as convenience milestone presets.

### 6. Single-Player Scope for v0
Factorio multiplayer requires network-wide input-action synchronization for external UDP. Single-player Space Age is the sole focus for v0; multiplayer is explicitly deferred.

## Consequences
- Singleplayer players must configure `--enable-lua-udp 34199` once in their launch options.
- The companion mod works in standard singleplayer menu launches without dedicated server orchestration.
- The Lua state remains strictly authoritative for save progression and fog of war.
