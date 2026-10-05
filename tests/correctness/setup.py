#!/usr/bin/env python3
"""Install the four public tools locally; never start or contact a database."""
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    if sys.version_info < (3, 11):
        raise SystemExit('Use Python 3.11 or newer for this setup.')
    cache = ROOT / '.cache'
    cache.mkdir(exist_ok=True)
    for name, spec in json.loads((ROOT / 'upstreams.json').read_text()).items():
        dest = cache / name
        if not dest.exists():
            run('git', 'clone', '--depth', '1', '--branch', spec['tag'], spec['url'], str(dest))
        head = subprocess.check_output(['git', '-C', str(dest), 'rev-parse', 'HEAD'], text=True).strip()
        if head != spec['commit']:
            raise SystemExit(f'{name}: unexpected revision {head}; refusing to overwrite checkout')
        run('git', '-C', str(dest), 'diff', '--exit-code', 'HEAD')
    if not (ROOT / '.venv').exists():
        venv.create(ROOT / '.venv', with_pip=True)
    run(str(ROOT / '.venv/bin/python'), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt'))
    env = dict(os.environ, GOCACHE=str(cache / 'go-build'), GOMODCACHE=str(cache / 'go-mod'))
    run('go', 'mod', 'download', cwd=ROOT / 'linearizability', env=env)
    run('go', 'build', '-o', str(cache / 'linearizability'), '.', cwd=ROOT / 'linearizability', env=env)
    print('Ready: Redis/Valkey upstream suites, Hypothesis, and Porcupine. See README.md.')


if __name__ == '__main__':
    main()
