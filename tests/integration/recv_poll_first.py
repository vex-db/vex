#!/usr/bin/env python3
"""Exercise the TCP receive path with only Python's standard library.

Usage: python3 tests/integration/recv_poll_first.py --host 127.0.0.1 --port 6380
"""
import argparse
import socket
import sys
import time
import uuid


parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=6380)
options = parser.parse_args()
prefix = "recv-poll-first:" + uuid.uuid4().hex + ":"


def frame(*args):
    encoded = [arg if isinstance(arg, bytes) else str(arg).encode() for arg in args]
    return b"*" + str(len(encoded)).encode() + b"\r\n" + b"".join(
        b"$" + str(len(arg)).encode() + b"\r\n" + arg + b"\r\n" for arg in encoded
    )


class Client:
    def __init__(self):
        self.socket = socket.create_connection((options.host, options.port), timeout=15)
        self.file = self.socket.makefile("rb")

    def read(self):
        line = self.file.readline()
        if not line:
            raise AssertionError("server closed before reply")
        kind = line[:1]
        body = line[1:-2]
        if kind == b"-":
            raise AssertionError(body.decode(errors="replace"))
        if kind == b":":
            return int(body)
        if kind == b"+":
            return body
        if kind == b"$":
            size = int(body)
            if size < 0:
                return None
            value = self.file.read(size)
            if self.file.read(2) != b"\r\n":
                raise AssertionError("malformed bulk reply")
            return value
        if kind == b"*":
            return [self.read() for _ in range(int(body))]
        raise AssertionError(f"unexpected RESP type: {line!r}")

    def call(self, *args):
        self.socket.sendall(frame(*args))
        return self.read()

    def close(self):
        self.file.close()
        self.socket.close()


key = prefix + "marker"
binary_key = prefix + "binary"
set_keys = []
client = Client()
try:
    # Idle worker wakeup: let the connection reach its receive wait first.
    time.sleep(0.05)
    assert client.call("PING") == b"PONG"
    # The first completion can rearm poll-first after reporting an empty
    # socket; wake that receive from an idle interval as well.
    time.sleep(0.05)
    assert client.call("PING") == b"PONG"

    # Fragmented RESP request: the second half completes a single command.
    fragmented = frame("ECHO", "fragmented")
    split = len(fragmented) // 2
    client.socket.sendall(fragmented[:split])
    time.sleep(0.02)
    client.socket.sendall(fragmented[split:])
    assert client.read() == b"fragmented"

    assert client.call("SET", key, "value") == b"OK"
    set_keys.append(key)
    assert client.call("GET", key) == b"value"

    binary_value = b"\x00vex\r\n\xff\x00payload"
    assert client.call("SET", binary_key, binary_value) == b"OK"
    set_keys.append(binary_key)
    assert client.call("GET", binary_key) == binary_value

    # One write larger than the server's 64 KiB receive buffer. Verify every
    # reply in order so a lost or duplicated recv completion cannot pass.
    burst = [f"burst-{i:04d}" for i in range(5000)]
    client.socket.sendall(b"".join(frame("ECHO", item) for item in burst))
    for item in burst:
        assert client.read() == item.encode(), item
finally:
    client.close()

    # Close and reconnect repeatedly, cleaning up isolated keys from the first
    # replacement connection. Keep retries bounded so a dead server fails quickly.
    had_error = sys.exc_info()[0] is not None
    cleanup_error = None
    for attempt in range(10):
        reconnected = None
        try:
            reconnected = Client()
            assert reconnected.call("PING") == b"PONG"
            if attempt == 0 and set_keys:
                # The worker's concurrent-KV fast path handles one DEL key;
                # issue separate commands so cleanup is valid on that path.
                for cleanup_key in set_keys:
                    assert reconnected.call("DEL", cleanup_key) == 1
        except Exception as error:
            cleanup_error = error
        finally:
            if reconnected is not None:
                reconnected.close()
    if cleanup_error is not None and not had_error:
        raise cleanup_error

print("PASS: idle wakeup, fragmented request, binary payload, >64 KiB ordered burst, 10 reconnects")
