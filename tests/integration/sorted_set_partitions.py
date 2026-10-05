#!/usr/bin/env python3
"""Start disposable local servers to test partition overrides and concurrent commands."""
import argparse
import os
import pathlib
import socket
import subprocess
import tempfile
import time

from sorted_set_concurrency import Client, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=pathlib.Path, required=True)
    options = parser.parse_args()
    binary = str(options.binary.resolve())
    with tempfile.TemporaryDirectory(prefix='vex-partitions-') as directory:
        env = dict(os.environ, VEX_CONFIG='', VEX_PIN_WORKERS='0')
        for args in (['--sorted-set-partitions'], *(['--sorted-set-partitions', x] for x in ('0', '3', '-1', '8192', 'abc'))):
            result = subprocess.run([binary, *args], cwd=directory, env=env, capture_output=True, timeout=10)
            assert result.returncode != 0, args
        for count in (1, 256, 1024):
            # Config is used for 1; CLI overrides it for 1024; 256 uses the default.
            config = pathlib.Path(directory) / 'test.conf'
            config.write_text('sorted-set-partitions 1\n' if count != 256 else '')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            args = [binary, '--reactor', '--workers', '4', '--host', '127.0.0.1', '--port', str(port), '--no-persistence', '--config', str(config)]
            if count == 1024:
                args += ['--sorted-set-partitions', str(count)]
            with tempfile.TemporaryFile() as log:
                process = subprocess.Popen(args, cwd=directory, env=env, stdout=log, stderr=log)
                try:
                    for _ in range(100):
                        if process.poll() is not None:
                            log.seek(0)
                            raise RuntimeError(log.read().decode())
                        try:
                            with Client(('127.0.0.1', port)) as client:
                                assert client.call('PING') == b'PONG'
                            break
                        except OSError:
                            time.sleep(.1)
                    else:
                        raise TimeoutError('server did not become ready')
                    run(('127.0.0.1', port))
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                log.seek(0)
                assert f'sorted-set-partitions={count}' in log.read().decode()
            print(f'PASS: startup and concurrency with {count} partitions', flush=True)
        config.write_text('sorted-set-partitions 3\n')
        result = subprocess.run([binary, '--config', str(config)], cwd=directory, env=env, capture_output=True, timeout=10)
        assert result.returncode != 0
    print('PASS: invalid CLI/config values rejected; default and CLI precedence verified')


if __name__ == '__main__':
    main()
