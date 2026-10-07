#!/usr/bin/env python3
"""Bounded local Docker profile; owns only its named containers/network."""
import argparse
import json
import math
import os
import pathlib
import re
import subprocess
import time


ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / "bench" / "loadtest" / "runs" / "2026-09-22-local-docker"
NETWORK = "vex-local-profile-20260922"
SERVER = "vex-local-profile-20260922-server"
CLIENT = "vex-local-profile-20260922-client"
PROFILER = "vex-local-profile-20260922-profiler"
SERVER_IMAGE = "vex-perf-local:arm64"
CLIENT_IMAGE = "redislabs/memtier_benchmark@sha256:f5e23c14a55aafe5ab0455084c529195fefa42752503589c8fdbacc8d2407f77"
PORT = 6379
KEYS = 1_000_000
KEY_MINIMUM = 1_000_000
KEY_MAXIMUM = 1_999_999
KEY_PREFIX = "vex:key::"
VALUE_BYTES = 256
THREADS = 32
CONNECTIONS = 128
PIPELINE = 1
RATIO = "1:4"
SERVER_QUOTA = 2
CLIENT_QUOTA = 4
WORKERS = 2
SERVER_CPUSET = "0-1"
TRANSPORT = "tcp"
TCP_HOST = None
CLIENT_NETWORK = None
UNIX_SOCKET = None
SOCKET_VOLUME = None
SAMPLE_SECONDS = 30
WARMUP_SECONDS = 10
EXPECTED_HASH = os.environ.get("VEX_LOCAL_BINARY_SHA256", "")
REQUIRE_MONOTONIC_CLOCK = False
CLOCK_SOURCE = "CLOCK_MONOTONIC"
CLOCK_TAG = "2.5.1"
CLOCK_COMMIT = "5f634d171b83efca9640c5a87606c47b34d3d330"
BENCHMARK_ERROR_MARKERS = ("handle error response:", "response parsing failed", "parse error", "parser error", "failed to parse response", "unable to parse response")


def has_kernel_addresses(text):
    for line in text.splitlines():
        token = line.split(maxsplit=1)[0] if line.split() else ""
        if re.fullmatch(r"[0-9a-fA-F]{8,}", token) and token.strip("0"):
            return True
    return False


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, indent=2) + "\n")
    else:
        path.write_text(str(value))


def docker(*args, check=True, timeout=120, capture=True):
    p = subprocess.run(["docker", *map(str, args)], text=True, capture_output=capture, timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"docker {args[:3]} failed: {p.stderr[-2000:]}")
    return p.stdout if capture else ""


def dex(name, *args, check=True, timeout=120):
    return docker("exec", name, *args, check=check, timeout=timeout)


def logs(name):
    p = subprocess.run(["docker", "logs", name], text=True, capture_output=True, timeout=120)
    if p.returncode:
        raise RuntimeError(p.stderr[-2000:])
    return p.stdout + p.stderr


def require_absent(name):
    p = subprocess.run(["docker", "inspect", name], text=True, capture_output=True)
    assert p.returncode != 0, f"refusing to replace existing Docker object: {name}"


def transport_args():
    if TRANSPORT == "uds":
        assert UNIX_SOCKET, "UDS transport requires UNIX_SOCKET"
        return ["-S", UNIX_SOCKET]
    assert TRANSPORT == "tcp", TRANSPORT
    return ["-s", TCP_HOST or SERVER, "-p", str(PORT)]


def assert_no_benchmark_errors(stderr):
    lowered = stderr.lower()
    hits = [marker for marker in BENCHMARK_ERROR_MARKERS if marker in lowered]
    assert not hits, f"benchmark reported response/parser errors: {hits}"


def expected_load_config(seconds):
    return {"server": None if TRANSPORT == "uds" else TCP_HOST or SERVER, "port": 0 if TRANSPORT == "uds" else PORT, "unix socket": UNIX_SOCKET if TRANSPORT == "uds" else None, "pipeline": PIPELINE, "threads": THREADS, "clients": 4, "test_time": seconds, "data_size": VALUE_BYTES, "ratio": RATIO, "key_prefix": KEY_PREFIX, "key_minimum": KEY_MINIMUM, "key_maximum": KEY_MAXIMUM}


