#!/usr/bin/env python3
"""Exercise all four tools with disposable, loopback-only Docker servers."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import redis

ROOT = Path(__file__).resolve().parent
REDIS_IMAGE = 'redis@sha256:2e940b1efbe5bae595d9cc7c4f5ecdc84497a63b2a8e1bfe7624a0fae2f05127'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vex-binary', type=Path, required=True, help='Linux binary compatible with the supplied image')
    p.add_argument('--vex-image', required=True, help='Local runtime image; its entrypoint is overridden')
    p.add_argument('--adaptive', action='store_true', help='Enable experimental adaptive ownership for the Vex smoke server')
    p.add_argument('--output', type=Path, required=True)
    o = p.parse_args()
    binary = o.vex_binary.resolve(strict=True)
    o.output = o.output.resolve()
    o.output.mkdir(parents=True, exist_ok=False)
    containers = []
    runs = []

    def start(name, image, args, extra=()):
        cmd = ['docker', 'run', '-d', '--rm', '--cpus', '2', '--memory', '2g',
               '--label', 'vex.correctness=smoke', '-p', '127.0.0.1::6379', *extra, image, *args]
        cid = subprocess.check_output(cmd, text=True).strip()
        containers.append((name, cid))
        port = int(subprocess.check_output(['docker', 'port', cid, '6379/tcp'], text=True).strip().rsplit(':', 1)[1])
        c = redis.Redis(host='127.0.0.1', port=port, socket_timeout=1, socket_connect_timeout=1)
        try:
            for _ in range(100):
                try:
                    if c.ping(): return port
                except redis.ConnectionError:
                    pass
                time.sleep(.1)
            raise RuntimeError(f'{name} did not become ready')
        finally:
            c.close()

    def run(name, args):
        with (o.output / (name + '.log')).open('w') as log:
            result = subprocess.run(args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        runs.append(dict(name=name, command=args, exit_code=result.returncode))
        print(name, 'PASS' if result.returncode == 0 else 'FAIL (see log)', flush=True)

    try:
        reference = start('redis-reference', REDIS_IMAGE, ['redis-server', '--save', '', '--appendonly', 'no'])
        control = start('redis-control', REDIS_IMAGE, ['redis-server', '--save', '', '--appendonly', 'no'])
        vex = start('vex', o.vex_image,
            ['--reactor', '--workers', '4', '--port', '6379', '--host', '0.0.0.0', '--no-persistence'],
            ['--entrypoint', '/vex-test', '-v', f'{binary}:/vex-test:ro',
             '-e', 'VEX_CONFIG=', '-e', 'VEX_PIN_WORKERS=0',
             '-e', 'VEX_EXPERIMENTAL_ADAPTIVE_OWNER=' + ('1' if o.adaptive else '0')])
        for target, port in [('control', control), ('vex', vex)]:
            for upstream in ('redis', 'valkey'):
                name = f'{target}-{upstream}'
                run(name, [sys.executable, str(ROOT/'upstream.py'), upstream, '--port', str(port),
                    '--suite', 'unit/type/incr', '--timeout', '60', '--allow-destructive', '--core-profile',
                    '--output', str(o.output/name)])
            name = target + '-hypothesis'
            run(name, [sys.executable, str(ROOT/'differential.py'), '--target', f'redis://127.0.0.1:{port}',
                '--reference', f'redis://127.0.0.1:{reference}', '--examples', '10', '--steps', '30',
                '--output', str(o.output/name)])
            for kind in ('register', 'zset'):
                name = f'{target}-porcupine-{kind}'
                run(name, [str(ROOT/'.cache/linearizability'), '-addr', f'127.0.0.1:{port}',
                    '-kind', kind, '-rounds', '3', '-operations', '30', '-output', str(o.output/name)])
            run(target+'-integers', [sys.executable, str(ROOT/'integer_regressions.py'),
                '--target', f'redis://127.0.0.1:{port}'])
            run(target+'-regressions', [sys.executable, str(ROOT/'regressions.py'),
                '--target', f'redis://127.0.0.1:{port}'])
            run(target+'-key-lifecycle', [sys.executable, str(ROOT/'key_lifecycle_regressions.py'),
                '--target', f'redis://127.0.0.1:{port}'])
    finally:
        cleanup = []
        for name, cid in containers:
            with (o.output/(name+'-server.log')).open('w') as log:
                subprocess.run(['docker', 'logs', cid], stdout=log, stderr=subprocess.STDOUT)
            result = subprocess.run(['docker', 'rm', '-f', cid], capture_output=True, text=True)
            cleanup.append(dict(name=name, container=cid, exit_code=result.returncode))
        (o.output/'summary.json').write_text(json.dumps(dict(vex_binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            redis_image=REDIS_IMAGE, vex_image=o.vex_image, adaptive=o.adaptive, runs=runs, cleanup=cleanup), indent=2)+'\n')
    return 0 if len(runs) == 16 and all(r['exit_code'] == 0 for r in runs+cleanup) else 1


if __name__ == '__main__':
    sys.exit(main())
