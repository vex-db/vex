#!/usr/bin/env python3
"""Run pinned upstream Tcl tests against a disposable external server."""
import argparse
import datetime
import difflib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SUITES = ['unit/type/string', 'unit/type/incr', 'unit/type/list',
          'unit/type/set', 'unit/type/hash', 'unit/type/zset', 'unit/expire']
# Internal implementation hooks are not Redis wire-protocol requirements.
EXCLUDED_TAGS = ['needs:debug', 'needs:pfdebug', 'needs:repl', 'needs:config-maxmemory',
                 'needs:config-resetstat', 'needs:reset', 'needs:save']


def summarize(log, returncode):
    clean = re.sub(r'\x1b\[[0-9;]*m', '', log)
    events = clean.split('                   The End', 1)[0]
    statuses = {name: len(re.findall(r'^\[' + name + r'\]', clean, re.M))
                for name in ('ok', 'err', 'skip', 'ignore', 'exception')}
    unsupported_count = sum(bool(re.search(r'unknown command|unsupported command', block, re.I))
                            for block in re.findall(r'^\[err\]:.*?(?=^\[|\Z)', events, re.M | re.S))
    unsupported = [line for line in clean.splitlines()
                   if re.search(r'unknown command|unsupported command', line, re.I)]
    return dict(passed=statuses['ok'], failed=statuses['err'] - unsupported_count,
                unsupported=unsupported_count, exceptions=statuses['exception'], skipped=statuses['skip'],
                ignored=statuses['ignore'], unsupported_diagnostics=unsupported,
                # A zero exit with no actual tests is not a passing suite.
                status='passed' if returncode == 0 and statuses['ok'] > 0 and statuses['err'] == 0 and statuses['exception'] == 0 else 'failed')


def core_checkout(source, dest):
    """Keep upstream immutable; excluded bootstrap commands stay visible."""
    dest.mkdir()
    for entry in source.iterdir():
        if entry.name not in ('.git', 'tests'):
            (dest / entry.name).symlink_to(entry.resolve(), target_is_directory=entry.is_dir())
    shutil.copytree(source/'tests', dest/'tests', ignore=shutil.ignore_patterns('tmp'))
    server = dest/'tests/support/server.tcl'
    original = server.read_text()
    if original.count('    r function flush\n') != 1:
        raise RuntimeError('Upstream bootstrap changed; review the core adapter')
    patched = original
    for command in ('function flush', 'script flush'):
        before = f'    r {command}\n'
        if patched.count(before) > 1:
            raise RuntimeError('Ambiguous bootstrap patch')
        patched = patched.replace(before,
            '    send_data_packet $::test_server_fd ignore {Core profile: bootstrap ' + command.upper() + ' not tested}\n')
    server.write_text(patched)
    return ''.join(difflib.unified_diff(original.splitlines(True), patched.splitlines(True),
                                      fromfile='upstream/tests/support/server.tcl', tofile='core/tests/support/server.tcl'))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('engine', choices=['redis', 'valkey'])
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--suite', action='append', help='Repeat to select upstream test files; default: core data types and expiry')
    p.add_argument('--test', action='append', help='Exact test name or /regexp, repeatable (narrow smoke checks)')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--timeout', type=int, default=900, help='Seconds per test file')
    p.add_argument('--core-profile', action='store_true',
                   help='Exclude FUNCTION/SCRIPT FLUSH bootstrap; only core data-type/expiry files allowed')
    p.add_argument('--allow-destructive', action='store_true', required=True,
                   help='Required: these suites flush data and may change server configuration')
    o = p.parse_args()
    if o.core_profile and not set(o.suite or SUITES) <= set(SUITES):
        p.error('core-profile supports only the documented core files')
    source = ROOT / '.cache' / o.engine
    spec = json.loads((ROOT / 'upstreams.json').read_text())[o.engine]
    if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != spec['commit']:
        raise SystemExit('Upstream revision mismatch; rerun setup after checking the cached checkout.')
    subprocess.run(['git', '-C', str(source), 'diff', '--exit-code', 'HEAD'], check=True)
    o.output.mkdir(parents=True, exist_ok=False)
    if o.core_profile:
        adapted = o.output.resolve()/'upstream-core'
        patch = core_checkout(source, adapted)
        (o.output/'core-profile.patch').write_text(patch)
        source = adapted
    records = []
    for suite in o.suite or SUITES:
        command = ['./runtest', '--host', o.host, '--port', str(o.port), '--clients', '1', '--durable',
                   '--single', suite, '--singledb', '--ignore-encoding', '--ignore-digest',
                   '--tags', ' '.join('-' + x for x in EXCLUDED_TAGS)]
        for pattern in o.test or []:
            command += ['--only', pattern]
        logpath = o.output / (suite.replace('/', '-') + '.log')
        with logpath.open('w') as log:
            process = subprocess.Popen(command, cwd=source, stdout=log, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True)
            try:
                code = process.wait(timeout=o.timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as interruption:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                code = 124
                log.write('\nHARNESS INTERRUPTED OR TIMED OUT: suite incomplete\n')
                if isinstance(interruption, KeyboardInterrupt):
                    raise
        record = dict(suite=suite, command=command, exit_code=code,
                      **summarize(logpath.read_text(), code))
        records.append(record)
        (o.output / 'results.json').write_text(json.dumps(dict(upstream=spec,
            target=dict(host=o.host, port=o.port), excluded_tags=EXCLUDED_TAGS,
            profile='core (FUNCTION/SCRIPT FLUSH bootstrap excluded)' if o.core_profile else 'strict upstream bootstrap',
            options=['singledb', 'ignore-encoding', 'ignore-digest'],
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            suites=records), indent=2) + '\n')
        print(suite, record['status'], f"passed={record['passed']} failed={record['failed']} unsupported={record['unsupported']} skipped={record['skipped']} ignored={record['ignored']} exceptions={record['exceptions']}", flush=True)
    return 0 if all(r['status'] == 'passed' for r in records) else 1


if __name__ == '__main__':
    sys.exit(main())
