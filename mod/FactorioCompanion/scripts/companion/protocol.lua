local util = require("scripts.companion.util")

local protocol = {
  VERSION = 1,
  MAX_PACKET_SIZE = 4096
}

local function finite_integer(value, default)
  if value == nil then
    return default
  end
  local number = tonumber(value)
  if not number or number ~= number or number == math.huge or number == -math.huge then
    return nil
  end
  if number ~= math.floor(number) or number < 0 then
    return nil
  end
  return number
end

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
  if #raw_str > protocol.MAX_PACKET_SIZE then
    return nil, "packet_too_large"
  end

  local decode_ok, obj = pcall(util.dec, raw_str)
  if not decode_ok then
    return nil, "decoder_error"
  end
  if not obj or type(obj) ~= "table" then
    return nil, "invalid_json"
  end

  if obj.v ~= protocol.VERSION then
    return nil, "unsupported_version: " .. tostring(obj.v)
  end

  if type(obj.type) ~= "string" or obj.type == "" then
    return nil, "missing_type"
  end

  local seq = finite_integer(obj.seq, 0)
  if seq == nil then
    return nil, "invalid_seq"
  end
  local tick = finite_integer(obj.tick, 0)
  if tick == nil then
    return nil, "invalid_tick"
  end
  if obj.exchange_id ~= nil and type(obj.exchange_id) ~= "string" then
    return nil, "invalid_exchange_id"
  end
  if obj.payload ~= nil and type(obj.payload) ~= "table" then
    return nil, "invalid_payload"
  end

  obj.seq = seq
  obj.tick = tick
  obj.exchange_id = obj.exchange_id or ""
  obj.payload = obj.payload or {}

  return obj, nil
end

return protocol
