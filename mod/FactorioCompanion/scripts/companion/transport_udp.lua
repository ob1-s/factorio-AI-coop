local util = require("scripts.companion.util")
local protocol = require("scripts.companion.protocol")
local campaign = require("scripts.companion.campaign")
local chat = require("scripts.companion.chat")
local context_builder = require("scripts.companion.context")

local transport = {}

local DEFAULT_DAEMON_PORT = 34200
local HEARTBEAT_INTERVAL = 120       -- two seconds at 60 UPS
local HEARTBEAT_TIMEOUT = 900        -- forgiving of local model stalls
local ERROR_STATUS_TICKS = 180
local MAX_DATAGRAM_SIZE = 4096

local VALID_STATUS = {
  offline = true,
  connecting = true,
  ready = true,
  thinking = true,
  streaming = true,
  error = true
}

-- This is runtime state, not save state.  The first tick after a save reload
-- starts a fresh UDP session, so an in-flight exchange from the save's old
-- future cannot leave the UI stuck or consume a late packet.
local runtime_bootstrapped = false

local function now()
  return (game and game.tick) or 0
end

local function get_player(player_index)
  if not game then
    return nil
  end

  if player_index and tonumber(player_index) and tonumber(player_index) > 0 then
    local ok, player = pcall(function()
      return game.get_player(tonumber(player_index))
    end)
    if ok and player then
      return player
    end
  end

  local ok, player = pcall(function()
    return game.get_player(1)
  end)
  if ok and player then
    return player
  end

  local connected = game.connected_players
  if connected then
    return connected[1]
  end
  return nil
end

local function log_packet_drop(reason)
  util.log("discarding UDP packet: " .. tostring(reason))
end

function transport.ensure_storage()
  storage = storage or {}
  if type(storage.transport) ~= "table" then
    storage.transport = {}
  end

  local state = storage.transport
  if not VALID_STATUS[state.status] then
    state.status = "offline"
  end
  if type(state.last_hello_sent) ~= "number" then
    state.last_hello_sent = -HEARTBEAT_INTERVAL
  end
  if type(state.last_heartbeat_sent) ~= "number" then
    state.last_heartbeat_sent = -HEARTBEAT_INTERVAL
  end
  if type(state.last_heartbeat_received) ~= "number" then
    state.last_heartbeat_received = -1
  end
  if type(state.last_packet_received) ~= "number" then
    state.last_packet_received = -1
  end
  if type(state.control_seq) ~= "number" then
    state.control_seq = 0
  end
  if type(state.active_exchange) ~= "table" then
    state.active_exchange = nil
  end
  if type(state.seen_seq) ~= "table" then
    state.seen_seq = {}
  end
  if type(state.model) ~= "string" then
    state.model = "unknown"
  end
  if type(state.daemon_version) ~= "string" then
    state.daemon_version = ""
  end
  if type(state.daemon_session_id) ~= "string" then
    state.daemon_session_id = ""
  end
  if state.pending_hello_seq ~= nil and type(state.pending_hello_seq) ~= "number" then
    state.pending_hello_seq = nil
  end
  if type(state.error_until_tick) ~= "number" then
    state.error_until_tick = -1
  end
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
  if not VALID_STATUS[status_str] then
    return false, "invalid_status"
  end

  local changed = storage.transport.status ~= status_str
  storage.transport.status = status_str
  player = player or get_player()
  if player and changed then
    -- A broken GUI should not turn a malformed/network packet into a game
    -- script crash.  The transport state remains authoritative if rendering
    -- fails.
    local ok, err = pcall(chat.update_connection_status, player, status_str)
    if not ok then
      util.log("could not render connection status: " .. tostring(err))
    end
  end
  return true
end

