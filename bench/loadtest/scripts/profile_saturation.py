#!/usr/bin/env python3
"""Bounded saturation matrix and attribution capture for the compact Vex build.

This runner assumes the namespace, NodePool, client pod, holder and service
already exist. It owns only fresh benchmark-server pods and writes raw results
under the requested output directory; importing this module performs no cluster
operation.
"""
import argparse
import hashlib
import json
import pathlib
import statistics
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[3]
import scrum_benchmark as bench
from scrum_results import metrics


CTX = "scrum-fresh"
NS = "vex-saturation-20260922"
LOADGEN = "loadgen-large"
OUT = ROOT / "bench" / "loadtest" / "runs" / "2026-09-22-saturation"
DESTINATION_HOST = "benchmark-server"
DESTINATION_PORT = 6379
BINARY_NAME = "vex-linux-amd64"
BINARY_SHA256 = "200015d348ba5b5ce642fa3dd502d8249e3bb02c229f3bfef43a260f01778f0e"
SERVER_MACHINE = "c6a.2xlarge"
CLIENT_MACHINE = "c6a.8xlarge"
SERVER_CPUS = 6
CLIENT_CPUS = 24
KEYS = 1_000_000
KEY_MINIMUM = 1_000_000
KEY_MAXIMUM = 1_999_999
KEY_PREFIX = "vex:key::"
VALUE_BYTES = 256
THREADS = 32
RATIO = "1:4"
WARMUP_SECONDS = 10
SAMPLE_SECONDS = 30
THROTTLE_THRESHOLD = 0.01

BASE_CELLS = (
    ("p1-c32", 1, 32),
    ("p1-c128", 1, 128),
    ("p1-c512", 1, 512),
    ("p8-c128", 8, 128),
)


def configure(namespace=NS, output=OUT, context=CTX, destination_host=DESTINATION_HOST, destination_port=DESTINATION_PORT):
    """Configure the imported helper only when the CLI explicitly runs."""
    global NS, OUT, CTX, DESTINATION_HOST, DESTINATION_PORT
    NS, OUT, CTX = namespace, pathlib.Path(output), context
    DESTINATION_HOST, DESTINATION_PORT = destination_host, int(destination_port)
    bench.NS, bench.OUT, bench.LOADGEN, bench.CTX = NS, OUT, LOADGEN, CTX
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / ".gitignore").write_text("*\n!.gitignore\n")


def save(name, value):
    bench.save(name, value)


def kub(*args, **kwargs):
    return bench.kub(*args, **kwargs)


def execpod(pod, container, *args, **kwargs):
    return bench.execpod(pod, container, *args, **kwargs)


def parse_cpu_stat(text):
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return values


def cpu_delta(before, after):
    keys = ("usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec")
    delta = {}
    for key in keys:
        assert key in before and key in after, f"missing cgroup cpu.stat counter: {key}"
        delta[key] = after[key] - before[key]
        assert delta[key] >= 0, f"cgroup cpu.stat counter reset during sample: {key}"
    return delta


def read_cpu(scope, pod, container, stem, phase):
    raw = execpod(pod, container, "cat", "/sys/fs/cgroup/cpu.stat")
    save(f"{stem}-{scope}-cpu-{phase}.stat", raw)
    return parse_cpu_stat(raw)


def capture_kernel(scope, pod, container, stem, phase):
    paths = {
        "proc-stat": "/proc/stat",
        "softirq": "/proc/softirqs",
        "netdev": "/proc/net/dev",
        "snmp": "/proc/net/snmp",
    }
    files = {}
    for label, path in paths.items():
        name = f"{stem}-{scope}-{phase}-{label}.txt"
        save(name, execpod(pod, container, "cat", path))
        files[label] = name
    return files


def process_samples(pod, stem, phase):
    name = f"{stem}-{pod}-process.jsonl"
    save(name, execpod(pod, "sampler", "cat", "/results/process.jsonl"))
    return name


def process_identity(stem, phase):
    """Capture PID/start-time/executable for the live server process."""
    raw = execpod(
        "benchmark-server", "profiler", "sh", "-ec",
        "for p in /proc/[0-9]*; do case \"$(cat \"$p/comm\" 2>/dev/null)\" in vex*) pid=$(basename \"$p\"); echo PID=$pid START=$(awk '{print $22}' \"$p/stat\") EXE=$(readlink \"$p/exe\");; esac; done",
    )
    save(f"{stem}-process-identity-{phase}.txt", raw)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    assert len(lines) == 1, raw
    return lines[0]


