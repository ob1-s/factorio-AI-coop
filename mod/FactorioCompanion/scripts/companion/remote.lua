local util = require("scripts.companion.util")
local world = require("scripts.companion.world")
local markers = require("scripts.companion.markers")
local panels = require("scripts.companion.panels")
local chat = require("scripts.companion.chat")
local ui = require("scripts.companion.ui")
local selftest = require("scripts.companion.selftest")

local remote_if = { version = "0.1.0" }

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
    return util.ok({
      tick = game.tick,
      connected_players = #game.connected_players,
      outbox_pending = #storage.outbox,
      chat_open = player and chat.is_open(player) or false,
      unread = storage.unread,
      agent_busy = storage.agent_busy
    })
  end

  iface.drain_outbox = function(limit)
    return util.ok({ messages = chat.drain_outbox(limit) })
  end

  iface.deliver_agent_message = function(text)
    local player = get_player()
    if not player then
      return util.err("no players connected")
    end
    return util.ok({ message_id = chat.deliver_agent_message(player, tostring(text)) })
  end

  iface.stream_start = function()
    local player = get_player()
    if not player then
      return util.err("no players connected")
    end
    chat.stream_start(player)
    return util.ok({})
  end

  iface.stream_append = function(chunk)
    local player = get_player()
    if not player then
      return util.err("no players connected")
    end
    chat.stream_append(player, tostring(chunk))
    return util.ok({})
  end

  iface.stream_end = function(final_text)
    local player = get_player()
    if not player then
      return util.err("no players connected")
    end
    chat.stream_end(player, final_text and tostring(final_text) or "")
    return util.ok({})
  end

  iface.set_agent_busy = function(busy)
    storage.agent_busy = busy == true or busy == "true"
    local player = get_player()
    if player then
      if storage.agent_busy then
        chat.set_status(player, { "companion.status-thinking" })
      else
        chat.set_status(player, "")
      end
    end
    return util.ok({ agent_busy = storage.agent_busy })
  end

  iface.ui_spec = function(json_str)
    local spec = util.dec(json_str)
    if not spec then
      return util.err("invalid json: " .. string.sub(tostring(json_str), 1, 200))
    end
    return ui.dispatch(spec)
  end

  iface.overview = function()
    local ok, result = pcall(world.overview)
    if not ok then
      return util.err(result)
    end
    return util.begin_query("overview", result)
  end

  iface.player_state = function()
    local state = world.player_state()
    if not state then
      return util.err("no player")
    end
    return util.begin_query("player_state", state)
  end

  iface.inventory = function()
    local ok, result = pcall(world.inventory)
    if not ok then
      return util.err(result)
    end
    return util.begin_query("inventory", result)
  end

  iface.research = function()
    local ok, result = pcall(world.research)
    if not ok then
      return util.err(result)
    end
    return util.begin_query("research", result)
  end

  iface.area = function(surface, x, y, radius)
    local ok, result = pcall(world.area, surface, tonumber(x), tonumber(y), tonumber(radius))
    if not ok then
      return util.err(result)
    end
    return util.begin_query("area", result)
  end

  iface.find = function(json_str)
    local spec = util.dec(json_str) or {}
    local ok, result = pcall(world.find, spec)
    if not ok then
      return util.err(result)
    end
    return util.begin_query("find", result)
  end

  iface.entity_at = function(surface, x, y)
    local ok, result = pcall(world.entity_at, surface, tonumber(x), tonumber(y))
    if not ok then
      return util.err(result)
    end
    return util.begin_query("entity_at", result)
  end

  iface.charted_bounds = function(surface)
    local ok, result = pcall(world.charted_bounds, surface)
    if not ok then
      return util.err(result)
    end
    return util.begin_query("charted_bounds", result)
  end

  iface.screenshot = function(json_str)
    local opts = util.dec(json_str) or {}
    local player = get_player()
    if not player then
      return util.err("no players connected")
    end
    local name = opts.name and string.gsub(tostring(opts.name), "[^%w%-_]", "") or ("shot_" .. game.tick)
    local path = "companion/" .. name .. ".png"
    game.take_screenshot({
      by_player = player.index,
      path = path,
      position = opts.position and { x = tonumber(opts.position.x), y = tonumber(opts.position.y) } or player.position,
      resolution = { x = math.min(tonumber(opts.width) or 1280, 4000), y = math.min(tonumber(opts.height) or 720, 4000) },
      zoom = tonumber(opts.zoom) or 0.5,
      anti_alias = opts.anti_alias ~= false,
      show_gui = opts.show_gui == true,
      show_entity_info = false,
      hide_fog = opts.hide_fog == true,
      allow_in_replay = false
    })
    game.set_wait_for_screenshots_to_finish()
    return util.ok({ file = path })
  end

  iface.fetch_chunk = function(qid, index)
    return util.fetch_chunk(qid, index)
  end

  iface.selftest = function()
    return selftest.run()
  end

  remote.add_interface("copilot", iface)
end

return remote_if
