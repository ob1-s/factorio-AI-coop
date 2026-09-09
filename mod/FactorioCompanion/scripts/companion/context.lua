local campaign = require("scripts.companion.campaign")
local world = require("scripts.companion.world")

local context = {}

local function safe_call(fn, ...)
  local ok, result = pcall(fn, ...)
  if ok then
    return result
  end
  return nil
end

function context.build(player)
  player = player or game.get_player(1)
  if not player then
    return {}
  end

  -- These are the only fields sent as the model-bound context snapshot.  World
  -- and conversation IDs remain transport metadata, not hidden model context.
  local snapshot = {
    capabilities = campaign.get_capabilities()
  }

  if campaign.has("telemetry") then
    local player_state = safe_call(world.player_state, player)
    if player_state then
      snapshot.player = player_state
    end
  end

  if campaign.has("research") then
    local research = safe_call(world.research)
    if research then
      snapshot.research = research
    end
  end

  if campaign.has("inventory") then
    local inventory = safe_call(world.inventory, player)
    if inventory then
      snapshot.inventory = inventory
    end
  end

  -- Current perception is visibility-gated per entity.  It must not be inferred
  -- from charted state, because a charted chunk can currently be under fog.
  if campaign.has("radar_vision") then
    local perception = safe_call(world.visible_perception, player)
    if perception then
      snapshot.visible_perception = perception
    end
  end

  -- Charted knowledge is intentionally static map metadata only.  In particular,
  -- do not enumerate current resources or enemies in charted-but-fogged chunks.
  if campaign.has("map_analysis") then
    local knowledge = safe_call(world.charted_knowledge, player.surface, player)
    if knowledge then
      snapshot.charted_knowledge = knowledge
    end
  end

  return snapshot
end

return context