def parse_clock_metadata(stderr):
    lines = [line for line in stderr.splitlines() if line.startswith("MEMTIER_CLOCK ")]
    assert len(lines) <= 1, "clock metadata: expected at most one MEMTIER_CLOCK line"
    if not lines:
        assert not REQUIRE_MONOTONIC_CLOCK, "clock metadata: required MEMTIER_CLOCK line is missing"
        return None
    try:
        metadata = json.loads(lines[0][len("MEMTIER_CLOCK "):])
    except json.JSONDecodeError as exc:
        raise AssertionError("clock metadata: malformed JSON") from exc
    assert isinstance(metadata, dict), "clock metadata: JSON object required"
    assert metadata.get("clock_source") == CLOCK_SOURCE
    assert metadata.get("source_tag") == CLOCK_TAG
    assert metadata.get("source_commit") == CLOCK_COMMIT
    offset = metadata.get("epoch_offset_us")
    assert isinstance(offset, int) and not isinstance(offset, bool), "clock metadata: epoch_offset_us must be an integer"
    metadata["epoch_offset_us"] = offset
    return metadata


def rss_in_runtime_window(process_rows, runtime, clock_metadata):
    start_ms, finish_ms = float(runtime["Start time"]), float(runtime["Finish time"])
    if clock_metadata:
        offset_us = clock_metadata["epoch_offset_us"]
        return [proc["rss_bytes"] for row in process_rows if "monotonic_us" in row and start_ms <= (row["monotonic_us"] + offset_us) / 1000 <= finish_ms for proc in row["processes"]]
    return [proc["rss_bytes"] for row in process_rows if start_ms / 1000 <= row["time"] <= finish_ms / 1000 for proc in row["processes"]]


def container_preflight():
    files = {"uname": ("uname", "-a"), "boot_id": ("cat", "/proc/sys/kernel/random/boot_id"), "capabilities": ("grep", "^CapEff:", "/proc/self/status"), "uid_map": ("cat", "/proc/self/uid_map"), "kptr_restrict": ("cat", "/proc/sys/kernel/kptr_restrict"), "perf_event_paranoid": ("cat", "/proc/sys/kernel/perf_event_paranoid"), "kallsyms": ("cat", "/proc/kallsyms")}
    result = {}
    for key, args in files.items():
        result[key] = dex(PROFILER, *args, check=False).strip()
    result["kallsyms_nonzero"] = has_kernel_addresses(result["kallsyms"])
    save("preflight.json", {k: v for k, v in result.items() if k != "kallsyms"})
    save("kallsyms.txt", result["kallsyms"])
    save("container-preflight.json", {k: v for k, v in result.items() if k != "kallsyms"})
    save("container-kallsyms.txt", result["kallsyms"])
    return result


