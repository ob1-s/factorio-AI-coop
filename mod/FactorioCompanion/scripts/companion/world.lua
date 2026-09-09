local util = require("scripts.companion.util")
local campaign = require("scripts.companion.campaign")

local world = {}

local MAX_VISIBLE_ENTITIES = 25
local MAX_VISIBLE_ALERTS = 25

local function get_player(player)
  if player then
    return player
  end
  if game and game.get_player then
    return game.get_player(1)
  end
  return nil
end

local function safe_force()
  return game and game.forces and game.forces.player or nil
end

local function surface_by_name(name, player)
  player = get_player(player)
  if name == nil or name == "" then
    return (player and player.surface) or (game.surfaces.nauvis or game.surfaces[1])
  end
  if type(name) ~= "string" then
    return nil
  end
  return game.surfaces[name]
end

local function same_surface(left, right)
  if not left or not right then
    return false
  end
  if left.index and right.index then
    return left.index == right.index
  end
  return left.name == right.name
end

local function numeric(value)
  return tonumber(value)
end

local function entity_position(entity)
  local ok, position = pcall(function() return entity.position end)
  if not ok or not position or type(position.x) ~= "number" or type(position.y) ~= "number" then
    return nil
  end
  return position
end

local function entity_is_valid(entity)
  local ok, valid = pcall(function()
    return entity and entity.valid ~= false
  end)
  return ok and valid == true
end

-- This is the only predicate used for current entity perception.  is_chunk_visible
-- currently implies charted in Factorio, but checking both states explicitly keeps
-- the boundary obvious and prevents a future/API/mock regression from widening it.
local function entity_is_currently_visible(entity, force, surface)
  if not force or not surface or not entity_is_valid(entity) then
    return false
  end

  local entity_surface
  local ok_surface, value = pcall(function() return entity.surface end)
  if ok_surface then
    entity_surface = value
  end
  if entity_surface and not same_surface(entity_surface, surface) then
    return false
  end

  local position = entity_position(entity)
  if not position then
    return false
  end

  local visibility_surface = entity_surface or surface
  local ok_charted, charted = pcall(function()
    return util.is_charted(force, visibility_surface, position)
  end)
  if not ok_charted or charted ~= true then
    return false
  end
  return util.is_visible(force, visibility_surface, position)
end

local function entity_snapshot(entity, force, surface)
  if not entity_is_currently_visible(entity, force, surface) then
    return nil
  end
  local position = entity_position(entity)
  local ok_name, name = pcall(function() return entity.name end)
  local ok_type, entity_type = pcall(function() return entity.type end)
  if not ok_name or not name or name == "character" then
    return nil
  end

  -- Keep this deliberately small.  In particular, do not serialize unit numbers,
  -- forces, inventories, energy, recipes, status, or other engine/debug state.
  return {
    name = tostring(name),
    type = ok_type and entity_type and tostring(entity_type) or nil,
    x = util.round(position.x, 0),
    y = util.round(position.y, 0)
  }
end

