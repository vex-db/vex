#!/usr/bin/env python3
"""Run the fixed nine-cell Vex/Redis/Dragonfly AWS comparison.

The manifests and digest-pinned engine inventory are inputs, deliberately kept
outside this runner.  Each cell gets a new server process and a new preload;
results are append-only files under the dated run root.
"""
import argparse
import json
import pathlib
import statistics
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent))
import profile_aws_request_path as aws  # shared memtier validators/accounting rules

BASE = ROOT / "bench/loadtest/runs/2026-09-25-three-engine"
NS = "vex-compare-20260925"
CTX = "scrum-fresh"
SERVER, CLIENT = "server", "client"
ENGINE_CONTAINER, SAMPLER, MEMTIER = "engine", "sampler", "memtier"
KEYS, VALUE_BYTES, PIPELINE, CONNECTIONS = 1_000_000, 256, 1, 128
WARMUP_SECONDS, MEASURE_SECONDS, SERVER_CPU, CLIENT_CPU = 10, 60, 6, 24
MEMTIER_SHA256 = "f894894dcfd4cf3f38636b7ec679762d2c6ac5ea0363f98e68703844cf9a3207"
ORDERS = (("vex", "redis", "dragonfly"), ("redis", "dragonfly", "vex"), ("dragonfly", "vex", "redis"))
CELLS = tuple((f"round{round_no}-{engine}", engine) for round_no, order in enumerate(ORDERS, 1) for engine in order)
K = ["kubectl", "--context", CTX, "-n", NS]
aws.local.REQUIRE_MONOTONIC_CLOCK = True

# This runs in the Python sampler, which shares the primary container's PID
# namespace.  Cgroup files intentionally come from `primary_cgroup()` instead.
SNAPSHOT = r'''
import hashlib,json,os,pathlib,time
P=pathlib.Path
def proc(pid):
 p=P('/proc')/str(pid); q=(p/'stat').read_text().rsplit(')',1)[1].split()
 st={a:b.strip() for a,b in (x.split(':',1) for x in (p/'status').read_text().splitlines() if ':' in x)}
 tasks=[]
 for t in (p/'task').iterdir():
  try:
   z=(t/'stat').read_text().rsplit(')',1)[1].split(); tasks.append({'tid':int(t.name),'cpu_seconds':(int(z[11])+int(z[12]))/os.sysconf('SC_CLK_TCK'),'user_seconds':int(z[11])/os.sysconf('SC_CLK_TCK'),'system_seconds':int(z[12])/os.sysconf('SC_CLK_TCK'),'affinity':sorted(os.sched_getaffinity(int(t.name)))})
  except (OSError,ValueError,IndexError): pass
 return {'pid':int(pid),'start':q[19],'uid':int(st['Uid'].split()[0]),'rss_bytes':int(st.get('VmRSS','0 kB').split()[0])*1024,'hwm_bytes':int(st.get('VmHWM','0 kB').split()[0])*1024,'tasks':tasks}
def locate(name):
 found=[]
 for p in P('/proc').glob('[0-9]*'):
  try:
   if (p/'comm').read_text().strip()==name: found.append(p)
  except OSError: pass
 assert len(found)==1,(name,[x.name for x in found]); return found[0]
def light(pid):
 p=P('/proc')/str(pid);q=(p/'stat').read_text().rsplit(')',1)[1].split();st={a:b.strip() for a,b in (x.split(':',1) for x in (p/'status').read_text().splitlines() if ':' in x)}
 return {'pid':int(pid),'start':q[19],'rss_bytes':int(st.get('VmRSS','0 kB').split()[0])*1024,'hwm_bytes':int(st.get('VmHWM','0 kB').split()[0])*1024,'cpu_seconds':(int(q[11])+int(q[12]))/os.sysconf('SC_CLK_TCK')}
def snap(name):
 p=locate(name)
 return {'monotonic':time.monotonic(),'realtime':time.time(),'boot':P('/proc/sys/kernel/random/boot_id').read_text().strip(),'process':proc(p.name),'exe_sha256':hashlib.sha256((p/'exe').read_bytes()).hexdigest(),'node':{x:P('/proc/'+x).read_text() for x in ('stat','softirqs','net/dev','net/snmp','vmstat','meminfo')}}
'''