-- Factorio's helpers.send_udp is a void API on successful calls.  Therefore
-- nil is success; an explicit false or a thrown error is failure.
function transport.send_raw(raw_str, player)
  player = player or get_player()
  if not player then
    return false, "no_player"
  end
  if type(raw_str) ~= "string" or raw_str == "" then
    return false, "empty_packet"
  end
  if #raw_str > MAX_DATAGRAM_SIZE then
    return false, "packet_too_large"
  end
  if not helpers or type(helpers.send_udp) ~= "function" then
    return false, "udp_helper_unavailable"
  end

  local port = transport.get_daemon_port()
  local ok, result = pcall(function()
    return helpers.send_udp(port, raw_str, player.index)
  end)
  if not ok then
    util.log("helpers.send_udp failed: " .. tostring(result))
    return false, tostring(result)
  end
  if result == false then
    util.log("helpers.send_udp reported failure")
    return false, "send_udp_failed"
  end
  return true
end

local function next_control_seq()
  transport.ensure_storage()
  storage.transport.control_seq = storage.transport.control_seq + 1
  return storage.transport.control_seq
end

local function clear_active_exchange()
  local active = storage.transport.active_exchange
  if active and active.id then
    storage.transport.seen_seq[active.id] = nil
  end
  storage.transport.active_exchange = nil
end

function transport.abort_active_exchange(player, message)
  transport.ensure_storage()
  local active = storage.transport.active_exchange
  if not active then
    return false
  end

  clear_active_exchange()
  storage.stream = nil
  if player then
    local ok, err = pcall(chat.stream_error, player, message or "The companion connection was lost.")
    if not ok then
      util.log("could not render exchange error: " .. tostring(err))
    end
  end
  return true
end

local function mark_offline(player, reason)
  transport.ensure_storage()
  local changed = storage.transport.status ~= "offline"
  transport.abort_active_exchange(player, reason)
  transport.set_status("offline", player)
  if changed and reason then
    util.log("companion daemon offline: " .. tostring(reason))
  end
end

local function packet_world(payload)
  if type(payload) ~= "table" then
    return nil, false
  end
  local world_id = payload.world_id
  if world_id == nil then
    world_id = payload.active_world_id
  elseif payload.active_world_id ~= nil and payload.active_world_id ~= world_id then
    return nil, false
  end
  if world_id == nil then
    return nil, true
  end
  if type(world_id) ~= "string" or world_id == "" then
    return nil, false
  end
  return world_id, true
end

local function world_matches(payload, allow_missing)
  local world_id, valid = packet_world(payload)
  if not valid then
    return false
  end
  if not world_id then
    return allow_missing == true
  end
  return world_id == campaign.get_world_id()
end

local function active_exchange_matches(pkt, active)
  if not active or pkt.exchange_id ~= active.id then
    return false
  end
  -- Responses are always tied to the current world and daemon generation. A
  -- missing identity is not a safe response to an in-flight exchange.
  if not world_matches(pkt.payload, false) then
    return false
  end

  local payload = pkt.payload
  if active.request_id and payload.request_id ~= active.request_id then
    return false
  end
  if active.turn_id and payload.request_turn_id ~= active.turn_id then
    return false
  end
  if active.client_session_id and payload.client_session_id ~= active.client_session_id then
    return false
  end
  if type(payload.daemon_session_id) ~= "string" or payload.daemon_session_id == "" then
    return false
  end
  if storage.transport.daemon_session_id ~= "" and payload.daemon_session_id ~= storage.transport.daemon_session_id then
    return false
  end
  return true
end

local function update_daemon_session(payload, player)
  local daemon_session_id = payload.daemon_session_id or payload.connection_id
  -- Every daemon response must identify its process generation. Without this
  -- field a delayed/local packet cannot be distinguished from the current
  -- daemon, and must never reset an active stream.
  if type(daemon_session_id) ~= "string" or daemon_session_id == "" then
    return false
  end
  if storage.transport.daemon_session_id ~= "" and daemon_session_id ~= storage.transport.daemon_session_id then
    -- A daemon restart is a new transport generation. Drop any old stream
    -- before accepting its heartbeat or hello acknowledgement.
    transport.abort_active_exchange(player, "The companion daemon restarted while replying.")
  end
  storage.transport.daemon_session_id = daemon_session_id
  return true