def verify_sampled_values(destination_host=None, destination_port=None):
    destination_host = DESTINATION_HOST if destination_host is None else destination_host
    destination_port = DESTINATION_PORT if destination_port is None else int(destination_port)
    code = rf'''import json,random,socket
s=socket.create_connection(({destination_host!r},{int(destination_port)}),10); f=s.makefile('rb')
r=random.Random(22); ids=[1000000,1999999]+r.sample(range(1000000,2000000),30)
for i in ids:
 k=('vex:key::'+str(i)).encode(); assert len(k)==16
 s.sendall(b'*2\r\n$3\r\nGET\r\n$16\r\n'+k+b'\r\n')
 line=f.readline(); assert line.startswith(b'$'), line
 n=int(line[1:]); assert n==256, (i,n)
 value=f.read(n); assert len(value)==n and f.read(2)==b'\r\n'
print(json.dumps({{'sampled_keys':len(ids),'key_bytes':16,'value_bytes':256,'valid':True}}))
'''
    return json.loads(execpod(LOADGEN, "sampler", "python3", "-c", code))


def validate_raw(data, pipeline, connections, duration_seconds, destination_host=None, destination_port=None):
    destination_host = DESTINATION_HOST if destination_host is None else destination_host
    destination_port = DESTINATION_PORT if destination_port is None else int(destination_port)
    cfg = data["configuration"]
    expected_clients = connections // THREADS
    expected = {
        "pipeline": pipeline,
        "threads": THREADS,
        "clients": expected_clients,
        "test_time": duration_seconds,
        "server": destination_host,
        "port": int(destination_port),
        "data_size": VALUE_BYTES,
        "ratio": RATIO,
        "key_prefix": KEY_PREFIX,
        "key_minimum": KEY_MINIMUM,
        "key_maximum": KEY_MAXIMUM,
    }
    for key, value in expected.items():
        assert cfg.get(key) == value, (key, cfg.get(key), value)
    runtime = data["ALL STATS"]["Runtime"]
    assert runtime["Time unit"] == "MILLISECONDS"
    expected_ms = duration_seconds * 1000
    assert int((duration_seconds - 1) * 1000) <= runtime["Total duration"] <= int((duration_seconds + 3) * 1000), runtime
    assert str(runtime.get("Interrupted", "false")).lower() == "false"
    summary = metrics(data)
    totals = data["ALL STATS"]["Totals"]
    assert int(totals["Count"]) > 0
    return summary | {
        "operation_count": int(totals["Count"]),
        "runtime_ms": int(runtime["Total duration"]),
        "connection_errors": int(totals.get("Connection Errors", 0)),
        "misses_per_sec": float(totals.get("Misses/sec", 0)),
        "configuration": cfg,
    }


def run_memtier(stem, pipeline, connections, seconds, preload=False):
    threads = 1 if preload else THREADS
    clients = 1 if preload else connections // THREADS
    args = [
        "memtier_benchmark", "-s", DESTINATION_HOST, "-p", str(DESTINATION_PORT),
        "-t", str(threads), "-c", str(clients), "--pipeline", "128" if preload else str(pipeline),
        "--ratio", "1:0" if preload else RATIO, "--key-prefix", KEY_PREFIX,
        "--key-minimum", str(KEY_MINIMUM), "--key-maximum", str(KEY_MAXIMUM),
        "--data-size", str(VALUE_BYTES), "--hide-histogram",
        "--print-percentiles", "50,95,99", "--json-out-file", f"/tmp/{stem}.json",
    ]
    if preload:
        args += ["--requests", str(KEYS), "--key-pattern", "S:S"]
    else:
        args += ["--test-time", str(seconds), "--key-pattern", "R:R", "--distinct-client-seed"]
    started = time.time()
    proc = subprocess.run(
        ["kubectl", "--context", CTX, "-n", NS, "exec", LOADGEN, "-c", "loadgen", "--", *args],
        text=True, capture_output=True, timeout=900 if preload else seconds + 180,
    )
    finished = time.time()
    save(f"{stem}.log", proc.stdout)
    save(f"{stem}.stderr.log", proc.stderr)
    if proc.returncode:
        raise RuntimeError(f"{stem}: memtier failed: {proc.stderr[-2000:]}")
    raw = execpod(LOADGEN, "loadgen", "cat", f"/tmp/{stem}.json")
    save(f"{stem}.json", raw)
    data = json.loads(raw)
    if preload:
        cfg = data["configuration"]
        assert cfg["key_minimum"] == KEY_MINIMUM and cfg["key_maximum"] == KEY_MAXIMUM
        assert int(data["ALL STATS"]["Totals"]["Count"]) == KEYS
        return data, started, finished
    return data, started, finished


