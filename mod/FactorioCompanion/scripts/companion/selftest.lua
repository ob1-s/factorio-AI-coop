local util = require("scripts.companion.util")
local chat = require("scripts.companion.chat")
local markers = require("scripts.companion.markers")
local panels = require("scripts.companion.panels")
local world = require("scripts.companion.world")
local context = require("scripts.companion.context")
local campaign = require("scripts.companion.campaign")
local ui = require("scripts.companion.ui")
local protocol = require("scripts.companion.protocol")
local transport = require("scripts.companion.transport_udp")

local selftest = {}

function selftest.run()
  local results = {}
  local player = game.get_player(1)

  local function with_capabilities(keys, fn)
    campaign.ensure_storage()
    local old_capabilities = storage.companion.capabilities
    local old_level = storage.companion.level
    storage.companion.capabilities = {}
    for _, key in ipairs(keys) do
      storage.companion.capabilities[key] = true
    end
    local ok, result = pcall(fn)
    storage.companion.capabilities = old_capabilities
    storage.companion.level = old_level
    if not ok then
      error(result)
    end
    return result
  end

  local function check(name, fn)
    if not player and (name:find("^chat%.") or name:find("^markers_") or name:find("^tasks_") or name == "ui_spec_dispatch" or name:find("^context_") or name:find("^visibility_") or name:find("^fow_") or name == "world_overview_boundary" or name:find("^transport%.") or name == "protocol.malformed_safe") then
      results[#results + 1] = { test = name, passed = true, skipped = true, error = nil }
      return
    end
    local ok, err = xpcall(fn, function(e)
      return tostring(e) .. "\\n" .. debug.traceback(e, 2)
    end)
    results[#results + 1] = { test = name, passed = ok, error = ok and nil or tostring(err):sub(1, 800) }
  end

  check("ping", function()
    assert(game.tick >= 0)
  end)

  check("protocol.malformed_safe", function()
    local malformed = {
      "",
      "not-json",
      "[]",
      '{"v":1,"type":"heartbeat","payload":42}',
      '{"v":1,"type":"assistant_delta","seq":-1,"payload":{}}'
    }
    for _, raw in ipairs(malformed) do
      local ok, packet, err = pcall(protocol.parse_packet, raw)
      assert(ok, "parser raised on malformed packet: " .. raw)
      assert(packet == nil and err, "malformed packet was accepted: " .. raw)
    end
  end)

  check("campaign.capability_api", function()
    local was_research = campaign.has("research")
    local was_map = campaign.has("map_analysis")
    campaign.lock("research")
    campaign.lock("map_analysis")
    assert(not campaign.has("research"), "lock did not remove research")
    assert(not campaign.has("map_analysis"), "lock did not remove map analysis")
    assert(campaign.unlock("map_analysis"), "unlock did not report a new capability")
    assert(campaign.has("map_analysis"), "unlock did not expose capability")
    campaign.lock("map_analysis")
    if was_research then campaign.unlock("research") end
    if was_map then campaign.unlock("map_analysis") end
  end)

  check("transport.busy_policy", function()
    transport.ensure_storage()
    local old_active = storage.transport.active_exchange
    local old_status = storage.transport.status
    storage.transport.active_exchange = { id = "ex-selftest", request_id = "request-selftest" }
    storage.transport.status = "thinking"
    local allowed, reason = transport.can_start_exchange(player)
    assert(not allowed and reason == "busy", "rapid message was not rejected as busy")
    storage.transport.active_exchange = old_active
    storage.transport.status = old_status
  end)

  check("transport.stale_completion_ignored", function()
    transport.ensure_storage()
    local old_active = storage.transport.active_exchange
    local old_status = storage.transport.status
    local old_head = campaign.get_conversation_head()
    local current_world = campaign.get_world_id()
    storage.transport.active_exchange = {
      id = "ex-selftest-current",
      request_id = "request-selftest-current",
      turn_id = "client-selftest-turn",
      parent_turn_id = old_head,
      world_id = current_world,
      client_session_id = campaign.get_runtime_session_id(),
      last_seq = -1,
      stream_started = false
    }
    storage.transport.status = "thinking"
    local raw = protocol.create_packet("assistant_end", "ex-selftest-current", 4, {
      world_id = "another-world",
      turn_id = "turn-stale",
      full_text = "stale"
    })
    transport.handle_packet({ payload = raw, player_index = player.index })
    assert(storage.transport.active_exchange ~= nil, "stale completion consumed active request")
    assert(campaign.get_conversation_head() == old_head, "stale completion changed conversation head")
    storage.transport.active_exchange = old_active
    storage.transport.status = old_status
  end)

  check("chat.record_udp_only", function()
    local id = chat.record_user_message(player, "selftest-message", "client-test-turn", "ex-test")
    assert(id, "no message id")
    assert(storage.outbox == nil, "legacy conversational outbox still exists")
    local entry = storage.chat_log[player.index][#storage.chat_log[player.index]]
    assert(entry.text == "selftest-message", "text mismatch")
    assert(entry.exchange_id == "ex-test", "exchange identity not recorded")
  end)

  check("chat.stream_cycle", function()
    local player = game.get_player(1)
    chat.open(player)
    chat.stream_start(player)
    chat.stream_append(player, "hel")
    chat.stream_append(player, "lo world")
    chat.stream_end(player, "authoritative reply", "turn-authoritative")
    local entry = storage.chat_log[player.index][#storage.chat_log[player.index]]
    assert(entry.text == "authoritative reply", "authoritative completion not rendered")
    assert(entry.text ~= "hello world", "cosmetic stream was promoted to history")
    chat.close(player)
  end)

  check("chat.badge", function()
    local player = game.get_player(1)
    chat.close(player)
    chat.update_badge(player)
    local pill = player.gui.relative.companion_chat_pill
    assert(pill and pill.valid, "pill missing from gui.relative")
    chat.open(player)
    chat.update_badge(player)
    assert(pill.visible == false, "pill should hide while chat open")
    chat.close(player)
    chat.update_badge(player)
    assert(pill.visible == true, "pill should show while chat closed")
  end)

  check("chat.deliver_while_closed", function()
    local player = game.get_player(1)
    chat.close(player)
    local before_unread = storage.unread
    chat.deliver_agent_message(player, "closed-delivery-test")
    assert(storage.unread == before_unread + 1, "unread not incremented")
  end)

  check("chunker_roundtrip", function()
    local payload = string.rep("0123456789abcdef", 400)
    local envelope = util.dec(util.begin_query("test", payload))
    assert(envelope and envelope.total_chunks >= 2, "expected multiple chunks")
    local reassembled = ""
    for i = 1, envelope.total_chunks do
      local resp = util.dec(util.fetch_chunk(envelope.query, i))
      reassembled = reassembled .. resp.chunk
    end
    assert(reassembled == payload, "payload corrupted: " .. #reassembled .. " vs " .. #payload)
    local done_resp = util.dec(util.fetch_chunk(envelope.query))
    assert(done_resp.done == true, "no done flag")
  end)

  check("fow_uncharted_hidden", function()
    local force = game.forces.player
    local surface = player.surface
    local probe = { x = player.position.x + 100000, y = player.position.y + 100000 }
    assert(not util.is_charted(force, surface, probe), "probe chunk unexpectedly charted")
    local result = util.dec(with_capabilities({ "radar_vision" }, function()
      return world.area(surface.name, probe.x, probe.y, 16, player)
    end))
    assert(result.ok and result.data.visible == false and result.data.charted == false, "fow leak in area query")
    assert(result.data.entities and #result.data.entities == 0, "uncharted entities returned")
    assert(result.data.uncharted_entities_nearby == nil, "hidden entity count leaks through area query")
  end)

  check("visibility_boundary", function()
    local visible_chunk = util.pos_to_chunk(player.position)
    local fogged_chunk = { x = visible_chunk.x + 2, y = visible_chunk.y }
    local uncharted_chunk = { x = visible_chunk.x + 4, y = visible_chunk.y }
    local fake_surface = { name = player.surface.name, index = player.surface.index }
    local fake_force = {
      is_chunk_charted = function(_, chunk)
        return (chunk.x == visible_chunk.x and chunk.y == visible_chunk.y) or
          (chunk.x == fogged_chunk.x and chunk.y == fogged_chunk.y)
      end,
      is_chunk_visible = function(_, chunk)
        return chunk.x == visible_chunk.x and chunk.y == visible_chunk.y
      end
    }
    local function at(chunk)
      return { x = chunk.x * 32 + 1, y = chunk.y * 32 + 1 }
    end
    local visible_position = at(visible_chunk)
    local fogged_position = at(fogged_chunk)
    local uncharted_position = at(uncharted_chunk)
    local candidates = {
      { valid = true, name = "small-biter", type = "unit", position = fogged_position, surface = fake_surface },
      { valid = true, name = "iron-ore", type = "resource", position = uncharted_position, surface = fake_surface, amount = 1000000 },
      { valid = true, name = "wooden-chest", type = "container", position = visible_position, surface = fake_surface, unit_number = 4242, force = { name = "player" } }
    }
    local visible = world.filter_visible_entities(candidates, fake_force, fake_surface, 25)
    assert(#visible == 1, "charted/fogged or uncharted entity crossed visibility boundary")
    assert(visible[1].name == "wooden-chest", "visible entity was not preserved")
    assert(visible[1].amount == nil and visible[1].unit_number == nil and visible[1].force == nil, "hidden entity internals serialized")
    local serialized = util.enc({ visible_perception = { nearby_entities = visible } })
    assert(not serialized:find("small%-biter", 1, false), "fogged enemy leaked into serialized context")
    assert(not serialized:find("iron%-ore", 1, false), "uncharted resource leaked into serialized context")
  end)

  check("context_capability_gate", function()
    with_capabilities({}, function()
      local locked = context.build(player)
      assert(locked.player == nil and locked.research == nil and locked.inventory == nil, "locked context field leaked")
      assert(locked.visible_perception == nil and locked.charted_knowledge == nil, "locked map/perception field leaked")
      assert(world.player_state(player) == nil and world.research() == nil, "legacy telemetry/research bypassed capability lock")
      assert(world.inventory(player) == nil and world.visible_perception(player) == nil, "legacy inventory/radar bypassed capability lock")
      assert(world.charted_knowledge(player.surface, player) == nil, "legacy charted knowledge bypassed capability lock")
    end)

    local unlocked = with_capabilities({ "telemetry", "research", "inventory", "radar_vision", "map_analysis" }, function()
      return context.build(player)
    end)
    assert(unlocked.player and unlocked.research and unlocked.inventory, "unlocked context field missing")
    assert(unlocked.visible_perception and unlocked.charted_knowledge, "unlocked perception/map field missing")
    local serialized = util.enc(unlocked)
    assert(not serialized:find('"evolution"', 1, true), "exact evolution factor leaked into context")
    assert(not serialized:find('"world_id"', 1, true) and not serialized:find('"conversation_head"', 1, true), "transport internals leaked into model context")
  end)

  check("world_overview_boundary", function()
    local overview = with_capabilities({ "telemetry", "research", "inventory", "radar_vision", "map_analysis" }, function()
      return world.overview(player)
    end)
    local serialized = util.enc(overview)
    assert(not serialized:find('"evolution"', 1, true), "exact evolution factor leaked through legacy overview")
    assert(not serialized:find('"production_top"', 1, true), "production internals leaked through legacy overview")
    assert(not serialized:find('"electric_network"', 1, true), "power internals leaked through legacy overview")
    assert(not serialized:find('"rocket_parts"', 1, true), "rocket internals leaked through legacy overview")
  end)

  check("markers_draw_clear", function()
    local player = game.get_player(1)
    local id = markers.draw({ position = { x = player.position.x + 3, y = player.position.y }, label = "t", color = "green" }, player)
    assert(id, "marker draw failed")
    assert(markers.list(player)[#markers.list(player)].id == id, "marker not listed")
    assert(markers.remove_by_id(player, id), "marker remove failed")
  end)

  check("tasks_lifecycle", function()
    local player = game.get_player(1)
    panels.set_tasks(player, { { text = "task A" }, { text = "task B", status = "active" } })
    local tasks = panels.get_tasks(player)
    assert(#tasks == 2, "tasks not set")
    local tid = tasks[1].id
    assert(panels.update_task(player, tid, { status = "done" }), "update failed")
    assert(panels.remove_task(player, tid), "remove failed")
    assert(#panels.get_tasks(player) == 1)
    -- clean up test residue so the real task board stays empty
    for _, t in ipairs(panels.get_tasks(player)) do
      panels.remove_task(player, t.id)
    end
    local board = player.gui.screen.companion_tasks
    if board and board.valid then
      board.visible = false
    end
  end)

  check("ui_spec_dispatch", function()
    local r = util.dec(ui.dispatch({ kind = "info.show", title = "T", body = "B", rows = { { "k", "v" } } }))
    assert(r.ok, "info.show failed")
    r = util.dec(ui.dispatch({ kind = "bogus.kind" }))
    assert(not r.ok, "bogus kind should fail")
    panels.hide_info(player) -- don't leave the test panel on screen
  end)

  check("sprites_valid", function()
    for _, sprite in pairs(panels.valid_sprites()) do
      assert(helpers.is_valid_sprite_path(sprite), "invalid sprite: " .. sprite)
    end
    local logo_ok, logo_err = pcall(rendering.draw_sprite, {
      sprite = panels.logo_sprite(),
      target = { x = 0, y = -100000 },
      surface = game.surfaces.nauvis,
      time_to_live = 1
    })
    assert(logo_ok, "logo sprite invalid: " .. tostring(logo_err))
  end)

  local passed = 0
  for _, r in ipairs(results) do
    if r.passed then
      passed = passed + 1
    end
  end

  return util.enc({
    ok = passed == #results,
    passed = passed,
    total = #results,
    results = results
  })
end

return selftest