def run(args, timeout=120, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{args[:9]} exited {result.returncode}: {result.stderr[-2000:]}")
    return result


def kub(*args, timeout=120, check=True, read_only=False, retry_safe=False):
    retryable = retry_safe or read_only or (args and args[0] in ("get", "logs", "wait"))
    for attempt in range(4):
        try:
            return run(K + list(args), timeout, check).stdout
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            transient = isinstance(exc, subprocess.TimeoutExpired) or any(
                text in str(exc).lower() for text in
                ("connection reset", "unexpected eof", "tls handshake timeout", "unable to connect to the server",
                 "error reading from error stream", "read: operation timed out"))
            if not retryable or not transient or attempt == 3:
                raise
            time.sleep(2)


def ex(pod, container, *args, timeout=120, read_only=False):
    read_only = read_only or (args and args[0] in ("cat", "sha256sum"))
    return kub("exec", pod, "-c", container, "--", *args, timeout=timeout, read_only=read_only)


def write(path, value):
    assert not path.exists(), f"refusing to overwrite {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")


def save(out, name, value):
    write(out / name, value)


def engine_inventory():
    raw = json.loads((BASE / "engines.json").read_text())
    engines = raw.get("engines", raw)
    assert set(engines) == {"vex", "redis", "dragonfly"}, engines.keys()
    for name, item in engines.items():
        if "binary_sha256" not in item:
            for alias in ("expected_binary_sha256", "expected_binary_hash", "live_expected_binary_hash"):
                if alias in item:
                    item["binary_sha256"] = item[alias]
                    break
        if "command" not in item:
            for alias in ("server_command", "server_commands"):
                if alias in item:
                    item["command"] = item[alias]
                    break
        for key in ("image", "process_name", "binary_sha256", "command"):
            assert key in item, (name, key)
        assert "@sha256:" in item["image"] and len(item["binary_sha256"]) == 64, item
        assert isinstance(item["command"], list), item
    return engines


def pod(pod_name):
    return json.loads(kub("get", "pod", pod_name, "-o", "json"))


def container(pod_json, name):
    return next(x for x in pod_json["spec"]["containers"] if x["name"] == name)


def status(pod_json, name):
    return next(x for x in pod_json["status"]["containerStatuses"] if x["name"] == name)


def primary_cgroup(pod_name, container_name):
    # Keep this a shell/cat operation inside the primary workload container.
    raw = ex(pod_name, container_name, "sh", "-ec", "for x in cpu.stat cpu.max cpuset.cpus.effective memory.current memory.events memory.swap.current; do echo ===$x; cat /sys/fs/cgroup/$x; done", read_only=True)
    chunks, current = {}, None
    for line in raw.splitlines():
        if line.startswith("==="):
            current = line[3:]; chunks[current] = []
        elif current is not None:
            chunks[current].append(line)
    required = {"cpu.stat", "cpu.max", "cpuset.cpus.effective", "memory.current", "memory.events", "memory.swap.current"}
    assert set(chunks) == required, raw
    def fields(lines): return {a: int(b) for a, b in (x.split() for x in lines)}
    return {"raw": raw, "cpu": fields(chunks["cpu.stat"]), "cpu_max": "\n".join(chunks["cpu.max"]).strip(), "cpuset": "\n".join(chunks["cpuset.cpus.effective"]).strip(), "memory_current": int(chunks["memory.current"][0]), "memory_events": fields(chunks["memory.events"]), "swap_current": int(chunks["memory.swap.current"][0])}


def snapshot_server(process_name):
    data = json.loads(ex(SERVER, SAMPLER, "python3", "-c", SNAPSHOT + "\nprint(json.dumps(snap(" + repr(process_name) + ")))", read_only=True))
    data["cgroup"] = primary_cgroup(SERVER, ENGINE_CONTAINER)
    return data


def memtier_args(host, seconds, preload=False):
    args = ["memtier_benchmark", "-s", host, "-p", "6379", "-t", "1" if preload else "32", "-c", "1" if preload else "4", "--pipeline", "128" if preload else str(PIPELINE), "--ratio", "1:0" if preload else "1:4", "--key-prefix", "vex:key::", "--key-minimum", "1000000", "--key-maximum", "1999999", "--data-size", str(VALUE_BYTES), "--hide-histogram", "--print-percentiles", "50,95,99", "--json-out-file", "/results/load.json"]
    return args + (["--requests", str(KEYS), "--key-pattern", "S:S"] if preload else ["--test-time", str(seconds), "--key-pattern", "R:R", "--distinct-client-seed"])


