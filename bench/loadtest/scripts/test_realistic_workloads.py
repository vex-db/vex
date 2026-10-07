"""Small regression checks for rate semantics and measurement rejection."""
import copy
import json
import pathlib
import types
import unittest
from unittest.mock import patch

import realistic_workloads as workload


class WorkloadChecks(unittest.TestCase):
    def test_mixed_timeout_is_recorded_and_client_replaced(self):
        import hashlib
        import tempfile
        from contextlib import ExitStack
        import dragonfly_hypotheses as h
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=pathlib.Path(tmp);out=root/'cells/r1-zset-mixed-c16-p16-candidate';out.mkdir(parents=True)
            o=types.SimpleNamespace(output=root,suite='hypotheses-mixed',rounds=1,engines=['candidate'],keys=1000000,seconds=60,resume=False)
            ensure=stack.enter_context(patch.object(h.d,'ensure_client'))
            stack.enter_context(patch.object(h.d,'start',return_value=(out,'host',{'process_name':'vex'},{})))
            stack.enter_context(patch.object(h.s,'memtier_hash',return_value=h.s.MEMTIER_SHA256))
            stack.enter_context(patch.object(h.s,'run',return_value=types.SimpleNamespace(returncode=0,stdout='--monitor-input --monitor-pattern',stderr='')))
            stack.enter_context(patch.object(h.s,'kub'))
            stack.enter_context(patch.object(h.s,'ex',side_effect=['',hashlib.sha256(h.trace(mixed=True).encode()).hexdigest()]))
            stack.enter_context(patch.object(h.s,'snapshot_server',return_value={}))
            stack.enter_context(patch.object(h,'client_snapshot',return_value={}))
            stack.enter_context(patch.object(h.d.scaling,'remote_job',side_effect=[types.SimpleNamespace(returncode=0,stdout='{}',stderr=''),RuntimeError('remote job did not finish/export within its bound: test')]))
            capture=stack.enter_context(patch.object(h,'capture_timeout'))
            result=h.run_hypotheses(o,{})
            self.assertEqual(result[0]['status'],'timeout');self.assertEqual(ensure.call_count,2)
            capture.assert_called_once();self.assertTrue((out/'failed.json').exists());self.assertFalse((out/'complete.json').exists())

    def test_hypothesis_trace_and_commands_keep_work_fixed(self):
        import dragonfly_hypotheses as h
        import shlex
        normal=[shlex.split(x)[3:] for x in h.trace().splitlines()]
        hot=[shlex.split(x)[3:] for x in h.trace(True).splitlines()]
        self.assertEqual(len(normal),32768)
        self.assertEqual({x[1] for x in normal},{'lb:'+str(n) for n in range(64)})
        self.assertEqual({x[1] for x in hot},{'lb:0'})
        for a,b in zip(normal,hot):
            self.assertEqual(a[0],'ZINCRBY');self.assertEqual(a[2],'1')
            self.assertEqual(a[2:],b[2:]);self.assertTrue(0<=int(a[3][2:])<4096)
        o=types.SimpleNamespace(memtier='memtier_benchmark',host='host',port=6379,threads=32,clients=32,
             prefix='vex:hyp:',keys=1000000,output=pathlib.Path('/results'),profile='baseline')
        for pipeline in (1,16):
            cmd=h.args(o,'measure',60,pipeline)
            self.assertEqual(cmd[cmd.index('--pipeline')+1],str(pipeline))
            self.assertEqual(cmd[cmd.index('--ratio')+1],'1:4')
        self.assertEqual(len(h.CASES),7)
        mixed=[shlex.split(x)[3:] for x in h.trace(mixed=True).splitlines()]
        self.assertEqual(sum(x[0]=='ZRANK' for x in mixed),6554)
        self.assertTrue(all(x[0] in ('ZRANK','ZINCRBY') for x in mixed))

    def test_budget_search_confirms_each_limit_with_three_trials(self):
        import tempfile
        import scaling_diagnostics_aws as d
        from contextlib import ExitStack
        limits={'1':300000,'2':400000,'5':500000}
        def phase(o,out,host,engine,topology,rate,seconds,label):
            return dict(rate=rate,seconds=seconds,label=label,client_valid=True,
                        latency_budgets={k:{'pass':rate<=v} for k,v in limits.items()})
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=pathlib.Path(tmp);out=root/'cells/r1-budget-candidate';out.mkdir(parents=True)
            o=types.SimpleNamespace(output=root,engines=['candidate'],keys=1000000,seconds=20,resume=False)
            stack.enter_context(patch.object(d,'ensure_client'))
            start=stack.enter_context(patch.object(d,'start',return_value=(out,'host',{},{})))
            stack.enter_context(patch.object(d.scaling,'phase',side_effect=phase))
            stack.enter_context(patch.object(d.scaling,'remote_job',return_value=types.SimpleNamespace(returncode=0,stdout='ok',stderr='')))
            stack.enter_context(patch.object(d.s,'postload_checks'))
            result=d.run_budgets(o,{})[0]
            self.assertFalse(start.call_args.kwargs['pin'])
            for key,budget in result['budgets'].items():
                rate=budget['confirmed_rate'];self.assertGreater(rate,0);self.assertLessEqual(rate,limits[key])
                trials=[r for r in result['confirmations'] if r['rate']==rate]
                self.assertEqual(len(trials),3)
                self.assertTrue(all(r['seconds']==60 and r['latency_budgets'][key]['pass'] for r in trials))

    def test_latency_budgets_include_loss_and_overflow(self):
        from scaling_aws import latency_budget
        row=dict(client_valid=True,offered=1000,completed=1000,completed_in_window=1000,
                 errors=0,uncompleted=0,over_1ms=30,latency_histogram_10us=[0]*10001)
        row['latency_histogram_10us'][150]=20
        row['latency_histogram_10us'][0]=970
        row['latency_histogram_10us'][250]=9
        row['latency_histogram_10us'][10000]=1
        self.assertFalse(latency_budget(row,1)['pass'])
        self.assertTrue(latency_budget(row,2)['pass'])
        self.assertTrue(latency_budget(row,5)['pass'])
        self.assertAlmostEqual(latency_budget(row,5)['over_budget_or_uncompleted_fraction'],.001)
        for key,value in [('uncompleted',1),('errors',1),('client_valid',False),('completed_in_window',989)]:
            self.assertFalse(latency_budget({**row,key:value},5)['pass'])
        row['latency_histogram_10us'][500]=10
        row['latency_histogram_10us'][0]-=10
        self.assertFalse(latency_budget(row,5)['pass'])

    def test_diagnostic_resume_preserves_results_and_requires_same_boot(self):
        import hashlib
        import tempfile
        import scaling_diagnostics_aws as diagnostic
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root=pathlib.Path(tmp);binary=root/'driver';binary.write_bytes(b'driver')
            protocol=dict(namespace='vex-scaling-test',suite='diagnostics',seconds=90,rounds=3,keys=1000000,engines=[],
                          binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
            node={'metadata':{'uid':'node'},'status':{'nodeInfo':{'bootID':'boot'}}}
            for name,data in [('protocol.json',protocol),('engines.json',{}),('nodes-active.json',{'items':[node]})]:
                (root/name).write_text(json.dumps(data))
            complete=root/'cells/r1-done';complete.mkdir(parents=True);(complete/'complete.json').write_text('{}')
            partial=root/'cells/r2-partial';partial.mkdir();(partial/'evidence').write_text('preserve')
            argv=['runner','--source',tmp,'--output',tmp,'--namespace','vex-scaling-test','--binary',str(binary),'--resume']
            stack.enter_context(patch('sys.argv',argv))
            for key in ['BASE','NS','K']:stack.enter_context(patch.object(diagnostic.s,key,getattr(diagnostic.s,key)))
            stack.enter_context(patch.object(diagnostic.s,'run',return_value=types.SimpleNamespace(stdout='{"Account":"208168340597"}',stderr='',returncode=0)))
            kub=stack.enter_context(patch.object(diagnostic.s,'kub',return_value=json.dumps({'items':[{**node,'status':{'nodeInfo':{'bootID':'changed'}}}]})))
            stack.enter_context(patch.object(diagnostic,'ensure_client'))
            run=stack.enter_context(patch.object(diagnostic,'run_diagnostics',return_value=[]))
            with self.assertRaisesRegex(AssertionError,'changed hosts or boots'):diagnostic.main()
            run.assert_not_called();self.assertTrue(partial.exists())
            kub.return_value=json.dumps({'items':[node]})
            diagnostic.main()
            self.assertTrue((complete/'complete.json').exists())
            self.assertEqual(next((root/'interruptions').glob('*/r2-partial/evidence')).read_text(),'preserve')
            self.assertFalse(partial.exists())
            self.assertTrue(all(call.args[0]=='get' for call in kub.call_args_list))

    def test_scaling_diagnostic_controls_change_one_factor(self):
        from scaling_diagnostics_aws import diagnostics
        cases = {x[0]: x[1:] for x in diagnostics(1)}
        self.assertEqual(len(cases), 5)
        baseline = cases['8p-wide']
        # Each ablation changes one setting; rate lists differ only in coverage.
        for name, changed in [('4p-wide', 0), ('8p-client24', 2),
                              ('8unpinned-wide', 1), ('8p-shifted-wide', 4)]:
            row = cases[name]
            self.assertEqual([i for i in (0, 1, 2, 4) if row[i] != baseline[i]], [changed])
        self.assertEqual(baseline[3], [640000, 720000, 800000])
        self.assertNotEqual(diagnostics(1)[0][0], diagnostics(2)[0][0])

    def test_only_read_only_transport_calls_are_retried(self):
        import compare_aws_engines as shared
        import subprocess
        error = RuntimeError("error reading from error stream: next reader: read tcp 192.0.2.1:1234->192.0.2.2:443: read: operation timed out")
        ok = subprocess.CompletedProcess([], 0, "read result", "")
        with patch.object(shared, "run", side_effect=[error, ok]) as call, patch.object(shared.time, "sleep"):
            self.assertEqual(shared.kub("get", "pod", "server"), "read result")
            self.assertEqual(call.call_count, 2)
        with patch.object(shared, "run", side_effect=error) as call:
            with self.assertRaises(RuntimeError):
                shared.ex("client", "memtier", "/results/openloop")
            self.assertEqual(call.call_count, 1)

    def test_detached_trial_survives_lost_launch_ack(self):
        import scaling_aws as scaling
        import subprocess
        import sys
        import tempfile
        launches = []
        def local_transport(args, timeout=120, check=True):
            command = args[args.index("--") + 1:]
            if command[0] == "python3":
                command[0] = sys.executable
            result = subprocess.run(command, text=True, capture_output=True,
                                    timeout=timeout, check=check)
            if len(command) > 2 and "subprocess.Popen" in command[2]:
                launches.append(command)
                return subprocess.CompletedProcess(args, 1, "", "simulated lost launch response")
            return result
        with tempfile.TemporaryDirectory() as tmp, patch.object(scaling.s, "run", side_effect=local_transport):
            result = scaling.remote_job([sys.executable, "-c", "print('survived')"], tmp + "/trial", 10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "survived")
        self.assertEqual(len(launches), 1)

    def test_scaling_slo_rejects_loss_and_client_limits(self):
        from scaling_aws import passing
        row = dict(client_valid=True, slo_violation_fraction=.01,
                   completed_in_window=99000, offered=100000, errors=0, uncompleted=0)
        self.assertTrue(passing(row))
        for key, value in (("client_valid", False), ("slo_violation_fraction", .01001),
                           ("completed_in_window", 98999), ("errors", 1), ("uncompleted", 1)):
            self.assertFalse(passing({**row, key: value}))

    def test_balanced_engine_order(self):
        from realistic_aws import engine_order
        names = ["candidate", "redis", "dragonfly"]
        orders = [engine_order(names, 1, i) for i in range(3)]
        for position in range(3):
            self.assertEqual({order[position] for order in orders}, set(names))
        self.assertEqual(engine_order(names, 2, 0), orders[1])
        self.assertEqual(names, ["candidate", "redis", "dragonfly"])
        self.assertEqual(engine_order(["control", "candidate"], 1, 1), ["candidate", "control"])

    def test_client_memory_snapshot_contract(self):
        import realistic_aws as aws
        point = {"boot": "boot-id", "monotonic": 1, "node": {"vmstat": "pswpin 0\npswpout 0\n"}}
        group = {"swap_current": 0, "memory_events": {"high": 0, "max": 0, "oom": 0, "oom_kill": 0}}
        with patch.object(aws.shared, "ex", return_value=json.dumps(point)), patch.object(aws.shared, "primary_cgroup", return_value=group):
            before = aws.client_snapshot()
        after = copy.deepcopy(before)
        aws.shared.assert_memory(before, after)
        after["node"]["vmstat"] = "pswpin 1\npswpout 0\n"
        with self.assertRaises(AssertionError):
            aws.shared.assert_memory(before, after)

    def test_rates_and_burst(self):
        o = types.SimpleNamespace(memtier="memtier_benchmark", host="server", port=6379,
            threads=32, clients=4, prefix="test:", keys=1000, output=pathlib.Path("/tmp/check"), profile="hot")
        args = workload.command(o, "measure", 60, 128000)
        self.assertEqual(args[args.index("--rate-limiting") + 1], "1000")
        self.assertEqual(args[args.index("--key-pattern") + 1], "G:G")
        self.assertEqual([r for _, _, r in workload.stages("burst", 60, 128000)], [128000, 384000, 128000])
        with self.assertRaises(ValueError):
            workload.command(o, "measure", 60, 100)

    def test_rejects_invalid_and_allows_expected_misses(self):
        # A real checked-in-run shape, reduced here to the required fields.
        bucket = {"Average Latency": .2, "Max Latency": 1, "Min Latency": .1,
                  "Accumulated Latency": 200, "Percentile Latencies":
                  {"p50.00": .2, "p95.00": .4, "p99.00": .8, "p99.90": 1}}
        data = {"configuration": {"threads": 2, "clients": 2, "pipeline": 1}, "ALL STATS": {
            "Runtime": {"Time unit": "MILLISECONDS", "Total duration": 1000, "Interrupted": "false"},
            "Totals": {**bucket, "Count": 1000, "Ops/sec": 1000, "Connection Errors": 0,
                       "Time-Serie": {"0": copy.deepcopy(bucket)}},
            "Gets": {"Hits/sec": 600, "Misses/sec": 200}}}
        stderr = "MEMTIER_CLOCK " + json.dumps({"clock_source": "CLOCK_MONOTONIC", "source_tag": "2.5.1",
            "source_commit": workload.validation.CLOCK_COMMIT, "epoch_offset_us": 1})
        result = workload.validate_result(data, stderr, 1, 2000, True)
        self.assertEqual(result["miss_fraction"], .25)
        self.assertFalse(result["target_attained"])
        with self.assertRaises(AssertionError):
            workload.validate_result(data, stderr, 1, 2000, False)
        for mutation in ("Connection Errors", "Count"):
            bad = copy.deepcopy(data)
            bad["ALL STATS"]["Totals"][mutation] = 1 if mutation == "Connection Errors" else 0
            with self.assertRaises(AssertionError):
                workload.validate_result(bad, stderr, 1, 2000, True)
        with self.assertRaises(AssertionError):
            workload.validate_result(data, "", 1, 2000, True)
        with self.assertRaises(AssertionError):
            workload.validate_result(data, stderr + "\nhandle error response: ERR", 1, 2000, True)


if __name__ == "__main__":
    unittest.main()