def setup(server_env=None):
    assert EXPECTED_HASH, "set VEX_LOCAL_BINARY_SHA256 to the ARM64 binary hash"
    assert not (OUT / "settings.json").exists(), "refusing to mix an existing local profile"
    for name in (SERVER, CLIENT, PROFILER, NETWORK, SOCKET_VOLUME):
        if name:
            require_absent(name)
    if SOCKET_VOLUME:
        docker("volume", "create", SOCKET_VOLUME)
    docker("network", "create", NETWORK)
    env_args = [arg for key, value in (server_env or {}).items() for arg in ("--env", f"{key}={value}")]
    socket_mount = ["--mount", f"type=volume,source={SOCKET_VOLUME},target=/bench-sockets"] if SOCKET_VOLUME else []
    server_args = ["--reactor", "--workers", str(WORKERS), "--no-persistence", "--port", str(PORT)]
    if UNIX_SOCKET:
        server_args += ["--unixsocket", UNIX_SOCKET]
    docker("run", "-d", "--name", SERVER, "--network", NETWORK, "--cpuset-cpus", SERVER_CPUSET, "--cpus", str(SERVER_QUOTA), "--memory", "1g", "--memory-swap", "1g", "--security-opt", "seccomp=unconfined", *socket_mount, *env_args, "--entrypoint", "/usr/local/bin/vex", SERVER_IMAGE, *server_args)
    client_network = CLIENT_NETWORK or NETWORK
    docker("run", "-d", "--name", CLIENT, "--network", client_network, "--cpuset-cpus", "2-5", "--cpus", "4", "--memory", "512m", "--memory-swap", "512m", *socket_mount, "--entrypoint", "sh", CLIENT_IMAGE, "-c", "sleep 10000")
    sampler = """import json,os,pathlib,time
with open('/tmp/process.jsonl','a',buffering=1) as f:
 while True:
  rows=[]
  for p in pathlib.Path('/proc').glob('[0-9]*'):
   try:
    name=(p/'comm').read_text().strip()
    if not name.startswith('vex'): continue
    st=(p/'status').read_text(); rss=next((int(x.split()[1])*1024 for x in st.splitlines() if x.startswith('VmRSS:')),0)
    q=(p/'stat').read_text().rsplit(')',1)[1].split(); rows.append({'pid':int(p.name),'name':name,'rss_bytes':rss,'cpu_seconds':(int(q[11])+int(q[12]))/os.sysconf('SC_CLK_TCK')})
   except (OSError,ValueError,IndexError): pass
  f.write(json.dumps({'time':time.time(),'monotonic_us':time.monotonic_ns()//1000,'processes':rows})+'\\n'); time.sleep(1)
"""
    docker("run", "-d", "--name", PROFILER, "--network", NETWORK, "--pid", f"container:{SERVER}", "--cpuset-cpus", "6", "--cpus", "1", "--memory", "256m", "--memory-swap", "256m", "--user", "0", "--cap-add", "PERFMON", "--cap-add", "SYS_PTRACE", "--cap-add", "SYSLOG", "--security-opt", "seccomp=unconfined", *socket_mount, "--entrypoint", "python3", SERVER_IMAGE, "-u", "-c", sampler)
    for _ in range(30):
        try:
            dex(PROFILER, "true")
            break
        except RuntimeError:
            time.sleep(1)
    else:
        raise RuntimeError("profiler did not start")
    pf = {f"container_{k}": v for k, v in container_preflight().items() if k != "kallsyms"}
    save("settings.json", {"network": NETWORK, "client_network": client_network, "server": SERVER, "client": CLIENT, "profiler": PROFILER, "socket_volume": SOCKET_VOLUME, "server_image": SERVER_IMAGE, "client_image": CLIENT_IMAGE, "server_cpus": SERVER_CPUSET, "client_cpus": "2-5", "profiler_cpu": "6", "server_quota": SERVER_QUOTA, "client_quota": CLIENT_QUOTA, "workers": WORKERS, "transport": TRANSPORT, "tcp_host": TCP_HOST or SERVER if TRANSPORT == "tcp" else None, "unix socket": UNIX_SOCKET if TRANSPORT == "uds" else None, "unix_socket_listener": UNIX_SOCKET, "require_monotonic_clock": REQUIRE_MONOTONIC_CLOCK, "keys": KEYS, "key_bytes": 16, "value_bytes": VALUE_BYTES, "pipeline": PIPELINE, "ratio": RATIO, "threads": THREADS, "connections": CONNECTIONS, "warmup_seconds": WARMUP_SECONDS, "sample_seconds": SAMPLE_SECONDS, "binary_sha256": EXPECTED_HASH, "preflight": pf})
    wait_ready()
    live, live_raw = identity()
    save("live-binary-identity.txt", live_raw)
    settings = json.loads((OUT / "settings.json").read_text())
    settings["live_binary_sha256"] = next(line.split()[0] for line in live_raw.splitlines() if re.fullmatch(r"[0-9a-fA-F]{64}", line.split()[0]))
    save("settings.json", settings)


def wait_ready():
    for _ in range(60):
        try:
            ping()
            startup = logs(SERVER)
            save("startup.log", startup)
            backend = [line for line in startup.splitlines() if "io_uring backend active" in line]
            assert len(backend) >= WORKERS, startup[-3000:]
            return
        except (AssertionError, RuntimeError, json.JSONDecodeError, OSError):
            time.sleep(1)
    raise RuntimeError("local server did not become ready with io_uring")


