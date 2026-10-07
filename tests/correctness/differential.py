#!/usr/bin/env python3
"""Hypothesis state-machine comparison of a disposable Vex and Redis server."""
import argparse
import json
from pathlib import Path
import socket
import sys
import traceback
import uuid

from hypothesis import HealthCheck, seed, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
import redis


def packed(value):
    if isinstance(value, bytes):
        return {'hex': value.hex()}
    if isinstance(value, (list, tuple)):
        return [packed(v) for v in value]
    if isinstance(value, dict):
        return {str(k): packed(v) for k, v in value.items()}
    return value


def response(client, command):
    try:
        return ('ok', client.execute_command(*command))
    except redis.ResponseError as e:
        text = str(e)
        # Wording differs between implementations; compare error class, not exact prose.
        if 'unknown command' in text.lower() or 'unsupported command' in text.lower():
            return ('unsupported', text)
        return ('error', type(e).__name__, 'WRONGTYPE' if text.startswith('WRONGTYPE') else 'ERR')


def snapshot(client, key):
    kind = client.type(key)
    if kind == b'none': return (kind, None)
    if kind == b'string': return (kind, client.get(key))
    if kind == b'list': return (kind, client.lrange(key, 0, -1))
    if kind == b'hash': return (kind, sorted(client.hgetall(key).items()))
    if kind == b'set': return (kind, sorted(client.smembers(key)))
    if kind == b'zset': return (kind, client.zrange(key, 0, -1, withscores=True))
    raise AssertionError(f'Unexpected key type {kind!r}')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target', required=True, help='Disposable Vex redis:// URL')
    p.add_argument('--reference', required=True, help='Separate disposable Redis redis:// URL')
    p.add_argument('--seed', type=int, default=20260929)
    p.add_argument('--examples', type=int, default=100)
    p.add_argument('--steps', type=int, default=100)
    p.add_argument('--output', type=Path, required=True)
    o = p.parse_args()
    if o.examples < 1 or o.steps < 1: p.error('examples and steps must be positive')
    clients = [redis.Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3,
               decode_responses=False, protocol=2, single_connection_client=True)
               for url in (o.target, o.reference)]
    identities = []
    for client in clients:
        k = client.connection_pool.connection_kwargs
        addresses = {item[4][0] for item in socket.getaddrinfo(k['host'], k['port'])}
        identities.append((addresses, k['port'], k.get('db', 0)))
    if identities[0][0] & identities[1][0] and identities[0][1:] == identities[1][1:]:
        raise SystemExit('Target and reference must be different databases.')
    o.output.mkdir(parents=True, exist_ok=False)
    prefix = 'vex-correctness:' + uuid.uuid4().hex + ':'
    keys = [prefix + str(i) for i in range(6)]
    counts = {'actions': 0, 'examples_started': 0}
    for client in clients: client.ping()

    class Compare(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.trace = []
            counts['examples_started'] += 1
            for client in clients: client.delete(*keys)

        def check(self, command):
            counts['actions'] += 1
            a, b = [response(c, command) for c in clients]
            self.trace.append(dict(command=packed(command), target=packed(a), reference=packed(b)))
            self.require(a == b and a[0] != 'unsupported', f'Response mismatch: {command!r}: {a!r} != {b!r}')

        def require(self, condition, message):
            if not condition:
                (o.output / 'failure-trace.json').write_text(json.dumps(self.trace, indent=2) + '\n')
                raise AssertionError(message)

        @rule(index=st.integers(0, 5), value=st.binary(max_size=64),
              op=st.sampled_from(['SET', 'GET', 'DEL', 'APPEND', 'EXISTS', 'STRLEN', 'SETNX', 'GETSET', 'GETDEL']))
        def strings(self, index, value, op):
            args = [op, keys[index]]
            if op in ('SET', 'APPEND', 'SETNX', 'GETSET'): args.append(value)
            self.check(args)

        @rule(index=st.integers(0, 5), delta=st.integers(-100, 100))
        def increment(self, index, delta):
            self.check(['INCRBY', keys[index], delta])

        @rule(index=st.integers(0, 5), value=st.binary(max_size=32), flag=st.sampled_from(['NX', 'XX']))
        def conditional_set(self, index, value, flag):
            self.check(['SET', keys[index], value, flag])

        @rule(index=st.integers(0, 5), member=st.sampled_from([b'a', b'b', b'', b'\x00\xff']),
              value=st.binary(max_size=32), number=st.integers(-20, 20),
              op=st.sampled_from(['HSET', 'HDEL', 'SADD', 'SREM', 'LPUSH', 'RPUSH', 'LPOP',
                                  'RPOP', 'ZADD', 'ZREM', 'ZINCRBY', 'ZRANK', 'ZSCORE']))
        def collections(self, index, member, value, number, op):
            args = [op, keys[index]]
            if op == 'HSET': args += [member, value]
            elif op in ('LPUSH', 'RPUSH'): args += [value]
            elif op in ('ZADD', 'ZINCRBY'): args += [number, member]
            elif op not in ('LPOP', 'RPOP'): args += [member]
            # Normalize score formatting ("1" and "1.000000" are the same score).
            if op in ('ZINCRBY', 'ZSCORE'):
                counts['actions'] += 1
                answers = [response(c, args) for c in clients]
                answers = [(x[0], float(x[1])) if x[0] == 'ok' and x[1] is not None else x for x in answers]
                self.trace.append(dict(command=packed(args), target=packed(answers[0]), reference=packed(answers[1])))
                self.require(answers[0] == answers[1] and answers[0][0] != 'unsupported', f'Score mismatch: {args!r}: {answers!r}')
            else:
                self.check(args)

        @rule(index=st.integers(0, 5), op=st.sampled_from(['EXPIRE', 'PEXPIRE', 'PERSIST', 'TTL', 'PTTL']))
        def expiry(self, index, op):
            # Immediate expiry and no-TTL sentinels avoid wall-clock timing comparisons.
            self.check([op, keys[index]] + ([0] if op in ('EXPIRE', 'PEXPIRE') else []))

        @invariant()
        def same_state(self):
            for key in keys:
                a, b = [snapshot(c, key) for c in clients]
                self.require(a == b, f'State mismatch for {key}: {a!r} != {b!r}')

        def teardown(self):
            for client in clients: client.delete(*keys)

    result = dict(seed=o.seed, examples=o.examples, steps=o.steps, key_prefix=prefix,
                  targets=[{'host': c.connection_pool.connection_kwargs['host'],
                            'port': c.connection_pool.connection_kwargs['port']} for c in clients])
    try:
        run_state_machine_as_test(seed(o.seed)(Compare), settings=settings(max_examples=o.examples,
            stateful_step_count=o.steps, deadline=None, database=None,
            suppress_health_check=[HealthCheck.too_slow]))
        result['status'] = 'passed'
    except Exception:
        result['status'] = 'failed'
        (o.output / 'failure.txt').write_text(traceback.format_exc())
        traceback.print_exc()
    finally:
        result.update(counts)
        (o.output / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
        for client in clients: client.close()
    print(result['status'], counts)
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
