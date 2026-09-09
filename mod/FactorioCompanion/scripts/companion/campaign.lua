local util = require("scripts.companion.util")

local campaign = {}

storage = storage or {}

-- This is deliberately runtime-only.  A save reload can rewind every value in
-- storage, so it must never be used as the canonical conversation head.  It
-- only namespaces tentative client turn/request references until the daemon
-- returns the authoritative completed turn id in assistant_end.
local runtime_session_id

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
      local tick = (game and game.tick) or 0
      local r = math.floor((tick * 31 + math.random(0, 255)) % 16)
      v = (c == "x") and r or ((r % 4) + 8)
    end
    return string.format("%x", v)
  end)
end

local function get_runtime_session_id()
  if not runtime_session_id then
    runtime_session_id = generate_uuid()
  end
  return runtime_session_id
end

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
  if not storage.companion.world_id then
    storage.companion.world_id = generate_uuid()
    util.log("generated new companion world_id: " .. storage.companion.world_id)
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

function campaign.next_turn_id()
  campaign.ensure_storage()
  storage.companion.turn_counter = storage.companion.turn_counter + 1
  -- The daemon must treat this as a client reference, not as the canonical
  -- completed turn id.  Including a runtime namespace prevents the common
  -- turn_13 collision when a save is rolled back and a new branch is created.
  return "client-turn-" .. campaign.get_world_id() .. "-" .. get_runtime_session_id() .. "-" .. tostring(storage.companion.turn_counter)
end

function campaign.next_request_id()
  campaign.ensure_storage()
  storage.companion.request_counter = storage.companion.request_counter + 1
  return "request-" .. campaign.get_world_id() .. "-" .. get_runtime_session_id() .. "-" .. tostring(storage.companion.request_counter)
end

function campaign.get_runtime_session_id()
  return get_runtime_session_id()
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

-- Small campaign-facing API.  Campaign code can use this without knowing
-- anything about UDP, UI state, or the model provider.
function campaign.capability_state()
  return {
    world_id = campaign.get_world_id(),
    conversation_head = campaign.get_conversation_head(),
    capabilities = campaign.get_capabilities(),
    level = campaign.get_level()
  }
end

return campaign
