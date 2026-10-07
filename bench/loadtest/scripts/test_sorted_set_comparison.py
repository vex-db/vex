"""Offline guard against accidentally applying Vex settings to other engines."""
import argparse
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import compare_sorted_set_scaling as bench


class ComparisonConfigTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        build = pathlib.Path(directory.name)
        for version in ('before', 'after'):
            binary = build / f'static-{version}/bin/vex'
            binary.parent.mkdir(parents=True)
            binary.write_bytes(version.encode())
        patcher = patch.object(bench, 'BUILD', build)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_three_engine_protocol_and_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = argparse.Namespace(output=pathlib.Path(tmp)/'run',
                namespace='vex-scaling-offline-test', three_engines=True, cpus=[4,8,16], zone='ap-south-1b')
            bench.prepare(options)
            protocol = json.loads((options.output/'protocol.json').read_text())
            self.assertEqual(set(protocol['binaries']), {'redis', 'dragonfly', 'after'})
            self.assertEqual(protocol['primary_budget_ms'], 5)
            self.assertEqual(protocol['zone'], 'ap-south-1b')
            resources = json.loads((options.output/'resources.json').read_text())['items'][1:]
            for pool in resources:
                zone = next(r['values'] for r in pool['spec']['template']['spec']['requirements'] if r['key']=='topology.kubernetes.io/zone')
                self.assertEqual(zone, ['ap-south-1b'])
            for cpu in (4, 8, 16):
                for name in protocol['engines']:
                    out = options.output/f'c{cpu}-{name}'
                    out.mkdir()
                    # Stop exactly at pod creation: inspect the concrete request.
                    with patch.object(bench.s, 'kub', side_effect=['', RuntimeError('offline stop')]):
                        with self.assertRaisesRegex(RuntimeError, 'offline stop'):
                            bench.start(options, cpu, name, out)
                    engine = json.loads((out/'server.json').read_text())['spec']['containers'][0]
                    self.assertEqual(engine['resources']['limits'], {'cpu':str(cpu), 'memory':'8Gi'})
                    if name == 'redis':
                        self.assertEqual(engine['command'][-2:], ['--io-threads', str(cpu)])
                    elif name == 'dragonfly':
                        self.assertIn(f'--proactor_threads={cpu}', engine['command'])
                        self.assertIn('--maxmemory=6gb', engine['command'])
                    else:
                        self.assertIn(f'--workers {cpu}', engine['command'][-1])
                        self.assertEqual(engine['env'], [{'name':'VEX_PIN_WORKERS','value':'0'}])

    def test_resume_rejects_changed_hosts_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = argparse.Namespace(output=pathlib.Path(tmp)/'run',
                namespace='vex-scaling-offline-test', three_engines=True, cpus=[4,8,16], zone='ap-south-1b')
            bench.prepare(options)
            initial = {'items':[{'metadata':{'uid':str(i)}, 'status':{'nodeInfo':{'bootID':'old'}}} for i in range(2)]}
            (options.output/'nodes.json').write_text(json.dumps(initial))
            live = json.loads(json.dumps(initial))
            live['items'][0]['status']['nodeInfo']['bootID'] = 'rebooted'
            resources = json.loads((options.output/'resources.json').read_text())['items']
            with patch.object(bench.s, 'kub', side_effect=[*[json.dumps(r) for r in resources], json.dumps(live)]) as kub:
                with self.assertRaisesRegex(AssertionError, 'host changed'):
                    bench.resume(options)
                self.assertTrue(all(call.args[0]=='get' for call in kub.call_args_list))


if __name__ == '__main__':
    unittest.main()
