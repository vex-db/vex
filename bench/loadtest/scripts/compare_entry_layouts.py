#!/usr/bin/env python3
"""Bounded direct versus indirect ConcurrentKV entry-layout pilot."""
import argparse
import hashlib
import json
import pathlib
import statistics

import compare_sq_head_cache as health_helpers
import profile_local_docker as p


ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / "bench" / "loadtest" / "runs" / "2026-09-22-entry-layouts"
NETWORK = "vex-entry-layout-20260922"
ORDER = (("1-control", "control"), ("2-candidate", "candidate"), ("3-candidate", "candidate"), ("4-control", "control"))
RSS_SAVING_BYTES = 24 * 1024 * 1024


def configure(output, image, client_image, sha):
    p.OUT = pathlib.Path(output)
    p.OUT.mkdir(parents=True, exist_ok=True)
    p.NETWORK = NETWORK
    p.SERVER, p.CLIENT, p.PROFILER = (NETWORK + "-" + x for x in ("server", "client", "profiler"))
    p.SERVER_IMAGE, p.CLIENT_IMAGE, p.EXPECTED_HASH = image, client_image, sha
    p.WORKERS, p.SERVER_CPUSET, p.SERVER_QUOTA = 2, "0-1", 2
    p.KEYS, p.KEY_MINIMUM, p.KEY_MAXIMUM = 1_000_000, 1_000_000, 1_999_999
    p.VALUE_BYTES, p.THREADS, p.CONNECTIONS, p.PIPELINE, p.RATIO = 256, 32, 128, 1, "1:4"
    p.SAMPLE_SECONDS, p.WARMUP_SECONDS = 60, 10
    p.TRANSPORT, p.UNIX_SOCKET, p.SOCKET_VOLUME = "tcp", None, None
    p.REQUIRE_MONOTONIC_CLOCK = True


def require_clean():
    for name in (p.SERVER, p.CLIENT, p.PROFILER, p.NETWORK):
        p.require_absent(name)


def ensure_stable(rows, variant):
    selected = [row for row in rows if row["variant"] == variant]
    if len(selected) < 2:
        return
    median = statistics.median(row["ops_per_sec"] for row in selected)
    variation = (max(row["ops_per_sec"] for row in selected) - min(row["ops_per_sec"] for row in selected)) / median
    if variation > 0.05:
        raise RuntimeError(f"inconclusive: {variant} within-mode variation {variation:.3%} exceeds 5%")


def evaluate(rows):
    controls = [row for row in rows if row["variant"] == "control"]
    candidates = [row for row in rows if row["variant"] == "candidate"]
    assert len(controls) == len(candidates) == 2
    pairs = []
    for control_label, candidate_label in (("1-control", "2-candidate"), ("4-control", "3-candidate")):
        control = next(row for row in controls if row["cell"] == control_label)
        candidate = next(row for row in candidates if row["cell"] == candidate_label)
        pairs.append({"control_ops_per_sec": control["ops_per_sec"], "candidate_ops_per_sec": candidate["ops_per_sec"], "throughput_ratio": candidate["ops_per_sec"] / control["ops_per_sec"], "control_p99_ms": control["p99_ms"], "candidate_p99_ms": candidate["p99_ms"], "control_cpu_per_op": control["server_cpu_seconds_per_op"], "candidate_cpu_per_op": candidate["server_cpu_seconds_per_op"], "control_rss_bytes": control["server_rss_bytes"], "candidate_rss_bytes": candidate["server_rss_bytes"], "rss_saving_bytes": control["server_rss_bytes"] - candidate["server_rss_bytes"]})
    control_ops = statistics.median(row["ops_per_sec"] for row in controls)
    candidate_ops = statistics.median(row["ops_per_sec"] for row in candidates)
    median_rss_saving = statistics.median(row["server_rss_bytes"] for row in controls) - statistics.median(row["server_rss_bytes"] for row in candidates)
    reasons = []
    for pair in pairs:
        if pair["throughput_ratio"] < .98: reasons.append("paired throughput below 98%")
        if pair["candidate_p99_ms"] > pair["control_p99_ms"] * 1.05: reasons.append("paired p99 regression above 5%")
        if pair["candidate_cpu_per_op"] > pair["control_cpu_per_op"] * 1.02: reasons.append("paired CPU/op regression above 2%")
        if pair["rss_saving_bytes"] < RSS_SAVING_BYTES: reasons.append("paired RSS saving below 24 MiB")
    if candidate_ops < control_ops * .98: reasons.append("median throughput below 98%")
    if median_rss_saving < RSS_SAVING_BYTES: reasons.append("median RSS saving below 24 MiB")
    return {"pairs": pairs, "control_ops_median": control_ops, "candidate_ops_median": candidate_ops, "median_rss_saving_bytes": median_rss_saving, "primary_pass": not reasons, "reasons": reasons, "interpretation": "bounded memory-layout pilot; no default promotion claim"}


