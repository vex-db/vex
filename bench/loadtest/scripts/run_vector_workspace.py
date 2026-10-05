#!/usr/bin/env python3
"""Bounded write-buffer HNSW workspace screen in fresh native Docker processes."""
import argparse
import hashlib
import json
import pathlib
import statistics

from run_table_screen import command, resource_check, WRAPPER

ROOT = pathlib.Path(__file__).resolve().parents[3]
ORDER = ('control', 'workspace', 'workspace', 'control')
COUNT, DIM, QUERIES = 10_000, 64, 100


def record(rows, phase):
    selected = [r for r in rows if r.get('phase') == phase]
    if len(selected) != 1:
        raise ValueError(f'expected one {phase} record')
    return selected[0]


def validate(rows, variant, phase, stride, total):
    row = record(rows, phase)
    for key, expected in dict(mode=variant, count=COUNT, dim=DIM, stride=stride,
                              fixed_queries=QUERIES, total_queries=total, k=10, ef=50,
                              allocation_counting=phase == 'accounting').items():
        if row.get(key) != expected:
            raise ValueError(f'configuration mismatch: {key}')
    if total % QUERIES or row['checksum'] != (row['correctness_checksum'] * (total // QUERIES)) % 2**64:
        raise ValueError('repeated query checksum differs from correctness trace')
    if not 0 <= row['recall'] <= 1:
        raise ValueError('invalid recall')
    if not (0 < row['p50_ns'] <= row['p99_ns'] <= row['elapsed_ns']):
        raise ValueError('invalid latency data')
    if not (0 < row['cpu_ns_per_query'] <= row['elapsed_ns'] / total * 1.02):
        raise ValueError('invalid CPU/query')
    end = record(rows, 'teardown')
    capacity = record(rows, 'workspace_capacity')
    if min(row['rss_before'], row['rss_loaded'], end['rss_complete'], capacity['rss_after_queries']) <= 0:
        raise ValueError('missing RSS')
    if phase == 'accounting':
        if end['core_live_bytes'] or end['scratch_live_bytes']:
            raise ValueError('live allocation after destruction')
        if row['scratch_live_bytes'] != capacity['expected_scratch_bytes']:
            raise ValueError('scratch components do not reconcile')
        if row['core_alloc_delta'] != row['core_free_delta'] or row['scratch_alloc_delta'] != row['scratch_free_delta']:
            raise ValueError('unbalanced steady allocations')
        if variant == 'workspace' and (row['scratch_alloc_delta'] or row['scratch_free_delta']):
            raise ValueError('workspace still allocates scratch on the fixed warmed trace')
        if variant == 'control' and row['scratch_live_bytes']:
            raise ValueError('control unexpectedly retains workspace')
    elif row['core_alloc_delta'] is not None or row['scratch_alloc_delta'] is not None:
        raise ValueError('timing must be uninstrumented')
    return row


def compare(cells):
    for stride in (1, 64):
        rows = [record(c['data'], c['phase']) for c in cells if c['stride'] == stride]
        if len({(r['correctness_checksum'], r['recall']) for r in rows}) > 1:
            raise ValueError('cross-process correctness or recall mismatch')
    timed = [record(c['data'], 'timed') for c in cells if c['phase'] == 'timed']
    for variant in ('control', 'workspace'):
        samples = [r['elapsed_ns'] / r['total_queries'] for r in timed if r['mode'] == variant]
        if len(samples) > 1 and (max(samples) - min(samples)) / statistics.median(samples) > .05:
            raise ValueError(f'inconclusive: {variant} repeated timing varies by more than 5%')
    if len(timed) != 4:
        return None
    paired = []
    for control, candidate in ((timed[0], timed[1]), (timed[3], timed[2])):
        paired.append({'qps_gain': control['elapsed_ns'] / candidate['elapsed_ns'] - 1,
                       'cpu_change': candidate['cpu_ns_per_query'] / control['cpu_ns_per_query'] - 1,
                       'p99_change': candidate['p99_ns'] / control['p99_ns'] - 1})
    gain = statistics.median(r['elapsed_ns'] for r in (timed[0], timed[3])) / statistics.median(r['elapsed_ns'] for r in (timed[1], timed[2])) - 1
    accepted = all(p['qps_gain'] >= .05 and p['cpu_change'] < 0 and p['p99_change'] <= .05 for p in paired)
    return {'isolated_screen_passed': accepted, 'paired': paired, 'median_qps_gain': gain,
            'interpretation': '10K write-buffer library screen; no server or mapped-tier claim'}


def run_cell(output, image, binary, sha, variant, phase, stride, total, repeat):
    label = f'{phase}-{stride}-{repeat}-{variant}'
    folder = output / label
    folder.mkdir()
    name = 'vex-vector-screen-20260923-' + label
    if command(['docker', 'inspect', name], check=False).returncode == 0:
        raise RuntimeError(f'refusing existing container {name}')
    args = ['docker', 'run', '--name', name, '--network', 'none', '--read-only',
            '--cpuset-cpus', '0', '--cpus', '1', '--memory', '1536m', '--memory-swap', '1536m',
            '--mount', f'type=bind,source={binary},target=/opt/vector-screen,readonly',
            '--entrypoint', 'python3', image, '-c', WRAPPER.replace('/opt/table-screen', '/opt/vector-screen'),
            variant, str(COUNT), str(DIM), str(QUERIES), phase, str(stride), str(total)]
    (folder/'command.json').write_text(json.dumps(args, indent=2)+'\n')
    print('START', label, flush=True)
    try:
        result = command(args, check=False)
        (folder/'stdout.jsonl').write_text(result.stdout)
        (folder/'stderr.txt').write_text(result.stderr)
        (folder/'container.json').write_text(command(['docker', 'inspect', name], check=False).stdout)
        if result.returncode:
            raise RuntimeError(f'{label} process failed: {result.returncode}')
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        resources = [r['resource'] for r in rows if 'resource' in r]
        if len(resources) != 1:
            raise ValueError('missing resource record')
        resource_check(resources[0], sha)
        data = [r for r in rows if 'resource' not in r]
        validate(data, variant, phase, stride, total)
        cell = dict(variant=variant, phase=phase, stride=stride, repeat=repeat, data=data, resource=resources[0])
        (folder/'parsed.json').write_text(json.dumps(cell, indent=2)+'\n')
        print('DONE', label, flush=True)
        return cell
    finally:
        command(['docker', 'rm', '-f', name], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=pathlib.Path, required=True)
    parser.add_argument('--output', type=pathlib.Path, default=ROOT/'bench/loadtest/runs/2026-09-23-table-screen/vector-matrix')
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    image = json.loads(command(['docker', 'image', 'inspect', 'vex-sq-head-cache:arm64']).stdout)[0]
    if image['Architecture'] != 'arm64':
        raise ValueError('native ARM64 image required')
    args.output.mkdir(parents=True, exist_ok=True)
    protocol = args.output/'protocol.json'
    if protocol.exists():
        raise ValueError('refusing to mix prior results')
    protocol.write_text(json.dumps(dict(binary_sha256=sha, image_id=image['Id'], count=COUNT,
        dim=DIM, unique_queries=QUERIES, timed_total=100_000, accounting_total=1_000,
        accounting_strides=[1,64], timing_stride=1, timing_order=ORDER, repeat_variation_max=.05,
        paired_qps_gain_min=.05, paired_p99_regression_max=.05, cpu_set='0', quota=1), indent=2)+'\n')
    cells = []
    try:
        for stride in (1, 64):
            for variant in ('control', 'workspace'):
                cells.append(run_cell(args.output, image['Id'], binary, sha, variant, 'accounting', stride, 1000, 1))
                (args.output/'cells.json').write_text(json.dumps(cells, indent=2)+'\n')
                compare(cells)
        for repeat, variant in enumerate(ORDER, 1):
            cells.append(run_cell(args.output, image['Id'], binary, sha, variant, 'timed', 1, 100_000, repeat))
            (args.output/'cells.json').write_text(json.dumps(cells, indent=2)+'\n')
            compare(cells)
        result = dict(status='complete', comparison=compare(cells), cells=cells)
    except Exception as exc:
        (args.output/'summary.json').write_text(json.dumps(dict(status='stopped', reason=str(exc), cells=cells), indent=2)+'\n')
        raise
    (args.output/'summary.json').write_text(json.dumps(result, indent=2)+'\n')


if __name__ == '__main__':
    main()