-- Public only as a narrow testable seam; callers still need to opt into the
-- radar capability before using the resulting data.
function world.filter_visible_entities(candidates, force, surface, limit)
  limit = math.max(0, math.min(tonumber(limit) or MAX_VISIBLE_ENTITIES, 2000))
  local visible = {}
  for _, entity in ipairs(candidates or {}) do
    if #visible >= limit then
      break
    end
    local snapshot = entity_snapshot(entity, force, surface)
    if snapshot then
      visible[#visible + 1] = snapshot
    end
  end
  table.sort(visible, function(a, b)
    if a.name ~= b.name then
      return a.name < b.name
    end
    if a.x ~= b.x then
      return a.x < b.x
    end
    return a.y < b.y
  end)
  return visible
end

local function visible_alerts(player, force, surface)
  local alerts = {}
  local ok_raw, raw_alerts = pcall(function() return player.get_alerts{} end)
  if not ok_raw or type(raw_alerts) ~= "table" then
    return alerts
  end

  local function append(alert_type, entry)
    if #alerts >= MAX_VISIBLE_ALERTS or type(entry) ~= "table" then
      return
    end
    local entity = entry.entity or entry.target
    local snapshot = entity and entity_snapshot(entity, force, surface) or nil
    if snapshot then
      alerts[#alerts + 1] = {
        type = tostring(alert_type),
        name = snapshot.name,
        x = snapshot.x,
        y = snapshot.y
      }
    end
  end

  -- Factorio's alert table is grouped by alert source and then alert type.  Be
  -- defensive about shape changes, but never accept an alert with only a raw
  -- position: it has no visibility-verifiable entity target.
  for outer_type, by_type in pairs(raw_alerts) do
    if type(by_type) == "table" then
      if by_type.entity or by_type.target then
        append(outer_type, by_type)
      else
        for alert_type, entries in pairs(by_type) do
          if type(entries) == "table" then
            for _, entry in pairs(entries) do
              append(alert_type, entry)
              if #alerts >= MAX_VISIBLE_ALERTS then
                break
              end
            end
          end
          if #alerts >= MAX_VISIBLE_ALERTS then
            break
          end
        end
      end
    end
    if #alerts >= MAX_VISIBLE_ALERTS then
      break
    end
  end

  table.sort(alerts, function(a, b)
    if a.type ~= b.type then
      return a.type < b.type
    end
    if a.x ~= b.x then
      return a.x < b.x
    end
    return a.y < b.y
  end)
  return alerts
end

function world.visible_perception(player)
  if not campaign.has("radar_vision") then
    return nil
  end
  player = get_player(player)
  local force = safe_force()
  local surface = player and player.surface
  if not player or not force or not surface then
    return nil
  end

  local position = player.position
  local radius = 48
  local candidates = {}
  pcall(function()
    candidates = surface.find_entities_filtered({
      area = { { position.x - radius, position.y - radius }, { position.x + radius, position.y + radius } },
      limit = 100
    })
  end)

  return {
    nearby_entities = world.filter_visible_entities(candidates, force, surface, MAX_VISIBLE_ENTITIES),
    alerts = visible_alerts(player, force, surface)
  }
end

function world.player_state(player)
  if not campaign.has("telemetry") then
    return nil
  end
  player = get_player(player)
  if not player then
    return nil
  end

  local position = player.position
  local character = player.character
  local state = {
    name = player.name,
    position = { x = util.round(position.x, 1), y = util.round(position.y, 1) },
    surface = player.surface.name,
    health = character and util.round(character.health, 1) or nil,
    health_ratio = character and util.round(character.get_health_ratio(), 2) or nil,
    max_health = (character and (function()
      local ok, max_health = pcall(function() return character.prototype.max_health end)
      return ok and max_health or nil
    end)()) or nil,
    walking = player.walking_state.walking,
    mining = player.mining_state.mining,
    vehicle = player.vehicle and player.vehicle.name or nil,
    craft_queue_count = (function()
      local ok, queue = pcall(function() return player.crafting_queue end)
      return ok and queue and #queue or 0
    end)()
  }

  -- Alerts are dynamic perception, not generic telemetry.  They are exposed only
  -- through visible_perception when radar_vision is unlocked.
  return state
end

function world.inventory(player)
  if not campaign.has("inventory") then
    return nil
  end
  player = get_player(player)
  if not player then
    return nil
  end

  local function summarize(inventory_id, limit)
    local ok_inventory, inventory = pcall(function()
      return player.get_inventory(inventory_id)
    end)
    if not ok_inventory or not inventory or inventory.valid == false then
      return {}
    end
    local ok_contents, contents = pcall(function() return inventory.get_contents() end)
    if not ok_contents or type(contents) ~= "table" then
      return {}
    end
    local out = {}
    for key, stack in pairs(contents) do
      -- Factorio 2.0 with Quality returns an array of
      -- {name, quality, count}; older saves/API variants may return a
      -- name -> count dictionary.  Normalize both without ever retaining
      -- the engine's raw stack table in model context.
      local name, quality, count
      if type(stack) == "table" then
        name = stack.name or key
        quality = stack.quality
        count = tonumber(stack.count)
      else
        name = key
        count = tonumber(stack)
      end
      if name and count then
        local entry = { item = tostring(name), count = count }
        if quality then
          entry.quality = tostring(quality)
        end
        out[#out + 1] = entry
      end
    end
    table.sort(out, function(a, b)
      if a.count ~= b.count then
        return a.count > b.count
      end
      return a.item < b.item
    end)
    while #out > limit do
      table.remove(out)
    end
    return out
  end

  return {
    main = summarize(defines.inventory.character_main, 50),
    trash = summarize(defines.inventory.character_trash, 20),
    armor_modules = summarize(defines.inventory.character_armor, 10),
    gun_ammo = summarize(defines.inventory.character_gun_ammo, 10)
  }
end

function world.research()
  if not campaign.has("research") then
    return nil
  end
  local force = safe_force()
  if not force then
    return nil
  end

  local queue = {}
  local ok_queue, research_queue = pcall(function() return force.research_queue end)
  if ok_queue then
    for _, technology in ipairs(research_queue or {}) do
      -- Factorio 2.0 exposes research_queue as technology IDs (strings).
      -- Keep the table fallback for older/proxied API shapes used by tests.
      local name
      if type(technology) == "string" then
        name = technology
      elseif type(technology) == "table" then
        local ok_name, value = pcall(function()
          return technology.name or (technology.technology and technology.technology.name)
        end)
        if ok_name then
          name = value
        end
      end
      if name then
        queue[#queue + 1] = { tech = tostring(name) }
      end
    end
  end

  local current
  local ok_current, current_research = pcall(function() return force.current_research end)
  if ok_current and current_research then
    local ok_name, name = pcall(function() return current_research.name end)
    current = ok_name and name or nil
  end

  local progress
  if current then
    local ok_progress, value = pcall(function() return force.research_progress end)
    if ok_progress and type(value) == "number" then
      progress = util.round(value, 3)
    end
  end

  local unlocked_count
  local ok_technologies, technologies = pcall(function() return force.technologies end)
  if ok_technologies and technologies then
    unlocked_count = 0
    for _, technology in pairs(technologies) do
      local ok_researched, researched = pcall(function() return technology.researched end)
      if ok_researched and researched then
        unlocked_count = unlocked_count + 1
      end
    end
  end

  return {
    queue = queue,
    current = current,
    progress = progress,
    unlocked_count = unlocked_count
  }
end

-- Only map-level chart state is retained as charted knowledge.  We intentionally
-- do not enumerate current entities/resources from charted-but-fogged chunks:
-- that would turn a present-day engine query into false historical knowledge.
local function charted_bounds_data(surface, player, force)
  player = get_player(player)
  if not surface or not player or not force then
    return { charted = false }
  end

  local center = util.pos_to_chunk(player.position)
  local minx, miny, maxx, maxy = math.huge, math.huge, -math.huge, -math.huge
  for dx = -64, 64, 4 do
    for dy = -64, 64, 4 do
      local chunk = { x = center.x + dx, y = center.y + dy }
      local ok, charted = pcall(function()
        return util.is_charted(force, surface, { x = chunk.x * 32, y = chunk.y * 32 })
      end)
      if ok and charted then
        local tx, ty = chunk.x * 32, chunk.y * 32
        minx = math.min(minx, tx)
        miny = math.min(miny, ty)
        maxx = math.max(maxx, tx)
        maxy = math.max(maxy, ty)
      end
    end
  end

  if minx == math.huge then
    return { charted = false }
  end
  return {
    charted = true,
    approx_bounds = { min_x = minx, min_y = miny, max_x = maxx, max_y = maxy },
    sampled_radius_chunks = 64,
    sample_stride_chunks = 4
  }
end

function world.charted_knowledge(surface, player)
  if not campaign.has("map_analysis") then
    return nil
  end
  player = get_player(player)
  surface = surface or (player and player.surface)
  local force = safe_force()
  if not player or not surface or not force then
    return nil
  end
  return {
    surface = surface.name,
    bounds = charted_bounds_data(surface, player, force)
  }
end

-- Kept for the legacy remote query, but it shares the same map_analysis gate and
-- contains chart state only.  It never returns entities, resources, or evolution.
function world.charted_bounds(surface_name, player)
  if not campaign.has("map_analysis") then
    return util.err("capability_locked:map_analysis")
  end
  player = get_player(player)
  local surface = surface_by_name(surface_name, player)
  if not surface then
    return util.err("no such surface")
  end
  local force = safe_force()
  return util.ok(charted_bounds_data(surface, player, force))
end

-- Legacy overview callers are deliberately reduced to the same capability-gated
-- surface as the UDP context.  Debug/global simulation fields are not exported.
function world.overview(player)
  player = get_player(player)
  local overview = { capabilities = campaign.get_capabilities() }
  if not player then
    return overview
  end

  if campaign.has("telemetry") then
    overview.player = world.player_state(player)
  end
  if campaign.has("research") then
    overview.research = world.research()
  end
  if campaign.has("inventory") then
    overview.inventory = world.inventory(player)
  end
  if campaign.has("radar_vision") then
    overview.visible_perception = world.visible_perception(player)
  end
  if campaign.has("map_analysis") then
    overview.charted_knowledge = world.charted_knowledge(player.surface, player)
  end
  return overview
end

local function bounded_radius(value, fallback, maximum)
  local radius = numeric(value) or fallback
  return math.max(0, math.min(radius, maximum))
end

function world.area(surface_name, x, y, radius, player)
  if not campaign.has("radar_vision") then
    return util.err("capability_locked:radar_vision")
  end
  x, y = numeric(x), numeric(y)
  if not x or not y then
    return util.err("invalid position")
  end
  player = get_player(player)
  local surface = surface_by_name(surface_name, player)
  if not surface then
    return util.err("no such surface: " .. tostring(surface_name))
  end
  local force = safe_force()
  if not force then
    return util.err("no player force")
  end
  radius = bounded_radius(radius, 32, 128)
  local center = { x = x, y = y }
  local ok_charted, charted = pcall(function() return util.is_charted(force, surface, center) end)
  if not ok_charted or not charted then
    return util.ok({
      area = { x = x, y = y, radius = radius },
      surface = surface.name,
      charted = false,
      visible = false,
      entities = {},
      note = "map area is uncharted; no entity information available"
    })
  end

  local ok_found, found = pcall(function()
    return surface.find_entities_filtered({
      area = { { x - radius, y - radius }, { x + radius, y + radius } },
      limit = 2000
    })
  end)
  if not ok_found then
    return util.err("unable to inspect visible area")
  end

  local groups = {}
  for _, entity in ipairs(world.filter_visible_entities(found or {}, force, surface, 2000)) do
    local key = entity.name
    local group = groups[key]
    if not group then
      group = { entity = key, type = entity.type, count = 0 }
      groups[key] = group
    end
    group.count = group.count + 1
  end

  local list = {}
  for _, group in pairs(groups) do
    list[#list + 1] = group
  end
  table.sort(list, function(a, b)
    if a.count ~= b.count then
      return a.count > b.count
    end
    return a.entity < b.entity
  end)
  return util.ok({
    area = { x = x, y = y, radius = radius },
    surface = surface.name,
    charted = true,
    visible = util.is_visible(force, surface, center),
    distinct = #list,
    entities = list
  })
end

function world.find(spec, player)
  if not campaign.has("radar_vision") then
    return util.err("capability_locked:radar_vision")
  end
  spec = type(spec) == "table" and spec or {}
  player = get_player(player)
  local surface = surface_by_name(spec.surface, player)
  if not surface then
    return util.err("no such surface")
  end
  local force = safe_force()
  if not force then
    return util.err("no player force")
  end

  local origin = player and player.position or { x = 0, y = 0 }
  if type(spec.near) == "table" and numeric(spec.near.x) and numeric(spec.near.y) then
    origin = { x = numeric(spec.near.x), y = numeric(spec.near.y) }
  end
  local radius = bounded_radius(spec.radius, 512, 2048)
  local ok_matches, matches = pcall(function()
    return surface.find_entities_filtered({
      name = spec.name,
      type = spec.type,
      area = { { origin.x - radius, origin.y - radius }, { origin.x + radius, origin.y + radius } },
      limit = 500
    })
  end)
  if not ok_matches then
    return util.err("invalid entity query")
  end

  local results = {}
  for _, entity in ipairs(world.filter_visible_entities(matches or {}, force, surface, 500)) do
    local distance = math.sqrt((entity.x - origin.x) ^ 2 + (entity.y - origin.y) ^ 2)
    results[#results + 1] = {
      name = entity.name,
      type = entity.type,
      position = { x = entity.x, y = entity.y },
      distance = util.round(distance, 1)
    }
  end
  table.sort(results, function(a, b)
    if a.distance ~= b.distance then
      return a.distance < b.distance
    end
    return a.name < b.name
  end)
  while #results > MAX_VISIBLE_ENTITIES do
    table.remove(results)
  end
  return util.ok({
    query = { name = spec.name, near = origin, radius = radius },
    matches = results,
    visible_only = true
  })
end

function world.entity_at(surface_name, x, y, player)
  if not campaign.has("radar_vision") then
    return util.err("capability_locked:radar_vision")
  end
  x, y = numeric(x), numeric(y)
  if not x or not y then
    return util.err("invalid position")
  end
  player = get_player(player)
  local surface = surface_by_name(surface_name, player)
  if not surface then
    return util.err("no such surface")
  end
  local force = safe_force()
  if not force then
    return util.err("no player force")
  end

  local at = { x = x, y = y }
  local ok_charted, charted = pcall(function() return util.is_charted(force, surface, at) end)
  if not ok_charted or not charted then
    return util.ok({ surface = surface.name, at = at, charted = false, visible = false, entities = {} })
  end

  local ok_entities, entities = pcall(function()
    return surface.find_entities({ { x - 0.6, y - 0.6 }, { x + 0.6, y + 0.6 } })
  end)
  if not ok_entities then
    return util.err("unable to inspect entity position")
  end

  local out = {}
  for _, entity in ipairs(world.filter_visible_entities(entities or {}, force, surface, 100)) do
    out[#out + 1] = {
      name = entity.name,
      type = entity.type,
      position = { x = entity.x, y = entity.y }
    }
  end
  return util.ok({
    surface = surface.name,
    at = at,
    charted = true,
    visible = util.is_visible(force, surface, at),
    entities = out
  })
end

return world
