# Factorio AI Companion UDP Protocol Specification (v1)

This document defines the lightweight, versioned UDP protocol between the **Factorio Companion Mod** and the **Companion Daemon**.

---

## 1. Transport & Addressing

- **Medium**: Localhost UDP (`127.0.0.1`).
- **Factorio Listening Port**: Default `34199` (configured via Factorio CLI `--enable-lua-udp 34199`).
- **Companion Daemon Listening Port**: Default `34200`.
- **Packet Encoding**: UTF-8 JSON datagrams.
- **Maximum Datagram Size**: 4096 bytes (safe below OS MTU and well within Factorio's 256KB socket buffer).

---

## 2. Common Envelope Format

Every packet exchanged over UDP contains a standard envelope:

```json
{
  "v": 1,
  "type": "<message_type>",
  "exchange_id": "<string>",
  "seq": 0,
  "tick": 12345,
  "payload": {}
}
```

- `v` (uint): Protocol version, currently `1`.
- `type` (string): Discriminator string.
- `exchange_id` (string): Unique ID for the conversational exchange (`"ex_<world_id>_<turn_id>"`), or empty for session-level control (`hello`, `heartbeat`).
- `seq` (uint): Monotonically increasing sequence number per exchange.
- `tick` (uint): Current Factorio game tick when sent.
- `payload` (object): Message-specific data payload.

---

## 3. Reliability & Streaming Philosophy

1. **Lightweight Reliability without TCP Emulation**:
   - Factorio turns incoming UDP into deterministic game engine input actions. We deliberately avoid per-delta ACKs or retransmissions to prevent flooding the engine's input closure.
   - Sequence numbers allow the receiver to discard duplicate or out-of-order packets.
2. **Authoritative Completion**:
   - `assistant_delta` delivers incremental text for cosmetic live streaming in the GUI.
   - If a transient delta packet is dropped or arrives late, `assistant_end` delivers the complete, authoritative `full_text`. The UI reconciles to `full_text` upon completion.
3. **Streaming Cadence**:
   - The daemon buffers LLM tokens and emits `assistant_delta` at an interval of ~80–150ms or ~20–40 characters per packet. This guarantees fluid visual feedback (8–12 FPS typing) while keeping UDP traffic minimal.

---

## 4. Save Rollback & Timeline Branching

To prevent "remembering the future" when a player reloads an earlier save:
- **`world_id`**: A UUID generated when a save game is initialized and stored authoritatively in the Factorio save file (`storage.companion.world_id`).
- **`conversation_head`**: The ID of the latest completed conversation turn stored authoritatively in the Factorio save (`storage.companion.conversation_head`).
- Every conversation turn is a node in a directed acyclic tree maintained by the daemon:
  - Node contains: `turn_id`, `parent_turn_id`, `user_text`, `assistant_text`, `context_snapshot`, `timestamp`.
- When Factorio connects or reloads a save, it announces `(world_id, conversation_head)`.
- If `conversation_head` points to an ancestor node (because the player reloaded an older save), the daemon branches from that ancestor. Future turns recorded after that point in previous timelines are preserved in the tree but excluded from the active conversational history.

---

## 5. Message Types

### 5.1 `hello` (Mod → Daemon)
Initiated on game load, unpause, or reconnect to handshake and synchronize state.

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
    "world_id": "550e8400-e29b-41d4-a716-446655440000",
    "conversation_head": "turn_12",
    "player_name": "engineer",
    "capabilities": ["telemetry", "research", "inventory"]
  }
}
```

### 5.2 `hello_ack` (Daemon → Mod)
Daemon acknowledgment of handshake.

```json
{
  "v": 1,
  "type": "hello_ack",
  "exchange_id": "",
  "seq": 1,
  "tick": 600,
  "payload": {
    "daemon_version": "0.2.0",
    "model": "hermes-3-llama-3.1-8b",
    "active_world_id": "550e8400-e29b-41d4-a716-446655440000",
    "active_turn_id": "turn_12",
    "history_depth": 12,
    "status": "ready"
  }
}
```

### 5.3 `heartbeat` (Mod ⇄ Daemon)
Exchanged every 60–180 ticks to maintain connection liveness and track latency.

```json
{
  "v": 1,
  "type": "heartbeat",
  "exchange_id": "",
  "seq": 45,
  "tick": 2700,
  "payload": {
    "ping_tick": 2700,
    "state": "ready"
  }
}
```

### 5.4 `user_message` (Mod → Daemon)
Dispatched when the player submits a message in the companion GUI. Includes an explicit grounded context snapshot for this turn.

```json
{
  "v": 1,
  "type": "user_message",
  "exchange_id": "ex_turn_13",
  "seq": 1,
  "tick": 3600,
  "payload": {
    "turn_id": "turn_13",
    "parent_turn_id": "turn_12",
    "world_id": "550e8400-e29b-41d4-a716-446655440000",
    "text": "What are we researching right now and do we have enough copper plates?",
    "context": {
      "player": {
        "name": "engineer",
        "position": { "x": 12.5, "y": -4.2 },
        "surface": "nauvis",
        "health": 250.0,
        "max_health": 250.0,
        "walking": false,
        "vehicle": null
      },
      "capabilities": ["telemetry", "research", "inventory"],
      "research": {
        "current": "steel-processing",
        "progress": 0.42,
        "queue": ["oil-processing"],
        "unlocked_count": 14
      },
      "inventory": {
        "copper-plate": 142,
        "iron-plate": 87,
        "electronic-circuit": 34
      },
      "visible_perception": {
        "nearby_entities": [
          { "name": "iron-chest", "x": 10, "y": -5, "contents": { "iron-plate": 200 } }
        ],
        "active_alerts": []
      },
      "charted_knowledge": {
        "discovered_bounds": { "min_x": -160, "min_y": -160, "max_x": 160, "max_y": 160 }
      }
    }
  }
}
```

### 5.5 `assistant_start` (Daemon → Mod)
Notifies the mod that the LLM has accepted the prompt and generation has begun. Transitions UI to `thinking` / `streaming`.

```json
{
  "v": 1,
  "type": "assistant_start",
  "exchange_id": "ex_turn_13",
  "seq": 1,
  "tick": 3615,
  "payload": {
    "model": "hermes-3-llama-3.1-8b"
  }
}
```

### 5.6 `assistant_delta` (Daemon → Mod)
Incremental text chunk for live GUI typing effect.

```json
{
  "v": 1,
  "type": "assistant_delta",
  "exchange_id": "ex_turn_13",
  "seq": 2,
  "tick": 3625,
  "payload": {
    "delta": "We're currently 42% through Steel Processing."
  }
}
```

### 5.7 `assistant_end` (Daemon → Mod)
Signals completion of the exchange. Supplies authoritative final text and updates the persistent conversation head.

```json
{
  "v": 1,
  "type": "assistant_end",
  "exchange_id": "ex_turn_13",
  "seq": 7,
  "tick": 3680,
  "payload": {
    "turn_id": "turn_13",
    "full_text": "We're currently 42% through Steel Processing. In your inventory, you have 142 copper plates, which should be plenty for basic crafting.",
    "duration_ms": 1150
  }
}
```

### 5.8 `error` (Daemon → Mod / Mod → Daemon)
Signals a processing, timeout, or model failure. Clears generation state and displays the error in-game.

```json
{
  "v": 1,
  "type": "error",
  "exchange_id": "ex_turn_13",
  "seq": 8,
  "tick": 3700,
  "payload": {
    "code": "MODEL_TIMEOUT",
    "message": "LLM request timed out after 30 seconds."
  }
}
```
