local util = require("scripts.companion.util")
local campaign = require("scripts.companion.campaign")
local world = require("scripts.companion.world")

local context = {}

local function safe_force()
  return game.forces.player
end

function context.build(player)
  player = player or game.get_player(1)
  if not player then
    return {}
  end

  local force = safe_force()
  local surface = player.surface
  local capabilities = campaign.get_capabilities()
  local snapshot = {
    capabilities = capabilities,
    world_id = campaign.get_world_id(),
    conversation_head = campaign.get_conversation_head()
  }

  -- 1. Telemetry (vitals, position, surface)
  if campaign.has("telemetry") then
    local char = player.character
    snapshot.player = {
      name = player.name,
      surface = surface.name,
      position = { x = util.round(player.position.x, 1), y = util.round(player.position.y, 1) },
      health = char and util.round(char.health, 1) or nil,
      max_health = (char and (function()
        local ok, mh = pcall(function() return char.prototype.max_health end)
        return ok and mh or nil
      end)()) or nil,
      health_ratio = char and util.round(char.get_health_ratio(), 2) or nil,
      walking = player.walking_state.walking,
      mining = player.mining_state.mining,
      vehicle = player.vehicle and player.vehicle.name or nil
    }
  end

  -- 2. Research (current tech, progress, queue)
  if campaign.has("research") then
    snapshot.research = world.research()
  end

  -- 3. Inventory (main, armor, ammo)
  if campaign.has("inventory") then
    snapshot.inventory = world.inventory(player)
  end

  -- 4. Radar Vision (LIVE entities & alerts: STRICTLY REQUIRES is_chunk_visible)
  if campaign.has("radar_vision") then
    local visible_entities = {}
    -- Search in a small radius around player (up to 48 tiles)
    local pos = player.position
    local r = 48
    local candidates = surface.find_entities_filtered({
      area = { { pos.x - r, pos.y - r }, { pos.x + r, pos.y + r } },
      limit = 100
    })
    for _, ent in ipairs(candidates) do
      if ent.valid and ent.name ~= "character" then
        -- Strict requirement: MUST be currently visible
        if util.is_visible(force, surface, ent.position) then
          visible_entities[#visible_entities + 1] = {
            name = ent.name,
            type = ent.type,
            x = util.round(ent.position.x, 0),
            y = util.round(ent.position.y, 0)
          }
          if #visible_entities >= 25 then
            break
          end
        end
      end
    end

    -- Filter alerts: only alerts whose entity/target is currently visible
    local live_alerts = {}
    local raw_alerts = player.get_alerts{}
    for _, by_type in pairs(raw_alerts) do
      for alert_type, list in pairs(by_type) do
        for _, entry in pairs(list) do
          local ent = entry.entity or entry.target
          if ent and ent.valid and util.is_visible(force, surface, ent.position) then
            live_alerts[#live_alerts + 1] = {
              type = tostring(alert_type),
              name = ent.name,
              x = util.round(ent.position.x, 0),
              y = util.round(ent.position.y, 0)
            }
          end
        end
      end
    end

    snapshot.visible_perception = {
      nearby_entities = visible_entities,
      alerts = live_alerts
    }
  end

  -- 5. Map Analysis (Charted knowledge: REQUIRES is_chunk_charted)
  if campaign.has("map_analysis") then
    snapshot.charted_knowledge = {
      bounds = world.charted_bounds(surface.name),
      evolution = util.round(force.get_evolution_factor(surface), 3)
    }
  end

  return snapshot
end

return context
