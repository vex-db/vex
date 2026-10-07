#!/usr/bin/env python3
"""Disposable-server correctness checks for the opt-in fixed-owner prototype."""
import argparse
import concurrent.futures
import contextlib
import os
import pathlib
import socket
import subprocess
import tempfile
import time

from sorted_set_concurrency import Client, frame, run


@contextlib.contextmanager
def server(binary, key, workers=4, password=None, adaptive=False, log_path=None):
    with tempfile.TemporaryDirectory(prefix='vex-owner-') as directory, tempfile.TemporaryFile() as log:
        env = dict(os.environ, VEX_CONFIG='', VEX_PIN_WORKERS='0')
        env.pop('VEX_EXPERIMENTAL_OWNER_KEY', None)
        env.pop('VEX_EXPERIMENTAL_ADAPTIVE_OWNER', None)
        if adaptive:
            env['VEX_EXPERIMENTAL_ADAPTIVE_OWNER'] = '1'
        if key is not None:
            env['VEX_EXPERIMENTAL_OWNER_KEY'] = key
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        command = [str(pathlib.Path(binary).resolve()), '--reactor', '--workers', str(workers),
                   '--host', '127.0.0.1', '--port', str(port), '--no-persistence']
        if password is not None:
            command += ['--requirepass', password]
        process = subprocess.Popen(command,
                                   cwd=directory, env=env, stdout=log, stderr=log)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read().decode())
                try:
                    with Client(('127.0.0.1', port)) as c:
                        assert c.call('PING') == b'PONG'
                    break
                except OSError:
                    time.sleep(.05)
            else:
                raise TimeoutError('server startup')
            yield ('127.0.0.1', port), process
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if log_path is not None:
                log.seek(0)
                pathlib.Path(log_path).write_bytes(log.read())


def check_pipeline(address, key='hot'):
    def client(index):
        with Client(address) as c:
            member = f'm:{index}'
            # Mix routed/non-routed commands and exceed the 32-command batch.
            commands, expected = [], []
            for n in range(1, 100):
                commands += [('ZINCRBY', key, 1, member), ('PING',), ('ZSCORE', key, member)]
                expected += [f'{n}.000000'.encode(), b'PONG', f'{n}.000000'.encode()]
            c.socket.sendall(b''.join(frame(*cmd) for cmd in commands))
            assert [c.read() for _ in commands] == expected
            c.socket.sendall(b''.join(frame('ZINCRBY', key, 1, member) for _ in range(100)))
            assert [float(c.read()) for _ in range(100)] == list(range(100, 200))
            # Fragmented RESP and inline parsing both route safely.
            data = frame('ZSCORE', key, member)
            for chunk in [data[:9], data[9:15], data[15:]]:
                c.socket.sendall(chunk)
            assert float(c.read()) == 199
            c.socket.sendall(f'ZSCORE {key} {member}\r\n'.encode())
            assert float(c.read()) == 199
            assert c.call('SELECT', 1) == b'OK'
            assert c.call('ZINCRBY', key, 7, member) == b'7.000000'
            assert c.call('SELECT', 0) == b'OK'
            assert float(c.call('ZSCORE', key, member)) == 199
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        list(pool.map(client, range(16)))
    with Client(address) as c:
        assert c.call('ZCARD', key) == 16
        # More than eight args exercises the full RESP parser.
        assert c.call('ZADD', key, 1, 'a', 2, 'b', 3, 'c', 4, 'd') == 4
        assert c.call('ZRANGE', key, 0, 3) == [b'a', b'b', b'c', b'd']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='vex-owner-invalid-') as directory:
        for key, extra in (('', ['--no-persistence']), ('hot', [])):
            result = subprocess.run([str(pathlib.Path(args.binary).resolve()), '--reactor', '--workers', '4',
                                     '--port', '0', *extra], cwd=directory,
                                    env=dict(os.environ, VEX_CONFIG='', VEX_EXPERIMENTAL_OWNER_KEY=key),
                                    capture_output=True, timeout=10)
            assert result.returncode != 0 and b'InvalidExperimentalOwnerConfiguration' in result.stderr
    with server(args.binary, 'hot') as (address, _):
        check_pipeline(address)
    with server(args.binary, 'hot', password='owner-test') as (address, _):
        for index in range(8):
            with Client(address) as c:
                try:
                    c.call('ZINCRBY', 'hot', 1000, 'm')
                except AssertionError as error:
                    assert b'NOAUTH' in error.args[0]
                else:
                    raise AssertionError('owner routing bypassed authentication')
                c.socket.sendall(frame('AUTH', 'owner-test') + frame('ZINCRBY', 'hot', 1, 'm'))
                assert c.read() == b'OK'
                assert float(c.read()) == index + 1
    # Reuse the existing stress suite with ownership targeting its transaction,
    # WATCH, range-lifetime and regular update keys in separate fresh processes.
    for key in ('z:0', 'atomic', 'watched', 'race:0'):
        with server(args.binary, key) as (address, _):
            run(address)
        print('PASS owner:', key, flush=True)
    print('PASS fixed-owner reply ordering, batching, fragments, inline, DB isolation')


if __name__ == '__main__':
    main()