def ping():
    connect = f"s=socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect({UNIX_SOCKET!r})" if TRANSPORT == "uds" else f"s=socket.create_connection(({SERVER!r},{PORT}),5)"
    code = rf'''import socket
{connect}; s.sendall(b'*1\r\n$4\r\nPING\r\n'); assert s.recv(7)==b'+PONG\r\n'
'''
    dex(PROFILER, "python3", "-c", code)


def probe_values():
    connect = f"s=socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect({UNIX_SOCKET!r})" if TRANSPORT == "uds" else f"s=socket.create_connection(({SERVER!r},{PORT}),5)"
    code = rf'''import json,random,socket
{connect}; f=s.makefile('rb')
def cmd(x):
 s.sendall(('*'+str(len(x))+'\r\n' + ''.join('$'+str(len(a))+'\r\n'+a+'\r\n' for a in x)).encode()); return f.readline()
ids=[{KEY_MINIMUM},{KEY_MAXIMUM}]+random.Random(22).sample(range({KEY_MINIMUM},{KEY_MAXIMUM}+1),30)
for i in ids:
 k=('{KEY_PREFIX}'+str(i)).encode(); assert len(k)==16; h=cmd(['GET',k.decode()]); assert h.startswith(b'$') and int(h[1:])=={VALUE_BYTES}; assert len(f.read({VALUE_BYTES}))=={VALUE_BYTES} and f.read(2)==b'\r\n'
h=cmd(['DBSIZE']); print(json.dumps({{'sampled_keys':len(ids),'value_bytes':{VALUE_BYTES},'dbsize':int(h[1:]),'valid':True}}))
'''
    return json.loads(dex(PROFILER, "python3", "-c", code))


def cpu_stat(name):
    out = dex(name, "cat", "/sys/fs/cgroup/cpu.stat")
    return {x.split()[0]: int(x.split()[1]) for x in out.splitlines() if len(x.split()) == 2}


def pressure(stem, phase):
    for name, path in (("meminfo", "/proc/meminfo"), ("memory-pressure", "/proc/pressure/memory")):
        save(f"{stem}-{phase}-{name}.txt", dex(PROFILER, "cat", path, check=False))
    for container in (SERVER, CLIENT):
        for leaf in ("memory.current", "memory.events", "memory.swap.current"):
            save(f"{stem}-{container}-{phase}-{leaf.replace('.', '-')}.txt", dex(container, "cat", f"/sys/fs/cgroup/{leaf}", check=False))


def identity():
    out = dex(PROFILER, "sh", "-ec", "for p in /proc/[0-9]*; do case \"$(cat \"$p/comm\" 2>/dev/null)\" in vex*) echo PID=$(basename $p) START=$(awk '{print $22}' $p/stat) EXE=$(readlink $p/exe); sha256sum $p/exe;; esac; done")
    lines = [x for x in out.splitlines() if x.startswith("PID=")]
    assert len(lines) == 1, out
    live_hash = next((line.split()[0] for line in out.splitlines() if re.fullmatch(r"[0-9a-fA-F]{64}", line.split()[0])), "")
    assert live_hash and (not EXPECTED_HASH or live_hash == EXPECTED_HASH), out
    return lines[0], out


def stats(name):
    return json.loads(docker("stats", "--no-stream", "--format", "{{json .}}", name))


def parse_mem(value):
    match = re.match(r"([0-9.]+)([A-Za-z]+)", value.split()[0])
    assert match, value
    n, unit = match.groups()
    scale = {"B": 1, "KiB": 1024, "MiB": 1048576, "GiB": 1073741824, "kB": 1000, "MB": 1000000, "GB": 1000000000}
    return float(n) * scale.get(unit, 1)


