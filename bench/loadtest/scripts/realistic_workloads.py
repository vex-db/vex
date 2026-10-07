#!/usr/bin/env python3
"""Run bounded cache workload profiles with the existing monotonic memtier build."""
import argparse
import hashlib
import json
import math
import pathlib
import shutil
import subprocess
import time
import uuid

import profile_local_docker as validation

PROFILES = ("baseline", "hot", "mixed", "churn", "burst", "soak")
SIZES = "32:50,256:30,1024:15,4096:5"


def stages(profile, seconds, rate):
    if profile == "burst":
        return [("steady", seconds, rate), ("burst", seconds, rate * 3),
                ("recovery", seconds, rate)]
    return [("measure", seconds, rate)]


def command(o, phase, seconds, rate, preload=False):
    args = [o.memtier, "-s", o.host, "-p", str(o.port),
            "-t", "1" if preload else str(o.threads),
            "-c", "1" if preload else str(o.clients),
            "--pipeline", "128" if preload else "1",
            "--key-prefix", o.prefix, "--key-minimum", "1", "--key-maximum", str(o.keys),
            "--ratio", "1:0" if preload else "1:4", "--distinct-client-seed",
            "--hide-histogram", "--print-percentiles", "50,95,99,99.9",
            "--json-out-file", str(o.output / (phase + ".json"))]
    args += (["--data-size", "256"] if o.profile == "baseline" else
             ["--data-size-list", SIZES, "--random-data"])
    if preload:
        return args + ["--requests", str(o.keys), "--key-pattern", "S:S"]
    args += ["--test-time", str(seconds), "--key-pattern", "G:G" if o.profile == "hot" else "R:R"]
    if o.profile == "hot":
        args += ["--key-median", str((o.keys + 1) // 2), "--key-stddev", str(max(1, o.keys // 100))]
    if o.profile in ("churn", "soak"):
        args += ["--expiry-range", "1-10"]
    if rate:
        connections = o.threads * o.clients
        if rate % connections:
            raise ValueError("total rate must be divisible by threads * clients")
        args += ["--rate-limiting", str(rate // connections)]
    return args


def validate_result(data, stderr, seconds, target_rate, allow_misses):
    validation.REQUIRE_MONOTONIC_CLOCK = True
    validation.assert_no_benchmark_errors(stderr)
    clock = validation.parse_clock_metadata(stderr)
    validation.validate_latency(data)
    stats = data["ALL STATS"]
    total, runtime = stats["Totals"], stats["Runtime"]
    assert str(runtime.get("Interrupted", False)).lower() == "false", "interrupted run"
    assert total["Count"] > 0 and math.isfinite(total["Ops/sec"]) and total["Ops/sec"] > 0
    assert all(s.get("Connection Errors", 0) == 0 for s in stats.values() if isinstance(s, dict))
    duration = runtime["Total duration"] / 1000
    assert duration >= seconds * .9, (duration, seconds)
    misses = stats.get("Gets", {}).get("Misses/sec", 0)
    hits = stats.get("Gets", {}).get("Hits/sec", 0)
    assert allow_misses or misses == 0, "unexpected miss in preloaded workload"
    p = total["Percentile Latencies"]
    return {"ops_per_sec": total["Ops/sec"], "p50_ms": p["p50.00"],
            "p95_ms": p["p95.00"], "p99_ms": p["p99.00"], "p999_ms": p["p99.90"],
            "miss_fraction": misses / (hits + misses) if hits + misses else None,
            "target_ops_per_sec": target_rate, "target_attained": None if not target_rate else
            total["Ops/sec"] >= .95 * target_rate, "clock": clock,
            "client_cpu": stats.get("CPU"), "duration_seconds": duration}


def run_phase(o, phase, seconds, rate, preload=False):
    args = command(o, phase, seconds, rate, preload)
    (o.output / (phase + ".command.json")).write_text(json.dumps(args, indent=2))
    with (o.output / (phase + ".stdout")).open("w") as stdout, (o.output / (phase + ".stderr")).open("w") as stderr:
        subprocess.run(args, stdout=stdout, stderr=stderr, check=True,
                       timeout=max(300, seconds + 120) if not preload else 1200)
    raw = json.loads((o.output / (phase + ".json")).read_text())
    log = (o.output / (phase + ".stderr")).read_text()
    row = validate_result(raw, log, 0 if preload else seconds, rate,
                          o.profile in ("churn", "soak"))
    cfg = raw["configuration"]
    assert cfg["server"] == o.host and cfg["port"] == o.port
    assert cfg["key_prefix"] == o.prefix and cfg["key_maximum"] == o.keys
    assert cfg["threads"] == (1 if preload else o.threads)
    assert cfg["clients"] == (1 if preload else o.clients)
    assert cfg["pipeline"] == (128 if preload else 1)
    assert cfg["ratio"] == ("1:0" if preload else "1:4")
    assert cfg["rate_limit"] == (0 if preload else rate // (o.threads * o.clients))
    assert cfg["key_pattern"] == ("S:S" if preload else "G:G" if o.profile == "hot" else "R:R")
    assert cfg["expiry_range"] == ("1:10" if not preload and o.profile in ("churn", "soak") else "0:0")
    if o.profile == "baseline":
        assert cfg["data_size"] == 256
    else:
        assert cfg["data_size_list"] == SIZES and cfg["random_data"] == "true"
    if preload:
        assert raw["ALL STATS"]["Totals"]["Count"] == o.keys
    row["phase"] = phase
    print(json.dumps(row), flush=True)
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--profile", choices=PROFILES, required=True)
    p.add_argument("--output", type=pathlib.Path, required=True)
    p.add_argument("--memtier", default="memtier_benchmark")
    p.add_argument("--keys", type=int, default=1_000_000)
    p.add_argument("--threads", type=int, default=32)
    p.add_argument("--clients", type=int, default=4)
    p.add_argument("--rate", type=int, default=128_000, help="aggregate rate cap; 0 = unlimited")
    p.add_argument("--seconds", type=int, help="per stage; default 60, or 1800 for soak")
    p.add_argument("--warmup", type=int, default=10)
    o = p.parse_args()
    o.seconds = o.seconds if o.seconds is not None else (1800 if o.profile == "soak" else 60)
    if min(o.keys, o.threads, o.clients, o.seconds, o.warmup) <= 0 or o.rate < 0:
        p.error("counts/durations must be positive; rate must be nonnegative")
    if o.profile == "burst" and o.rate == 0:
        p.error("burst requires a nonzero rate")
    o.prefix = "vex-realistic:" + uuid.uuid4().hex + ":"
    o.output = o.output.resolve()
    for phase, seconds, rate in stages(o.profile, o.seconds, o.rate):
        command(o, phase, seconds, rate)  # Reject invalid rates before loading data.
    o.output.mkdir(parents=True, exist_ok=False)
    exe = pathlib.Path(shutil.which(o.memtier) or o.memtier)
    manifest = {**vars(o), "output": str(o.output), "memtier_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
                "started_unix": time.time(), "arrival_model": "closed-loop, per-connection rate capped",
                "sizes": "256" if o.profile == "baseline" else SIZES}
    (o.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    run_phase(o, "preload", 0, 0, True)
    run_phase(o, "warmup", o.warmup, o.rate)
    rows = [run_phase(o, phase, seconds, rate) for phase, seconds, rate in stages(o.profile, o.seconds, o.rate)]
    (o.output / "summary.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
