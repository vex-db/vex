"""Offline safety checks for the complete sweep and confirmation fallback."""
import pathlib
import json
import sys
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import sweep_request_scaling_aws as runner
import scaling_aws


class RequestScalingTest(unittest.TestCase):
    def test_pipeline_override_preserves_preload_and_connection_count(self):
        for clients, pipeline in ((1, 16), (16, 1)):
            command = runner.g.command('host', '/results/test', 'get', clients, 60, pipeline=pipeline)
            self.assertEqual(command[command.index('--pipeline') + 1], str(pipeline))
            self.assertEqual(int(command[command.index('-t') + 1]) * int(command[command.index('-c') + 1]) * pipeline, 512)
        preload = runner.g.command('host', '/results/test', 'get', 1, 0, preload=True, pipeline=16)
        self.assertEqual(preload[preload.index('--pipeline') + 1], '128')

    def test_remote_poll_timeout_does_not_relaunch_workload(self):
        ok = lambda text: subprocess.CompletedProcess([], 0, text, '')
        responses = [ok(''), subprocess.TimeoutExpired('poll', 30), ok('0'), ok('result'), ok('')]
        with patch.object(scaling_aws.s, 'run', side_effect=responses) as run, \
             patch.object(scaling_aws.time, 'sleep'):
            result = scaling_aws.remote_job(['benchmark'], '/unique-job', 240)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'result')
        launches = [call for call in run.call_args_list if any('subprocess.Popen' in str(x) for x in call.args[0])]
        self.assertEqual(len(launches), 1)

    def test_multiple_cells_write_one_final_summary_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / 'run'
            args = ['sweep', '--output', str(out), '--binary', 'unused',
                    '--namespace', 'vex-scaling-offline', '--cpus', '1', '2']
            command_result = types.SimpleNamespace(returncode=0, stderr='', stdout='{"Account":"208168340597"}')
            def prepare(options):
                options.output.mkdir()
                (options.output/'protocol.json').write_text('{}')
            with patch.object(sys, 'argv', args), \
                 patch.object(runner, 'prepare', side_effect=prepare), \
                 patch.object(runner, 'cell', side_effect=lambda o, cpu, workload: dict(cpu=cpu, workload=workload)), \
                 patch.object(runner.c, 'trace', return_value='offline fixture'), \
                 patch.object(runner.s, 'kub', return_value=''), \
                 patch.object(runner.s, 'memtier_hash'), \
                 patch.object(runner.s, 'run', return_value=command_result):
                runner.main()
            self.assertEqual(len(json.loads((out/'summary.json').read_text())), 6)
            self.assertEqual(json.loads((out/'cleanup-request.json').read_text())['exit'], 0)

    def test_all_connections_and_failed_confirmation_fallback(self):
        calls = []

        def phase(*args):
            connections, label = args[5], args[7]
            calls.append(label)
            passing = connections < 1024 and not label.startswith('confirm-c32-')
            return dict(connections=connections, ops_per_sec=1000000 / connections,
                        p99_ms=1 if passing else 6, client_valid=True,
                        pass_5ms=passing, label=label)

        with tempfile.TemporaryDirectory() as tmp:
            options = types.SimpleNamespace(output=pathlib.Path(tmp), deadline=time.monotonic()+30)
            with patch.object(runner.b, 'start', return_value=('host', {}, {}, {})), \
                 patch.object(runner.b, 'ena', return_value={'server': {'allowance': 0}}), \
                 patch.object(runner.g, 'phase'), \
                 patch.object(runner.c, 'phase', side_effect=phase), \
                 patch.object(runner.s, 'sampled_values', return_value={'verified': True}), \
                 patch.object(runner.s, 'assert_final_topology'):
                result = runner.cell(options, 4, 'get')
        self.assertEqual([x for x in calls if x.startswith('search-')],
                         [f'search-c{x}' for x in (32, 64, 128, 256, 512, 1024)])
        self.assertEqual(len(result['confirmed']), 3)
        self.assertEqual({r['connections'] for r in result['confirmed']}, {64})
        self.assertTrue(result['network_valid'])
        self.assertFalse(result['search'][-1]['pass_5ms'])


if __name__ == '__main__':
    unittest.main()