def validate_latency(data):
    """Reject impossible memtier latency counters before deriving summaries."""
    all_stats = data["ALL STATS"]
    runtime = all_stats["Runtime"]
    totals = all_stats["Totals"]
    assert runtime.get("Time unit") == "MILLISECONDS", "latency validation: Runtime.Time unit must be MILLISECONDS"
    duration = float(runtime.get("Total duration", 0))
    assert math.isfinite(duration) and duration > 0, f"latency validation: invalid runtime duration {duration!r}"
    config = data["configuration"]
    try:
        threads, clients, pipeline = (int(config[key]) for key in ("threads", "clients", "pipeline"))
    except (KeyError, TypeError, ValueError) as exc:
        raise AssertionError("latency validation: configuration threads/clients/pipeline are required") from exc
    assert threads > 0 and clients > 0 and pipeline > 0, "latency validation: concurrency values must be positive"

    bound = duration + 1000.0
    max_accumulated = threads * clients * pipeline * bound
    required = ("Average Latency", "Max Latency", "Accumulated Latency", "Min Latency")

    def number(section, key, where):
        assert key in section, f"latency validation: missing {where}.{key}"
        try:
            numeric = float(section[key])
        except (TypeError, ValueError) as exc:
            raise AssertionError(f"latency validation: {where}.{key} is not numeric") from exc
        assert math.isfinite(numeric) and numeric >= 0, f"latency validation: {where}.{key} must be finite/nonnegative, got {numeric!r}"
        return numeric

    def check_bucket(bucket, where):
        assert isinstance(bucket, dict), f"latency validation: {where} must be an object"
        values = {key: number(bucket, key, where) for key in required}
        assert values["Max Latency"] <= bound, f"latency validation: {where}.Max Latency {values['Max Latency']}ms exceeds runtime bound {bound}ms"
        assert values["Accumulated Latency"] <= max_accumulated, f"latency validation: {where}.Accumulated Latency {values['Accumulated Latency']}ms exceeds closed-loop bound {max_accumulated}ms"
        assert values["Average Latency"] <= values["Max Latency"], f"latency validation: {where}.Average Latency exceeds Max Latency"
        percentile = bucket.get("Percentile Latencies", bucket)
        assert isinstance(percentile, dict), f"latency validation: {where}.Percentile Latencies must be an object"
        for key, value in percentile.items():
            if re.fullmatch(r"p\d+(?:\.\d+)?", str(key)):
                numeric = number(percentile, key, f"{where}.Percentile Latencies")
                assert numeric <= bound, f"latency validation: {where}.Percentile Latencies.{key} exceeds runtime bound {bound}ms"

    check_bucket(totals, "Totals")
    percentile = totals.get("Percentile Latencies")
    assert isinstance(percentile, dict), "latency validation: Totals.Percentile Latencies is required"
    for key in ("p50.00", "p95.00", "p99.00"):
        numeric = number(percentile, key, "Totals.Percentile Latencies")
        assert numeric <= bound, f"latency validation: Totals.Percentile Latencies.{key} exceeds runtime bound {bound}ms"
    series = totals.get("Time-Serie")
    assert isinstance(series, dict) and series, "latency validation: Totals.Time-Serie is required"
    for key, bucket in series.items():
        check_bucket(bucket, f"Totals.Time-Serie.{key}")


