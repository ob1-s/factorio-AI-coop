local util = require("scripts.companion.util")

local markers = {}

storage = storage or {}
storage.markers = storage.markers or {}
storage.next_marker_id = storage.next_marker_id or 1

local COLORS = {
  red = { r = 1, g = 0.2, b = 0.2 },
  green = { r = 0.3, g = 1, b = 0.3 },
  blue = { r = 0.3, g = 0.6, b = 1 },
  yellow = { r = 1, g = 0.9, b = 0.2 },
  orange = { r = 1, g = 0.55, b = 0.15 },
  purple = { r = 0.7, g = 0.4, b = 1 },
  white = { r = 1, g = 1, b = 1 }
}

function markers.color(name)
  if type(name) == "table" then
    return { r = name.r or name[1] or 1, g = name.g or name[2] or 1, b = name.b or name[3] or 1, a = 1 }
  end
  return COLORS[name] or COLORS.red
end

function markers.clear(player_index)
  local m = storage.markers[player_index]
  if m then
    rendering.clear(util.MOD_NAME, game.get_player(player_index).surface)
    storage.markers[player_index] = nil
    return true
  end
end

function markers.remove_expired(player_index)
  local list = storage.markers[player_index]
  if not list then
    return
  end
  local alive = {}
  local dirty = false
  for _, entry in ipairs(list) do
    if entry.expires and game.tick > entry.expires then
      if rendering.is_valid(entry.render_ids[1]) then
        for _, rid in ipairs(entry.render_ids) do
          rendering.destroy(rid)
        end
      end
      dirty = true
    else
      alive[#alive + 1] = entry
    end
  end
  storage.markers[player_index] = alive
end

function markers.draw(spec, player)
  local surface = spec.surface and game.surfaces[spec.surface] or player.surface
  if not surface then
    return nil, "bad surface"
  end
  local pos = spec.position
  if not pos or not pos.x or not pos.y then
    return nil, "missing position"
  end
  local color = markers.color(spec.color)
  local ttl_ticks = spec.expire_seconds and (spec.expire_seconds * 60) or (10 * 60 * 60)
  local ids = {}
  local radius = spec.radius or 1.5

  ids[#ids + 1] = rendering.draw_circle({
    color = color,
    width = 2,
    radius = radius,
    target = pos,
    surface = surface,
    time_to_live = ttl_ticks,
    players = { player.index },
    draw_on_ground = false,
    only_in_alt_mode = false,
    visible = true
  })

  ids[#ids + 1] = rendering.draw_text({
    text = spec.label or "",
    target = { x = pos.x, y = pos.y - radius - 0.8 },
    surface = surface,
    color = color,
    scale = 0.9,
    alignment = "center",
    players = { player.index },
    time_to_live = ttl_ticks,
    visible_in_map_view = spec.on_map ~= false,
    visible = true
  })

  if spec.icon then
    ids[#ids + 1] = rendering.draw_sprite({
      sprite = spec.icon,
      target = pos,
      surface = surface,
      x_scale = 0.6,
      y_scale = 0.6,
      players = { player.index },
      time_to_live = ttl_ticks,
      visible_in_map_view = spec.on_map ~= false,
      visible = true
    })
  end

  local id = storage.next_marker_id
  storage.next_marker_id = id + 1
  storage.markers[player.index] = storage.markers[player.index] or {}
  table.insert(storage.markers[player.index], {
    id = id,
    label = spec.label,
    render_ids = ids,
    expires = ttl_ticks < 3600000 and (game.tick + ttl_ticks) or nil,
    position = pos
  })

  return id
end

function markers.arrow(from_pos, to_pos, color_name, player, seconds)
  local surface = player.surface
  local ids = {}
  local ttl = (seconds or 20) * 60
  ids[#ids + 1] = rendering.draw_line({
    from = from_pos,
    to = to_pos,
    color = markers.color(color_name),
    width = 4,
    surface = surface,
    time_to_live = ttl,
    players = { player.index },
    dash_length = 0.8,
    gap_length = 0.5,
    visible = true,
    visible_in_map_view = true
  })
  return ids
end

function markers.list(player)
  markers.remove_expired(player.index)
  local out = {}
  for _, entry in ipairs(storage.markers[player.index] or {}) do
    out[#out + 1] = { id = entry.id, label = entry.label, position = entry.position }
  end
  return out
end

function markers.remove_by_id(player, mid)
  local list = storage.markers[player.index]
  if not list then
    return false
  end
  for i, entry in ipairs(list) do
    if entry.id == tonumber(mid) then
      for _, rid in ipairs(entry.render_ids) do
        if rendering.is_valid(rid) then
          rendering.destroy(rid)
        end
      end
      table.remove(list, i)
      return true
    end
  end
  return false
end

return markers