def run_mt(stem, connections, pipeline, seconds=20):
    """Run one timed sample against the already-running server.

    Parent diagnostics use this hook so a profile can share the matrix's
    configured namespace/output without restarting or mutating the server.
    """
    data, started, finished = run_memtier(stem, pipeline, connections, seconds)
    result = validate_raw(data, pipeline, connections, seconds)
    return {"data": data, "start": started, "end": finished, **result}


def profiler_container():
    return {
        "name": "profiler",
        "image": bench.IMAGES["python"],
        "command": ["sh", "-c", "sleep 10000"],
        "securityContext": {
            "runAsUser": 0,
            "capabilities": {"add": ["PERFMON", "SYS_PTRACE"]},
            "seccompProfile": {"type": "Unconfined"},
        },
        "resources": {
            "requests": {"cpu": "50m", "memory": "128Mi"},
            "limits": {"cpu": "2", "memory": "1Gi"},
        },
    }


def start_server(stem, binary):
    kub("delete", "pod", "benchmark-server", "--ignore-not-found", "--wait=true", "--timeout=60s")
    server = {
        "name": "server", "image": bench.IMAGES["vex"],
        "command": ["sh", "-ec", "while [ ! -x /tmp/vex-benchmark ]; do sleep 1; done; exec /tmp/vex-benchmark \"$@\"", "vex-benchmark"],
        "args": ["--reactor", "--workers", str(SERVER_CPUS), "--no-persistence", "--port", "6379"],
        "ports": [{"containerPort": 6379}],
        "resources": {"requests": {"cpu": str(SERVER_CPUS), "memory": "10Gi"},
                      "limits": {"cpu": str(SERVER_CPUS), "memory": "10Gi"}},
        "readinessProbe": {"tcpSocket": {"port": 6379}, "periodSeconds": 2, "failureThreshold": 90},
    }
    obj = bench.pod("benchmark-server", "server", SERVER_MACHINE, [server], True)
    obj["metadata"]["labels"]["endpoint"] = "server"
    obj["spec"]["containers"].append(profiler_container())
    bench.apply(obj)
    kub("wait", "--for=jsonpath={.status.phase}=Running", "pod/benchmark-server", "--timeout=300s", timeout=320)
    kub("cp", str(binary), "benchmark-server:/tmp/vex-upload", "-c", "server", timeout=120)
    execpod("benchmark-server", "server", "sh", "-ec", "chmod 755 /tmp/vex-upload; mv /tmp/vex-upload /tmp/vex-benchmark")
    kub("wait", "--for=condition=Ready", "pod/benchmark-server", "--timeout=300s", timeout=320)
    for _ in range(30):
        try:
            if bench.cli("PING") == "PONG":
                break
        except RuntimeError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("benchmark server did not become ready")
    pod = json.loads(kub("get", "pod", "benchmark-server", "-o", "json"))
    save(f"{stem}-server-pod.json", pod)
    startup = kub("logs", "benchmark-server", "-c", "server")
    save(f"{stem}-startup.log", startup)
    backend = [line for line in startup.splitlines() if "event_loop: io_uring backend active" in line]
    assert len(backend) >= SERVER_CPUS, f"missing worker backend logs: {startup[-2000:]}"
    identity = execpod(
        "benchmark-server", "profiler", "sh", "-ec",
        "for p in /proc/[0-9]*; do case \"$(cat \"$p/comm\" 2>/dev/null)\" in vex*) echo PID=$(basename \"$p\"); readlink \"$p/exe\"; sha256sum \"$p/exe\";; esac; done",
    )
    save(f"{stem}-live-binary-identity.txt", identity)
    assert BINARY_SHA256 in identity, identity
    return pod, backend


