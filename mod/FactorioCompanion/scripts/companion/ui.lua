local util = require("scripts.companion.util")
local markers = require("scripts.companion.markers")
local panels = require("scripts.companion.panels")
local campaign = require("scripts.companion.campaign")

local ui = {}

local function require_capability(key)
  if campaign.has(key) then
    return true
  end
  return false, util.err("capability_locked:" .. key)
end

local function get_player()
  local p = game.get_player(1)
  if not p then
    for _, player in pairs(game.players) do
      return player
    end
  end
  return p
end

function ui.dispatch(spec)
  if type(spec) ~= "table" or not spec.kind then
    return util.err("spec must be an object with a 'kind' field")
  end
  local player = get_player()
  if not player then
    return util.err("no players connected")
  end

  local kind = spec.kind
  if kind == "markers.set" then
    local allowed, err = require_capability("markers")
    if not allowed then return err end
    markers.clear(player.index)
    for _, m in ipairs(spec.markers or {}) do
      markers.draw(m, player)
    end
    return util.ok({ cleared_then_drew = #(spec.markers or {}) })
  elseif kind == "marker.add" then
    local allowed, err = require_capability("markers")
    if not allowed then return err end
    local id, err = markers.draw(spec.marker or spec, player)
    if err then
      return util.err(err)
    end
    return util.ok({ marker_id = id })
  elseif kind == "markers.clear" then
    local allowed, err = require_capability("markers")
    if not allowed then return err end
    markers.clear(player.index)
    return util.ok({})
  elseif kind == "arrow.add" then
    local allowed, err = require_capability("markers")
    if not allowed then return err end
    local ids = markers.arrow(spec.from, spec.to, spec.color or "yellow", player, spec.expire_seconds)
    return util.ok({ count = #ids })
  elseif kind == "tasks.set" then
    local allowed, err = require_capability("tasks")
    if not allowed then return err end
    panels.set_tasks(player, spec.tasks or {})
    panels.render_tasks(player, spec)
    return util.ok({ count = #(spec.tasks or {}) })
  elseif kind == "tasks.render" then
    local allowed, err = require_capability("tasks")
    if not allowed then return err end
    panels.render_tasks(player, spec)
    return util.ok({})
  elseif kind == "task.add" then
    local allowed, err = require_capability("tasks")
    if not allowed then return err end
    local id = panels.add_task(player, spec.task or spec)
    if id then
      panels.render_tasks(player, {})
    end
    return id and util.ok({ task_id = id }) or util.err("missing text")
  elseif kind == "task.update" then
    local allowed, err = require_capability("tasks")
    if not allowed then return err end
    local done = panels.update_task(player, spec.id, spec)
    return done and util.ok({}) or util.err("no such task " .. tostring(spec.id))
  elseif kind == "task.remove" then
    local allowed, err = require_capability("tasks")
    if not allowed then return err end
    local done = panels.remove_task(player, spec.id)
    return done and util.ok({}) or util.err("no such task " .. tostring(spec.id))
  elseif kind == "tasks.get" then
    local allowed, err = require_capability("tasks")
    if not allowed then return err end
    return util.ok({ tasks = panels.get_tasks(player) })
  elseif kind == "info.show" then
    panels.render_info(player, spec.panel or spec)
    return util.ok({})
  elseif kind == "info.hide" then
    panels.hide_info(player)
    return util.ok({})
  elseif kind == "chat.system_notice" then
    local chat = require("scripts.companion.chat")
    chat.system_notice(player, tostring(spec.text or ""))
    return util.ok({})
  elseif kind == "camera.ping" then
    local allowed, err = require_capability("markers")
    if not allowed then return err end
    if spec.position then
      local pos = { x = tonumber(spec.position.x) or 0, y = tonumber(spec.position.y) or 0 }
      player.centered_on = { position = pos }
      markers.arrow(pos, { x = pos.x, y = pos.y - 3 }, "yellow", player, 10)
    end
    return util.ok({})
  else
    return util.err("unknown kind: " .. tostring(kind))
  end
end

return ui
