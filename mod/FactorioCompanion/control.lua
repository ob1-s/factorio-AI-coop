local remote_if = require("scripts.companion.remote")
local chat = require("scripts.companion.chat")
local panels = require("scripts.companion.panels")
local markers = require("scripts.companion.markers")
local util = require("scripts.companion.util")

local function ensure_storage()
  storage.chat_log = storage.chat_log or {}
  storage.outbox = storage.outbox or {}
  storage.next_msg_id = storage.next_msg_id or 1
  storage.queries = storage.queries or {}
  storage.next_query_id = storage.next_query_id or 1
  storage.markers = storage.markers or {}
  storage.next_marker_id = storage.next_marker_id or 1
  storage.tasks = storage.tasks or {}
  storage.next_task_id = storage.next_task_id or 1
  storage.panel_spec = storage.panel_spec or {}
  storage.dirty = storage.dirty or {}
  storage.agent_busy = storage.agent_busy or false
  storage.unread = storage.unread or 0
  storage.stream = storage.stream or nil
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
  util.log("initialized, version " .. remote_if.version)
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
  end
end)

script.on_event(defines.events.on_player_joined_game, function(event)
  local player = game.get_player(event.player_index)
  if player then
    init_player(player)
    chat.update_badge(player)
  end
end)

script.on_event(defines.events.on_player_removed, function(event)
  storage.chat_log[event.player_index] = nil
  storage.tasks[event.player_index] = nil
  storage.markers[event.player_index] = nil
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

util.log("control.lua loaded")
