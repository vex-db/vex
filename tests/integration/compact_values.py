#!/usr/bin/env python3
"""Run against an isolated reactor test server; requires only Python's stdlib.
Usage: python3 tests/integration/compact_values.py --host 127.0.0.1 --port 6380
"""
import argparse, concurrent.futures, socket, uuid
parser = argparse.ArgumentParser()
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--port', type=int, default=6380)
options = parser.parse_args()
prefix = 'compact-test:' + uuid.uuid4().hex + ':'

def key(name):
    return prefix + name

class Client:

    def __init__(self):
        self.socket = socket.create_connection((options.host, options.port), timeout=15)
        self.file = self.socket.makefile('rb')

    def call(self, *args):
        data = [a if isinstance(a, bytes) else str(a).encode() for a in args]
        self.socket.sendall(b'*' + str(len(data)).encode() + b'\r\n' + b''.join((b'$' + str(len(a)).encode() + b'\r\n' + a + b'\r\n' for a in data)))
        return self.read()

    def read(self):
        line = self.file.readline()
        kind = line[:1]
        body = line[1:-2]
        if kind == b'-':
            raise AssertionError(body)
        if kind == b':':
            return int(body)
        if kind == b'+':
            return body
        if kind == b'$':
            n = int(body)
            if n < 0:
                return None
            data = self.file.read(n)
            assert self.file.read(2) == b'\r\n'
            return data
        if kind == b'*':
            return [self.read() for _ in range(int(body))]
        raise AssertionError(line)

    def close(self):
        self.file.close()
        self.socket.close()

def stress(i):
    c = Client()
    try:
        for j in range(1000):
            n = (16, 32, 33, 128, 256, 257, 768, 1024)[j % 8]
            assert c.call('SET', key('contended'), bytes([65 + i]) * n) == b'OK'
            data = c.call('GET', key('contended'))
            assert data and len(data) in (16, 32, 33, 128, 256, 257, 768, 1024)
            assert data == data[:1] * len(data), 'torn value'
    finally:
        c.close()
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    list(pool.map(stress, range(8)))
c = Client()
for n in (32, 33, 256, 257, 1024, 4096, 4097, 65536, 768, 16):
    value = bytes((j % 256 for j in range(n)))
    assert c.call('SET', key('src'), value) == b'OK'
    assert c.call('SET', key('dst'), value) == b'OK'
    assert c.call('GET', key('src')) == value
    if n <= 1024:
        assert c.call('SCANGET', 0, key('src')) == [b'0', [key('src').encode(), value]]
    assert c.call('MGET', key('src'), key('dst')) == [value, value]
    assert c.call('DEL', key('dst')) == 1
assert c.call('SET', key('integer'), '42') == b'OK'
assert c.call('INCR', key('integer')) == 43
assert c.call('GET', key('integer')) == b'43'
assert c.call('SET', key('expiry'), 'x' * 256, 'PX', 10000) == b'OK'
assert c.call('TTL', key('expiry')) > 0
assert c.call('SET', key('expiry'), 'x' * 32) == b'OK'
assert c.call('TTL', key('expiry')) == -1
c.call('DEL', *(key(k) for k in ('contended', 'src', 'dst', 'integer', 'expiry')))
c.close()
print('PASS: 8 concurrent clients × 1000 varying-size updates; GET/MGET/DEL/INCR/TTL; binary values intact')