end

local function mark_packet_received(is_heartbeat)
  transport.ensure_storage()
  storage.transport.last_packet_received = now()
  if is_heartbeat then
    storage.transport.last_heartbeat_received = now()
  end
end

function transport.send_hello(player)
  transport.ensure_storage()
  player = player or get_player()
  if not player then
    return false, "no_player"
  end

  -- A hello is a new connection attempt.  Do not allow an exchange created
  -- by the old connection to survive a reconnect or save reload.
  transport.abort_active_exchange(player, "The companion connection was restarted.")
  storage.transport.last_packet_received = -1
  storage.transport.last_heartbeat_received = -1

  campaign.clear_runtime_session_id()
  local hello_seq = next_control_seq()
  storage.transport.pending_hello_seq = hello_seq

  local payload = {
    mod_version = "0.2.0",
    factorio_version = "2.0",
    world_id = campaign.get_world_id(),
    conversation_head = campaign.get_conversation_head(),
    player_name = player.name,
    capabilities = campaign.get_capabilities()
  }

  local pkt = protocol.create_packet("hello", "", hello_seq, payload)
  local sent, err = transport.send_raw(pkt, player)
  storage.transport.last_hello_sent = now()
  if not sent then
    mark_offline(player, err)
    return false, err
  end

  transport.set_status("connecting", player)
  util.log("sent hello to daemon (world_id=" .. tostring(payload.world_id or "<unbound>") .. ", head=" .. payload.conversation_head .. ")")
  return true
end

function transport.send_heartbeat(player)
  transport.ensure_storage()
  player = player or get_player()
  if not player then
    return false, "no_player"
  end

  local world_id = campaign.get_world_id()
  local client_session_id = campaign.get_runtime_session_id()
  if not world_id or not client_session_id then
    return false, "session_unbound"
  end

  local pkt = protocol.create_packet("heartbeat", "", next_control_seq(), {
    ping_tick = now(),
    state = storage.transport.status,
    world_id = world_id,
    client_session_id = client_session_id,
    daemon_session_id = storage.transport.daemon_session_id ~= "" and storage.transport.daemon_session_id or nil
  })
  local sent, err = transport.send_raw(pkt, player)
  if not sent then
    mark_offline(player, err)
    return false, err
  end
  storage.transport.last_heartbeat_sent = now()
  return true
end

function transport.can_start_exchange(player)
  transport.ensure_storage()
  if storage.transport.active_exchange then
    return false, "busy"
  end
  if storage.transport.status ~= "ready" then
    return false, storage.transport.status
  end
  if not campaign.get_world_id() or not campaign.get_runtime_session_id() then
    transport.set_status("connecting", player)
    return false, "connecting"
  end
  if storage.transport.last_packet_received < 0 or now() - storage.transport.last_packet_received > HEARTBEAT_TIMEOUT then
    mark_offline(player, "heartbeat_timeout")
    return false, "offline"
  end
  return true
end

