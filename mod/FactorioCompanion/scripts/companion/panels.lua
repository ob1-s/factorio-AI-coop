local util = require("scripts.companion.util")

local panels = {}

storage = storage or {}
storage.tasks = storage.tasks or {}
storage.next_task_id = storage.next_task_id or 1

local STATUS_SPRITE = {
  pending = "utility/set_bar_slot",
  active = "utility/play",
  done = "utility/check_mark",
  failed = "utility/warning_icon"
}

local LOGO_SPRITE = "utility/clock"

local STATUS_ORDER = { active = 1, pending = 2, done = 3, failed = 4 }

function panels.valid_sprites()
  local all = {}
  for _, sprite in pairs(STATUS_SPRITE) do
    all[#all + 1] = sprite
  end
  all[#all + 1] = LOGO_SPRITE
  return all
end

function panels.logo_sprite()
  return LOGO_SPRITE
end

local function ensure_tasks_root(player)
  local root = player.gui.screen.companion_tasks
  if not (root and root.valid) then
    root = player.gui.screen.add({
      type = "frame",
      name = "companion_tasks",
      style = "companion_frame",
      direction = "vertical"
    })
    local header = root.add({ type = "flow", name = "header", direction = "horizontal" })
    header.style.horizontally_stretchable = true
    header.style.vertical_align = "center"
    header.add({ type = "label", name = "title", style = "frame_title", caption = {"companion.taskboard"} })
    local spacer = header.add({ type = "empty-widget", name = "spacer", style = "companion_header_spacer" })
    spacer.style.horizontally_stretchable = true
    header.add({
      type = "sprite-button",
      name = "companion_tasks_close",
      sprite = "utility/close",
      style = "frame_action_button"
    })
    header.drag_target = root
    root.add({ type = "scroll-pane", name = "body", direction = "vertical", style = "companion_scroll" })
  end
  return root
end

local function render_task_list(player)
  local root = ensure_tasks_root(player)
  local body = root.body
  body.clear()
  local tasks = storage.tasks[player.index] or {}
  local ordered = {}
  for _, t in ipairs(tasks) do
    ordered[#ordered + 1] = t
  end
  table.sort(ordered, function(a, b)
    local sa, sb = STATUS_ORDER[a.status] or 9, STATUS_ORDER[b.status] or 9
    if sa ~= sb then
      return sa < sb
    end
    return a.id < b.id
  end)
  for _, task in ipairs(ordered) do
    local row = body.add({
      type = "frame",
      style = "companion_task_row",
      direction = "horizontal"
    })
    row.style.horizontally_stretchable = true
    row.add({
      type = "sprite-button",
      name = "companion_task_toggle_" .. task.id,
      sprite = STATUS_SPRITE[task.status] or STATUS_SPRITE.pending,
      style = "companion_task_status_button"
    })
    local col = row.add({ type = "flow", direction = "vertical" })
    col.style.horizontally_stretchable = true
    local lbl = col.add({ type = "label", caption = task.text, style = "companion_task_text" })
    lbl.style.horizontally_stretchable = true
    if task.detail and task.detail ~= "" then
      local det = col.add({ type = "label", caption = task.detail, style = "companion_task_detail" })
      det.style.horizontally_stretchable = true
    end
  end
  if #ordered == 0 then
    body.add({ type = "label", caption = {"companion.no-tasks"}, style = "companion_task_detail" })
  end
  return root
end

function panels.render_tasks(player, spec)
  spec = spec or {}
  local root = render_task_list(player)
  if spec.title then
    root.header.title.caption = spec.title
  end
  if spec.visible ~= nil then
    root.visible = spec.visible
  end
end

function panels.set_tasks(player, tasks_spec)
  storage.tasks[player.index] = {}
  for _, t in ipairs(tasks_spec or {}) do
    panels.add_task(player, t)
  end
  render_task_list(player)
end

function panels.add_task(player, t)
  if not t or not t.text then
    return nil
  end
  local id = storage.next_task_id
  storage.next_task_id = id + 1
  storage.tasks[player.index] = storage.tasks[player.index] or {}
  storage.tasks[player.index][#storage.tasks[player.index] + 1] = {
    id = id,
    text = tostring(t.text),
    detail = t.detail and tostring(t.detail) or "",
    status = t.status or "pending"
  }
  return id
end

function panels.update_task(player, tid, fields)
  tid = tonumber(tid)
  for _, t in ipairs(storage.tasks[player.index] or {}) do
    if t.id == tid then
      if fields.status then
        t.status = fields.status
      end
      if fields.text then
        t.text = fields.text
      end
      if fields.detail then
        t.detail = fields.detail
      end
      render_task_list(player)
      return true
    end
  end
  return false
end

function panels.remove_task(player, tid)
  tid = tonumber(tid)
  local list = storage.tasks[player.index]
  if not list then
    return false
  end
  for i, t in ipairs(list) do
    if t.id == tid then
      table.remove(list, i)
      render_task_list(player)
      return true
    end
  end
  return false
end

function panels.get_tasks(player)
  return storage.tasks[player.index] or {}
end

local function ensure_info_root(player)
  local root = player.gui.screen.companion_info
  if not (root and root.valid) then
    root = player.gui.screen.add({
      type = "frame",
      name = "companion_info",
      style = "companion_frame",
      direction = "vertical"
    })
    local header = root.add({ type = "flow", name = "header", direction = "horizontal" })
    header.style.horizontally_stretchable = true
    header.style.vertical_align = "center"
    header.add({ type = "label", name = "title", style = "frame_title" })
    local spacer = header.add({ type = "empty-widget", name = "spacer", style = "companion_header_spacer" })
    spacer.style.horizontally_stretchable = true
    header.add({ type = "sprite-button", name = "companion_info_close", sprite = "utility/close", style = "frame_action_button" })
    header.drag_target = root
    root.add({ type = "scroll-pane", name = "body", style = "companion_scroll" })
  end
  return root
end

function panels.render_info(player, spec)
  spec = spec or {}
  local root = ensure_info_root(player)
  root.header.title.caption = spec.title or ""
  local body = root.body
  body.clear()
  if spec.body then
    local b = body.add({ type = "label", caption = spec.body, style = "companion_info_body" })
    b.style.horizontally_stretchable = true
  end
  if spec.rows then
    local tbl = body.add({ type = "table", column_count = 2, style = "companion_table" })
    tbl.style.horizontally_stretchable = true
    for _, row in ipairs(spec.rows) do
      tbl.add({ type = "label", caption = tostring(row[1]), style = "companion_table_key" })
      local v = tbl.add({ type = "label", caption = tostring(row[2]), style = "companion_table_value" })
      v.style.horizontally_stretchable = true
    end
  end
  if not root.visible then
    root.visible = true
  end
  return true
end

function panels.hide_info(player)
  local root = player.gui.screen.companion_info
  if root and root.valid then
    root.visible = false
  end
end

function panels.toggle_tasks(player)
  local root = player.gui.screen.companion_tasks
  if root and root.valid then
    root.visible = not root.visible
    return root.visible
  else
    render_task_list(player)
    return true
  end
end

function panels.handle_click(player, element_name)
  if element_name == "companion_tasks_close" then
    local root = player.gui.screen.companion_tasks
    if root then
      root.visible = false
    end
    return true
  elseif element_name == "companion_info_close" then
    panels.hide_info(player)
    return true
  elseif string.find(element_name, "^companion_task_toggle_", 1, false) then
    local tid = tonumber(string.match(element_name, "(%d+)$"))
    for _, t in ipairs(storage.tasks[player.index] or {}) do
      if t.id == tid then
        t.status = (t.status == "done") and "pending" or "done"
        render_task_list(player)
        return true
      end
    end
  end
  return false
end

return panels
