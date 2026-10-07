#!/usr/bin/env python3
"""Minimal reproductions discovered while setting up the public correctness tools."""
import argparse
import sys
import uuid

import redis

from differential import snapshot


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target', required=True, help='Disposable server redis:// URL')
    o = p.parse_args()
    c = redis.Redis.from_url(o.target, protocol=2, socket_timeout=3, socket_connect_timeout=3)
    key = 'vex-correctness:regression:' + uuid.uuid4().hex
    failures = 0
    cases = [
        ('HSET new empty field: reply and stored state', [('HSET', key, 'a', b'')], 1, (b'hash', [(b'a', b'')])),
        ('EXPIRE zero removes the key type', [('INCRBY', key, 0), ('EXPIRE', key, 0)], True, (b'none', None)),
    ]
    try:
        for name, commands, expected_response, expected_state in cases:
            c.delete(key)
            actual = None
            for command in commands: actual = c.execute_command(*command)
            actual_state = snapshot(c, key)
            passed = actual == expected_response and actual_state == expected_state
            print('PASS' if passed else 'FAIL', name, repr(actual), repr(actual_state))
            failures += not passed
    finally:
        c.delete(key)
        c.close()
    return 1 if failures else 0


if __name__ == '__main__': sys.exit(main())
