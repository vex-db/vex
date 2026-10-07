#!/usr/bin/env python3
"""Run fresh native Docker processes for the direct/dense table screen."""
import argparse
import hashlib
import json
import pathlib
import statistics
import subprocess
import time


ROOT = pathlib.Path(__file__).resolve().parents[3]
ORDER = ("direct", "dense", "dense", "direct")
CASES = ((800_000, 32), (1_000_000, 32), (800_000, 256), (1_000_000, 256))
PHASES = ('randomhit', 'miss', 'hotset', 'equaloverwrite', 'mixed_80_20', 'delete_reinsert')
WRAPPER = r'''
import hashlib, json, pathlib, subprocess, sys, time
def snapshot():
    root = pathlib.Path('/sys/fs/cgroup')
    def pairs(name):
        return {k: int(v) for k,v in (line.split() for line in (root/name).read_text().splitlines())}
    mem = {line.split(':')[0]: int(line.split()[1])*1024 for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')}
    return {'monotonic_ns': time.monotonic_ns(), 'cpu': pairs('cpu.stat'),
            'memory_events': pairs('memory.events'), 'swap': int((root/'memory.swap.current').read_text()),
            'memory_peak': int((root/'memory.peak').read_text()), 'vm_available': mem['MemAvailable']}
sha = hashlib.sha256(pathlib.Path('/opt/table-screen').read_bytes()).hexdigest()
before = snapshot()
result = subprocess.run(['/opt/table-screen', *sys.argv[1:]], capture_output=True, text=True)
after = snapshot()
sys.stdout.write(result.stdout)
sys.stdout.write(result.stderr)
print(json.dumps({'resource': {'before': before, 'after': after, 'binary_sha256': sha, 'exit_code': result.returncode}}))
sys.exit(result.returncode)
'''


def command(args, *, check=True, timeout=600):
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=timeout)


def resource_check(resource, expected_sha):
    if resource['binary_sha256'] != expected_sha or resource['exit_code'] != 0:
        raise ValueError('binary identity or process exit failed')
    before, after = resource['before'], resource['after']
    duration_us = (after['monotonic_ns'] - before['monotonic_ns']) / 1000
    if duration_us <= 0:
        raise ValueError('nonpositive process duration')
    throttle = (after['cpu']['throttled_usec'] - before['cpu']['throttled_usec']) / duration_us
    if throttle >= .01:
        raise ValueError(f'CPU throttling {throttle:.2%} exceeds 1%')
    if before['swap'] or after['swap']:
        raise ValueError('swap observed')
    if min(before['vm_available'], after['vm_available']) < 512 * 1024**2:
        raise ValueError('VM available memory below 512 MiB')
    for key, value in after['memory_events'].items():
        if value != before['memory_events'].get(key, 0):
            raise ValueError(f'memory event {key} changed')
    return throttle


def phase_record(data, phase):
    matches = [row for row in data if row.get('phase') == phase]
    if len(matches) != 1:
        raise ValueError(f'expected exactly one {phase} record')
    return matches[0]


