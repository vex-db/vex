"""Offline accounting check for the AWS ownership comparison."""
from compare_adaptive_aws import summarize


def test_summary():
    raw = {'Seconds': 60, 'ClientStderr': '', 'ClientThreadCPUSeconds': {'1': 12},
           'Clients': [{'Error': '', 'Ops': 160, 'Writes': [128], 'Cold': False,
                        'Histogram': [10] + [0] * 20000} for _ in range(32)]}
    before = {'cgroup': {'cpu': {'usage_usec': 0, 'throttled_usec': 0}}}
    after = {'cgroup': {'cpu': {'usage_usec': 5120, 'throttled_usec': 0}}}
    row = summarize(raw, before, after, before, after)
    assert row['cpu_us_op'] == 1 and row['writes'] == 4096
    assert row['hot_batch_p99_ms'] == .01 and row['client_valid']
    raw['ClientThreadCPUSeconds'] = {'1': 59}
    assert not summarize(raw, before, after, before, after)['client_valid']
    raw['Clients'][0]['Error'] = 'bad reply'
    try:
        summarize(raw, before, after, before, after)
    except AssertionError:
        pass
    else:
        raise AssertionError('invalid client output accepted')


if __name__ == '__main__':
    test_summary()
    print('PASS accounting, latency, client CPU and error gates')
