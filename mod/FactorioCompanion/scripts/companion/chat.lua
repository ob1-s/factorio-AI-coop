local util = require("scripts.companion.util")
local panels = require("scripts.companion.panels")

local chat = {}

storage = storage or {}
storage.chat_log = storage.chat_log or {}
storage.outbox = storage.outbox or {}
storage.next_msg_id = storage.next_msg_id or 1
storage.agent_busy = false
storage.unread = 0
storage.stream = nil
storage.dirty = {}

local MAX_MESSAGE_LENGTH = 2000

local function player_index(player)
  return player.index
end

function chat.push_history(entry, player)
  local limit = settings.get_player_settings(player)["companion-chat-history-limit"].value or 200
  local log = storage.chat_log[player_index(player)]
  log[#log + 1] = entry
  while #log > limit do
    table.remove(log, 1)
  end
end

function chat.enqueue_user_message(player, text)
  local id = storage.next_msg_id
  storage.next_msg_id = id + 1
  local entry = {
    id = id,
    kind = "user",
    from = player.name,
    text = string.sub(text, 1, MAX_MESSAGE_LENGTH),
    tick = game.tick,
    surface = player.surface.name,
    position = { x = math.floor(player.position.x), y = math.floor(player.position.y) }
  }
  chat.push_history(entry, player)
  storage.outbox[#storage.outbox + 1] = entry
  return id
end

local function fmt_time(tick)
  local total_min = math.floor(tick / 60 / 60)
  return string.format("%02d:%02d", math.floor(total_min / 60) % 100, total_min % 60)
end

local STYLES_BY_KIND = {
  user = "companion_chat_user",
  agent = "companion_chat_agent",
  system = "companion_chat_system"
}

local function render_log(player)
  local root = player.gui.screen.companion_chat
  if not (root and root.valid) then
    return
  end
  local body = root.body.log_flow
  body.clear()
  for _, entry in ipairs(storage.chat_log[player_index(player)]) do
    local row = body.add({ type = "flow", direction = "vertical" })
    row.style.horizontally_stretchable = true
    local meta_style = entry.kind == "user" and "companion_chat_meta_user" or "companion_chat_meta"
    if entry.kind ~= "system" then
      local meta = row.add({
        type = "label",
        caption = entry.from .. "  ·  " .. fmt_time(entry.tick),
        style = meta_style
      })
      meta.style.horizontally_stretchable = true
    end
    local msg = row.add({
      type = "frame",
      style = STYLES_BY_KIND[entry.kind] or STYLES_BY_KIND.system,
      direction = "horizontal"
    })
    msg.style.horizontally_stretchable = true
    local lbl = msg.add({ type = "label", caption = entry.text, style = "companion_chat_text" })
    lbl.style.horizontally_stretchable = true
  end
  if storage.stream then
    local row = body.add({ type = "flow", direction = "vertical" })
    row.style.horizontally_stretchable = true
    row.add({ type = "label", caption = "companion  ·  typing", style = "companion_chat_meta" })
    local frame = row.add({ type = "frame", style = STYLES_BY_KIND.agent, direction = "horizontal" })
    frame.style.horizontally_stretchable = true
    local lbl = frame.add({ type = "label", caption = storage.stream.text .. " ▌", style = "companion_chat_text" })
    lbl.style.horizontally_stretchable = true
  end
  body.scroll_to_bottom()
  storage.dirty[player_index(player)] = nil
end

function chat.ensure_root(player)
  local gui = player.gui.screen
  if gui.companion_chat and gui.companion_chat.valid then
    return gui.companion_chat
  end

  local root = gui.add({
    type = "frame",
    name = "companion_chat",
    style = "companion_frame",
    direction = "vertical"
  })

  local header = root.add({ type = "flow", name = "header", direction = "horizontal" })
  header.style.horizontally_stretchable = true
  header.style.vertical_align = "center"

  header.add({
    type = "sprite-button",
    name = "companion_chat_logo",
    sprite = panels.logo_sprite(),
    style = "companion_logo_button"
  })
  header.add({ type = "label", name = "title", caption = {"companion.title"}, style = "frame_title" })
  header.add({ type = "label", name = "status", caption = "", style = "companion_status_label" })
  local spacer = header.add({ type = "empty-widget", name = "spacer", style = "companion_header_spacer" })
  spacer.style.horizontally_stretchable = true
  header.add({
    type = "sprite-button",
    name = "companion_chat_tasks",
    sprite = "utility/set_bar_slot",
    tooltip = {"companion.toggle-tasks-tooltip"},
    style = "frame_action_button"
  })
  header.add({
    type = "sprite-button",
    name = "companion_chat_close",
    sprite = "utility/close",
    style = "frame_action_button"
  })

  local body = root.add({ type = "scroll-pane", name = "body", style = "companion_chat_scroll" })
  body.vertical_scroll_policy = "auto"
  body.horizontal_scroll_policy = "never"
  body.add({ type = "flow", name = "log_flow", direction = "vertical" })
  local log_flow = body.log_flow
  log_flow.style.horizontally_stretchable = true
  log_flow.style.vertically_stretchable = true

  local input_row = root.add({ type = "flow", name = "input_row", direction = "horizontal" })
  input_row.style.horizontally_stretchable = true
  input_row.style.vertical_align = "center"
  local field = input_row.add({
    type = "textfield",
    name = "input",
    style = "companion_input"
  })
  field.style.horizontally_stretchable = true
  field.word_wrap = false
  input_row.add({
    type = "sprite-button",
    name = "send",
    sprite = "utility/export",
    style = "companion_send_button",
    tooltip = {"companion.send-tooltip"}
  })

  root.style.width = 440
  body.style.height = 380

  root.location = { x = 60, y = 120 }
  return root
end

function chat.open(player)
  local root = chat.ensure_root(player)
  root.visible = true
  root.input.focus()
  render_log(player)
  storage.unread = 0
  chat.update_badge(player)
end

function chat.close(player)
  local root = player.gui.screen.companion_chat
  if root then
    root.visible = false
  end
end

function chat.is_open(player)
  local root = player.gui.screen.companion_chat
  return root and root.valid and root.visible
end

function chat.toggle(player)
  if chat.is_open(player) then
    chat.close(player)
  else
    chat.open(player)
  end
end

function chat.update_badge(player)
  local root = player.gui.screen.companion_chat_pill
  if not (root and root.valid) then
    root = player.gui.screen.add({
      type = "sprite-button",
      name = "companion_chat_pill",
      sprite = "utility/playing_time",
      mouse_button_filter = { "left" },
      style = "companion_pill_button",
      tooltip = {"companion.open-tooltip"}
    })
    root.ignored_by_interaction = false
  end
  local anchor = {
    gui = defines.relative_gui_type.controller_gui,
    position = defines.relative_gui_position.right,
    names = {
      [defines.gui_type.item] = {},
      [defines.gui_type.entity] = {}
    }
  }
  root.anchor = anchor
  root.caption = storage.unread > 0 and tostring(storage.unread) or ""
  root.visible = not chat.is_open(player)
end

function chat.set_status(player, status_text)
  local root = chat.ensure_root(player)
  root.header.status.caption = status_text or ""
end

function chat.deliver_agent_message(player, text)
  local id = storage.next_msg_id
  storage.next_msg_id = id + 1
  local entry = { id = id, kind = "agent", from = "companion", text = text, tick = game.tick }
  chat.push_history(entry, player)
  if storage.stream then
    storage.stream = nil
  end
  if chat.is_open(player) then
    render_log(player)
  else
    storage.unread = storage.unread + 1
    if settings.get_player_settings(player)["companion-notifications"].value then
      player.print({"companion.notify-new-message"})
    end
  end
  chat.update_badge(player)
  return id
end

function chat.stream_start(player)
  storage.stream = { text = "" }
  if chat.is_open(player) then
    render_log(player)
  end
  chat.set_status(player, {"companion.status-thinking"})
end

function chat.stream_append(player, chunk)
  if not storage.stream then
    storage.stream = { text = "" }
  end
  storage.stream.text = storage.stream.text .. chunk
  if #storage.stream.text > 8000 then
    storage.stream.text = storage.stream.text:sub(-8000)
  end
  if chat.is_open(player) then
    render_log(player)
  end
end

function chat.stream_end(player, final_text)
  if final_text and final_text ~= "" then
    chat.deliver_agent_message(player, final_text)
  else
    if storage.stream then
      local t = storage.stream.text
      storage.stream = nil
      if t ~= "" then
        chat.deliver_agent_message(player, t)
      end
    end
  end
  chat.set_status(player, "")
end

function chat.drain_outbox(limit)
  limit = tonumber(limit) or 20
  local out = {}
  while #out < limit and #storage.outbox > 0 do
    out[#out + 1] = table.remove(storage.outbox, 1)
  end
  return out
end

function chat.system_notice(player, text)
  local id = storage.next_msg_id
  storage.next_msg_id = id + 1
  chat.push_history({ id = id, kind = "system", from = "", text = text, tick = game.tick }, player)
  if chat.is_open(player) then
    render_log(player)
  else
    player.print(text)
  end
end

function chat.on_gui_click(player, element_name)
  if element_name == "send" then
    local root = chat.ensure_root(player)
    local text = root.input.text
    if text and text ~= "" then
      root.input.text = ""
      chat.enqueue_user_message(player, text)
      chat.set_status(player, {"companion.status-thinking"})
    end
    return true
  elseif element_name == "companion_chat_close" then
    chat.close(player)
    chat.update_badge(player)
    return true
  elseif element_name == "companion_chat_tasks" then
    panels.toggle_tasks(player)
    return true
  elseif element_name == "companion_chat_pill" then
    chat.open(player)
    return true
  elseif element_name == "companion_chat_logo" then
    chat.system_notice(player, {"companion.hint"})
    return true
  end
  return false
end

function chat.on_gui_confirmed(player, element_name)
  if element_name == "input" then
    local root = player.gui.screen.companion_chat
    if root then
      local text = root.input.text
      if text and text ~= "" then
        root.input.text = ""
        chat.enqueue_user_message(player, text)
        chat.set_status(player, {"companion.status-thinking"})
      end
      root.input.focus()
    end
    return true
  end
  return false
end

function chat.render_if_dirty(player)
  if storage.dirty[player_index(player)] and chat.is_open(player) then
    render_log(player)
  end
end

function chat.mark_dirty()
  for _, p in ipairs(game.connected_players) do
    storage.dirty[p.index] = true
  end
end

return chat
