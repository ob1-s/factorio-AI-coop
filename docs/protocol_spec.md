# Factorio Companion UDP protocol v1

This is the wire contract between the `FactorioCompanion` mod and
`python -m bridge.daemon`. It is a local single-player transport: packets are
UTF-8 JSON datagrams sent over loopback UDP.

## Transport

| Endpoint | Default | Role |
| --- | ---: | --- |
| Factorio UDP receive port | `34199` | enabled by Factorio's `--enable-lua-udp=34199` launch option |
| Daemon UDP listen port | `34200` | receives `hello`, `heartbeat`, and `user_message` |

The mod sends with Factorio's native `helpers.send_udp` to the daemon port.
The daemon sends replies to the source address and port of the received
datagram, so it does not need a second fixed Factorio destination port.
Both sides reject a datagram larger than **4096 UTF-8 bytes**. The payload must
fit in one datagram; there is no fragmentation protocol.

The daemon binds to loopback by default and rejects non-loopback senders. Keep
the mod and daemon on the same machine for v1.

## Envelope

Every datagram has this compact JSON shape:

```json
{
  "v": 1,
  "type": "user_message",
  "exchange_id": "ex-request-world-a-session-1",
  "seq": 1,
  "tick": 12345,
  "payload": {}
}
```

Fields:

- `v`: required integer protocol version, currently `1`.
- `type`: one of `hello`, `hello_ack`, `heartbeat`, `user_message`,
  `assistant_start`, `assistant_delta`, `assistant_end`, or `error`.
- `exchange_id`: empty for session control; non-empty for an exchange.
- `seq`: non-negative integer. Control sequences and exchange sequences are
  monotonic from the sender's perspective; the receiver discards stale
  exchange packets.
- `tick`: non-negative Factorio tick when known. The daemon uses `0` because
  it does not own the simulation clock.
- `payload`: JSON object.

Malformed JSON, invalid UTF-8, unsupported versions/types, non-integer sequence
or tick fields, wrong payload types, and over-budget packets are dropped
without raising out of either receive loop. The daemon additionally validates
required fields for each message type before touching the session store or
provider.

## Identity and persistence

The IDs have different ownership and lifetimes:

| Field | Owner | Lifetime/meaning |
| --- | --- | --- |
| `world_id` | Factorio save | Persisted save identity. The daemon uses it to isolate timelines. |
| `conversation_head` | Factorio save | Persisted completed head; `turn_0` is the virtual root. |
| `client_session_id` | Factorio runtime | Fresh runtime connection identity; not trusted as durable history. |
| `request_id` | Factorio runtime | Unique request reference for one submitted message. |
| `turn_id` / `client_turn_id` | Factorio runtime | Provisional client reference; may rewind with a save. |
| `turn_id` in `assistant_end` | daemon | Canonical `turn_<random>` ID, globally unique in SQLite. |
| `parent_turn_id` | daemon/session | Completed ancestor used to build the active branch. |
| `daemon_session_id` | daemon process | New generation identity on every daemon process start. |
| `exchange_id` | Factorio | Request transport key, normally `ex-` followed by `request_id`. |

Factorio stores only the canonical `assistant_end.turn_id` as its next
`conversation_head`. A repeated provisional client ID can therefore never
overwrite a prior daemon turn. The SQLite store keeps completed turns as a
parent-linked DAG; changing the save head to an ancestor creates a future fork
without deleting descendants. Pending and aborted turns are not included in
model history.

## Message types

### `hello` — mod → daemon

Sent after player join, save reload, or reconnect.

```json
{
  "v": 1,
  "type": "hello",
  "exchange_id": "",
  "seq": 1,
  "tick": 600,
  "payload": {
    "mod_version": "0.2.0",
    "factorio_version": "2.0",
    "world_id": "world-uuid",
    "conversation_head": "turn_0",
    "player_name": "engineer",
    "capabilities": ["inventory", "research", "telemetry"],
    "client_session_id": "client-session-uuid"
  }
}
```

`world_id`, `conversation_head`, and `capabilities` are required by the v1
mod. The other fields are descriptive or identity metadata.

### `hello_ack` — daemon → mod

Acknowledges the save identity and reports the daemon generation.

```json
{
  "v": 1,
  "type": "hello_ack",
  "exchange_id": "",
  "seq": 1,
  "tick": 0,
  "payload": {
    "daemon_version": "0.2.0",
    "model": "local-model",
    "world_id": "world-uuid",
    "active_world_id": "world-uuid",
    "active_turn_id": "turn_0",
    "history_depth": 0,
    "status": "ready",
    "client_session_id": "client-session-uuid",
    "daemon_session_id": "daemon-process-uuid"
  }
}
```

The mod accepts the acknowledgement only for its current world and runtime
session. A changed `daemon_session_id` cancels any old in-game stream before
the new generation is accepted.

### `heartbeat` — mod ⇄ daemon

Control traffic is exchanged roughly every two seconds while a player is
connected, including during model inference.

```json
{
  "v": 1,
  "type": "heartbeat",
  "exchange_id": "",
  "seq": 12,
  "tick": 720,
  "payload": {
    "ping_tick": 720,
    "state": "streaming",
    "world_id": "world-uuid",
    "client_session_id": "client-session-uuid",
    "daemon_session_id": "daemon-process-uuid"
  }
}
```

The daemon answers with its current `state` (`ready` or `busy`) and generation
identity. Heartbeats are liveness signals, not acknowledgements for individual
model deltas. If the mod receives no packets within its timeout window, it
returns to `offline`, clears its active exchange, and retries `hello`.

### `user_message` — mod → daemon

