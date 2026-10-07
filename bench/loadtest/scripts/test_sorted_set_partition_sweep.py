"""Offline checks for the same-binary partition sweep protocol."""
import argparse
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import sweep_sorted_set_partitions_aws as sweep


class PartitionSweepTest(unittest.TestCase):
    def test_hot_reads_and_writes_target_same_key(self):
        lines = sweep.trace(64, 4096, True).splitlines()
        self.assertEqual(len(lines), 32768)
        self.assertTrue(all('"lb:0"' in line for line in lines))
        self.assertEqual(sum('"ZRANK"' in line for line in lines), 6554)
        self.assertEqual(sweep.trace(64, 4096, True), '\n'.join(lines)+'\n')

    def test_matrix_and_concrete_server_command(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(sweep.b, 'BUILD'):
            options = argparse.Namespace(output=pathlib.Path(tmp)/'run', namespace='vex-scaling-offline',
                binary=pathlib.Path(__file__), zone='ap-south-1b')
            protocol = sweep.prepare(options)
            self.assertEqual(len(protocol['order']), 45)
            self.assertEqual(len({tuple(row.values()) for row in protocol['order']}), 45)
            out = options.output/'example';out.mkdir()
            with patch.object(sweep.s, 'kub', side_effect=['', RuntimeError('offline stop')]):
                with self.assertRaisesRegex(RuntimeError, 'offline stop'):
                    sweep.b.start(options, 8, 'after', out, partitions=512)
            import json
            primary = json.loads((out/'server.json').read_text())['spec']['containers'][0]
            self.assertIn('--workers 8', primary['command'][-1])
            self.assertIn('--sorted-set-partitions 512', primary['command'][-1])
            self.assertEqual(primary['resources']['limits'], {'cpu':'8','memory':'8Gi'})
            self.assertEqual(primary['env'], [{'name':'VEX_PIN_WORKERS','value':'0'}])

    def test_resume_rejects_changed_hosts_before_mutation(self):
        import json
        with tempfile.TemporaryDirectory() as tmp, patch.object(sweep.b, 'BUILD'):
            options = argparse.Namespace(output=pathlib.Path(tmp)/'run', namespace='vex-scaling-offline',
                binary=pathlib.Path(__file__), zone='ap-south-1b')
            sweep.prepare(options)
            initial={'items':[{'metadata':{'uid':'server'},'status':{'nodeInfo':{'bootID':'original'}},'spec':{'providerID':'aws:///instance'}}]}
            (options.output/'nodes.json').write_text(json.dumps(initial))
            changed=json.loads(json.dumps(initial));changed['items'][0]['status']['nodeInfo']['bootID']='rebooted'
            with patch.object(sweep.s,'kub',return_value=json.dumps(changed)) as kub:
                with self.assertRaisesRegex(AssertionError,'hosts changed'):
                    sweep.resume(options)
                self.assertTrue(all(call.args[0]=='get' for call in kub.call_args_list))

    def test_remote_checker_compiles(self):
        compile(sweep.CHECK, '<remote checker>', 'exec')


if __name__ == '__main__': unittest.main()