def run_load(stem, seconds):
    probe_values()
    pressure(stem, "before")
    before_id, before_raw = identity(); save(f"{stem}-identity-before.txt", before_raw)
    server_before, client_before = cpu_stat(SERVER), cpu_stat(CLIENT)
    save(f"{stem}-server-cpu-before.stat", server_before)
    save(f"{stem}-client-cpu-before.stat", client_before)
    args = ["docker", "exec", CLIENT, "memtier_benchmark", *transport_args(), "-t", str(THREADS), "-c", "4", "--pipeline", str(PIPELINE), "--ratio", RATIO, "--key-prefix", KEY_PREFIX, "--key-minimum", str(KEY_MINIMUM), "--key-maximum", str(KEY_MAXIMUM), "--data-size", str(VALUE_BYTES), "--hide-histogram", "--print-percentiles", "50,95,99", "--json-out-file", f"/tmp/{stem}.json", "--test-time", str(seconds), "--key-pattern", "R:R", "--distinct-client-seed"]
    load_start = time.time()
    observed = []
    # Stream progress to files while sampling; waiting with unread pipes can
    # block a longer workload once the OS pipe buffer fills.
    with (OUT / f"{stem}.log").open("w") as stdout, (OUT / f"{stem}.stderr.log").open("w") as stderr_file:
        proc = subprocess.Popen(args, text=True, stdout=stdout, stderr=stderr_file)
        deadline = time.monotonic() + seconds + 120
        while proc.poll() is None and time.monotonic() < deadline:
            observed.append({"time": time.time(), "server": stats(SERVER), "client": stats(CLIENT)})
            time.sleep(1)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
            raise TimeoutError(f"memtier exceeded bounded timeout: {stem}")
        proc.wait(timeout=30)
    load_end = time.time()
    stderr = (OUT / f"{stem}.stderr.log").read_text()
    clock_metadata = parse_clock_metadata(stderr)
    save(f"{stem}-clock.json", clock_metadata or {"required": REQUIRE_MONOTONIC_CLOCK, "present": False})
    assert proc.returncode == 0, stderr[-2000:]
    raw = dex(CLIENT, "cat", f"/tmp/{stem}.json")
    data = json.loads(raw); save(f"{stem}.json", data)
    assert_no_benchmark_errors(stderr)
    validate_latency(data)
    runtime, totals, cfg = data["ALL STATS"]["Runtime"], data["ALL STATS"]["Totals"], data["configuration"]
    expected = expected_load_config(seconds)
    for key, value in expected.items(): assert cfg.get(key) == value, (key, cfg.get(key), value)
    assert int(runtime["Total duration"]) >= (seconds - 1) * 1000 and int(runtime["Total duration"]) <= (seconds + 3) * 1000
    assert str(runtime.get("Interrupted", "false")).lower() == "false"
    assert all(int(section.get("Connection Errors", 0)) == 0 for section in data["ALL STATS"].values() if isinstance(section, dict))
    assert int(totals.get("Connection Errors", 0)) == 0 and float(totals.get("Misses/sec", 0)) == 0
    server_after, client_after = cpu_stat(SERVER), cpu_stat(CLIENT)
    save(f"{stem}-server-cpu-after.stat", server_after)
    save(f"{stem}-client-cpu-after.stat", client_after)
    after_id, after_raw = identity(); save(f"{stem}-identity-after.txt", after_raw); assert before_id == after_id
    post = probe_values(); save(f"{stem}-values.json", post); assert post["dbsize"] == KEYS
    pressure(stem, "after")
    save(f"{stem}-stats.json", observed)
    process_text = dex(PROFILER, "cat", "/tmp/process.jsonl")
    save(f"{stem}-process.jsonl", process_text)
    process_rows = [json.loads(line) for line in process_text.splitlines() if line.strip()]
    timed_rss = rss_in_runtime_window(process_rows, runtime, clock_metadata)
    assert timed_rss, "profiler returned no timed server RSS samples"
    server_usage = (server_after["usage_usec"] - server_before["usage_usec"]) / 1e6
    client_usage = (client_after["usage_usec"] - client_before["usage_usec"]) / 1e6
    server_throttle = (server_after.get("throttled_usec", 0) - server_before.get("throttled_usec", 0)) / (seconds * 1e6)
    client_throttle = (client_after.get("throttled_usec", 0) - client_before.get("throttled_usec", 0)) / (seconds * 1e6)
    per_thread = data["ALL STATS"].get("CPU", {}).get("Per Thread", {})
    assert per_thread, "missing per-thread client CPU metrics"
    max_thread_pct = max((float(x.get("cores_used", 0)) * 100 for x in per_thread.values()), default=0)
    result = {"cell": stem, "server_identity": before_id, "ops_per_sec": float(totals["Ops/sec"]), "p99_ms": float(totals["Percentile Latencies"]["p99.00"]), "runtime_ms": int(runtime["Total duration"]), "operations": int(totals["Count"]), "server_cpu_seconds_per_op": server_usage / int(totals["Count"]), "client_cpu_seconds_per_op": client_usage / int(totals["Count"]), "server_rss_bytes": max(timed_rss, default=0), "server_headroom_fraction": max(0, 1 - server_usage / (seconds * SERVER_QUOTA)), "client_headroom_fraction": max(0, 1 - client_usage / (seconds * CLIENT_QUOTA)), "client_max_thread_avg_pct": max_thread_pct, "server_throttle_fraction": server_throttle, "client_throttle_fraction": client_throttle, "constrained": server_throttle >= .01 or client_throttle >= .01 or max_thread_pct >= 95 or max(0, 1 - client_usage / (seconds * CLIENT_QUOTA)) < .05, "stats_samples": len(observed), "server_container_memory_bytes": max((parse_mem(x["server"]["MemUsage"]) for x in observed), default=0), "client_container_memory_bytes": max((parse_mem(x["client"]["MemUsage"]) for x in observed), default=0), "clock_metadata": clock_metadata}
    save(f"{stem}-result.json", result)
    return result


