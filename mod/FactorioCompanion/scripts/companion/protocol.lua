local util = require("scripts.companion.util")

local protocol = {
  VERSION = 1
}

function protocol.create_packet(msg_type, exchange_id, seq, payload)
  return util.enc({
    v = protocol.VERSION,
    type = tostring(msg_type),
    exchange_id = tostring(exchange_id or ""),
    seq = tonumber(seq) or 0,
    tick = game and game.tick or 0,
    payload = payload or {}
  })
end

function protocol.parse_packet(raw_str)
  if type(raw_str) ~= "string" or raw_str == "" then
    return nil, "empty_packet"
  end

  local obj = util.dec(raw_str)
  if not obj or type(obj) ~= "table" then
    return nil, "invalid_json"
  end

  if obj.v ~= protocol.VERSION then
    return nil, "unsupported_version: " .. tostring(obj.v)
  end

  if type(obj.type) ~= "string" or obj.type == "" then
    return nil, "missing_type"
  end

  obj.seq = tonumber(obj.seq) or 0
  obj.tick = tonumber(obj.tick) or 0
  obj.exchange_id = tostring(obj.exchange_id or "")
  obj.payload = type(obj.payload) == "table" and obj.payload or {}

  return obj, nil
end

return protocol