def rss_metrics(path, start, end, prefix):
    samples = []
    for line in pathlib.Path(path).read_text().splitlines():
        row = json.loads(line)
        if start <= row["time"] <= end:
            procs = [p for p in row["processes"] if p["name"].startswith(prefix)]
            if procs:
                samples.append((row["time"], sum(p["rss_bytes"] for p in procs), sum(p["cpu_seconds"] for p in procs)))
    assert len(samples) >= 2, f"insufficient {prefix} sampler rows in {path}"
    sample_span = max(0.001, samples[-1][0] - samples[0][0])
    process_cpu_seconds = max(0.0, samples[-1][2] - samples[0][2])
    return {
        "peak_rss_mib": max(x[1] for x in samples) / 1048576,
        "mean_rss_mib": statistics.mean(x[1] for x in samples) / 1048576,
        "sample_span_seconds": sample_span,
        "process_cpu_seconds": process_cpu_seconds,
        "process_cpu_cores": process_cpu_seconds / sample_span,
    }


def classify_throttle(cpu, wall_seconds, scope, quota):
    throttled = cpu.get("throttled_usec", 0)
    quota_capacity = wall_seconds * 1_000_000 * quota
    quota_ratio = throttled / quota_capacity if quota_capacity else 0.0
    wall_ratio = throttled / (wall_seconds * 1_000_000) if wall_seconds else 0.0
    return {
        "scope": scope,
        "threshold": THROTTLE_THRESHOLD,
        "cpu_quota": quota,
        "throttled_quota_capacity_ratio": quota_ratio,
        "throttled_wall_ratio": wall_ratio,
        "significant": quota_ratio >= THROTTLE_THRESHOLD or wall_ratio >= THROTTLE_THRESHOLD,
    }