def preload():
    pressure("preload", "before")
    args = ["docker", "exec", CLIENT, "memtier_benchmark", *transport_args(), "-t", "1", "-c", "1", "--pipeline", "128", "--ratio", "1:0", "--key-prefix", KEY_PREFIX, "--key-minimum", str(KEY_MINIMUM), "--key-maximum", str(KEY_MAXIMUM), "--data-size", str(VALUE_BYTES), "--requests", str(KEYS), "--key-pattern", "S:S", "--json-out-file", "/tmp/preload.json"]
    p = subprocess.run(args, text=True, capture_output=True, timeout=900); save("preload.log", p.stdout); save("preload.stderr.log", p.stderr); clock_metadata = parse_clock_metadata(p.stderr); save("preload-clock.json", clock_metadata or {"required": REQUIRE_MONOTONIC_CLOCK, "present": False}); assert p.returncode == 0, p.stderr[-2000:]
    data = json.loads(dex(CLIENT, "cat", "/tmp/preload.json")); save("preload.json", data); assert_no_benchmark_errors(p.stderr); assert int(data["ALL STATS"]["Totals"]["Count"]) == KEYS
    post = probe_values(); save("preload-values.json", post); assert post["dbsize"] == KEYS
    pressure("preload", "after")


def load_settings():
    global EXPECTED_HASH
    settings = json.loads((OUT / "settings.json").read_text())
    expected = settings.get("live_binary_sha256", "")
    assert expected and (not EXPECTED_HASH or EXPECTED_HASH == expected), (EXPECTED_HASH, expected)
    EXPECTED_HASH = expected


def baseline():
    assert (OUT / "settings.json").exists() and not (OUT / "baseline.json").exists()
    load_settings()
    preload(); run_load("warmup", WARMUP_SECONDS)
    rows = [run_load("baseline-1", SAMPLE_SECONDS), run_load("baseline-2", SAMPLE_SECONDS)]
    assert len({row["server_identity"] for row in rows}) == 1
    save("baseline.json", {"rows": rows, "status": "complete"})


