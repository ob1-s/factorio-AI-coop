local remote_if = require("scripts.companion.remote")
local chat = require("scripts.companion.chat")
local panels = require("scripts.companion.panels")
local markers = require("scripts.companion.markers")
local util = require("scripts.companion.util")
local campaign = require("scripts.companion.campaign")
local transport_udp = require("scripts.companion.transport_udp")

local function ensure_storage()
  storage.chat_log = storage.chat_log or {}
  -- Migration: the persisted RCON conversational queue is obsolete.  User
  -- messages are recorded only after a successful UDP dispatch.
  storage.outbox = nil
  storage.agent_busy = nil
  storage.next_msg_id = storage.next_msg_id or 1
  storage.queries = storage.queries or {}
  storage.next_query_id = storage.next_query_id or 1
  storage.markers = storage.markers or {}
  storage.next_marker_id = storage.next_marker_id or 1
  storage.tasks = storage.tasks or {}
  storage.next_task_id = storage.next_task_id or 1
  storage.panel_spec = storage.panel_spec or {}
  storage.dirty = storage.dirty or {}
  storage.unread = storage.unread or 0
  storage.stream = storage.stream or nil
  campaign.ensure_storage()
  transport_udp.ensure_storage()
end

local function init_player(player)
  storage.chat_log[player.index] = storage.chat_log[player.index] or {}
end

script.on_init(function()
  ensure_storage()
  remote_if.register()
  for _, player in pairs(game.players) do
    init_player(player)
  end
  util.log("initialized, version " .. remote_if.version .. ", world_id: " .. campaign.get_world_id())
end)


script.on_load(function()
  remote_if.register()
end)

script.on_configuration_changed(function()
  ensure_storage()
  for _, player in pairs(game.players) do
    init_player(player)
    local root = player.gui.screen.companion_chat
    if root then
      root.destroy()
    end
    local tasks = player.gui.screen.companion_tasks
    if tasks then
      tasks.destroy()
    end
    local info = player.gui.screen.companion_info
    if info then
      info.destroy()
    end
    local pill = player.gui.relative.companion_chat_pill
    if pill then
      pill.destroy()
    end
    chat.update_badge(player)
    transport_udp.send_hello(player)
  end
end)

script.on_event(defines.events.on_player_joined_game, function(event)
  local player = game.get_player(event.player_index)
  if player then
    init_player(player)
    chat.update_badge(player)
    transport_udp.send_hello(player)
  end
end)

script.on_event(defines.events.on_player_removed, function(event)
  storage.chat_log[event.player_index] = nil
  storage.tasks[event.player_index] = nil
  storage.markers[event.player_index] = nil
end)

script.on_event(defines.events.on_udp_packet_received, function(event)
  transport_udp.handle_packet(event)
end)

script.on_event(defines.events.on_gui_click, function(event)
  local player = game.get_player(event.player_index)
  if not player then
    return
  end
  local name = event.element.name or ""
  if not string.find(name, "^companion_") and name ~= "send" then
    return
  end
  if chat.on_gui_click(player, name) then
    return
  end
  panels.handle_click(player, name)
end)

script.on_event(defines.events.on_gui_confirmed, function(event)
  local player = game.get_player(event.player_index)
  if not player then
    return
  end
  chat.on_gui_confirmed(player, event.element.name or "")
end)

script.on_event("companion-toggle-chat", function(event)
  local player = game.get_player(event.player_index)
  if player then
    chat.toggle(player)
    chat.update_badge(player)
  end
end)

script.on_event("companion-toggle-tasks", function(event)
  local player = game.get_player(event.player_index)
  if player then
    panels.toggle_tasks(player)
  end
end)

script.on_event(defines.events.on_tick, function()
  transport_udp.on_tick()

  if game.tick % 60 == 0 then
    for _, player in ipairs(game.connected_players) do
      markers.remove_expired(player.index)
      chat.render_if_dirty(player)
    end
  end
end)

commands.add_command("copilot-selftest", "Run companion mod self tests", function(event)
  local player = game.get_player(event.player_index)
  local result
  if player then
    result = require("scripts.companion.selftest").run()
    player.print(result)
  else
    print(require("scripts.companion.selftest").run())
  end
end)

commands.add_command("companion-level", "Set companion capability level (0..4)", function(event)
  local level = tonumber(event.parameter)
  if level then
    local caps = campaign.set_level(level)
    local msg = "Companion level set to " .. tostring(level) .. " (capabilities: " .. table.concat(caps, ", ") .. ")"
    if event.player_index then
      local p = game.get_player(event.player_index)
      if p then p.print(msg) end
    else
      print(msg)
    end
  else
    local msg = "Usage: /companion-level <0..4>"
    if event.player_index then
      local p = game.get_player(event.player_index)
      if p then p.print(msg) end
    else
      print(msg)
    end
  end
end)

commands.add_command("companion-unlock", "Unlock a companion capability flag", function(event)
  local flag = event.parameter
  if flag and flag ~= "" then
    campaign.unlock(flag)
    local msg = "Companion unlocked capability: " .. flag
    if event.player_index then
      local p = game.get_player(event.player_index)
      if p then p.print(msg) end
    else
      print(msg)
    end
  end
end)

commands.add_command("companion-lock", "Lock a companion capability flag", function(event)
  local flag = event.parameter
  if flag and flag ~= "" then
    campaign.lock(flag)
    local msg = "Companion locked capability: " .. flag
    if event.player_index then
      local p = game.get_player(event.player_index)
      if p then p.print(msg) end
    else
      print(msg)
    end
  end
end)

util.log("control.lua loaded")