def run_cell(cell, pod, backend, client_uid, client_node, pre_sampled):
    label, pipeline, connections = cell
    stem = f"{label}"
    current_client = json.loads(kub("get", "pod", LOADGEN, "-o", "json"))
    assert current_client["metadata"]["uid"] == client_uid
    assert current_client["spec"]["nodeName"] == client_node
    assert pod["spec"]["nodeName"] != client_node
    server_uid = pod["metadata"]["uid"]
    before_keys = int(bench.cli("DBSIZE") or 0)
    assert before_keys == KEYS, (stem, before_keys)
    assert pre_sampled["valid"] and pre_sampled["sampled_keys"] >= 32

    warmup, _, _ = run_memtier(f"{stem}-warmup", pipeline, connections, WARMUP_SECONDS)
    validate_raw(warmup, pipeline, connections, WARMUP_SECONDS)
    server_process_before = process_identity(stem, "before")
    kernel_before = {
        "server": capture_kernel("server", "benchmark-server", "server", stem, "before"),
        "client": capture_kernel("client", LOADGEN, "loadgen", stem, "before"),
    }
    server_before = read_cpu("server", "benchmark-server", "server", stem, "before")
    client_before = read_cpu("client", LOADGEN, "loadgen", stem, "before")
    data, _, _ = run_memtier(stem, pipeline, connections, SAMPLE_SECONDS)
    server_after = read_cpu("server", "benchmark-server", "server", stem, "after")
    client_after = read_cpu("client", LOADGEN, "loadgen", stem, "after")
    kernel_after = {
        "server": capture_kernel("server", "benchmark-server", "server", stem, "after"),
        "client": capture_kernel("client", LOADGEN, "loadgen", stem, "after"),
    }
    server_proc_file = process_samples("benchmark-server", stem, "server")
    client_proc_file = process_samples(LOADGEN, stem, "client")
    result = validate_raw(data, pipeline, connections, SAMPLE_SECONDS)
    runtime = data["ALL STATS"]["Runtime"]
    wall_start = runtime["Start time"] / 1000
    wall_end = runtime["Finish time"] / 1000
    wall_seconds = max(0.001, (runtime["Total duration"] or 0) / 1000)
    server_cpu = cpu_delta(server_before, server_after)
    client_cpu = cpu_delta(client_before, client_after)
    server_rss = rss_metrics(OUT / server_proc_file, wall_start, wall_end, "vex")
    client_rss = rss_metrics(OUT / client_proc_file, wall_start, wall_end, "memtier")
    server_process_after = process_identity(stem, "after")
    assert server_process_before == server_process_after, (server_process_before, server_process_after)
    post_sampled = verify_sampled_values()
    save(f"{stem}-post-sampled-values.json", post_sampled)
    assert post_sampled["valid"] and post_sampled["sampled_keys"] >= 32
    end_server = json.loads(kub("get", "pod", "benchmark-server", "-o", "json"))
    end_client = json.loads(kub("get", "pod", LOADGEN, "-o", "json"))
    assert end_server["metadata"]["uid"] == server_uid
    assert end_server["spec"]["nodeName"] == pod["spec"]["nodeName"]
    assert end_client["metadata"]["uid"] == client_uid
    assert end_client["spec"]["nodeName"] == client_node
    after_keys = int(bench.cli("DBSIZE"))
    assert after_keys == KEYS, (stem, after_keys)
    throttles = [
        classify_throttle(server_cpu, wall_seconds, "server", SERVER_CPUS),
        classify_throttle(client_cpu, wall_seconds, "client", CLIENT_CPUS),
    ]
    row = {
        "cell": label, "pipeline": pipeline, "connections": connections, "threads": THREADS,
        "clients": connections // THREADS, "ratio": RATIO, "value_bytes": VALUE_BYTES,
        "key_count_before": before_keys, "key_count_after": after_keys,
        "operations": result["operation_count"], "ops_per_sec": result["ops"], "p99_ms": result["p99_ms"],
        "runtime_ms": result["runtime_ms"], "connection_errors": result["connection_errors"],
        "misses_per_sec": result["misses_per_sec"], "server_pod_uid": server_uid,
        "server_node": pod["spec"]["nodeName"], "client_pod_uid": client_uid, "client_node": client_node,
        "server_process_identity_before": server_process_before,
        "server_process_identity_after": server_process_after,
        "destination_host": DESTINATION_HOST, "destination_port": DESTINATION_PORT,
        "backend": backend, "binary_sha256": BINARY_SHA256, "sampled_values": pre_sampled,
        "post_sampled_values": post_sampled,
        "server_rss": server_rss, "client_rss": client_rss,
        "server_cpu_delta": server_cpu, "client_cpu_delta": client_cpu,
        "server_process_cpu_cores": server_rss["process_cpu_cores"],
        "server_cgroup_cpu_seconds_per_op": server_cpu.get("usage_usec", 0) / 1_000_000 / result["operation_count"],
        "client_cgroup_cpu_seconds_per_op": client_cpu.get("usage_usec", 0) / 1_000_000 / result["operation_count"],
        "client_process_cpu_cores": client_rss["process_cpu_cores"],
        "client_cpu_headroom_cores": max(0.0, CLIENT_CPUS - client_rss["process_cpu_cores"]),
        "client_cpu_headroom_fraction": max(0.0, 1.0 - client_rss["process_cpu_cores"] / CLIENT_CPUS),
        "client_memtier_cpu_cores": result["client_cpu_cores"],
        "client_max_thread_avg_pct": result["client_max_thread_avg_pct"],
        "client_headroom_constrained": (
            client_rss["process_cpu_cores"] >= CLIENT_CPUS * 0.95
            or result["client_max_thread_avg_pct"] >= 95.0
        ),
        "throttle": throttles, "constrained": any(x["significant"] for x in throttles),
        "constraint_reasons": [x["scope"] for x in throttles if x["significant"]],
        "kernel_snapshots": {"before": kernel_before, "after": kernel_after},
        "wall_seconds": wall_seconds,
    }
    save(f"{stem}-row.json", row)
    return row


def cells():
    forward = [(f"forward-{label}", pipeline, connections) for label, pipeline, connections in BASE_CELLS]
    reverse = [(f"reverse-{label}", pipeline, connections) for label, pipeline, connections in reversed(BASE_CELLS)]
    return forward + reverse


def advise_plateau(rows):
    by_level = {}
    for row in rows:
        if row["pipeline"] == 1 and not row.get("constrained") and not row.get("client_headroom_constrained"):
            level = row["cell"].split("-p1-", 1)[-1]
            by_level.setdefault(level, []).append(row["ops_per_sec"])
    ordered = [("p1-c32", by_level.get("c32")), ("p1-c128", by_level.get("c128")), ("p1-c512", by_level.get("c512"))]
    for previous, current in zip(ordered, ordered[1:]):
        if previous[1] and current[1] and len(previous[1]) == len(current[1]) == 2 and statistics.median(current[1]) <= statistics.median(previous[1]) * 1.05:
            return previous[0]
    return "forward-p1-c128 (first plateau candidate; confirm from collected rows)"