def self_test():
    global TRANSPORT, UNIX_SOCKET
    assert parse_mem("1.5MiB / 1GiB") == 1.5 * 1048576
    assert parse_mem("2GB / 4GB") == 2 * 1000000000
    old_transport, old_socket = TRANSPORT, UNIX_SOCKET
    try:
        TRANSPORT, UNIX_SOCKET = "tcp", None
        assert transport_args() == ["-s", SERVER, "-p", str(PORT)]
        TRANSPORT, UNIX_SOCKET = "uds", "/bench-sockets/vex.sock"
        assert transport_args() == ["-S", UNIX_SOCKET]
        uds_config = expected_load_config(60)
        assert uds_config["server"] is None and uds_config["port"] == 0 and uds_config["unix socket"] == UNIX_SOCKET
    finally:
        TRANSPORT, UNIX_SOCKET = old_transport, old_socket
    assert_no_benchmark_errors("MEMTIER_CLOCK {}\ncompleted successfully")
    for marker in BENCHMARK_ERROR_MARKERS:
        try:
            assert_no_benchmark_errors(marker)
        except AssertionError as exc:
            assert marker in str(exc)
        else:
            raise AssertionError(f"benchmark error marker was accepted: {marker}")
    clock_line = 'MEMTIER_CLOCK {"clock_source":"CLOCK_MONOTONIC","epoch_offset_us":1000000000000,"source_tag":"2.5.1","source_commit":"5f634d171b83efca9640c5a87606c47b34d3d330"}'
    clock = parse_clock_metadata(clock_line)
    assert clock["epoch_offset_us"] == 1000000000000
    rows = [{"time": 0, "monotonic_us": 2000000, "processes": [{"rss_bytes": 7}]}, {"time": 1000000000000, "monotonic_us": 4000000, "processes": [{"rss_bytes": 9}]}]
    runtime_window = {"Start time": 1000002000.0, "Finish time": 1000003000.0}
    assert rss_in_runtime_window(rows, runtime_window, clock) == [7]
    old_required = REQUIRE_MONOTONIC_CLOCK
    try:
        globals()["REQUIRE_MONOTONIC_CLOCK"] = True
        try:
            parse_clock_metadata("")
        except AssertionError as exc:
            assert "missing" in str(exc)
        else:
            raise AssertionError("strict clock validation accepted missing metadata")
    finally:
        globals()["REQUIRE_MONOTONIC_CLOCK"] = old_required
    valid = {"configuration": {"threads": 32, "clients": 4, "pipeline": 1}, "ALL STATS": {"Runtime": {"Time unit": "MILLISECONDS", "Total duration": 60002}, "Totals": {"Average Latency": 1.0, "Max Latency": 2.0, "Accumulated Latency": 128.0, "Min Latency": 0.1, "Percentile Latencies": {"p50.00": 0.5, "p95.00": 1.5, "p99.00": 1.9}, "Time-Serie": {"0": {"Average Latency": 1.0, "Max Latency": 3.0, "Accumulated Latency": 128.0, "Min Latency": 0.1, "Percentile Latencies": {"p99.00": 2.5}}}}}}
    bucket = valid["ALL STATS"]["Totals"]["Time-Serie"]["0"]
    bucket.update(bucket.pop("Percentile Latencies"))  # memtier stores bucket percentiles flat
    validate_latency(valid)
    p8 = json.loads(json.dumps(valid)); p8["configuration"]["pipeline"] = 8; p8["ALL STATS"]["Totals"]["Accumulated Latency"] = 1024.0; p8["ALL STATS"]["Totals"]["Time-Serie"]["0"]["Accumulated Latency"] = 1024.0
    validate_latency(p8)
    corrupt = json.loads(json.dumps(valid))
    corrupt["ALL STATS"]["Totals"].update({"Max Latency": 603979.775, "Accumulated Latency": 557421399.0})
    try:
        validate_latency(corrupt)
    except AssertionError as exc:
        assert "latency validation" in str(exc)
    else:
        raise AssertionError("latency validation accepted the recorded corrupt on-1 counters")
    missing = json.loads(json.dumps(valid)); del missing["ALL STATS"]["Totals"]["Min Latency"]
    try:
        validate_latency(missing)
    except AssertionError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("latency validation accepted missing aggregate latency")
    for path in (("Percentile Latencies",), ("Time-Serie", "0")):
        bad_percentile = json.loads(json.dumps(valid))
        target = bad_percentile["ALL STATS"]["Totals"]
        for key in path:
            target = target[key]
        target["p99.00"] = float("nan")
        try:
            validate_latency(bad_percentile)
        except AssertionError as exc:
            assert "finite/nonnegative" in str(exc)
        else:
            raise AssertionError("latency validation accepted NaN percentile latency")
    bad_bucket = json.loads(json.dumps(valid)); bad_bucket["ALL STATS"]["Totals"]["Time-Serie"]["0"]["Max Latency"] = 61003.0
    try:
        validate_latency(bad_bucket)
    except AssertionError as exc:
        assert "Time-Serie" in str(exc)
    else:
        raise AssertionError("latency validation accepted oversized time-series latency")
    print("Self-test passed: local Docker parsing and guards")


def main():
    global OUT
    ap = argparse.ArgumentParser(); ap.add_argument("command", choices=("setup", "baseline", "self-test")); ap.add_argument("--output", type=pathlib.Path, default=OUT); opt = ap.parse_args()
    OUT = opt.output
    if opt.command == "self-test": self_test()
    elif opt.command == "setup": setup()
    else: baseline()


if __name__ == "__main__": main()
