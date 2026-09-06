local util = require("scripts.companion.util")
local chat = require("scripts.companion.chat")
local markers = require("scripts.companion.markers")
local panels = require("scripts.companion.panels")
local world = require("scripts.companion.world")
local ui = require("scripts.companion.ui")

local selftest = {}

function selftest.run()
  local results = {}
  local player = game.get_player(1)

  local function check(name, fn)
    if not player and (name:find("^chat%.") or name:find("^markers_") or name:find("^tasks_") or name == "ui_spec_dispatch") then
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

  check("chat.enqueue_drain", function()
    local before = #storage.outbox
    local id = chat.enqueue_user_message(game.get_player(1), "selftest-message")
    assert(id, "no message id")
    local drained = chat.drain_outbox(10)
    assert(#drained >= 1, "nothing drained")
    assert(drained[#drained].text == "selftest-message", "text mismatch")
  end)

  check("chat.stream_cycle", function()
    local player = game.get_player(1)
    chat.open(player)
    chat.stream_start(player)
    chat.stream_append(player, "hel")
    chat.stream_append(player, "lo world")
    chat.stream_end(player, "")
    assert(#storage.chat_log[player.index] > 0)
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
    local surface = game.surfaces.nauvis or game.surfaces[1]
    local probe = { x = 50000, y = 50000 }
    if force.is_chunk_charted(surface, util.pos_to_chunk(probe)) then
      force.clear_chart(surface.name)
    end
    assert(not util.can_see(force, surface, probe), "probe chunk unexpectedly charted")
    local result = util.dec(world.area("nauvis", probe.x, probe.y, 16))
    assert(result.ok and result.data.visible == false, "fow leak in area query")
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