def run(output, control_image, control_sha, candidate_image, candidate_sha, client_image):
    output.mkdir(parents=True, exist_ok=True)
    (output / ".gitignore").write_text("*\n!.gitignore\n")
    assert not (output / "protocol.json").exists() and not (output / "summary.json").exists(), "refusing to mix an existing entry-layout pilot"
    assert not any((output / label).exists() for label, _ in ORDER), "refusing to mix an existing pilot cell"
    control_info = json.loads(p.docker("image", "inspect", control_image))[0]
    candidate_info = json.loads(p.docker("image", "inspect", candidate_image))[0]
    client_info = json.loads(p.docker("image", "inspect", client_image))[0]
    assert control_info["Architecture"] == candidate_info["Architecture"] == client_info["Architecture"] == "arm64"
    control_id, candidate_id, client_id = control_info["Id"], candidate_info["Id"], client_info["Id"]
    protocol = {"network": NETWORK, "order": ORDER, "control_image_id": control_id, "candidate_image_id": candidate_id, "client_image_id": client_id, "control_binary_sha256": control_sha, "candidate_binary_sha256": candidate_sha, "workers": 2, "server_cpus": "0-1", "server_quota": 2, "client_cpus": "2-5", "client_quota": 4, "keys": 1_000_000, "key_range": [1_000_000, 1_999_999], "key_bytes": 16, "value_bytes": 256, "threads": 32, "connections": 128, "pipeline": 1, "ratio": "1:4", "warmup_seconds": 10, "timed_seconds": 60, "clock_metadata_required": True, "gates": {"paired_throughput_min": .98, "median_throughput_min": .98, "paired_p99_max_ratio": 1.05, "paired_cpu_max_ratio": 1.02, "paired_and_median_rss_saving_bytes": RSS_SAVING_BYTES}, "interpretation": "bounded memory-layout pilot; no default promotion claim"}
    protocol["docker_version"] = p.docker("--version").strip()
    protocol["within_mode_variation_max"] = 0.05
    protocol["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in ("build.zig", "src/engine/kv/concurrent_kv.zig", "src/server/worker.zig", "src/server/event_loop.zig", "bench/loadtest/scripts/profile_local_docker.py", "bench/loadtest/scripts/compare_entry_layouts.py")}
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    rows = []
    try:
        for label, variant in ORDER:
            image, sha = (control_id, control_sha) if variant == "control" else (candidate_id, candidate_sha)
            cell = output / label
            configure(cell, image, client_id, sha)
            require_clean()
            try:
                print("START", label, flush=True)
                p.setup()
                backend = [line for line in p.logs(p.SERVER).splitlines() if "io_uring backend active" in line]
                assert len(backend) == 2, f"expected two backend log lines, got {len(backend)}"
                p.save("container-definitions.json", json.loads(p.docker("inspect", p.SERVER, p.CLIENT, p.PROFILER)))
                health_helpers.health(label, "before-preload")
                p.preload()
                health_helpers.health(label, "after-preload")
                warmup = p.run_load("warmup", 10)
                assert not warmup["constrained"], f"inconclusive: constrained warmup {label}"
                health_helpers.health(label, "after-warmup")
                row = p.run_load(label, 60) | {"variant": variant, "binary_sha256": sha, "server_image_id": image, "client_image_id": client_id}
                assert not row["constrained"], f"inconclusive: constrained timed cell {label}"
                health_helpers.health(label, "after")
                rows.append(row)
                (output / "cells.json").write_text(json.dumps(rows, indent=2) + "\n")
                ensure_stable(rows, variant)
                print("DONE", label, round(row["ops_per_sec"]), "ops/s", flush=True)
            finally:
                health_helpers.cleanup()
    except Exception as exc:
        (output / "summary.json").write_text(json.dumps({"status": "inconclusive", "reason": str(exc), "cells": rows}, indent=2) + "\n")
        raise
    summary = {"status": "complete", "cells": rows, "comparison": evaluate(rows)}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["comparison"], indent=2), flush=True)


def self_test():
    rows = []
    for label, variant in ORDER:
        rows.append({"cell": label, "variant": variant, "ops_per_sec": 100 if variant == "control" else 99, "p99_ms": 1, "server_cpu_seconds_per_op": 1, "server_rss_bytes": 100 * 1024 * 1024 if variant == "control" else 70 * 1024 * 1024})
    result = evaluate(rows)
    assert result["primary_pass"] and result["median_rss_saving_bytes"] == 30 * 1024 * 1024
    bad_order = list(reversed(rows))
    assert evaluate(bad_order)["primary_pass"]
    bad = [dict(row) for row in rows]
    next(row for row in bad if row["cell"] == "2-candidate")["server_rss_bytes"] = 90 * 1024 * 1024
    assert not evaluate(bad)["primary_pass"]
    unstable = [dict(row) for row in rows]
    next(row for row in unstable if row["cell"] == "3-candidate")["ops_per_sec"] = 90
    try:
        ensure_stable(unstable, "candidate")
    except RuntimeError as exc:
        assert "variation" in str(exc)
    else:
        raise AssertionError("unstable repeated candidate was accepted")
    print("Self-test passed: ordering and paired memory/throughput gates")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--control-image")
    ap.add_argument("--control-sha")
    ap.add_argument("--candidate-image")
    ap.add_argument("--candidate-sha")
    ap.add_argument("--client-image")
    ap.add_argument("--output", type=pathlib.Path, default=OUT)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
    else:
        assert all((args.control_image, args.control_sha, args.candidate_image, args.candidate_sha, args.client_image)), "all image and SHA arguments are required"
        assert len(args.control_sha) == len(args.candidate_sha) == 64
        run(args.output.resolve(), args.control_image, args.control_sha, args.candidate_image, args.candidate_sha, args.client_image)
