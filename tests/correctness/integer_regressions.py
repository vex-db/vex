#!/usr/bin/env python3
"""Focused integer/expiry checks against a disposable Redis-compatible server."""
import argparse
import uuid

import redis


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target', required=True)
    args = p.parse_args()
    client = redis.Redis.from_url(args.target, socket_timeout=3)
    key = 'vex-correctness:integer:' + uuid.uuid4().hex
    maximum, minimum = 2**63 - 1, -(2**63)

    def rejects(command, value):
        client.set(key, value)
        try:
            client.execute_command(*command)
        except redis.ResponseError:
            pass
        else:
            raise AssertionError(f'Expected error: {command}')
        assert client.get(key) == str(value).encode(), command

    try:
        for command, value in [
            (('INCR', key), maximum),
            (('DECR', key), minimum),
            (('INCRBY', key, 1), maximum),
            (('DECRBY', key, -1), maximum),
            (('DECRBY', key, minimum), 0),
            (('INCR', key, 'extra'), 7),
            (('DECR', key, 'extra'), 7),
            (('INCRBY', key, 1, 'extra'), 7),
            (('DECRBY', key, 1, 'extra'), 7),
        ]:
            rejects(command, value)
        # Repeat against an integer cached by INCR, not just a SET string.
        client.set(key, maximum - 1)
        assert client.incr(key) == maximum
        try:
            client.incr(key)
        except redis.ResponseError:
            pass
        else:
            raise AssertionError('Cached integer overflow accepted')
        assert client.get(key) == str(maximum).encode()
        for command in ('INCR', 'DECR', 'INCRBY', 'DECRBY'):
            client.set(key, 41, px=60000)
            argv = (command, key) if command in ('INCR', 'DECR') else (command, key, 1)
            client.execute_command(*argv)
            assert 0 < client.pttl(key) <= 60000, command
        for command in ('EXPIRE', 'PEXPIRE'):
            for ttl in (0, -1):
                client.set(key, 1)
                assert client.execute_command(command, key, ttl) == 1
                assert client.get(key) is None
                assert client.type(key) == b'none'
                assert not client.exists(key)
                assert client.execute_command(command, key, ttl) == 0
        print('PASS integer boundaries, rejected-command immutability, TTL preservation, immediate expiry')
    finally:
        client.delete(key)
        client.close()


if __name__ == '__main__':
    main()
