"""Guard against filling unmeasured cells or treating different hosts as scaling."""
import copy
import json
import math

from summarize_current_request_scaling import ROOT, SOURCES, consolidate

sources = {name: json.loads((ROOT / path).read_text()) for name, path in SOURCES.items() if name in ('r4', 'r5')}
result = consolidate(sources)
assert (result['completed'], result['expected']) == (9, 15)
assert {(r['cpu'], r['workload']) for r in result['missing']} == {
    (2, 'zset'), (4, 'get'), (4, 'hash'), (4, 'zset'), (8, 'hash'), (8, 'zset')}
ratios = {(r['cohort'], r['workload'], r['from_cpu'], r['to_cpu']): r
          for r in result['same_cohort_scaling']}
assert set(ratios) == {('r5', 'get', 1, 2), ('r5', 'hash', 1, 2), ('r4', 'get', 8, 16)}
r = ratios['r4', 'get', 8, 16]
assert math.isclose(r['throughput_ratio'], 1523293.57 / 858742.15)
assert math.isclose(r['quota_scaling_efficiency_pct'], r['throughput_ratio'] * 50)
assert all(r['ops_per_allocated_cpu'] == r['ops_per_sec'] / r['cpu'] for r in result['rows'])
# Invalid confirmations must not fill a cell or contribute to a scaling ratio.
invalid = copy.deepcopy(sources)
invalid['r4']['rows'][0]['valid'] = False
reduced = consolidate(invalid)
assert reduced['completed'] == 8 and len(reduced['same_cohort_scaling']) == 2
# Different builds cannot silently enter the current-build inventory.
mismatch = copy.deepcopy(sources)
mismatch['r4']['protocol']['binary_sha256'] = 'different'
try:
    consolidate(mismatch)
except AssertionError:
    pass
else:
    raise AssertionError('accepted incompatible builds')
print('PASS derived units, missing cells, invalid results and cohort boundaries')
# Repeated measurements retain their own host identity and replace only display cells.
repeated = copy.deepcopy(sources)
repeated['new'] = copy.deepcopy(sources['r5'])
repeated['new']['rows'] = [repeated['new']['rows'][0]]
repeated['new']['rows'][0]['ops_per_sec'] = 123456
merged = consolidate(repeated)
assert merged['completed'] == 9 and len(merged['cohort_rows']) == 10
assert next(r for r in merged['rows'] if r['cpu'] == 1 and r['workload'] == 'get')['ops_per_sec'] == 123456
assert merged['same_cohort_scaling'] == result['same_cohort_scaling']
print('PASS newer display values do not create cross-host scaling ratios')