def validate_data(data, variant, count, size, mode, ops):
    config = phase_record(data, 'configuration')
    expected = {'mode': variant, 'count': count, 'value_bytes': size, 'run': mode,
                'ops': ops, 'stripes': 256, 'key_bytes': 16, 'entry_bytes': 64}
    for key, value in expected.items():
        if config.get(key) != value:
            raise ValueError(f'configuration mismatch for {key}: {config.get(key)} != {value}')
    loaded = phase_record(data, 'loaded')
    if loaded['entries'] != count:
        raise ValueError('loaded key count mismatch')
    fixtures = 64 * count + 4 * min(ops, 1_000_000) + 2 * size
    for phase in PHASES:
        row = phase_record(data, phase)
        if row['entries'] != count:
            raise ValueError(f'{phase} ended with a different live dataset')
        histogram = row['capacity_histogram']
        if sum(item['stripes'] for item in histogram) != 256:
            raise ValueError(f'{phase} histogram misses stripes')
        if sum(item['stripes'] * item['capacity'] for item in histogram) != row['capacity']:
            raise ValueError(f'{phase} capacity does not reconcile')
        if not isinstance(row['checksum'], int):
            raise ValueError(f'{phase} missing observable checksum')
        if mode == 'timed':
            expected_ops = ((ops + 9) // 10) * 10 if phase == 'delete_reinsert' else ops
            if row['operations'] != expected_ops:
                raise ValueError(f'{phase} operation count mismatch')
            if row['elapsed_ns'] <= 0 or row['cpu_elapsed_ns'] <= 0 or row['operations'] <= 0:
                raise ValueError(f'{phase} has nonpositive timing/count')
            if row['cpu_elapsed_ns'] > row['elapsed_ns'] * 1.02:
                raise ValueError(f'{phase} CPU time exceeds single-thread wall time')
        else:
            if phase in ('equaloverwrite', 'mixed_80_20') and row['alloc_calls_delta'] != 0:
                raise ValueError(f'{phase} unexpectedly allocated')
            keys_and_values = count * (16 + (size if size > 32 else 0))
            if variant == 'direct':
                map_bytes = sum(item['stripes'] * (24 + 81 * item['capacity'])
                                for item in histogram if item['capacity'])
            else:
                map_bytes = 84 * row['capacity'] + row['dense_index_bytes_estimate']
            if fixtures + keys_and_values + map_bytes != row['live_requested_bytes']:
                raise ValueError(f'{phase} allocation components do not reconcile: '
                                 f'{fixtures + keys_and_values + map_bytes} != {row["live_requested_bytes"]}')
    completion = phase_record(data, 'completion')
    post_flush = phase_record(data, 'post_flush')
    if any(row['rss_bytes'] <= 0 for row in (loaded, post_flush, completion)):
        raise ValueError('missing or nonpositive RSS')
    if mode == 'accounting' and post_flush['live_requested_bytes'] != fixtures:
        raise ValueError('post-flush allocations do not match fixtures')
    if mode == 'accounting' and completion['live_requested_bytes'] != 0:
        raise ValueError('tracked allocations remain after destruction')


def compare_rows(rows):
    comparisons = []
    for count, size in CASES:
        selected = [r for r in rows if r['count'] == count and r['value_bytes'] == size]
        for mode in ('timed', 'accounting'):
            cells = [r for r in selected if r['mode'] == mode]
            if not cells:
                continue
            for phase in PHASES:
                records = [phase_record(cell['data'], phase) for cell in cells]
                if len({r['checksum'] for r in records}) != 1:
                    raise ValueError(f'{count}/{size}/{mode}/{phase} checksum mismatch')
                if mode != 'timed':
                    continue
                values = {variant: [phase_record(c['data'], phase)['elapsed_ns'] /
                                    phase_record(c['data'], phase)['operations']
                                    for c in cells if c['variant'] == variant]
                          for variant in ('direct', 'dense')}
                for variant, samples in values.items():
                    if len(samples) > 1:
                        variation = (max(samples) - min(samples)) / statistics.median(samples)
                        if variation > .05:
                            raise ValueError(f'inconclusive: {count}/{size}/{phase}/{variant} variation {variation:.2%} exceeds 5%')
                if all(len(v) == 2 for v in values.values()):
                    direct, dense = (statistics.median(values[v]) for v in ('direct', 'dense'))
                    comparisons.append({'count': count, 'value_bytes': size, 'phase': phase,
                                        'direct_ns_per_op': direct, 'dense_ns_per_op': dense,
                                        'dense_throughput_change': direct / dense - 1})
    return comparisons


def run_cell(output, image, binary, sha, variant, count, size, mode, repeat, ops):
    label = f'{count}-{size}-{mode}-{repeat}-{variant}'
    folder = output / label
    folder.mkdir()
    name = 'vex-table-screen-20260923-' + label
    existing = command(['docker', 'container', 'inspect', name], check=False)
    if existing.returncode == 0:
        raise RuntimeError(f'refusing to touch existing container {name}')
    args = ['docker', 'run', '--name', name, '--network', 'none', '--read-only',
            '--cpuset-cpus', '0', '--cpus', '1', '--memory', '1536m', '--memory-swap', '1536m',
            '--mount', f'type=bind,source={binary},target=/opt/table-screen,readonly',
            '--entrypoint', 'python3', image, '-c', WRAPPER,
            variant, str(count), str(size), mode, str(ops)]
    (folder / 'command.json').write_text(json.dumps(args, indent=2) + '\n')
    print('START', label, flush=True)
    try:
        result = command(args, check=False)
        (folder / 'stdout.jsonl').write_text(result.stdout)
        (folder / 'stderr.txt').write_text(result.stderr)
        inspected = command(['docker', 'inspect', name], check=False)
        (folder / 'container.json').write_text(inspected.stdout)
        if result.returncode:
            raise RuntimeError(f'{label} exited {result.returncode}: {result.stderr[-500:]}')
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        resources = [row['resource'] for row in rows if 'resource' in row]
        if len(resources) != 1:
            raise ValueError(f'{label} has missing/duplicate resource record')
        throttle = resource_check(resources[0], sha)
        data = [row for row in rows if 'resource' not in row]
        if not data:
            raise ValueError(f'{label} has no diagnostic output')
        validate_data(data, variant, count, size, mode, ops)
        row = {'cell': label, 'variant': variant, 'count': count, 'value_bytes': size,
               'mode': mode, 'repeat': repeat, 'data': data, 'resource': resources[0],
               'throttle_ratio': throttle}
        (folder / 'parsed.json').write_text(json.dumps(row, indent=2) + '\n')
        print('DONE', label, flush=True)
        return row
    finally:
        command(['docker', 'rm', '-f', name], check=False)


def self_test():
    base = {'monotonic_ns': 1, 'cpu': {'throttled_usec': 0}, 'swap': 0,
            'memory_events': {'oom': 0}, 'vm_available': 1024**3}
    after = base | {'monotonic_ns': 1_000_000_001}
    good = {'before': base, 'after': after, 'binary_sha256': 'same', 'exit_code': 0}
    assert resource_check(good, 'same') == 0
    for change in ({'swap': 1}, {'memory_events': {'oom': 1}},
                   {'cpu': {'throttled_usec': 10_000}}, {'vm_available': 1}):
        try:
            resource_check(good | {'after': after | change}, 'same')
        except ValueError:
            pass
        else:
            raise AssertionError(f'bad resource state accepted: {change}')
    def cell(variant, elapsed):
        data = [{'phase': 'configuration', 'mode': variant, 'count': 800_000,
                 'value_bytes': 32, 'run': 'timed', 'ops': 1_000_000, 'stripes': 256,
                 'key_bytes': 16, 'entry_bytes': 64},
                {'phase': 'loaded', 'entries': 800_000, 'rss_bytes': 1},
                {'phase': 'post_flush', 'rss_bytes': 1},
                {'phase': 'completion', 'live_requested_bytes': 0, 'rss_bytes': 1}]
        data += [{'phase': phase, 'entries': 800_000, 'checksum': i,
                  'capacity': 1_048_576, 'capacity_histogram': [{'capacity': 4096, 'stripes': 256}],
                  'elapsed_ns': elapsed, 'cpu_elapsed_ns': elapsed - 1,
                  'operations': 1_000_000} for i, phase in enumerate(PHASES)]
        validate_data(data, variant, 800_000, 32, 'timed', 1_000_000)
        return {'variant': variant, 'count': 800_000, 'value_bytes': 32, 'mode': 'timed', 'data': data}
    rows = [cell(variant, 100_000_000 if variant == 'direct' else 90_000_000) for variant in ORDER]
    assert len(compare_rows(rows)) == len(PHASES)
    def must_reject(data, variant, mode):
        try:
            validate_data(data, variant, 800_000, 32, mode, 1_000_000)
        except ValueError:
            return
        raise AssertionError('malformed diagnostic accepted')
    for variant in ('direct', 'dense'):
        data = cell(variant, 100_000_000)['data']
        phase_record(data, 'configuration')['run'] = 'accounting'
        fixtures = 64 * 800_000 + 4_000_000 + 64
        phase_record(data, 'post_flush')['live_requested_bytes'] = fixtures
        for phase in PHASES:
            row = phase_record(data, phase)
            row['dense_index_bytes_estimate'] = 8_389_632
            map_bytes = (256 * (24 + 81 * 4096) if variant == 'direct' else
                         84 * row['capacity'] + row['dense_index_bytes_estimate'])
            row.update(live_requested_bytes=fixtures + 16 * 800_000 + map_bytes, alloc_calls_delta=0)
        validate_data(data, variant, 800_000, 32, 'accounting', 1_000_000)
        for phase, key in [('randomhit', 'live_requested_bytes'),
                           ('equaloverwrite', 'alloc_calls_delta'),
                           ('post_flush', 'live_requested_bytes'),
                           ('completion', 'live_requested_bytes')]:
            bad = json.loads(json.dumps(data))
            phase_record(bad, phase)[key] += 1
            must_reject(bad, variant, 'accounting')
        if variant == 'dense':
            bad = json.loads(json.dumps(data))
            phase_record(bad, 'randomhit')['dense_index_bytes_estimate'] += 1
            must_reject(bad, variant, 'accounting')
    for defect in ('operations', 'histogram', 'post_flush'):
        bad = json.loads(json.dumps(rows[0]['data']))
        if defect == 'operations':
            phase_record(bad, 'randomhit')['operations'] = 1
        elif defect == 'histogram':
            phase_record(bad, 'randomhit')['capacity_histogram'][0]['stripes'] += 256
        else:
            bad = [r for r in bad if r['phase'] != 'post_flush']
        must_reject(bad, 'direct', 'timed')
    for defect in ('checksum', 'timing'):
        bad = json.loads(json.dumps(rows))
        record = phase_record(bad[2]['data'], 'randomhit')
        if defect == 'checksum':
            record['checksum'] += 1
        else:
            record['elapsed_ns'] = 70_000_000
        try:
            compare_rows(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f'{defect} mismatch accepted')
    print('Self-test passed: resources, complete data, checksums and repeatability guards')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=pathlib.Path)
    parser.add_argument('--image', default='vex-sq-head-cache:arm64')
    parser.add_argument('--output', type=pathlib.Path, default=ROOT / 'bench/loadtest/runs/2026-09-23-table-screen/matrix')
    parser.add_argument('--ops', type=int, default=10_000_000)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.binary is None or args.ops <= 0:
        parser.error('--binary and a positive --ops are required')
    binary = args.binary.resolve(strict=True)
    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    info = json.loads(command(['docker', 'image', 'inspect', args.image]).stdout)[0]
    if info['Architecture'] != 'arm64':
        raise ValueError('native ARM64 image required')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    protocol_path = output / 'protocol.json'
    if protocol_path.exists():
        raise ValueError('refusing to mix a previous matrix')
    protocol = {'image_id': info['Id'], 'binary_sha256': sha, 'cases': CASES,
                'order': ORDER, 'timed_operations': args.ops, 'accounting_operations': '2 * count', 'cpu_set': '0', 'quota': 1,
                'memory_bytes': 1536 * 1024**2, 'repeat_variation_max': .05,
                'timed_and_accounting_separate_processes': True,
                'started_unix': time.time(), 'interpretation': 'single-thread table diagnostic; not server throughput'}
    protocol_path.write_text(json.dumps(protocol, indent=2) + '\n')
    rows = []
    try:
        for count, size in CASES:
            for mode in ('accounting', 'timed'):
                for repeat, variant in enumerate(ORDER, 1):
                    operations = args.ops if mode == 'timed' else count * 2
                    rows.append(run_cell(output, info['Id'], binary, sha, variant, count, size, mode, repeat, operations))
                    (output / 'cells.json').write_text(json.dumps(rows, indent=2) + '\n')
                    compare_rows(rows)
        (output / 'summary.json').write_text(json.dumps({'status': 'complete', 'cells': rows, 'comparisons': compare_rows(rows)}, indent=2) + '\n')
    except Exception as exc:
        (output / 'summary.json').write_text(json.dumps({'status': 'stopped', 'reason': str(exc), 'cells': rows}, indent=2) + '\n')
        raise


if __name__ == '__main__':
    main()
