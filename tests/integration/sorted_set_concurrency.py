#!/usr/bin/env python3
"""Destructive sorted-set stress check; run against a disposable Vex server."""
import argparse
import concurrent.futures
import socket
import threading


def frame(*args):
    parts = [str(x).encode() for x in args]
    return b'*%d\r\n' % len(parts) + b''.join(b'$%d\r\n%s\r\n' % (len(x), x) for x in parts)


class Client:
    def __init__(self, address):
        self.socket = socket.create_connection(address, 30)
        self.file = self.socket.makefile('rb')

    def read(self):
        line = self.file.readline()
        assert line.endswith(b'\r\n'), line
        kind, body = line[:1], line[1:-2]
        if kind == b'-':
            raise AssertionError(body)
        if kind == b'+':
            return body
        if kind == b':':
            return int(body)
        if kind == b'$':
            n = int(body)
            if n == -1:
                return None
            result = self.file.read(n)
            assert self.file.read(2) == b'\r\n'
            return result
        if kind == b'*':
            return None if int(body) == -1 else [self.read() for _ in range(int(body))]
        raise AssertionError(line)

    def call(self, *args):
        self.socket.sendall(frame(*args))
        return self.read()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.file.close()
        self.socket.close()


def run(address):
    with Client(address) as c:
        assert c.call('FLUSHALL') == b'OK'

    def update(worker):
        with Client(address) as c:
            for batch in range(100):
                c.socket.sendall(b''.join(frame('ZINCRBY', f'z:{i % 64}', 1, f'm:{worker}') for i in range(64)))
                for _ in range(64):
                    assert float(c.read()) == batch + 1
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        list(pool.map(update, range(8)))
    with Client(address) as c:
        for i in range(64):
            assert c.call('ZCARD', f'z:{i}') == 8
            assert c.call('ZRANGE', f'z:{i}', 0, -1) == [f'm:{j}'.encode() for j in range(8)]
            for j in range(8):
                assert c.call('ZRANK', f'z:{i}', f'm:{j}') == j
                assert float(c.call('ZSCORE', f'z:{i}', f'm:{j}')) == 100
        assert c.call('DEL', 'z:0', 'z:1', 'z:0') == 2
        assert c.call('ZCARD', 'z:0') == 0
        long_key = 'long:' + 'x' * 1024
        assert c.call('ZADD', long_key, 1, 'm') == 1
        assert c.call('ZRANK', long_key, 'm') == 0
        assert c.call('DEL', long_key) == 1

    # EXEC must exclude ordinary sorted commands across different partitions.
    def transact(worker):
        with Client(address) as c:
            for _ in range(200):
                assert c.call('MULTI') == b'OK'
                assert c.call('ZINCRBY', 'tx:a', 1, 'm') == b'QUEUED'
                assert c.call('ZINCRBY', 'tx:b', 1, 'm') == b'QUEUED'
                assert c.call('ZRANK', 'tx:a', 'm') == b'QUEUED'
                result = c.call('EXEC')
                assert result[0] == result[1] and result[2] == 0, result
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        list(pool.map(transact, range(8)))
    # Ordinary reads must not observe an intermediate transaction value.
    with Client(address) as c:
        c.call('ZADD', 'atomic', 0, 'm')
    barrier = threading.Barrier(5)
    def observe(worker):
        with Client(address) as c:
            barrier.wait()
            for _ in range(500):
                if worker == 0:
                    c.socket.sendall(frame('MULTI') + frame('ZINCRBY', 'atomic', 1, 'm') + frame('ZINCRBY', 'atomic', -1, 'm') + frame('EXEC'))
                    assert [c.read() for _ in range(4)] == [b'OK', b'QUEUED', b'QUEUED', [b'1.000000', b'0.000000']]
                else:
                    assert float(c.call('ZSCORE', 'atomic', 'm')) == 0
    with concurrent.futures.ThreadPoolExecutor(5) as pool:
        list(pool.map(observe, range(5)))

    # Exercise map growth, not just updates of pre-existing member nodes.
    def grow(worker):
        with Client(address) as c:
            for i in range(500):
                key = f'grow:{worker}:{i}'
                assert c.call('ZADD', key, 1, 'm') == 1
                assert c.call('ZRANK', key, 'm') == 0
                if i % 2:
                    assert c.call('DEL', key) == 1
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        list(pool.map(grow, range(8)))

    with Client(address) as watcher, Client(address) as writer:
        for mutation in [('ZINCRBY', 'watched', 1, 'm'), ('DEL', 'watched'), ('FLUSHALL',)]:
            assert watcher.call('ZADD', 'watched', 1, 'm') in (0, 1)
            assert watcher.call('WATCH', 'watched') == b'OK'
            writer.call(*mutation)
            assert watcher.call('MULTI') == b'OK'
            assert watcher.call('ZCARD', 'watched') == b'QUEUED'
            assert watcher.call('EXEC') is None

    # Range bytes must remain valid while other workers delete/recreate/flush.
    def lifecycle(worker):
        with Client(address) as c:
            for i in range(300):
                key = f'race:{i % 8}'
                c.call('ZADD', key, 1, 'a', 2, 'b', 3, 'c')
                result = c.call('ZRANGE', key, 0, -1, 'WITHSCORES')
                assert result in ([], [b'a', b'1.000000', b'b', b'2.000000', b'c', b'3.000000']), result
                if worker == 0 and i % 7 == 0:
                    c.call('FLUSHALL')
                elif i % 3 == 0:
                    c.call('DEL', key)
                else:
                    assert c.call('ZREM', key, 'a', 'b', 'c') in (0, 3)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        list(pool.map(lifecycle, range(8)))
    with Client(address) as c:
        c.call('MULTI')
        c.call('FLUSHALL')
        c.call('ZADD', 'after', 9, 'm')
        assert c.call('EXEC') == [b'OK', 1]
        assert c.call('ZRANK', 'after', 'm') == 0
        c.call('FLUSHALL')
    print('PASS: concurrent updates, ranks, transactions, WATCH, ranges, deletion, flush')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=6380)
    args = parser.parse_args()
    run((args.host, args.port))
