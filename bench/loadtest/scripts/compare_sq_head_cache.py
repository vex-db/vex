#!/usr/bin/env python3
"""One local same-binary off/on ABBA pilot; reuses the Docker baseline runner."""
import argparse
import json
import pathlib
import statistics
import sys

import profile_local_docker as p

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / 'bench/loadtest/runs/2026-09-22-sq-head-cache'
ORDER = (('1-off', False), ('2-on', True), ('3-on', True), ('4-off', False))
ENABLED_LOG = 'sq_head_cache=true'


def configure(output, image, sha):
    p.OUT = pathlib.Path(output)
    p.OUT.mkdir(parents=True, exist_ok=True)
    p.NETWORK = 'vex-sq-head-cache-20260922'
    p.SERVER, p.CLIENT, p.PROFILER = (p.NETWORK + '-' + x for x in ('server', 'client', 'profiler'))
    p.SERVER_IMAGE, p.EXPECTED_HASH = image, sha


def cleanup():
    p.docker('rm', '-f', p.PROFILER, p.CLIENT, p.SERVER, check=False)
    p.docker('network', 'rm', p.NETWORK, check=False)


def health(stem, phase):
    p.pressure(stem, phase)
    mem = {line.split(':')[0]: int(line.split()[1]) for line in (p.OUT / f'{stem}-{phase}-meminfo.txt').read_text().splitlines()}
    assert mem['MemAvailable'] >= 512 * 1024, 'insufficient VM memory headroom'
    for container in (p.SERVER, p.CLIENT):
        events = dict(line.split() for line in (p.OUT / f'{stem}-{container}-{phase}-memory-events.txt').read_text().splitlines())
        assert all(int(events[key]) == 0 for key in ('high', 'max', 'oom', 'oom_kill')), events
        assert int((p.OUT / f'{stem}-{container}-{phase}-memory-swap-current.txt').read_text()) == 0
    return mem['MemAvailable']


def start(enabled, fallback=False):
    for name in (p.SERVER, p.CLIENT, p.PROFILER, p.NETWORK):
        p.require_absent(name)
    env = {'VEX_SQ_HEAD_CACHE': '1' if enabled else '0'}
    if fallback:
        env['VEX_URING_FLAGS'] = '0'
    p.setup(server_env=env)
    logs = p.logs(p.SERVER)
    expected = 2 if enabled and not fallback else 0
    assert logs.count(ENABLED_LOG) == expected, logs
    p.save('container-definitions.json', json.loads(p.docker('inspect', p.SERVER, p.CLIENT, p.PROFILER)))
    p.save('experiment-env.json', env)


def correctness(label, enabled, fallback, root, image, sha):
    configure(root / label, image, sha)
    # Check absence before entering cleanup so an existing run is never removed.
    for name in (p.SERVER, p.CLIENT, p.PROFILER, p.NETWORK):
        p.require_absent(name)
    try:
        start(enabled, fallback)
        for name in ('recv_poll_first.py', 'compact_values.py'):
            p.docker('cp', str(ROOT / 'tests/integration' / name), p.PROFILER + ':/tmp/' + name)
            result = p.dex(p.PROFILER, 'python3', '/tmp/' + name, '--host', p.SERVER, '--port', '6379', timeout=180)
            p.save(name + '.log', result)
        print('PASS correctness', label, flush=True)
    finally:
        cleanup()


def compare(rows):
    off = [r for r in rows if not r['enabled']]
    on = [r for r in rows if r['enabled']]
    assert len(off) == len(on) == 2
    med = lambda rs, key: statistics.median(r[key] for r in rs)
    gain = med(on, 'ops_per_sec') / med(off, 'ops_per_sec') - 1
    variation = max((max(r['ops_per_sec'] for r in rs) - min(r['ops_per_sec'] for r in rs)) / med(rs, 'ops_per_sec') for rs in (off, on))
    reasons = []
    if gain < .03: reasons.append('median gain below 3%')
    if gain <= variation: reasons.append('gain does not exceed observed within-mode variation')
    for a, b in ((rows[0], rows[1]), (rows[3], rows[2])):
        if b['ops_per_sec'] <= a['ops_per_sec']: reasons.append('paired throughput did not improve')
        if b['server_cpu_seconds_per_op'] >= a['server_cpu_seconds_per_op']: reasons.append('paired server CPU/op did not improve')
        if b['p99_ms'] > a['p99_ms'] * 1.05: reasons.append('paired p99 regression above 5%')
        if b['server_rss_bytes'] > a['server_rss_bytes'] * 1.02: reasons.append('paired RSS regression above 2%')
    valid = not any(r['constrained'] for r in rows)
    return {'measurement_valid': valid, 'signal_pass': valid and not reasons, 'gain': gain, 'within_mode_variation': variation, 'off_ops_median': med(off, 'ops_per_sec'), 'on_ops_median': med(on, 'ops_per_sec'), 'reasons': reasons, 'constrained': not valid}


def run(output, image, sha):
    output.mkdir(parents=True, exist_ok=True)
    (output / '.gitignore').write_text('*\n!.gitignore\n')
    assert not any((output / label).exists() for label, _ in ORDER), 'refusing to mix a previous pilot'
    image_info = json.loads(p.docker('image', 'inspect', image))[0]
    assert image_info['Architecture'] == 'arm64'
    image = image_info['Id']
    (output / 'protocol.json').write_text(json.dumps({'image': image, 'binary_sha256': sha, 'order': ORDER, 'timed_seconds': 60, 'warmup_seconds': 10, 'gain_gate': .03, 'p99_regression_gate': .05, 'rss_regression_gate': .02}, indent=2))
    for label, enabled, fallback in (('check-off', False, False), ('check-on', True, False), ('check-fallback', True, True)):
        correctness(label, enabled, fallback, output, image, sha)
    rows = []
    for label, enabled in ORDER:
        configure(output / label, image, sha)
        for name in (p.SERVER, p.CLIENT, p.PROFILER, p.NETWORK): p.require_absent(name)
        try:
            print('START', label, flush=True)
            start(enabled)
            health(label, 'before-preload')
            p.preload()
            health(label, 'after-preload')
            p.run_load('warmup', 10)
            row = p.run_load(label, 60) | {'enabled': enabled, 'binary_sha256': sha, 'errors': 0, 'misses': 0}
            health(label, 'after')
            rows.append(row)
            (output / 'cells.json').write_text(json.dumps(rows, indent=2))
            print('DONE', label, round(row['ops_per_sec']), 'ops/s', 'p99', row['p99_ms'], flush=True)
        finally:
            cleanup()
    result = compare(rows)
    (output / 'comparison.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)


def self_test():
    base = {'server_cpu_seconds_per_op': 1, 'p99_ms': 1, 'server_rss_bytes': 1, 'constrained': False}
    rows = [base | {'enabled': flag, 'ops_per_sec': 110 if flag else 100, 'server_cpu_seconds_per_op': .9 if flag else 1} for _, flag in ORDER]
    assert compare(rows)['signal_pass']
    rows[2]['constrained'] = True
    assert not compare(rows)['measurement_valid']
    print('Self-test passed: improvement and constraint gates')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=pathlib.Path, default=OUT)
    ap.add_argument('--image', default='vex-sq-head-cache:arm64')
    ap.add_argument('--sha')
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()
    if args.self_test: self_test()
    else:
        assert args.sha and len(args.sha) == 64, 'supply the native candidate SHA256'
        run(args.output.resolve(), args.image, args.sha)
