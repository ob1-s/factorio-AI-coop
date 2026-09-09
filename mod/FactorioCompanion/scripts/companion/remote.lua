local util = require("scripts.companion.util")
local chat = require("scripts.companion.chat")
local ui = require("scripts.companion.ui")
local selftest = require("scripts.companion.selftest")
local campaign = require("scripts.companion.campaign")
local transport = require("scripts.companion.transport_udp")

local remote_if = { version = "0.2.0" }

local function get_player()
  local p = game.get_player(1)
  if p then
    return p
  end
  return game.connected_players[1]
end

function remote_if.register()
  local iface = {}

  iface.ping = function()
    return util.ok({ pong = game.tick, version = remote_if.version })
  end

  iface.version = function()
    return remote_if.version
  end

  iface.status = function()
    local player = get_player()
    local active = storage.transport and storage.transport.active_exchange
    return util.ok({
      tick = game.tick,
      connected_players = #game.connected_players,
      chat_open = player and chat.is_open(player) or false,
      unread = storage.unread,
      transport_status = transport.get_status(),
      active_exchange_id = active and active.id or nil,
      world_id = campaign.get_world_id(),
      conversation_head = campaign.get_conversation_head(),
      capabilities = campaign.get_capabilities()
    })
  end

  -- Conversational delivery, streaming, and outbox draining intentionally
  -- have no RCON/remote-interface entry points.  UDP is the sole product
  -- message path; this interface is retained for diagnostics and campaign
  -- administration only.

  iface.ui_spec = function(json_str)
    local spec = util.dec(json_str)
    if not spec then
      return util.err("invalid json: " .. string.sub(tostring(json_str), 1, 200))
    end
    return ui.dispatch(spec)
  end

  iface.screenshot = function(json_str)
    return util.err("RCON world capture is disabled; use the UDP product path")
  end

  iface.selftest = function()
    return selftest.run()
  end

  iface.set_companion_level = function(level)
    return util.ok({ capabilities = campaign.set_level(level), level = campaign.get_level() })
  end

  iface.get_companion_level = function()
    return util.ok({ level = campaign.get_level(), capabilities = campaign.get_capabilities() })
  end

  iface.unlock_capability = function(feature_key)
    local unlocked, err = campaign.unlock(feature_key)
    if err then
      return util.err(err)
    end
    return util.ok({ unlocked = unlocked, capabilities = campaign.get_capabilities() })
  end

  iface.lock_capability = function(feature_key)
    local locked, err = campaign.lock(feature_key)
    if err then
      return util.err(err)
    end
    return util.ok({ locked = locked, capabilities = campaign.get_capabilities() })
  end

  iface.get_timeline_info = function()
    return util.ok(campaign.capability_state())
  end

  remote.add_interface("copilot", iface)
end

return remote_if
