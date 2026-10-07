"""Small offline checks: skipped tests and empty runs must not become passes."""
from upstream import summarize

assert summarize('[ok]: one\n[skip]: two\n', 0)['status'] == 'passed'
assert summarize('[ok]: one\n[err]: broken\nunknown command X\n', 1)['unsupported'] == 1
assert summarize('[ok]: one\n[err]: broken\n', 0)['status'] == 'failed'
assert summarize('[skip]: unsupported\n', 0)['status'] == 'failed'
assert summarize('[ok]: one\n', 124)['status'] == 'failed'
assert summarize('[err]: unknown command X\n', 1)['unsupported_diagnostics']
assert summarize('[ok]: one\n[exception]: startup failed\n', 0)['status'] == 'failed'
print('PASS: failed, empty, skipped and timed-out runs cannot masquerade as passing suites')
