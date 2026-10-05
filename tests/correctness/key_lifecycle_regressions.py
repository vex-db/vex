#!/usr/bin/env python3
"""Cross-store command contracts and bounded cross-type races on a disposable server."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import redis


def get_path_regressions(url, prefix):
    """Exercise hit, miss and wrong-type GET routing, including EXEC."""
    for db in (0, 1):
        client = redis.Redis.from_url(url, socket_timeout=5)
        client.execute_command('SELECT', db)
        for suffix in ('short', 'x' * 600):
            key = prefix + ':' + suffix
            try:
                for value in (b'', b'123', b'v' * 256, b'\x00\xff'):
                    client.set(key, value)
                    assert client.execute_command('gEt', key) == value
                    with client.pipeline() as tx:
                        tx.get(key)
                        assert tx.execute() == [value]
                client.delete(key)
                client.incr(key)
                assert client.get(key) == b'1'
                for seed in (('SET', key, '7'), ('HSET', key, 'f', '7'),
                             ('RPUSH', key, '7'), ('SADD', key, '7'),
                             ('ZADD', key, '7', 'm')):
                    client.delete(key)
                    client.execute_command(*seed)
                    if seed[0] != 'SET':
                        try:
                            client.get(key)
                        except redis.ResponseError as exc:
                            assert str(exc).startswith('WRONGTYPE')
                        else:
                            raise AssertionError('GET accepted a collection')
                    client.pexpire(key, 10)
                    time.sleep(.03)
                    # GET itself must handle expiry; do not precede it with EXISTS.
                    assert client.get(key) is None
                    client.set(key, 'replacement')
                    assert client.get(key) == b'replacement'
            finally:
                client.delete(key)
        client.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target', required=True)
    p.add_argument('--rounds', type=int, default=50)
    o = p.parse_args()
    if o.rounds < 1:
        p.error('--rounds must be positive')
    c = redis.Redis.from_url(o.target, socket_timeout=5, socket_connect_timeout=5)
    peer = redis.Redis.from_url(o.target, socket_timeout=5, socket_connect_timeout=5)
    key = 'vex-correctness:lifecycle:' + uuid.uuid4().hex
    seeds = {
        b'string': ('SET', key, '7'), b'hash': ('HSET', key, 'f', '7'),
        b'list': ('RPUSH', key, '7'), b'set': ('SADD', key, '7'),
        b'zset': ('ZADD', key, '7', 'm'),
    }
    writes = {k: v for k, v in seeds.items() if k != b'string'}

    def command(client, args):
        try:
            return client.execute_command(*args)
        except redis.ResponseError as exc:
            if str(exc).startswith('WRONGTYPE'):
                return 'WRONGTYPE'
            raise

    def absent():
        assert c.type(key) == b'none'
        assert c.exists(key) == 0
        assert c.pttl(key) == -2
        assert c.hlen(key) == c.llen(key) == c.scard(key) == c.zcard(key) == 0

    try:
        get_path_regressions(o.target, key + ':get')
        for kind, seed in seeds.items():
            c.delete(key)
            c.execute_command(*seed)
            assert c.type(key) == kind
            assert c.exists(key, key) == 2
            assert c.pttl(key) == -1
            assert c.set(key, '9', nx=True) is None
            if kind != b'string':
                assert command(c, ('GET', key)) == 'WRONGTYPE'
                assert command(c, ('INCR', key)) == 'WRONGTYPE'
            for wanted, write in writes.items():
                if wanted != kind:
                    assert command(c, write) == 'WRONGTYPE', (kind, write)
            assert c.pexpire(key, 60000)
            assert 0 < c.pttl(key) <= 60000
            assert c.persist(key)
            assert not c.persist(key)
            assert c.delete(key, key) == 1
            absent()
            c.execute_command(*seed)
            assert c.pexpire(key, 0)
            absent()
            # Verify actual timed expiry on reactor dispatch, not just metadata.
            c.execute_command(*seed)
            assert c.pexpire(key, 10)
            deadline = time.monotonic() + 2
            while c.exists(key) and time.monotonic() < deadline:
                time.sleep(.005)
            absent()
            c.execute_command(*seed)
            assert c.pttl(key) == -1
            assert c.set(key, 'replacement', xx=True)
            assert c.get(key) == b'replacement'
            assert c.delete(key) == 1
            absent()

        # Read-only commands must not invalidate WATCH after fallback routing.
        c.set(key, '7')
        with c.pipeline() as tx:
            tx.watch(key)
            assert peer.type(key) == b'string'
            assert peer.exists(key) == 1
            tx.multi()
            tx.get(key)
            assert tx.execute() == [b'7']
        c.delete(key)
        c.hset(key, 'f', '1')
        with c.pipeline() as tx:
            tx.watch(key)
            peer.hset(key, 'f', '2')
            tx.multi()
            tx.hget(key, 'f')
            try:
                tx.execute()
            except redis.WatchError:
                pass
            else:
                raise AssertionError('HSET did not invalidate WATCH')

        # Either ordering has a known legal result: SET wins; the collection
        # write either happened first or returned WRONGTYPE. No ghost may remain.
        histories = 0
        with ThreadPoolExecutor(max_workers=2) as pool:
            for kind, write in writes.items():
                for _ in range(o.rounds):
                    c.delete(key)
                    gate = threading.Barrier(2)

                    def race(client, args):
                        gate.wait(timeout=5)
                        return command(client, args)

                    first = pool.submit(race, c, ('SET', key, '9'))
                    second = pool.submit(race, peer, write)
                    assert first.result(timeout=10) is True
                    assert second.result(timeout=10) in (1, 'WRONGTYPE')
                    assert c.type(key) == b'string'
                    assert c.get(key) == b'9'
                    assert c.delete(key) == 1
                    absent()
                    assert c.execute_command(*write) == 1
                    assert c.type(key) == kind
                    histories += 1
        print(f'PASS all five key types, expiry, WATCH, and {histories} cross-type races')
    finally:
        c.delete(key)
        c.close()
        peer.close()


if __name__ == '__main__':
    main()
