local util = require("scripts.companion.util")

local campaign = {}

storage = storage or {}

-- Standard capability presets for milestone convenience
local PRESETS = {
  [0] = { "telemetry" },
  [1] = { "telemetry", "research" },
  [2] = { "telemetry", "research", "inventory" },
  [3] = { "telemetry", "research", "inventory", "map_analysis" },
  [4] = { "telemetry", "research", "inventory", "radar_vision", "map_analysis", "markers", "tasks" }
}

local function generate_uuid()
  local template = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx"
  local rng = game and game.create_random_generator and game.create_random_generator() or nil
  return string.gsub(template, "[xy]", function(c)
    local v
    if rng then
      v = (c == "x") and rng(0, 15) or (rng(8, 11))
    else
      local r = math.floor((game.tick * 31 + math.random(0, 255)) % 16)
      v = (c == "x") and r or ((r % 4) + 8)
    end
    return string.format("%x", v)
  end)
end

function campaign.ensure_storage()
  storage.companion = storage.companion or {}
  if not storage.companion.world_id then
    storage.companion.world_id = generate_uuid()
    util.log("generated new companion world_id: " .. storage.companion.world_id)
  end
  storage.companion.conversation_head = storage.companion.conversation_head or "turn_0"
  storage.companion.turn_counter = storage.companion.turn_counter or 0
  storage.companion.capabilities = storage.companion.capabilities or {
    telemetry = true,
    research = true,
    inventory = true
  }
  storage.companion.level = storage.companion.level or 2
end

function campaign.get_world_id()
  campaign.ensure_storage()
  return storage.companion.world_id
end

function campaign.get_conversation_head()
  campaign.ensure_storage()
  return storage.companion.conversation_head
end

function campaign.set_conversation_head(turn_id)
  campaign.ensure_storage()
  storage.companion.conversation_head = tostring(turn_id)
end

function campaign.next_turn_id()
  campaign.ensure_storage()
  storage.companion.turn_counter = storage.companion.turn_counter + 1
  return "turn_" .. tostring(storage.companion.turn_counter)
end

function campaign.has(feature_key)
  campaign.ensure_storage()
  return storage.companion.capabilities[tostring(feature_key)] == true
end

function campaign.unlock(feature_key)
  campaign.ensure_storage()
  local key = tostring(feature_key)
  local was = storage.companion.capabilities[key] == true
  storage.companion.capabilities[key] = true
  if not was then
    util.log("unlocked companion capability: " .. key)
    script.raise_event(defines.events.on_lua_shortcut or defines.events.on_tick, {
      name = "companion_capability_changed",
      capability = key,
      unlocked = true
    })
  end
  return not was
end

function campaign.lock(feature_key)
  campaign.ensure_storage()
  local key = tostring(feature_key)
  local was = storage.companion.capabilities[key] == true
  storage.companion.capabilities[key] = nil
  if was then
    util.log("locked companion capability: " .. key)
  end
  return was
end

function campaign.get_capabilities()
  campaign.ensure_storage()
  local list = {}
  for k, v in pairs(storage.companion.capabilities) do
    if v then
      list[#list + 1] = k
    end
  end
  table.sort(list)
  return list
end

function campaign.set_level(level)
  campaign.ensure_storage()
  level = math.max(0, math.min(tonumber(level) or 0, 4))
  storage.companion.level = level
  local preset = PRESETS[level] or PRESETS[0]
  storage.companion.capabilities = {}
  for _, key in ipairs(preset) do
    storage.companion.capabilities[key] = true
  end
  util.log("set companion level to " .. tostring(level) .. " (capabilities: " .. table.concat(preset, ", ") .. ")")
  return campaign.get_capabilities()
end

function campaign.get_level()
  campaign.ensure_storage()
  return storage.companion.level or 0
end

return campaign
