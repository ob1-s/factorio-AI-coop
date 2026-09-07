local util = {}

util.MOD_NAME = "FactorioCompanion"

function util.log(msg)
  log("[" .. util.MOD_NAME .. "] " .. msg)
end

function util.pprint(msg)
  print("[" .. util.MOD_NAME .. "] " .. msg)
end

function util.enc(obj)
  local ok, result = pcall(helpers.table_to_json, obj)
  if ok then
    return result
  end
  return '{"error":"encode failed"}'
end

function util.dec(str)
  if type(str) ~= "string" or str == "" then
    return nil
  end
  local ok, result = pcall(helpers.json_to_table, str)
  if ok then
    return result
  end
  return nil
end

function util.ok(payload)
  return util.enc({ ok = true, data = payload })
end

function util.err(msg)
  return util.enc({ ok = false, error = tostring(msg) })
end

function util.round(n, decimals)
  local mult = 10 ^ (decimals or 0)
  return math.floor(n * mult + 0.5) / mult
end

function util.pos_to_chunk(pos)
  return { x = math.floor(pos.x / 32), y = math.floor(pos.y / 32) }
end

function util.is_charted(force, surface, pos)
  if not force or not surface or not pos then
    return false
  end
  return force.is_chunk_charted(surface, util.pos_to_chunk(pos))
end

function util.is_visible(force, surface, pos)
  if not force or not surface or not pos then
    return false
  end
  local ok, visible = pcall(force.is_chunk_visible, surface, util.pos_to_chunk(pos))
  return ok and visible == true
end

function util.can_see(force, surface, pos)
  return util.is_visible(force, surface, pos)
end


local MAX_INLINE = 1800
local CHUNK_SIZE = 1400

storage = storage or {}
storage.queries = storage.queries or {}
storage.next_query_id = storage.next_query_id or 1

function util.begin_query(name, payload_str)
  local id = storage.next_query_id
  storage.next_query_id = id + 1
  local chunks = {}
  local i = 1
  while i <= #payload_str do
    chunks[#chunks + 1] = payload_str:sub(i, i + CHUNK_SIZE - 1)
    i = i + CHUNK_SIZE
  end
  storage.queries[id] = {
    name = name,
    chunks = chunks,
    created = game.tick,
    cursor = 0
  }
  for qid in pairs(storage.queries) do
    if storage.queries[qid].created + 3600 < game.tick then
      storage.queries[qid] = nil
    end
  end
  return util.enc({ ok = true, query = id, name = name, total_chunks = #chunks, inline = (#payload_str <= MAX_INLINE) and payload_str or nil })
end

function util.fetch_chunk(qid, index)
  local q = storage.queries[tonumber(qid)]
  if not q then
    return util.err("unknown query " .. tostring(qid))
  end
  local idx = tonumber(index) or (q.cursor + 1)
  local chunk = q.chunks[idx]
  if not chunk then
    storage.queries[tonumber(qid)] = nil
    return util.enc({ ok = true, done = true })
  end
  q.cursor = idx
  return util.enc({ ok = true, chunk = chunk, index = idx, total = #q.chunks })
end

return util
