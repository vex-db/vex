#!/usr/bin/env python3
"""Concurrent collection-map growth, expiry/retyping, and global enumeration."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import redis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--keys', type=int, default=384)
    args = parser.parse_args()
    assert args.workers > 0 and args.keys > 0
    prefix = 'partition-regression:' + uuid.uuid4().hex + ':'
    def client():
        return redis.Redis.from_url(args.target, socket_timeout=10, socket_connect_timeout=5)
    barrier = threading.Barrier(args.workers)
    stop = threading.Event()

    def enumerate_keys():
        c = client()
        try:
            while not stop.is_set():
                c.dbsize()
                list(c.scan_iter(match=prefix + '*', count=100))
        finally:
            c.close()

    def exercise(worker):
        c = client()
        c.select(worker % 2)
        keys = [f'{prefix}{worker}:{i}' for i in range(args.keys)]
        keys[-1] += ':long:' + 'x' * 600
        value = ('v:' + str(worker)).encode()
        try:
            barrier.wait(timeout=10)
            # Disjoint keys force top-level map growth while peers read/write.
            with c.pipeline(transaction=False) as p:
                for key in keys:
                    p.execute_command('hSeT', key + ':h', 'f', value)
                    p.execute_command('rPuSh', key + ':l', value)
                    p.execute_command('sAdD', key + ':s', value)
                assert p.execute() == [1] * (3 * len(keys))
            for _ in range(3):
                with c.pipeline(transaction=False) as p:
                    for key in keys:
                        p.hget(key + ':h', 'f')
                        p.lindex(key + ':l', 0)
                        p.sismember(key + ':s', value)
                    assert p.execute() == [value, value, True] * len(keys)
            # Expired hashes must be reclaimed under the new single-key list path.
            with c.pipeline(transaction=False) as p:
                for key in keys[:32]: p.pexpire(key + ':h', 1)
                assert all(p.execute())
            time.sleep(.03)
            with c.pipeline(transaction=False) as p:
                for key in keys[:32]: p.rpush(key + ':h', 'replacement')
                assert p.execute() == [1] * len(keys[:32])
            # Empty-key reclamation and subsequent cross-type recreation.
            with c.pipeline(transaction=False) as p:
                for key in keys:
                    p.lpop(key + ':l')
                    p.hset(key + ':l', 'f', value)
                    p.srem(key + ':s', value)
                    p.rpush(key + ':s', value)
                assert p.execute() == [value, 1, 1, 1] * len(keys)
            with c.pipeline(transaction=False) as p:
                for key in keys:
                    p.hget(key + ':l', 'f')
                    p.lindex(key + ':s', 0)
                assert p.execute() == [value, value] * len(keys)
        finally:
            with c.pipeline(transaction=False) as p:
                for key in keys: p.delete(key + ':h', key + ':l', key + ':s')
                p.execute()
            c.close()

    try:
        with ThreadPoolExecutor(max_workers=args.workers + 1) as pool:
            scanning = pool.submit(enumerate_keys)
            futures = [pool.submit(exercise, i) for i in range(args.workers)]
            try:
                for future in futures: future.result(timeout=120)
            finally:
                stop.set()
            scanning.result(timeout=15)
        c = client()
        try:
            assert not list(c.scan_iter(match=prefix + '*'))
        finally:
            c.close()
        print(f'PASS: {args.workers} concurrent clients, {args.workers * args.keys * 3} collection keys, growth/expiry/retyping/enumeration, DB 0/1 and long keys')
    finally:
        stop.set()


if __name__ == '__main__':
    main()
