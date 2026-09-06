#!/usr/bin/env python3
"""Factorio Companion agent — bridges in-game companion chat to Gemini.

usage:
  python3 agent.py --probe                  connect, ping, status, exit
  python3 agent.py --say "hello there"      one full user turn, then exit
  python3 agent.py                          run the poll loop (drain outbox)
  python3 agent.py --model gemini-2.5-flash --poll 0.5

env: GEMINI_API_KEY (or GEMINI_API_KEY_2 / GOOGLE_API_KEY),
     RCON_HOST / RCON_PORT / RCON_PASSWORD to override defaults.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gameapi
import gemini

SYSTEM_PROMPT = """You are Companion, a friendly AI copilot living inside the player's \
Factorio game through the FactorioCompanion mod. The player talks to you through a \
small in-game chat window and you reply in that window.

Style rules:
- Keep replies short and conversational: 1-4 sentences. This is a chat bubble, not a report.
- Plain text only: no markdown headers, no long bullet lists, no code fences.
- You are given live game context JSON (player state, research, sometimes more). Ground \
your answers in it and do not invent items, entities or counts that are not in the context.
- You know Factorio (Space Age / 2.0). If unsure about something the context does not \
show, say so briefly and give your best general advice.
- Light personality is welcome: you are the factory's helpful companion, not a corporate bot."""


class Agent:
    def __init__(self, game, model, poll=0.5, max_history=16):
        self.game = game
        self.model = model
        self.poll = poll
        self.max_history = max_history
        self.history = []

    def build_context(self):
        lines = []
        for label, fetch in (("player_state", self.game.player_state),
                             ("research", self.game.research)):
            try:
                lines.append(f"{label}: {json.dumps(fetch(), ensure_ascii=False)}")
            except Exception as exc:
                lines.append(f"{label}: unavailable ({exc})")
        return "\n".join(lines)

    def handle(self, text):
        self.game.set_busy(True)
        self.game.stream_start()
        try:
            context = self.build_context()
            turn = {"role": "user", "parts": [{"text": f"{text}\n\n[game context]\n{context}"}]}
            contents = self.history[-self.max_history:] + [turn]
            collected = []

            def on_delta(delta):
                collected.append(delta)
                for i in range(0, len(delta), 120):
                    self.game.stream_append(delta[i:i + 120])
                    time.sleep(0.03)

            full, mode = gemini.ask(self.model, contents, SYSTEM_PROMPT, on_delta)
            self.game.stream_end(full)
            self.history.append(turn)
            self.history.append({"role": "model", "parts": [{"text": full}]})
            print(f"[agent] replied ({mode}, {len(full)} chars): {full[:100]!r}", flush=True)
        except Exception as exc:
            try:
                self.game.stream_end("")  # flush partial + clear thinking status
                self.game.deliver(f"[companion bridge] error: {exc}")
            except Exception:
                pass
            print(f"[agent] error: {exc}", flush=True)
        finally:
            try:
                self.game.set_busy(False)
            except Exception:
                pass

    def run_forever(self):
        print(f"[agent] poll loop running (model={self.model}, poll={self.poll}s)", flush=True)
        while True:
            try:
                messages = self.game.drain_outbox()
                if messages:
                    text = "\n".join(str(m.get("text", "")).strip()
                                     for m in messages if m.get("text"))
                    if text:
                        print(f"[agent] player: {text[:140]!r}", flush=True)
                        self.handle(text)
                else:
                    time.sleep(self.poll)
            except KeyboardInterrupt:
                print("[agent] stopped", flush=True)
                return
            except Exception as exc:
                print(f"[agent] loop error: {exc}", flush=True)
                time.sleep(2)
                try:
                    self.game.connect()
                    print("[agent] reconnected", flush=True)
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=os.environ.get("COMPANION_MODEL", gemini.DEFAULT_MODEL))
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--host", default=os.environ.get("RCON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RCON_PORT", "34973")))
    parser.add_argument("--password", default=os.environ.get("RCON_PASSWORD"))
    parser.add_argument("--probe", action="store_true", help="connect + ping + status, then exit")
    parser.add_argument("--say", metavar="TEXT", help="run TEXT through a full agent turn, then exit")
    args = parser.parse_args()

    game = gameapi.Game(args.host, args.port, args.password)
    game.connect()
    print(f"[agent] connected rcon://{args.host}:{args.port}", flush=True)

    if args.probe:
        print("ping:  ", game.ping())
        print("status:", game.status())
        game.close()
        return

    agent = Agent(game, args.model, args.poll)
    if args.say:
        agent.handle(args.say)
        game.close()
        return
    agent.run_forever()
    game.close()


if __name__ == "__main__":
    main()
