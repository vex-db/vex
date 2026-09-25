#!/usr/bin/env python3
"""Focused idle-expiry and WATCH/EXEC regression; run against a reactor server."""
import argparse
import socket
import time
import uuid

p = argparse.ArgumentParser()
p.add_argument("--host", default="127.0.0.1")
p.add_argument("--port", type=int, default=6380)
o = p.parse_args()
prefix = "expiry-reclamation:" + uuid.uuid4().hex + ":"


class Client:
    def __init__(self):
        self.sock = socket.create_connection((o.host, o.port), 10)
        self.file = self.sock.makefile("rb")

    def call(self, *parts):
        parts = [x if isinstance(x, bytes) else str(x).encode() for x in parts]
        self.sock.sendall(b"*" + str(len(parts)).encode() + b"\r\n" + b"".join(b"$" + str(len(x)).encode() + b"\r\n" + x + b"\r\n" for x in parts))
        return self.read()

    def read(self):
        line = self.file.readline(); kind = line[:1]
        if kind == b"-": raise AssertionError(line)
        if kind == b"+": return line[1:-2]
        if kind == b":": return int(line[1:-2])
        if kind == b"$":
            n = int(line[1:-2])
            if n < 0: return None
            value = self.file.read(n); assert self.file.read(2) == b"\r\n"; return value
        if kind == b"*":
            n = int(line[1:-2])
            return None if n < 0 else [self.read() for _ in range(n)]
        raise AssertionError(line)


key = prefix + "idle"
c = Client()
assert c.call("SET", key, b"x" * 256, "PX", 100) == b"OK"
time.sleep(0.5)  # No requests while the maintenance thread owns expiry cleanup.
assert c.call("GET", key) is None
assert c.call("DBSIZE") == 0

watcher = Client()
assert c.call("SET", key, "v", "PX", 100) == b"OK"
assert watcher.call("WATCH", key) == b"OK"
assert watcher.call("MULTI") == b"OK"
assert watcher.call("GET", key) == b"QUEUED"
time.sleep(0.5)
assert watcher.call("EXEC") is None

transaction = prefix + "transaction"
assert c.call("SET", transaction, "ok") == b"OK"
normal = Client()
assert normal.call("MULTI") == b"OK"
assert normal.call("GET", transaction) == b"QUEUED"
assert normal.call("EXEC") == [b"ok"]
assert normal.call("WATCH", transaction) == b"OK"
assert normal.call("UNWATCH") == b"OK"
assert normal.call("MULTI") == b"OK"
assert normal.call("GET", transaction) == b"QUEUED"
assert normal.call("DISCARD") == b"OK"

refresh = prefix + "refresh"
assert c.call("SET", refresh, "old", "PX", 100) == b"OK"
assert c.call("SET", refresh, "live") == b"OK"
time.sleep(0.5)
assert c.call("GET", refresh) == b"live"
c.call("DEL", refresh)
c.call("DEL", key)
c.call("DEL", transaction)
print("PASS: idle PX expiry is reclaimed, transactions and WATCH work, and refreshed values survive")
