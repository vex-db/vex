"""Offline guards against interrupted, mismatched or delayed accounting."""
import copy
from analyze_request_accounting import counters

raw = {'ALL STATS': {'Runtime': {'Time unit': 'MILLISECONDS', 'Interrupted': 'false', 'Total duration': 60000}, 'Totals': {'Count': 1000000}}}
a = dict(boot='same', process=dict(pid=1, start=2), monotonic=0,
         node={'stat': 'cpu 0 0 0 0 0 0 0 0\n'},
         cgroup={'cpu': dict(usage_usec=0, user_usec=0, system_usec=0)})
b = copy.deepcopy(a); b['monotonic'] = 66
b['node']['stat'] = 'cpu 10 0 20 30 0 0 40 0\n'
b['cgroup']['cpu'] = dict(usage_usec=3000000, user_usec=1000000, system_usec=2000000)
r = counters(raw, a, b)
assert r['accepted'] and r['system_us_per_op'] == 2
assert r['host_system_softirq_ticks_per_million_ops'] == 60
assert r['host_busy_ticks_per_million_ops'] == 70
b['monotonic'] = 600
assert not counters(raw, a, b)['accepted']
b['boot'] = 'replacement'
try: counters(raw, a, b)
except AssertionError: pass
else: raise AssertionError('accepted replacement host')
print('PASS counter units, delayed collection exclusion and host continuity')
