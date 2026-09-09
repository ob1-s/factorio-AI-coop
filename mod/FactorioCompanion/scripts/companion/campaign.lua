local util = require("scripts.companion.util")

local campaign = {}

storage = storage or {}

-- Runtime-only identity. Factorio simulation state is deterministic, so this
-- value must be assigned by the external daemon during hello and must never be
-- persisted as durable campaign state.
local runtime_session_id

local IDENTITY_SCHEMA_VERSION = 1

-- Standard capability presets for milestone convenience
local PRESETS = {
  [0] = { "telemetry" },
  [1] = { "telemetry", "research" },
  [2] = { "telemetry", "research", "inventory" },
  [3] = { "telemetry", "research", "inventory", "map_analysis" },
  [4] = { "telemetry", "research", "inventory", "radar_vision", "map_analysis", "markers", "tasks" }
}

local function normalize_capability_key(feature_key)
  if type(feature_key) ~= "string" then
    return nil
  end
  local key = string.match(feature_key, "^%s*(.-)%s*$")
  if key == "" or #key > 80 then
    return nil
  end
  return key
end

function campaign.ensure_storage()
  storage.companion = storage.companion or {}

  -- One-time pre-v0 migration. Previous builds derived a UUID-looking value
  -- from Factorio's deterministic map-seeded RNG, so two same-seed games could
  -- collide and reloads could recreate runtime session IDs. Do not preserve
  -- that value as authoritative identity.
  if storage.companion.identity_schema_version ~= IDENTITY_SCHEMA_VERSION then
    if storage.companion.world_id ~= nil then
      util.log("migrating legacy deterministic companion identity; rebinding on next daemon hello")
    end
    storage.companion.world_id = nil
    storage.companion.conversation_head = "turn_0"
    storage.companion.identity_schema_version = IDENTITY_SCHEMA_VERSION
  end

  storage.companion.conversation_head = storage.companion.conversation_head or "turn_0"
  if type(storage.companion.turn_counter) ~= "number" then
    storage.companion.turn_counter = 0
  end
  if type(storage.companion.request_counter) ~= "number" then
    storage.companion.request_counter = 0
  end
  if type(storage.companion.capabilities) ~= "table" then
    storage.companion.capabilities = {
      telemetry = true,
      research = true,
      inventory = true
    }
  end
  if type(storage.companion.level) ~= "number" then
    storage.companion.level = 2
  end
end

function campaign.get_world_id()
  campaign.ensure_storage()
  return storage.companion.world_id
end

function campaign.set_world_id(world_id)
  campaign.ensure_storage()
  if type(world_id) ~= "string" or world_id == "" or #world_id > 200 then
    return false, "invalid world id"
  end
  local existing = storage.companion.world_id
  if existing ~= nil and existing ~= world_id then
    return false, "world id is already bound"
  end
  storage.companion.world_id = world_id
  return true
end

function campaign.get_conversation_head()
  campaign.ensure_storage()
  return storage.companion.conversation_head
end

function campaign.set_conversation_head(turn_id)
  campaign.ensure_storage()
  if type(turn_id) ~= "string" or turn_id == "" or #turn_id > 200 then
    return false, "invalid conversation head"
  end
  storage.companion.conversation_head = turn_id
  return true
end

function campaign.get_runtime_session_id()
  return runtime_session_id
end

function campaign.set_runtime_session_id(session_id)
  if type(session_id) ~= "string" or session_id == "" or #session_id > 200 then
    return false, "invalid runtime session id"
  end
  runtime_session_id = session_id
  return true
end

function campaign.clear_runtime_session_id()
  runtime_session_id = nil
end

function campaign.next_turn_id()
  campaign.ensure_storage()
  local world_id = campaign.get_world_id()
  local session_id = campaign.get_runtime_session_id()
  if not world_id or not session_id then
    return nil
  end
  storage.companion.turn_counter = storage.companion.turn_counter + 1
  return "client-turn-" .. world_id .. "-" .. session_id .. "-" .. tostring(storage.companion.turn_counter)
end

function campaign.next_request_id()
  campaign.ensure_storage()
  local world_id = campaign.get_world_id()
  local session_id = campaign.get_runtime_session_id()
  if not world_id or not session_id then
    return nil
  end
  storage.companion.request_counter = storage.companion.request_counter + 1
  return "request-" .. world_id .. "-" .. session_id .. "-" .. tostring(storage.companion.request_counter)
end

function campaign.has(feature_key)
  campaign.ensure_storage()
  local key = normalize_capability_key(feature_key)
  return key ~= nil and storage.companion.capabilities[key] == true
end

function campaign.unlock(feature_key)
  campaign.ensure_storage()
  local key = normalize_capability_key(feature_key)
  if not key then
    return false, "invalid capability key"
  end
  local was = storage.companion.capabilities[key] == true
  storage.companion.capabilities[key] = true
  if not was then
    util.log("unlocked companion capability: " .. key)
  end
  return not was
end

function campaign.lock(feature_key)
  campaign.ensure_storage()
  local key = normalize_capability_key(feature_key)
  if not key then
    return false, "invalid capability key"
  end
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
  level = math.floor(tonumber(level) or 0)
  level = math.max(0, math.min(level, 4))
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

function campaign.capability_state()
  return {
    world_id = campaign.get_world_id(),
    conversation_head = campaign.get_conversation_head(),
    capabilities = campaign.get_capabilities(),
    level = campaign.get_level()
  }
end

return campaign
