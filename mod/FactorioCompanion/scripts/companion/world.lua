local util = require("scripts.companion.util")

local world = {}

local function surface_by_name(name)
  if name == nil or name == "" then
    return game.surfaces.nauvis or game.surfaces[1]
  end
  return game.surfaces[name]
end

local function safe_force()
  return game.forces.player
end

function world.player_state(player)
  player = player or game.get_player(1)
  if not player then
    return nil
  end
  local pos = player.position
  local vehicle = player.vehicle and player.vehicle.name or nil
  local state = {
    name = player.name,
    position = { x = util.round(pos.x, 1), y = util.round(pos.y, 1) },
    surface = player.surface.name,
    health = player.health,
    max_health = player.prototype and nil or nil,
    walking = player.walking_state.walking,
    mining = player.mining_state.mining,
    in_combat = false,
    character_god_mode = player.character == nil,
    death_ticks_left = nil,
    vehicle = vehicle,
    craft_queue_count = #player.crafting_queue
  }
  local char = player.character
  if char then
    state.max_health = char.prototype.max_health
  end
  local alerts = {}
  local raw = player.get_alerts{}
  for _, by_type in pairs(raw) do
    for alert_type, list in pairs(by_type) do
      for _, entry in pairs(list) do
        local a = { type = tostring(alert_type) }
        local ent = entry.entity or entry.target
        if ent and ent.valid then
          a.name = ent.name
          a.position = { x = util.round(ent.position.x, 0), y = util.round(ent.position.y, 0) }
        end
        alerts[#alerts + 1] = a
      end
    end
  end
  state.alerts = alerts
  return state
end

function world.inventory(player)
  player = player or game.get_player(1)
  local function summarize(inv, limit)
    if not inv or not inv.valid then
      return {}
    end
    local counts = inv.get_contents()
    local out = {}
    for name, count in pairs(counts) do
      out[#out + 1] = { item = name, count = count }
    end
    table.sort(out, function(a, b) return a.count > b.count end)
    limit = limit or 40
    while #out > limit do
      table.remove(out)
    end
    return out
  end
  return {
    main = summarize(player.get_inventory(defines.inventory.character_main), 50),
    trash = summarize(player.get_inventory(defines.inventory.character_trash), 20),
    armor_modules = summarize(player.get_inventory(defines.inventory.character_armor), 10),
    gun_ammo = summarize(player.get_inventory(defines.inventory.character_gun_ammo), 10)
  }
end

function world.overview()
  local force = safe_force()
  local surface = surface_by_name(nil)
  local player = game.get_player(1)
  local o = {
    tick = game.tick,
    players_online = #game.connected_players,
    surfaces = {},
    evolution = force.get_evolution_factor(surface),
    pollution_on_player_chunk = nil,
    research = world.research(),
    player = world.player_state(player),
    production_top = world.production_top(force, surface, 12),
    power = world.power(surface, force),
    rocket = nil,
    time_played_min = util.round(game.tick / 60 / 60, 0)
  }
  if force.get_rocket_launched and force.get_item_launched("space-science-pack") then
    o.rocket = "space science flowing"
  elseif game.surfaces.nauvis and surface.find_entity("rocket-silo", { x = 0, y = 0 }) then
  end
  local silos = surface.find_entities_filtered({ type = "rocket-silo", limit = 5 })
  o.rockets = {}
  for _, silo in ipairs(silos) do
    if util.can_see(force, surface, silo.position) then
      o.rockets[#o.rockets + 1] = {
        position = { x = util.round(silo.position.x, 0), y = util.round(silo.position.y, 0) },
        launched = silo.launch_ended ~= nil and nil or (silo.rocket_parts or 0),
        parts_needed = silo.rocket_parts_required,
        status = tostring(silo.status)
      }
    end
  end
  for name, s in pairs(game.surfaces) do
    o.surfaces[#o.surfaces + 1] = name
  end
  if player then
    o.pollution_on_player_chunk = util.round(surface.get_pollution(player.position), 0)
  end
  o.charted_chunks = force.get_charted_forces and nil or nil
  return o
end

function world.research()
  local force = safe_force()
  local techs = {}
  local queue = force.research_queue
  local q = {}
  for _, item in ipairs(queue or {}) do
    q[#q + 1] = { tech = item.technology.name, infinite_level = item.infinite_level or nil }
  end
  local current = force.current_research
  local progress = nil
  if current then
    progress = util.round(force.research_progress, 3)
  end
  return {
    queue = q,
    current = current and current.name or nil,
    progress = progress,
    unlocked_count = force.technologies and (function()
      local n = 0
      for _, t in pairs(force.technologies) do
        if t.researched then
          n = n + 1
        end
      end
      return n
    end)() or nil
  }
end

function world.production_top(force, surface, top_n)
  top_n = top_n or 15
  local stats = force.get_item_production_statistics(surface)
  local items = {}
  for name, count in pairs(stats.input_counts) do
    items[#items + 1] = { item = name, produced_last_interval = stats.get_flow_count(name, defines.flow_precision_index.five_minutes, defines.flow_direction.input), consumed_last_interval = stats.get_flow_count(name, defines.flow_precision_index.five_minutes, defines.flow_direction.output), stock_delta = count }
  end
  table.sort(items, function(a, b) return math.abs(a.produced_last_interval) > math.abs(b.produced_last_interval) end)
  local out = {}
  for i = 1, math.min(top_n, #items) do
    out[i] = items[i]
  end
  return out
end

function world.power(surface, force)
  local poles = surface.find_entities_filtered({ type = "electric-pole", force = force, limit = 400 })
  local worst = nil
  local checked = 0
  local seen_networks = {}
  for _, pole in ipairs(poles) do
    if pole.valid and pole.electric_network_id and not seen_networks[pole.electric_network_id] then
      seen_networks[pole.electric_network_id] = true
      local stats = pole.electric_network_statistics
      if stats then
        local gen, drain = 0, 0
        for name, count in pairs(stats.output_counts) do
          gen = gen + stats.get_flow_count(name, defines.flow_precision_index.ten_seconds, defines.flow_direction.input)
        end
        for name, count in pairs(stats.input_counts) do
          drain = drain + stats.get_flow_count(name, defines.flow_precision_index.ten_seconds, defines.flow_direction.input)
        end
        checked = checked + 1
        local ratio = drain > 0 and gen / drain or 1
        if not worst or ratio < worst.ratio then
          worst = { ratio = util.round(ratio, 3), generation_per_s = util.round(gen, 1), consumption_per_s = util.round(drain, 1), sample_pole = { x = util.round(pole.position.x, 0), y = util.round(pole.position.y, 0) } }
        end
      end
    end
    if checked >= 6 then
      break
    end
  end
  return worst
end

function world.area(surface_name, x, y, radius)
  radius = math.min(radius or 32, 128)
  local surface = surface_by_name(surface_name)
  if not surface then
    return util.err("no such surface: " .. tostring(surface_name))
  end
  local force = safe_force()
  if not util.can_see(force, surface, { x = x, y = y }) then
    return util.ok({ area = { x = x, y = y, radius = radius }, visible = false, note = "fog of war: this chunk has not been charted; no information available" })
  end
  local found = surface.find_entities_filtered({ area = { { x - radius, y - radius }, { x + radius, y + radius } }, limit = 2000 })
  local groups = {}
  local total = 0
  local hidden = 0
  for _, ent in ipairs(found) do
    if ent.valid and ent.name ~= "character" then
      if util.can_see(force, surface, ent.position) then
        local key = ent.name
        local g = groups[key]
        if not g then
          g = { entity = key, type = ent.type, count = 0 }
          groups[key] = g
          total = total + 1
        end
        g.count = g.count + 1
      else
        hidden = hidden + 1
      end
    end
  end
  local list = {}
  for _, g in pairs(groups) do
    list[#list + 1] = g
  end
  table.sort(list, function(a, b) return a.count > b.count end)
  return util.ok({
    area = { x = x, y = y, radius = radius },
    surface = surface.name,
    distinct = total,
    entities = list,
    uncharted_entities_nearby = hidden
  })
end

function world.find(spec)
  spec = spec or {}
  local surface = surface_by_name(spec.surface)
  if not surface then
    return util.err("no such surface")
  end
  local force = safe_force()
  local origin = spec.near or (game.get_player(1) and game.get_player(1).position) or { x = 0, y = 0 }
  local radius = math.min(spec.radius or 512, 2048)
  local matches = surface.find_entities_filtered({
    name = spec.name,
    type = spec.type,
    area = { { origin.x - radius, origin.y - radius }, { origin.x + radius, origin.y + radius } },
    limit = 500
  })
  local results = {}
  for _, ent in ipairs(matches) do
    if ent.valid and util.can_see(force, surface, ent.position) then
      local d = math.sqrt((ent.position.x - origin.x) ^ 2 + (ent.position.y - origin.y) ^ 2)
      results[#results + 1] = {
        name = ent.name,
        position = { x = util.round(ent.position.x, 1), y = util.round(ent.position.y, 1) },
        distance = util.round(d, 1),
        status = ent.status and tostring(ent.status) or nil
      }
    end
  end
  table.sort(results, function(a, b) return a.distance < b.distance end)
  while #results > 25 do
    table.remove(results)
  end
  return util.ok({ query = { name = spec.name, near = origin, radius = radius }, matches = results, visible_only = true })
end

function world.entity_at(surface_name, x, y)
  local surface = surface_by_name(surface_name)
  if not surface then
    return util.err("no such surface")
  end
  local force = safe_force()
  if not util.can_see(force, surface, { x = x, y = y }) then
    return util.ok({ visible = false, note = "fog of war" })
  end
  local ents = surface.find_entities({ { x - 0.6, y - 0.6 }, { x + 0.6, y + 0.6 } })
  local out = {}
  for _, ent in ipairs(ents) do
    if ent.valid then
      local e = {
        name = ent.name,
        type = ent.type,
        position = { x = util.round(ent.position.x, 2), y = util.round(ent.position.y, 2) },
        unit_number = ent.unit_number,
        force = ent.force.name
      }
      if ent.recipe then
        e.recipe = ent.recipe.name
      end
      if ent.crafting_progress then
        e.crafting_progress = util.round(ent.crafting_progress, 2)
      end
      if ent.status then
        e.status = tostring(ent.status)
      end
      if ent.energy then
        e.energy = util.round(ent.energy, 0)
      end
      local inv = ent.get_inventory and ent.get_inventory(defines.inventory.chest) or nil
      if inv and inv.valid then
        local c = inv.get_contents()
        local lines = {}
        for n, cnt in pairs(c) do
          lines[#lines + 1] = n .. ":" .. cnt
        end
        table.sort(lines)
        e.contents = lines
      end
      out[#out + 1] = e
    end
  end
  return util.ok({ at = { x = x, y = y }, entities = out })
end

function world.charted_bounds(surface_name)
  local surface = surface_by_name(surface_name)
  if not surface then
    return util.err("bad surface")
  end
  local force = safe_force()
  local minx, miny, maxx, maxy = math.huge, math.huge, -math.huge, -math.huge
  local cx, cy = util.pos_to_chunk(game.get_player(1).position).x, util.pos_to_chunk(game.get_player(1).position).y
  for dx = -64, 64, 4 do
    for dy = -64, 64, 4 do
      local cp = { x = cx + dx, y = cy + dy }
      if force.is_chunk_charted(surface, cp) then
        local tx, ty = cp.x * 32, cp.y * 32
        if tx < minx then minx = tx end
        if ty < miny then miny = ty end
        if tx > maxx then maxx = tx end
        if ty > maxy then maxy = ty end
      end
    end
  end
  if minx == math.huge then
    return util.ok({ charted = false })
  end
  return util.ok({ charted = true, approx_bounds = { min_x = minx, min_y = miny, max_x = maxx, max_y = maxy }, sampled_radius_chunks = 256 })
end

return world
