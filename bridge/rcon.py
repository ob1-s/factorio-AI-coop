"""Minimal, dependency-free Factorio RCON client."""
import socket
import struct
import time


class RconError(Exception):
    pass


SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0
SERVERDATA_AUTH_RESPONSE = 2


def _pkt(req_id: int, ptype: int, payload: bytes) -> bytes:
    body = struct.pack("<ii", req_id, ptype) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


class Rcon:
    def __init__(self, host="127.0.0.1", port=27015, password="", timeout=5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock = None
        self._id = 0x4F58

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.sock.sendall(_pkt(self._id, SERVERDATA_AUTH, self.password.encode("utf-8")))
        # Factorio may send an empty RESPONSE_VALUE before AUTH_RESPONSE
        deadline = time.time() + self.timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RconError("auth timeout")
            pkt = self._recv_pkt(max(0.05, remaining))
            if pkt is None:
                continue
            rid, ptype = pkt[0], pkt[1]
            if ptype == SERVERDATA_AUTH_RESPONSE:
                if rid == -1:
                    raise RconError("auth failed: bad password")
                return True

    def _recv_exact(self, n, timeout=None):
        self.sock.settimeout(timeout if timeout is not None else self.timeout)
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed")
            buf += chunk
        return buf

    def _recv_pkt(self, timeout=None):
        try:
            hdr = self._recv_exact(4, timeout)
        except socket.timeout:
            return None
        (length,) = struct.unpack("<i", hdr)
        if length <= 0 or length > 10 * 1024 * 1024:
            raise RconError(f"bad packet length {length}")
        body = self._recv_exact(length, timeout)
        rid, ptype = struct.unpack("<ii", body[:8])
        payload = body[8:-2]
        return (rid, ptype, payload)

    def send_raw(self, command: str, collect_ms: float = 0.6):
        """Send command, return concatenated response payloads."""
        self._id += 1
        self.sock.sendall(_pkt(self._id, SERVERDATA_EXECCOMMAND, command.encode("utf-8")))
        out = []
        deadline = time.time() + collect_ms
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                pkt = self._recv_pkt(timeout=max(0.01, remaining))
            except socket.timeout:
                break
            if pkt is None:
                continue
            rid, ptype, payload = pkt
            out.append(payload.decode("utf-8", "replace"))
            deadline = max(deadline, time.time() + 0.05)
        return "".join(out)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.close()


if __name__ == "__main__":
    import sys
    host, port, pw = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    cmd = sys.argv[4] if len(sys.argv) > 4 else '/silent-command print("hello from ox")'
    with Rcon(host, port, pw) as r:
        print(repr(r.send_raw(cmd)))
