#!/usr/bin/env python3
"""Revalidate and combine the complete 4-CPU block with the replacement 8/16 block."""
import argparse
import csv
import json
from pathlib import Path
from summarize_sorted_set_scaling import summarize


def combine(first, replacement):
    protocols = [json.loads((r/'protocol.json').read_text()) for r in (first,replacement)]
    for key in ('server','client','memory','dataset','mixed','search','confirmation','binaries'):
        assert protocols[0][key] == protocols[1][key], (key, 'protocol changed')
    result = {'results':[], 'blocks':[], 'interpretation':'Matched hosts per CPU quota. Four CPUs on the first pair; eight and sixteen on the replacement pair. Closed-loop service latency; tested lower bounds, not exact capacity ceilings.'}
    for root,cpus in ((first,[4]),(replacement,[8,16])):
        data = summarize(root,cpus)
        result['blocks'].append({'run':root.name,'cpus':cpus,'zone':json.loads((root/'protocol.json').read_text()).get('zone','ap-south-1a')})
        for row in data['results']:
            assert row['network_valid']
            row['run'] = root.name
            result['results'].append(row)
    assert len(result['results']) == 27
    assert len({(r['cpu'],r['version'],r['profile']) for r in result['results']}) == 27
    result['vex_vs_dragonfly'] = []
    for cpu in (4,8,16):
        for profile in ('mixed','pressure','update'):
            v,d = [next(r for r in result['results'] if (r['cpu'],r['version'],r['profile'])==(cpu,e,profile)) for e in ('after','dragonfly')]
            result['vex_vs_dragonfly'].append(dict(cpu=cpu,profile=profile,throughput_ratio=v['median_ops_s']/d['median_ops_s'],p99_ratio=v['median_p99_ms']/d['median_p99_ms'],overlapping_throughput_ranges=max(v['min_ops_s'],d['min_ops_s'])<=min(v['max_ops_s'],d['max_ops_s'])))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('first',type=Path);p.add_argument('replacement',type=Path);p.add_argument('output',type=Path)
    o=p.parse_args();data=combine(o.first,o.replacement)
    o.output.write_text(json.dumps(data,indent=2)+'\n')
    with o.output.with_suffix('.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(data['results'][0]));w.writeheader();w.writerows(data['results'])
    print(json.dumps(data['vex_vs_dragonfly'],indent=2))
