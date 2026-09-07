local util = require("scripts.companion.util")
local protocol = require("scripts.companion.protocol")
local campaign = require("scripts.companion.campaign")
local chat = require("scripts.companion.chat")

local transport = {}

local DEFAULT_DAEMON_PORT = 34200
local HEARTBEAT_INTERVAL = 120
local HEARTBEAT_TIMEOUT = 360

function transport.ensure_storage()
  storage.transport = storage.transport or {}
  storage.transport.status = storage.transport.status or "offline"
  storage.transport.last_heartbeat_sent = storage.transport.last_heartbeat_sent or 0
  storage.transport.last_heartbeat_received = storage.transport.last_heartbeat_received or 0
  storage.transport.active_exchange = storage.transport.active_exchange or nil
  storage.transport.seen_seq = storage.transport.seen_seq or {}
  storage.transport.model = storage.transport.model or "unknown"
  storage.transport.daemon_version = storage.transport.daemon_version or ""
end

function transport.get_daemon_port()
  if settings and settings.global and settings.global["companion-daemon-port"] then
    return tonumber(settings.global["companion-daemon-port"].value) or DEFAULT_DAEMON_PORT
  end
  return DEFAULT_DAEMON_PORT
end

function transport.get_status()
  transport.ensure_storage()
  return storage.transport.status
end

function transport.set_status(status_str, player)
  transport.ensure_storage()
  storage.transport.status = status_str
  player = player or game.get_player(1)
  if player then
    chat.update_connection_status(player, status_str)
  end
end

function transport.send_raw(raw_str, player)
  player = player or game.get_player(1)
  if not player then
    return false, "no_player"
  end

  local port = transport.get_daemon_port()
  local ok, err = pcall(helpers.send_udp, port, raw_str, player.index)
  if not ok then
    util.log("helpers.send_udp failed: " .. tostring(err))
    return false, err
  end
  return true, nil
end

function transport.send_hello(player)
  transport.ensure_storage()
  player = player or game.get_player(1)
  if not player then
    return
  end

  transport.set_status("connecting", player)
  local payload = {
    mod_version = "0.2.0",
    factorio_version = "2.0",
    world_id = campaign.get_world_id(),
    conversation_head = campaign.get_conversation_head(),
    player_name = player.name,
    capabilities = campaign.get_capabilities()
  }

  local pkt = protocol.create_packet("hello", "", 1, payload)
  transport.send_raw(pkt, player)
  storage.transport.last_heartbeat_sent = game.tick
  util.log("sent hello to daemon (world_id=" .. payload.world_id .. ", head=" .. payload.conversation_head .. ")")
end

function transport.send_heartbeat(player)
  transport.ensure_storage()
  player = player or game.get_player(1)
  if not player then
    return
  end

  local pkt = protocol.create_packet("heartbeat", "", 1, {
    ping_tick = game.tick,
    state = storage.transport.status
  })
  transport.send_raw(pkt, player)
  storage.transport.last_heartbeat_sent = game.tick
end

function transport.send_user_message(player, text, turn_id, parent_turn_id, context_snapshot)
  transport.ensure_storage()
  local exchange_id = "ex_" .. tostring(turn_id)
  storage.transport.active_exchange = {
    id = exchange_id,
    turn_id = turn_id,
    start_tick = game.tick
  }
  storage.transport.seen_seq[exchange_id] = 0

  local payload = {
    turn_id = turn_id,
    parent_turn_id = parent_turn_id,
    world_id = campaign.get_world_id(),
    text = text,
    context = context_snapshot
  }

  local pkt = protocol.create_packet("user_message", exchange_id, 1, payload)
  transport.send_raw(pkt, player)
  transport.set_status("thinking", player)
  util.log("dispatched user_message " .. exchange_id)
end

function transport.handle_packet(event)
  transport.ensure_storage()
  local raw = event.payload
  local player = game.get_player(event.player_index) or game.get_player(1)
  if not player then
    return
  end

  local pkt, err = protocol.parse_packet(raw)
  if not pkt then
    util.log("discarding malformed packet: " .. tostring(err))
    return
  end

  local ptype = pkt.type
  local payload = pkt.payload

  if ptype == "hello_ack" then
    storage.transport.daemon_version = payload.daemon_version or ""
    storage.transport.model = payload.model or ""
    storage.transport.last_heartbeat_received = game.tick
    transport.set_status("ready", player)
    util.log("connected to companion daemon (model=" .. storage.transport.model .. ")")

  elseif ptype == "heartbeat" then
    storage.transport.last_heartbeat_received = game.tick
    if storage.transport.status == "connecting" or storage.transport.status == "offline" then
      transport.set_status("ready", player)
    end

  elseif ptype == "assistant_start" then
    local active = storage.transport.active_exchange
    if active and pkt.exchange_id == active.id then
      transport.set_status("streaming", player)
      chat.stream_start(player, payload.model)
    end

  elseif ptype == "assistant_delta" then
    local active = storage.transport.active_exchange
    if active and pkt.exchange_id == active.id then
      local last_seq = storage.transport.seen_seq[active.id] or 0
      if pkt.seq > last_seq then
        storage.transport.seen_seq[active.id] = pkt.seq
        chat.stream_append(player, payload.delta or "")
      else
        util.log("dropped duplicate/out-of-order delta seq " .. tostring(pkt.seq))
      end
    end

  elseif ptype == "assistant_end" then
    local active = storage.transport.active_exchange
    if active and pkt.exchange_id == active.id then
      chat.stream_end(player, payload.full_text or "", payload.turn_id or active.turn_id)
      if payload.turn_id then
        campaign.set_conversation_head(payload.turn_id)
      end
      storage.transport.active_exchange = nil
      transport.set_status("ready", player)
      util.log("completed exchange " .. active.id)
    end

  elseif ptype == "error" then
    local active = storage.transport.active_exchange
    if active and (pkt.exchange_id == "" or pkt.exchange_id == active.id) then
      chat.stream_error(player, payload.message or payload.code or "Unknown error")
      storage.transport.active_exchange = nil
      transport.set_status("ready", player)
      util.log("assistant error on " .. tostring(pkt.exchange_id) .. ": " .. tostring(payload.message))
    end
  end
end

function transport.on_tick()
  -- In Factorio 2.0, only poll UDP when a connected player exists
  if not game or #game.connected_players == 0 then
    return
  end

  local player = game.connected_players[1]
  if not player then
    return
  end

  -- Poll incoming UDP packets
  pcall(helpers.recv_udp, player.index)

  transport.ensure_storage()

  -- Check connection state and heartbeats
  if game.tick % 60 == 0 then
    local diff = game.tick - storage.transport.last_heartbeat_received
    if diff > HEARTBEAT_TIMEOUT and storage.transport.status ~= "offline" then
      transport.set_status("offline", player)
      util.log("companion daemon heartbeat timed out; switching to offline")
    end

    if game.tick % HEARTBEAT_INTERVAL == 0 then
      if storage.transport.status == "ready" then
        transport.send_heartbeat(player)
      elseif storage.transport.status == "offline" or storage.transport.status == "connecting" then
        transport.send_hello(player)
      end
    end
  end
end

return transport