def validate_preload(data, stderr, host):
    aws.local.assert_no_benchmark_errors(stderr)
    aws.local.parse_clock_metadata(stderr)
    cfg = data["configuration"]
    expected = {"server": host, "port": 6379, "threads": 1, "clients": 1, "pipeline": 128, "ratio": "1:0", "data_size": VALUE_BYTES, "key_prefix": "vex:key::", "key_minimum": 1000000, "key_maximum": 1999999}
    assert all(cfg.get(k) == v for k, v in expected.items()), cfg
    assert int(data["ALL STATS"]["Totals"]["Count"]) == KEYS
    assert str(data["ALL STATS"]["Runtime"].get("Interrupted", "false")).lower() == "false"


def sampled_values(host):
    aws.local.SERVER = host
    aws.local.dex = lambda unused, *args, **kwargs: ex(CLIENT, SAMPLER, *args, **kwargs)
    result = aws.local.probe_values()
    assert result["dbsize"] == KEYS, result
    return result


def memtier_hash():
    value = ex(CLIENT, MEMTIER, "sha256sum", "/usr/local/bin/memtier_benchmark").split()[0]
    assert value == MEMTIER_SHA256, value
    return value


def postload_checks(name, host, out, artifact, io_threads=6):
    code = r'''import json,socket
s=socket.create_connection((''' + repr(host) + r''',6379),10);f=s.makefile('rb')
def reply():
 line=f.readline(); assert line[:1] != b'-',line
 if line[:1] == b'$':
  n=int(line[1:])
  if n < 0: return None
  value=f.read(n).decode(); assert f.read(2)==b'\r\n'; return value
 if line[:1] == b'*': return [reply() for _ in range(int(line[1:]))]
 return line[1:].decode().strip()
def ask(parts):
 s.sendall(('*'+str(len(parts))+'\r\n'+''.join('$'+str(len(x))+'\r\n'+x+'\r\n' for x in parts)).encode());return reply()
result={'info_stats':ask(['INFO','stats'])}
if ''' + repr(name) + r''' == 'redis': result.update(redis_config=ask(['CONFIG','GET','io-threads']),info_threads=ask(['INFO','threads']))
if ''' + repr(name) + r''' == 'vex': result['debug_probes']=ask(['DEBUG','PROBES'])
print(json.dumps(result))'''
    result = json.loads(ex(SERVER, SAMPLER, "python3", "-c", code, read_only=True))
    stats = result["info_stats"]
    assert "evicted_keys:0" in stats, stats
    if name == "redis":
        assert result["redis_config"] == ["io-threads", str(io_threads)], result
        assert result["info_threads"], result
    if name == "vex":
        assert "probes_enabled=false" in result["debug_probes"], result
    save(out, artifact, result)
    return result


def assert_memory(before, after):
    for point in (before, after):
        cgroup = point["cgroup"]
        assert cgroup["swap_current"] == 0, cgroup
        assert not any(cgroup["memory_events"].get(k, 0) for k in ("high", "max", "oom", "oom_kill")), cgroup
    for field in ("pswpin", "pswpout"):
        def value(point): return int(dict(x.split() for x in point["node"]["vmstat"].splitlines())[field])
        assert value(before) == value(after), f"host paging changed: {field}"


def cpu(before, after, operations, quota, duration, reject_throttle):
    assert before["boot"] == after["boot"] and after["monotonic"] > before["monotonic"], (before["boot"], after["boot"], before["monotonic"], after["monotonic"])
    delta = aws.sat.cpu_delta(before["cgroup"]["cpu"], after["cgroup"]["cpu"])
    throttled = delta["throttled_usec"] / 1e6 / duration
    if reject_throttle:
        assert throttled <= .01, f"client CPU throttling {throttled:.2%}"
    return {"cpu_seconds": delta["usage_usec"] / 1e6, "cpu_us_per_op": delta["usage_usec"] / operations, "cpu_cores": delta["usage_usec"] / 1e6 / duration, "headroom_fraction": 1 - delta["usage_usec"] / 1e6 / duration / quota, "throttled_fraction": throttled, "delta": delta}