The message contains the complete, capability-gated context snapshot for this
turn. The daemon rebuilds model history from the submitted parent and the
completed SQLite ancestor chain.

```json
{
  "v": 1,
  "type": "user_message",
  "exchange_id": "ex-request-world-uuid-client-session-1",
  "seq": 1,
  "tick": 3600,
  "payload": {
    "request_id": "request-world-uuid-client-session-1",
    "client_session_id": "client-session-uuid",
    "turn_id": "client-turn-world-uuid-client-session-1",
    "client_turn_id": "client-turn-world-uuid-client-session-1",
    "parent_turn_id": "turn_0",
    "world_id": "world-uuid",
    "text": "What are we researching right now?",
    "context": {
      "capabilities": ["research", "telemetry"],
      "player": {
        "name": "engineer",
        "surface": "nauvis",
        "position": {"x": 12.5, "y": -4.2}
      },
      "research": {
        "current": "steel-processing",
        "progress": 0.42,
        "queue": [{"tech": "oil-processing"}],
        "unlocked_count": 14
      }
    }
  }
}
```

The context is model input, not an authorization token. Its fields are
constructed by the mod's capability-gated builder. Dynamic perception is
limited to entities and alerts that are both charted and currently visible;
charted map data is static sampled bounds only. The model does not receive
transport IDs, hidden entities/resources, production/power internals, or enemy
evolution state through this context.

### `assistant_start` — daemon → mod

Marks the beginning of a generation. Sequence `1` is reserved for this packet.

```json
{
  "v": 1,
  "type": "assistant_start",
  "exchange_id": "ex-request-world-uuid-client-session-1",
  "seq": 1,
  "tick": 0,
  "payload": {
    "world_id": "world-uuid",
    "model": "local-model",
    "request_id": "request-world-uuid-client-session-1",
    "request_turn_id": "client-turn-world-uuid-client-session-1",
    "client_session_id": "client-session-uuid",
    "daemon_session_id": "daemon-process-uuid"
  }
}
```

### `assistant_delta` — daemon → mod

Cosmetic streamed text. Each packet is independently discardable and is kept
well below the datagram budget by the daemon's delta batcher.

```json
{
  "v": 1,
  "type": "assistant_delta",
  "exchange_id": "ex-request-world-uuid-client-session-1",
  "seq": 2,
  "tick": 0,
  "payload": {
    "world_id": "world-uuid",
    "request_id": "request-world-uuid-client-session-1",
    "request_turn_id": "client-turn-world-uuid-client-session-1",
    "delta": "Steel Processing is at 42%.",
    "client_session_id": "client-session-uuid",
    "daemon_session_id": "daemon-process-uuid"
  }
}
```

### `assistant_end` — daemon → mod

The only successful completion signal. The daemon preflights this packet's
size, atomically commits the pending SQLite turn and head, then sends it. The
mod treats `full_text` as authoritative even if deltas were lost, duplicated,
or reordered.

```json
{
  "v": 1,
  "type": "assistant_end",
  "exchange_id": "ex-request-world-uuid-client-session-1",
  "seq": 3,
  "tick": 0,
  "payload": {
    "world_id": "world-uuid",
    "turn_id": "turn_0123456789abcdef0123456789abcdef",
    "request_turn_id": "client-turn-world-uuid-client-session-1",
    "request_id": "request-world-uuid-client-session-1",
    "parent_turn_id": "turn_0",
    "full_text": "Steel Processing is at 42%.",
    "duration_ms": 1150,
    "client_session_id": "client-session-uuid",
    "daemon_session_id": "daemon-process-uuid"
  }
}
```

The daemon-issued `turn_id` is the value the mod saves as its next
`conversation_head`.

### `error` — daemon → mod

Errors are request-scoped when `exchange_id` is non-empty. A request error
includes the same `world_id`, `request_id`, `request_turn_id`, client session,
and daemon generation fields as the stream, allowing the mod to reject stale
errors safely.

```json
{
  "v": 1,
  "type": "error",
  "exchange_id": "ex-request-world-uuid-client-session-1",
  "seq": 2,
  "tick": 0,
  "payload": {
    "code": "MODEL_TIMEOUT",
    "message": "The model request timed out.",
    "world_id": "world-uuid",
    "request_id": "request-world-uuid-client-session-1",
    "request_turn_id": "client-turn-world-uuid-client-session-1",
    "client_session_id": "client-session-uuid",
    "daemon_session_id": "daemon-process-uuid"
  }
}
```

Common codes include `BUSY`, `STALE_HEAD`, `STALE_SESSION`, `MODEL_HTTP`,
`MODEL_TIMEOUT`, `MODEL_STREAM`, `MODEL_EMPTY`, `MODEL_TOO_LARGE`, and
`DAEMON_ERROR`. The pending turn is marked aborted on a handled request
failure; the saved head does not advance.

## Ordering, reconnect, and duplicate policy

- A mod accepts assistant packets only for its active `exchange_id`, current
  `world_id`, current `client_session_id`, matching `request_id`/provisional
  turn, and current `daemon_session_id`.
- For an active exchange, a packet with `seq` less than or equal to the last
  accepted sequence is ignored. There are no per-delta acknowledgements or
  retransmissions.
- A duplicated `user_message` with the same fingerprint does not start a
  second model call. A completed exchange is cached briefly so the daemon can
  replay the authoritative `assistant_end` using its original final sequence.
- If a daemon or Factorio runtime generation changes, the old active request is
  cancelled and its pending row cannot enter history.
- One exchange may run per world. Independent worlds may use up to the daemon's
  configured worker limit.
