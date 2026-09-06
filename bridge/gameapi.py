"""High-level FactorioCompanion remote-interface ops over RCON."""
import json
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rcon import Rcon, RconError

IFACE = "copilot"


class GameError(Exception):
    pass


def lua_str(text):
    out = (
        str(text)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return '"' + out + '"'


class Game:
    def __init__(self, host="127.0.0.1", port=34973, password=None, timeout=6.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.password = password
        self.rcon = None

    def connect(self):
        candidates = [self.password] if self.password else ["copilot-local-pass", "testpass123"]
        last_error = None
        for pw in candidates:
            client = Rcon(self.host, self.port, pw or "", self.timeout)
            try:
                client.connect()
            except RconError as exc:
                last_error = exc
                continue
            self.rcon = client
            self.password = pw
            return self
        raise GameError(f"rcon connect failed: {last_error}")

    def close(self):
        if self.rcon:
            self.rcon.close()
            self.rcon = None

    @staticmethod
    def _lit(value):
        if value is None:
            return "nil"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, dict):
            return lua_str(json.dumps(value))
        return lua_str(value)

    def _call(self, fn, *args, collect_ms=1200, retries=2):
        bits = [lua_str(IFACE), lua_str(fn)] + [self._lit(a) for a in args]
        cmd = f"/silent-command rcon.print(remote.call({', '.join(bits)}))"
        last_error = None
        for attempt in range(retries + 1):
            raw = self.rcon.send_raw(cmd, collect_ms=collect_ms / 1000.0).strip()
            if not raw:
                last_error = GameError(f"empty response from {fn}")
                time.sleep(0.4)
                continue
            try:
                envelope = json.loads(raw)
            except ValueError:
                raise GameError(f"non-json response from {fn}: {raw[:200]!r}")
            if envelope.get("ok") is not True:
                raise GameError(f"{fn}: {envelope.get('error', envelope)}")
            return envelope
        raise last_error

    # -- chat / status -----------------------------------------------------

    def ping(self):
        return self._call("ping")["data"]

    def status(self):
        return self._call("status")["data"]

    def drain_outbox(self, limit=5):
        return self._call("drain_outbox", limit, collect_ms=400)["data"]["messages"]

    def set_busy(self, busy):
        return self._call("set_agent_busy", "true" if busy else "false", collect_ms=400)

    def stream_start(self):
        return self._call("stream_start", collect_ms=400)

    def stream_append(self, chunk):
        return self._call("stream_append", chunk, collect_ms=300)

    def stream_end(self, final_text=None):
        return self._call("stream_end", final_text if final_text else "", collect_ms=500)

    def deliver(self, text):
        return self._call("deliver_agent_message", text)

    # -- chunked queries ---------------------------------------------------

    def query(self, fn, *args):
        envelope = self._call(fn, *args)
        if envelope.get("inline"):
            return json.loads(envelope["inline"])
        qid = envelope["query"]
        total = envelope["total_chunks"]
        chunks = []
        for index in range(1, total + 1):
            piece = self._call("fetch_chunk", qid, index, collect_ms=500)
            if piece.get("done"):
                break
            chunks.append(piece["chunk"])
        return json.loads("".join(chunks))

    def player_state(self):
        return self.query("player_state")

    def research(self):
        return self.query("research")

    def overview(self):
        return self.query("overview")

    def find(self, spec):
        return self.query("find", spec)


if __name__ == "__main__":
    game = Game().connect()
    print("ping:", game.ping())
    print("status:", game.status())
    game.close()