def measure(out, label, engine, host, seconds, loaded_rss_before_warmup=None):
    # This is the existing client wrapper/accounting pattern, with the cgroup
    # snapshots read from memtier itself and process accounting from its sampler.
    wrapper = SNAPSHOT + "\nimport pathlib,subprocess,time\nargs=" + repr(memtier_args(host, seconds)) + r'''
def client_snap(): return {'monotonic':time.monotonic(),'boot':P('/proc/sys/kernel/random/boot_id').read_text().strip(),'node':{x:P('/proc/'+x).read_text() for x in ('stat','softirqs','net/dev','net/snmp','vmstat','meminfo')}}
before=client_snap(); before['cgroup_raw']=subprocess.check_output(['sh','-ec','for x in cpu.stat cpu.max cpuset.cpus.effective memory.current memory.events memory.swap.current; do echo ===$x; cat /sys/fs/cgroup/$x; done'],text=True)
samples=[]
with open('/results/load.stdout','w') as stdout,open('/results/load.stderr','w') as stderr:
 child=subprocess.Popen(args,stdout=stdout,stderr=stderr)
 while child.poll() is None:
  try: samples.append({'monotonic':time.monotonic(),'process':proc(child.pid)})
  except (OSError,ProcessLookupError): pass
  time.sleep(.25)
after=client_snap(); after['cgroup_raw']=subprocess.check_output(['sh','-ec','for x in cpu.stat cpu.max cpuset.cpus.effective memory.current memory.events memory.swap.current; do echo ===$x; cat /sys/fs/cgroup/$x; done'],text=True)
pathlib.Path('/results/client-accounting.json').write_text(json.dumps({'before':before,'after':after,'samples':samples,'returncode':child.returncode}))
assert child.returncode==0,child.returncode
'''
    phase = out / label
    assert not phase.exists(), f"refusing prior phase {phase}"
    phase.mkdir()
    sampler = SNAPSHOT + "\nimport pathlib\npid=locate(" + repr(engine["process_name"]) + ").name\nstop=pathlib.Path('/results/sample-stop'); stop.unlink(missing_ok=True)\nwith open('/results/server-samples.jsonl','w',buffering=1) as f:\n while not stop.exists():\n  f.write(json.dumps({'monotonic':time.monotonic(),'process':light(pid)})+'\\n');time.sleep(.5)\n"
    proc = subprocess.Popen(K + ["exec", SERVER, "-c", SAMPLER, "--", "python3", "-c", sampler], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        before = snapshot_server(engine["process_name"])
        ex(CLIENT, MEMTIER, "python3", "-c", wrapper, timeout=seconds + 150)
        after = snapshot_server(engine["process_name"])
    finally:
        ex(SERVER, ENGINE_CONTAINER, "sh", "-ec", "touch /results/sample-stop")
        _, sampler_stderr = proc.communicate(timeout=20)
        assert proc.returncode == 0, sampler_stderr
    raw = {"stdout": ex(CLIENT, MEMTIER, "cat", "/results/load.stdout"), "stderr": ex(CLIENT, MEMTIER, "cat", "/results/load.stderr"), "json": json.loads(ex(CLIENT, MEMTIER, "cat", "/results/load.json")), "client": json.loads(ex(CLIENT, MEMTIER, "cat", "/results/client-accounting.json")), "samples": ex(SERVER, SAMPLER, "cat", "/results/server-samples.jsonl")}
    for suffix, value in raw.items(): save(phase, f"measure.{suffix}", value)
    save(phase, "measure.server-before.json", before); save(phase, "measure.server-after.json", after)
    save(phase, "measure.server-before-cgroup.raw", before["cgroup"]["raw"]); save(phase, "measure.server-after-cgroup.raw", after["cgroup"]["raw"])
    aws.local.assert_no_benchmark_errors(raw["stderr"])
    clock = aws.local.parse_clock_metadata(raw["stderr"])
    aws.local.validate_latency(raw["json"])
    row = aws.sat.validate_raw(raw["json"], PIPELINE, CONNECTIONS, seconds, host)
    assert row["connection_errors"] == 0 and row["misses_per_sec"] == 0, row
    assert {k: before["process"][k] for k in ("pid", "start")} == {k: after["process"][k] for k in ("pid", "start")}
    assert before["exe_sha256"] == after["exe_sha256"] == engine["binary_sha256"]
    assert before["cgroup"]["cpu_max"] == after["cgroup"]["cpu_max"] == "600000 100000"
    client_before, client_after = raw["client"]["before"], raw["client"]["after"]
    def client_point(point):
        point["cgroup"] = parse_cgroup(point.pop("cgroup_raw")); return point
    client_before, client_after = client_point(client_before), client_point(client_after)
    assert client_before["cgroup"]["cpu_max"] == client_after["cgroup"]["cpu_max"] == "2400000 100000"
    assert_memory(before, after); assert_memory(client_before, client_after)
    server_cpu = cpu(before, after, row["operation_count"], SERVER_CPU, row["runtime_ms"] / 1000, False)
    client_cpu = cpu(client_before, client_after, row["operation_count"], CLIENT_CPU, row["runtime_ms"] / 1000, True)
    assert client_cpu["headroom_fraction"] >= .20, client_cpu
    samples = [json.loads(x) for x in raw["samples"].splitlines() if x]
    samples = [x for x in samples if before["monotonic"] <= x["monotonic"] <= after["monotonic"]]
    assert len(samples) >= seconds, len(samples)
    by_tid = {}
    for point in raw["client"]["samples"]:
        for task in point["process"]["tasks"]: by_tid.setdefault(task["tid"], []).append((point["monotonic"], task["cpu_seconds"]))
    hot = [((v[-1][1]-v[0][1])/(v[-1][0]-v[0][0])) for v in by_tid.values() if len(v)>1 and v[-1][0]-v[0][0] >= seconds*.8]
    assert len(hot) >= 32 and max(hot) < .90, (len(hot), max(hot, default=None))
    post_values = sampled_values(host); save(phase, "post-sampled-values.json", post_values)
    row.update(cell=label, engine=next(k for k, v in engine_inventory().items() if v == engine), clock=clock, server_cpu=server_cpu, client_cpu=client_cpu, client_max_thread_cpu_fraction=max(hot), loaded_rss_before_warmup_bytes=loaded_rss_before_warmup, sampled_peak_rss_bytes=max(x["process"]["rss_bytes"] for x in samples), sampled_median_rss_bytes=statistics.median(x["process"]["rss_bytes"] for x in samples), values_after=post_values, server_throttling_is_measurement=True, measurement_valid=True)
    save(phase, "summary.json", row)
    return row


def parse_cgroup(raw):
    chunks, current = {}, None
    for line in raw.splitlines():
        if line.startswith("==="): current = line[3:]; chunks[current] = []
        elif current: chunks[current].append(line)
    def fields(lines): return {a: int(b) for a, b in (x.split() for x in lines)}
    return {"raw": raw, "cpu": fields(chunks["cpu.stat"]), "cpu_max":"\n".join(chunks["cpu.max"]).strip(), "cpuset":"\n".join(chunks["cpuset.cpus.effective"]).strip(), "memory_current":int(chunks["memory.current"][0]), "memory_events":fields(chunks["memory.events"]), "swap_current":int(chunks["memory.swap.current"][0])}


def validate_pods(engine, initial_nodes, server_cpu=6, server_machine="c7g.2xlarge",
                  client_cpu=24, client_machine="c7g.8xlarge", server_memory="4Gi", zone="ap-south-1a"):
    server, client = pod(SERVER), pod(CLIENT)
    assert server["spec"].get("shareProcessNamespace") and client["spec"].get("shareProcessNamespace")
    assert server["spec"]["nodeName"] != client["spec"]["nodeName"]
    by_role = {x["metadata"]["labels"]["bench.vex.dev/role"]: x for x in initial_nodes["items"]}
    for role, item, expected_type in (("server", server, server_machine), ("client", client, client_machine)):
        node = by_role[role]; assert item["spec"]["nodeName"] == node["metadata"]["name"]
        assert node["metadata"]["labels"]["node.kubernetes.io/instance-type"] == expected_type
        assert node["metadata"]["labels"]["topology.kubernetes.io/zone"] == zone
    primary = container(server, ENGINE_CONTAINER)
    assert primary["image"] == engine["image"] and primary.get("command", []) + primary.get("args", []) == engine["command"], primary
    assert status(server, ENGINE_CONTAINER)["restartCount"] == 0 and engine["image"].split("@", 1)[1] in status(server, ENGINE_CONTAINER)["imageID"]
    assert primary["resources"]["limits"] == {"cpu": str(server_cpu), "memory": server_memory}, primary["resources"]
    client_primary = container(client, MEMTIER)
    assert client_primary["resources"]["limits"]["cpu"] == str(client_cpu), client_primary["resources"]
    assert "@sha256:" in client_primary["image"] and status(client, MEMTIER)["restartCount"] == 0
    assert client_primary["image"].split("@", 1)[1] in status(client, MEMTIER)["imageID"]
    assert container(server, SAMPLER)["image"] == container(client, SAMPLER)["image"] and "@sha256:" in container(server, SAMPLER)["image"]
    return server, client


def live_topology(server, client, identity):
    node_names = (server["spec"]["nodeName"], client["spec"]["nodeName"])
    nodes = {name: json.loads(kub("get", "node", name, "-o", "json")) for name in node_names}
    return {"server_pod_uid": server["metadata"]["uid"], "client_pod_uid": client["metadata"]["uid"], "server_node": {"name": node_names[0], "uid": nodes[node_names[0]]["metadata"]["uid"], "provider_id": nodes[node_names[0]]["spec"].get("providerID")}, "client_node": {"name": node_names[1], "uid": nodes[node_names[1]]["metadata"]["uid"], "provider_id": nodes[node_names[1]]["spec"].get("providerID")}, "server_restarts": status(server, ENGINE_CONTAINER)["restartCount"], "client_restarts": status(client, MEMTIER)["restartCount"], "server_identity": {k: identity["process"][k] for k in ("pid", "start")}, "server_hash": identity["exe_sha256"], "server_boot": identity["boot"]}


def assert_final_topology(initial, engine):
    server, client = pod(SERVER), pod(CLIENT)
    final_identity = snapshot_server(engine["process_name"])
    final = live_topology(server, client, final_identity)
    for key in ("server_pod_uid", "client_pod_uid", "server_node", "client_node", "server_restarts", "client_restarts", "server_identity", "server_hash", "server_boot"):
        assert final[key] == initial[key], (key, initial[key], final[key])
    return final, final_identity


def run_cell(label, name):
    engines = engine_inventory(); engine = engines[name]; out = BASE / "cells" / label
    assert not out.exists(), f"refusing prior cell {label}"
    out.mkdir(parents=True)
    try:
        save(out, "runner.py", HERE.read_text()); save(out, "engine.json", engine)
        kub("delete", "pod", SERVER, "--ignore-not-found", "--wait=true", "--timeout=60s")
        manifest = BASE / f"server-{name}.manifest.json"; assert manifest.exists(), manifest
        kub("create", "-f", str(manifest)); kub("wait", "--for=condition=Ready", f"pod/{SERVER}", "--timeout=180s", timeout=190)
        nodes = json.loads((BASE / "nodes-active.json").read_text()); server, client = validate_pods(engine, nodes)
        save(out, "server-pod.json", server); save(out, "client-pod.json", client); save(out, "nodes-active.json", nodes)
        host = server["status"]["podIP"]
        save(out, "startup.log", kub("logs", SERVER, "-c", ENGINE_CONTAINER))
        live = snapshot_server(engine["process_name"]); assert live["exe_sha256"] == engine["binary_sha256"]
        initial_topology = live_topology(server, client, live); save(out, "initial-live-topology.json", initial_topology)
        save(out, "loaded-before-preload.json", live)
        save(out, "memtier-binary-before.sha256", memtier_hash() + "\n")
        preload = run(K + ["exec", CLIENT, "-c", MEMTIER, "--", *memtier_args(host, 0, True)], timeout=1000)
        save(out, "preload.stdout", preload.stdout); save(out, "preload.stderr", preload.stderr)
        data = json.loads(ex(CLIENT, MEMTIER, "cat", "/results/load.json")); save(out, "preload.json", data); validate_preload(data, preload.stderr, host)
        save(out, "preloaded-values.json", sampled_values(host)); postload_checks(name, host, out, "postload-engine-checks.json"); loaded = snapshot_server(engine["process_name"]); save(out, "loaded-before-warmup.json", loaded)
        warmup = measure(out, "warmup", engine, host, WARMUP_SECONDS); save(out, "warmup-reference.json", warmup)
        row = measure(out, label, engine, host, MEASURE_SECONDS, loaded["process"]["rss_bytes"])
        postload_checks(name, host, out, "postmeasure-engine-checks.json")
        save(out, "memtier-binary-after.sha256", memtier_hash() + "\n")
        final_topology, final_identity = assert_final_topology(initial_topology, engine)
        save(out, "final-live-topology.json", final_topology); save(out, "final-server-identity.json", final_identity)
        return row
    except Exception as exc:
        if not (out / "failure.json").exists(): save(out, "failure.json", {"cell": label, "engine": name, "reason": str(exc)})
        raise


def summarize(rows):
    by_engine = {name: [x for x in rows if x["engine"] == name] for name in ("vex", "redis", "dragonfly")}
    engines = {}
    for name, points in by_engine.items():
        values = [x["ops_per_sec"] if "ops_per_sec" in x else x["ops"] for x in points]; median = statistics.median(values) if values else 0
        engines[name] = {"cells": [x["cell"] for x in points], "ops_per_sec": values, "median_ops_per_sec": median, "range_fraction": (max(values)-min(values))/median if median else None, "stable_5_percent": len(values)==3 and (max(values)-min(values))/median <= .05}
    stable = all(x["stable_5_percent"] for x in engines.values())
    ranked = sorted(engines, key=lambda n: engines[n]["median_ops_per_sec"], reverse=True) if stable else []
    winner = ranked[0] if stable and engines[ranked[0]]["median_ops_per_sec"] >= engines[ranked[1]]["median_ops_per_sec"] * 1.05 else None
    return {"cells": rows, "engines": engines, "stable": stable, "winner_5_percent_gate": winner, "unstable_reason": None if stable else "per-engine range exceeds 5%; no favorable reruns"}


def self_test():
    aws.local.self_test(); compile(SNAPSHOT, "sampler", "exec")
    compile(SNAPSHOT + "\nimport pathlib\npid=locate('vex').name\nstop=pathlib.Path('/results/sample-stop')\nwith open('/tmp/x','w') as f:\n while not stop.exists():\n  f.write(json.dumps({'monotonic':time.monotonic(),'process':light(pid)})+'\\n');time.sleep(.5)\n", "lightweight sampler", "exec")
    scope = {}; exec("import io\nf=io.BytesIO(b'$1\\r\\na\\r\\n$1\\r\\nb\\r\\n')\ndef reply():\n line=f.readline(); n=int(line[1:]); v=f.read(n).decode(); assert f.read(2)==b'\\r\\n'; return v\nassert [reply(),reply()]==['a','b']", scope)
    assert [x[0] for x in CELLS] == ["round1-vex", "round1-redis", "round1-dragonfly", "round2-redis", "round2-dragonfly", "round2-vex", "round3-dragonfly", "round3-vex", "round3-redis"]
    assert parse_cgroup("===cpu.stat\nusage_usec 1\nuser_usec 1\nsystem_usec 0\nnr_periods 1\nnr_throttled 0\nthrottled_usec 0\n===cpu.max\n600000 100000\n===cpuset.cpus.effective\n0-5\n===memory.current\n1\n===memory.events\nlow 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n===memory.swap.current\n0\n")["cpu_max"] == "600000 100000"
    print("three-engine runner self-test passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", choices=[x[0] for x in CELLS]); parser.add_argument("--run", choices=["all9"]); parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test: return self_test()
    assert bool(args.cell) != bool(args.run), "choose --cell or --run all9"
    if args.cell: return run_cell(args.cell, dict(CELLS)[args.cell])
    assert not (BASE / "comparison.json").exists(), "refusing to overwrite comparison"
    rows = []
    for label, name in CELLS:
        rows.append(run_cell(label, name)); write(BASE / "progress" / f"{label}.json", rows)
    write(BASE / "comparison.json", summarize(rows))


if __name__ == "__main__":
    main()