def run():
    binary = OUT / BINARY_NAME
    assert binary.exists(), f"missing compact binary: {binary}"
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == BINARY_SHA256
    assert not (OUT / "cells.json").exists(), "refusing to mix a prior matrix; remove cells.json for a fresh run"
    kub("wait", "--for=condition=Ready", f"pod/{LOADGEN}", "--timeout=300s", timeout=320)
    client = json.loads(kub("get", "pod", LOADGEN, "-o", "json"))
    client_uid = client["metadata"]["uid"]
    client_node = client["spec"]["nodeName"]
    save("client-pod.json", client)
    settings = {
        "namespace": NS, "context": CTX, "output": str(OUT), "binary_sha256": BINARY_SHA256,
        "server_machine": SERVER_MACHINE, "server_cpu_quota": SERVER_CPUS, "server_memory_limit": "10Gi",
        "client_machine": CLIENT_MACHINE, "client_cpu_quota": CLIENT_CPUS, "keys": KEYS,
        "key_bytes": 16, "key_prefix": KEY_PREFIX, "value_bytes": VALUE_BYTES, "ratio": RATIO,
        "threads": THREADS, "warmup_seconds": WARMUP_SECONDS, "sample_seconds": SAMPLE_SECONDS,
        "throttle_threshold": THROTTLE_THRESHOLD, "cells": [x[0] for x in cells()],
        "method": "one fresh server pod and preload for all eight cells; diagnostics captured outside timing results",
    }
    save("settings.json", settings)
    server_pod, backend = start_server("matrix", binary)
    assert server_pod["spec"]["nodeName"] != client_node
    server_uid = server_pod["metadata"]["uid"]
    save("server-pod.json", server_pod)
    save("matrix-nodes.json", json.loads(kub("get", "nodes", "-l", f"karpenter.sh/nodepool={NS}", "-o", "json")))
    run_memtier("matrix-preload", 1, 32, 0, preload=True)
    assert int(bench.cli("DBSIZE")) == KEYS
    pre_sampled = verify_sampled_values()
    save("matrix-pre-sampled-values.json", pre_sampled)
    assert pre_sampled["valid"] and pre_sampled["sampled_keys"] >= 32
    rows = []
    for cell in cells():
        print("START", cell[0], flush=True)
        row = run_cell(cell, server_pod, backend, client_uid, client_node, pre_sampled)
        assert row["server_pod_uid"] == server_uid
        rows = [old for old in rows if old.get("cell") != row["cell"]] + [row]
        save("cells.json", rows)
        print("DONE", row["cell"], f"{row['ops_per_sec']:,.0f} ops/s", f"p99={row['p99_ms']:.3f}ms", flush=True)
    save("client-final-cpu.stat", execpod(LOADGEN, "loadgen", "cat", "/sys/fs/cgroup/cpu.stat"))
    save("nodes-end.json", json.loads(kub("get", "nodes", "-l", f"karpenter.sh/nodepool={NS}", "-o", "json")))
    print("COMPLETE 8 cells; first plateau/profile candidate:", advise_plateau(rows), flush=True)


def self_test():
    fixture = ROOT / "bench" / "loadtest" / "runs" / "recv-poll-first" / "4-off-mixed.json"
    assert fixture.exists(), fixture
    data = json.loads(fixture.read_text())
    validate_raw(data, 1, 128, 60)
    broken = json.loads(fixture.read_text())
    broken["configuration"]["pipeline"] = 8
    try:
        validate_raw(broken, 1, 128, 60)
    except AssertionError:
        print("Self-test passed: valid fixture accepted and bad pipeline rejected")
    else:
        raise AssertionError("validator accepted a mismatched pipeline fixture")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default=NS)
    parser.add_argument("--output", type=pathlib.Path, default=OUT)
    parser.add_argument("--context", default=CTX)
    parser.add_argument("--self-test", action="store_true")
    options = parser.parse_args()
    if options.self_test:
        self_test()
        return
    configure(options.namespace, options.output, options.context)
    run()


if __name__ == "__main__":
    main()