local function copy_array(value, limit)
  if type(value) ~= "table" then
    return value
  end
  local copy = {}
  local count = math.min(#value, limit)
  for i = 1, count do
    copy[i] = value[i]
  end
  return copy
end

-- Context is intentionally a single datagram.  A well-stocked inventory or a
-- busy visible area can otherwise exceed the shared 4 KiB budget even though
-- every individual field is safe.  Trim only already-sanitized arrays, keep
-- their schema present, and never replace the capability-gated builder with
-- caller-provided data.
local function compact_context(source, list_limit)
  source = type(source) == "table" and source or {}
  local compact = {}
  for key, value in pairs(source or {}) do
    compact[key] = value
  end

  if type(source.inventory) == "table" then
    compact.inventory = {}
    for key, value in pairs(source.inventory) do
      compact.inventory[key] = copy_array(value, list_limit)
    end
  end

  if type(source.visible_perception) == "table" then
    compact.visible_perception = {}
    for key, value in pairs(source.visible_perception) do
      if key == "nearby_entities" or key == "alerts" then
        compact.visible_perception[key] = copy_array(value, list_limit)
      else
        compact.visible_perception[key] = value
      end
    end
  end

  return compact
end

function transport.send_user_message(player, text, turn_id, parent_turn_id, context_snapshot)
  transport.ensure_storage()
  player = player or get_player()
  if not player then
    return false, "no_player"
  end

  local can_send, reason = transport.can_start_exchange(player)
  if not can_send then
    return false, reason
  end
  if type(text) ~= "string" or text == "" then
    return false, "empty_message"
  end
  if type(turn_id) ~= "string" or turn_id == "" then
    return false, "invalid_turn_id"
  end
  if type(parent_turn_id) ~= "string" or parent_turn_id == "" then
    return false, "invalid_parent_turn_id"
  end
  -- Do not trust a caller-provided table at the LLM boundary.  The normal GUI
  -- path already builds context, but transport is also a callable module API;
  -- rebuilding here prevents any alternate caller from smuggling locked or
  -- hidden state into a user_message packet.
  local context_ok, safe_context = pcall(context_builder.build, player)
  if not context_ok or type(safe_context) ~= "table" then
    return false, "context_error"
  end
  context_snapshot = safe_context

  local world_id = campaign.get_world_id()
  if context_snapshot.world_id ~= nil and context_snapshot.world_id ~= world_id then
    return false, "context_world_mismatch"
  end

  local request_id = campaign.next_request_id()
  local exchange_id = "ex-" .. request_id
  local client_session_id = campaign.get_runtime_session_id()
  local payload = {
    request_id = request_id,
    client_session_id = client_session_id,
    turn_id = turn_id,
    client_turn_id = turn_id,
    parent_turn_id = parent_turn_id,
    world_id = world_id,
    text = text,
    context = context_snapshot
  }

  local pkt = protocol.create_packet("user_message", exchange_id, 1, payload)
  if pkt == '{"error":"encode failed"}' then
    return false, "context_encode_error"
  end

  if #pkt > MAX_DATAGRAM_SIZE then
    -- Preserve the complete context when it fits.  Otherwise progressively
    -- reduce only bounded arrays; the final attempt still carries the exact
    -- capability keys and all scalar fields needed by the model.
    for _, list_limit in ipairs({ 32, 16, 8, 4, 0 }) do
      context_snapshot = compact_context(context_snapshot, list_limit)
      payload.context = context_snapshot
      pkt = protocol.create_packet("user_message", exchange_id, 1, payload)
      if pkt ~= '{"error":"encode failed"}' and #pkt <= MAX_DATAGRAM_SIZE then
        break
      end
    end
  end
  if pkt == '{"error":"encode failed"}' then
    return false, "context_encode_error"
  end
  if #pkt > MAX_DATAGRAM_SIZE then
    return false, "packet_too_large"
  end

  -- The active exchange is committed only after send_raw succeeds.  A failed
  -- UDP call must never transition the UI into thinking.
  local sent, err = transport.send_raw(pkt, player)
  if not sent then
    mark_offline(player, err)
    return false, err
  end

  storage.transport.active_exchange = {
    id = exchange_id,
    request_id = request_id,
    turn_id = turn_id,
    parent_turn_id = parent_turn_id,
    world_id = world_id,
    client_session_id = client_session_id,
    start_tick = now(),
    last_seq = -1,
    stream_started = false
  }
  storage.transport.seen_seq[exchange_id] = -1
  transport.set_status("thinking", player)
  util.log("dispatched user_message " .. exchange_id .. " (turn=" .. turn_id .. ")")
  return true, nil, storage.transport.active_exchange
end

local function accept_sequence(active, seq)
  seq = tonumber(seq) or 0
  if seq <= active.last_seq then
    return false
  end
  active.last_seq = seq
  storage.transport.seen_seq[active.id] = seq
  return true
end

local function handle_packet_impl(event)
  if type(event) ~= "table" or type(event.payload) ~= "string" then
    log_packet_drop("missing string payload")
    return
  end

  local player = get_player(event.player_index)
  if not player then
    log_packet_drop("no player")
    return
  end

  local parse_ok, pkt, parse_err = pcall(protocol.parse_packet, event.payload)
  if not parse_ok then
    log_packet_drop("parser crashed: " .. tostring(pkt))
    return
  end
  if not pkt then
    log_packet_drop(parse_err)
    return
  end

  local ptype = pkt.type
  local payload = pkt.payload

  if ptype == "hello_ack" then
    local pending_hello_seq = storage.transport.pending_hello_seq

    if pending_hello_seq ~= nil then
      if tonumber(payload.hello_seq) ~= pending_hello_seq then
        log_packet_drop("hello_ack does not match the current hello")
        return
      end

      local assigned_world_id, valid_world = packet_world(payload)
      if not valid_world or not assigned_world_id then
        log_packet_drop("hello_ack has no valid assigned world")
        return
      end

      local current_world_id = campaign.get_world_id()
      if current_world_id == nil then
        local bound, bind_err = campaign.set_world_id(assigned_world_id)
        if not bound then
          log_packet_drop("could not bind assigned world: " .. tostring(bind_err))
          return
        end
      elseif current_world_id ~= assigned_world_id then
        log_packet_drop("hello_ack belongs to another world")
        return
      end

      local session_ok, session_err = campaign.set_runtime_session_id(payload.client_session_id)
      if not session_ok then
        log_packet_drop("hello_ack has invalid runtime session: " .. tostring(session_err))
        return
      end
      storage.transport.pending_hello_seq = nil
    else
      if not world_matches(payload, false) then
        log_packet_drop("hello_ack belongs to another world")
        return
      end
      if payload.client_session_id ~= nil and payload.client_session_id ~= campaign.get_runtime_session_id() then
        log_packet_drop("hello_ack belongs to another client session")
        return
      end
    end

    if not update_daemon_session(payload, player) then
      log_packet_drop("invalid daemon session id")
      return
    end
    if type(payload.daemon_version) == "string" then
      storage.transport.daemon_version = payload.daemon_version
    end
    if type(payload.model) == "string" then
      storage.transport.model = payload.model
    end
    mark_packet_received(false)
    storage.transport.last_heartbeat_received = now()
    storage.transport.error_until_tick = -1
    transport.set_status("ready", player)
    util.log("connected to companion daemon (model=" .. storage.transport.model .. ")")

  elseif ptype == "heartbeat" then
    if not world_matches(payload, false) then
      log_packet_drop("heartbeat belongs to another world")
      return
    end
    if payload.client_session_id ~= nil and payload.client_session_id ~= campaign.get_runtime_session_id() then
      log_packet_drop("heartbeat belongs to another client session")
      return
    end
    local previous_daemon_session_id = storage.transport.daemon_session_id
    local incoming_daemon_session_id = payload.daemon_session_id or payload.connection_id
    if not update_daemon_session(payload, player) then
      log_packet_drop("heartbeat belongs to another daemon session")
      return
    end
    mark_packet_received(true)
    local generation_changed = previous_daemon_session_id ~= ""
      and incoming_daemon_session_id ~= previous_daemon_session_id
    if generation_changed then
      campaign.clear_runtime_session_id()
      storage.transport.pending_hello_seq = nil
      storage.transport.last_hello_sent = -HEARTBEAT_INTERVAL
      transport.set_status("offline", player)
      return
    end

    if storage.transport.status == "connecting"
      or storage.transport.status == "offline"
      or storage.transport.status == "error" then
      if campaign.get_runtime_session_id() then
        transport.set_status("ready", player)
      else
        transport.set_status("connecting", player)
      end
    end

  elseif ptype == "assistant_start" then
    local active = storage.transport.active_exchange
    if not active_exchange_matches(pkt, active) then
      return
    end
    if pkt.seq ~= 1 then
      log_packet_drop("assistant_start must use sequence 1")
      return
    end
    if not accept_sequence(active, pkt.seq) then
      return
    end
    if payload.model ~= nil and type(payload.model) ~= "string" then
      log_packet_drop("assistant_start has invalid model")
      return
    end
    if active.stream_started then
      return
    end
    active.stream_started = true
    mark_packet_received(false)
    transport.set_status("streaming", player)
    local ok, err = pcall(chat.stream_start, player, payload.model)
    if not ok then
      util.log("could not render assistant_start: " .. tostring(err))
    end

  elseif ptype == "assistant_delta" then
    local active = storage.transport.active_exchange
    if not active_exchange_matches(pkt, active) then
      return
    end
    if type(payload.delta) ~= "string" then
      log_packet_drop("assistant_delta has no string delta")
      return
    end
    if not accept_sequence(active, pkt.seq) then
      return
    end
    if not active.stream_started then
      active.stream_started = true
      transport.set_status("streaming", player)
      pcall(chat.stream_start, player, payload.model)
    end
    mark_packet_received(false)
    local ok, err = pcall(chat.stream_append, player, payload.delta)
    if not ok then
      util.log("could not render assistant_delta: " .. tostring(err))
    end

  elseif ptype == "assistant_end" then
    local active = storage.transport.active_exchange
    if not active_exchange_matches(pkt, active) then
      return
    end
    if type(payload.full_text) ~= "string" or payload.full_text == "" or not payload.full_text:match("%S") then
      log_packet_drop("assistant_end has no authoritative full_text")
      transport.abort_active_exchange(player, "The companion sent an invalid completion.")
      storage.transport.error_until_tick = now() + ERROR_STATUS_TICKS
      transport.set_status("error", player)
      return
    end
    if type(payload.turn_id) ~= "string" or payload.turn_id == "" then
      log_packet_drop("assistant_end has no authoritative turn_id")
      transport.abort_active_exchange(player, "The companion sent an invalid turn identity.")
      storage.transport.error_until_tick = now() + ERROR_STATUS_TICKS
      transport.set_status("error", player)
      return
    end
    if payload.client_turn_id ~= nil and payload.client_turn_id ~= active.turn_id then
      log_packet_drop("assistant_end client turn mismatch")
      return
    end
    if payload.request_turn_id ~= nil and payload.request_turn_id ~= active.turn_id then
      log_packet_drop("assistant_end request turn mismatch")
      return
    end
    if payload.parent_turn_id ~= nil and payload.parent_turn_id ~= active.parent_turn_id then
      log_packet_drop("assistant_end parent turn mismatch")
      return
    end
    if pkt.seq < 2 or not accept_sequence(active, pkt.seq) then
      log_packet_drop("assistant_end has stale or invalid sequence")
      return
    end

    local head_ok, head_err = campaign.set_conversation_head(payload.turn_id)
    if not head_ok then
      log_packet_drop("assistant_end invalid completed turn: " .. tostring(head_err))
      transport.abort_active_exchange(player, "The companion sent an invalid completed turn.")
      storage.transport.error_until_tick = now() + ERROR_STATUS_TICKS
      transport.set_status("error", player)
      return
    end

    -- full_text is authoritative.  The streamed text is only a cosmetic
    -- preview and is discarded even when deltas were lost or reordered.
    mark_packet_received(false)
    local ok, err = pcall(chat.stream_end, player, payload.full_text, payload.turn_id)
    if not ok then
      util.log("could not render assistant_end: " .. tostring(err))
    end
    clear_active_exchange()
    storage.transport.error_until_tick = -1
    transport.set_status("ready", player)
    util.log("completed exchange " .. active.id .. " (head=" .. payload.turn_id .. ")")

  elseif ptype == "error" then
    local active = storage.transport.active_exchange
    if active then
      if not active_exchange_matches(pkt, active) then
        return
      end
      if not accept_sequence(active, pkt.seq) then
        return
      end
      local message = payload.message
      if type(message) ~= "string" or message == "" then
        message = type(payload.code) == "string" and payload.code or "Unknown companion error"
      end
      mark_packet_received(false)
      transport.abort_active_exchange(player, message)
      storage.transport.error_until_tick = now() + ERROR_STATUS_TICKS
      transport.set_status("error", player)
      util.log("assistant error on " .. active.id .. ": " .. message)
    elseif pkt.exchange_id ~= "" then
      -- Never let an old request-specific error alter the current connection
      -- or a later request.
      return
    elseif not world_matches(payload, true) then
      return
    else
      mark_packet_received(false)
      storage.transport.error_until_tick = now() + ERROR_STATUS_TICKS
      transport.set_status("error", player)
    end
  end
end

function transport.handle_packet(event)
  transport.ensure_storage()
  local ok, err = xpcall(function()
    handle_packet_impl(event)
  end, function(e)
    return tostring(e)
  end)
  if not ok then
    util.log("UDP packet handler recovered from error: " .. tostring(err))
  end
end

local function reset_runtime_connection(player)
  if runtime_bootstrapped then
    return
  end
  runtime_bootstrapped = true
  transport.abort_active_exchange(player, "The Factorio session was reloaded.")
  campaign.clear_runtime_session_id()
  storage.transport.pending_hello_seq = nil
  storage.transport.last_packet_received = -1
  storage.transport.last_heartbeat_received = -1
  storage.transport.last_hello_sent = -HEARTBEAT_INTERVAL
  storage.transport.last_heartbeat_sent = -HEARTBEAT_INTERVAL
  transport.set_status("offline", player)
end

function transport.on_tick()
  transport.ensure_storage()
  if not game or not game.connected_players or #game.connected_players == 0 then
    return
  end

  local player = game.connected_players[1] or get_player()
  if not player then
    return
  end

  reset_runtime_connection(player)

  if not helpers or type(helpers.recv_udp) ~= "function" then
    mark_offline(player, "udp_helper_unavailable")
  else
    local recv_ok, recv_err = pcall(function()
      helpers.recv_udp(player.index)
    end)
    if not recv_ok then
      -- This is diagnostic only; do not throw from on_tick.  The normal
      -- heartbeat timeout remains the liveness decision.
      if storage.transport.last_receive_error_tick == nil or now() - storage.transport.last_receive_error_tick > HEARTBEAT_INTERVAL * 5 then
        storage.transport.last_receive_error_tick = now()
        util.log("helpers.recv_udp failed: " .. tostring(recv_err))
      end
    end
  end

  if now() % 60 ~= 0 then
    return
  end

  local status = storage.transport.status
  if status ~= "offline" then
    local last_rx = storage.transport.last_packet_received
    if last_rx < 0 or now() - last_rx > HEARTBEAT_TIMEOUT then
      mark_offline(player, "heartbeat_timeout")
      status = "offline"
    elseif status == "error" and now() >= storage.transport.error_until_tick then
      transport.set_status("ready", player)
      status = "ready"
    end
  end

  if now() % HEARTBEAT_INTERVAL ~= 0 then
    return
  end

  if status == "offline" then
    if now() - storage.transport.last_hello_sent >= HEARTBEAT_INTERVAL then
      transport.send_hello(player)
    end
  elseif status == "connecting" then
    if now() - storage.transport.last_hello_sent >= HEARTBEAT_TIMEOUT then
      transport.send_hello(player)
    end
  elseif status == "ready" or status == "thinking" or status == "streaming" or status == "error" then
    if now() - storage.transport.last_heartbeat_sent >= HEARTBEAT_INTERVAL then
      transport.send_heartbeat(player)
    end
  end
end

return transport
