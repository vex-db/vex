#!/usr/bin/env python3
"""Bounded eight-scenario SQ-head-cache confirmation matrix."""
import argparse
import hashlib
import json
import pathlib
import statistics
import subprocess

import compare_sq_head_cache as c
import profile_local_docker as p


ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / "bench" / "loadtest" / "runs" / "2026-09-22-sq-head-confirmation"
IMAGE = "vex-sq-head-cache:arm64"
SHA = "4f3df6e8916d9c127a6a9a7940701e1faa3e11182c5809310f7bd2c42fc047d6"
BASE_KEYS = 100_000
BASE_MIN = 1_000_000
BASE_MAX = 1_099_999
BASE_ORDER = (("off-1", False), ("on-1", True), ("on-2", True), ("off-2", False))
SCENARIOS = (
    ("set-32-p1", 32, "1:0", 1),
    ("get-32-p1", 32, "0:1", 1),
    ("mixed-32-p1", 32, "1:4", 1),
    ("mixed-128-p1", 128, "1:4", 1),
    ("mixed-256-p1", 256, "1:4", 1),
    ("mixed-1024-p1", 1024, "1:4", 1),
    ("mixed-4096-p1", 4096, "1:4", 1),
    ("mixed-256-p8", 256, "1:4", 8),
)


class StopExperiment(RuntimeError):
    pass


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n" if isinstance(value, (dict, list)) else str(value))


def source_provenance(image):
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
    info = json.loads(p.docker("image", "inspect", image))[0]
    assert info.get("Architecture") == "arm64", info
    return {
        "git_head": git,
        "event_loop_sha256": digest(ROOT / "src/server/event_loop.zig"),
        "profile_helper_sha256": digest(ROOT / "bench/loadtest/scripts/profile_local_docker.py"),
        "comparison_helper_sha256": digest(ROOT / "bench/loadtest/scripts/compare_sq_head_cache.py"),
        "confirmation_script_sha256": digest(pathlib.Path(__file__).resolve()),
        "image_ref": image,
        "image_id": info["Id"],
        "image_architecture": info["Architecture"],
        "image_repo_digests": info.get("RepoDigests", []),
        "binary_sha256": SHA,
    }


def configure_scenario(path, value_bytes, ratio, pipeline, image):
    c.configure(path, image, SHA)
    p.NETWORK = "vex-sq-confirm-20260922"
    p.SERVER, p.CLIENT, p.PROFILER = (p.NETWORK + "-" + suffix for suffix in ("server", "client", "profiler"))
    p.KEYS = BASE_KEYS
    p.KEY_MINIMUM = BASE_MIN
    p.KEY_MAXIMUM = BASE_MAX
    p.VALUE_BYTES = value_bytes
    p.RATIO = ratio
    p.PIPELINE = pipeline
    p.SAMPLE_SECONDS = 60
    p.WARMUP_SECONDS = 10
    p.EXPECTED_HASH = SHA


def health(stem, phase):
    try:
        return c.health(stem, phase)
    except Exception as exc:
        raise StopExperiment(f"health gate failed ({stem}, {phase}): {exc}") from exc


def check_row(row, label):
    if row.get("constrained"):
        raise StopExperiment(f"{label} constrained: {row}")
    if row.get("operations", 0) <= 0 or row.get("server_identity") is None:
        raise StopExperiment(f"{label} invalid row: {row}")


def regression_gates(rows):
    off = [row for row in rows if not row["enabled"]]
    on = [row for row in rows if row["enabled"]]
    assert len(off) == len(on) == 2
    off_ops = statistics.median(row["ops_per_sec"] for row in off)
    on_ops = statistics.median(row["ops_per_sec"] for row in on)
    reasons = []
    if on_ops < off_ops * 0.98:
        reasons.append("median throughput regression above 2%")
    for off_row, on_row in ((rows[0], rows[1]), (rows[3], rows[2])):
        if on_row["ops_per_sec"] < off_row["ops_per_sec"] * 0.98:
            reasons.append(f"paired throughput regression above 2% ({off_row['cell']}/{on_row['cell']})")
        if on_row["p99_ms"] > off_row["p99_ms"] * 1.05:
            reasons.append(f"paired p99 regression above 5% ({off_row['cell']}/{on_row['cell']})")
        if on_row["server_rss_bytes"] > off_row["server_rss_bytes"] * 1.02:
            reasons.append(f"paired RSS regression above 2% ({off_row['cell']}/{on_row['cell']})")
        if on_row["server_cpu_seconds_per_op"] > off_row["server_cpu_seconds_per_op"] * 1.02:
            reasons.append(f"paired CPU/op regression above 2% ({off_row['cell']}/{on_row['cell']})")
    return {"off_ops_median": off_ops, "on_ops_median": on_ops, "throughput_delta": on_ops / off_ops - 1, "reasons": reasons, "passed": not reasons}


def run_scenario(root, name, value_bytes, ratio, pipeline, image):
    scenario_dir = root / name
    assert not scenario_dir.exists(), f"refusing to overwrite scenario output: {scenario_dir}"
    scenario_dir.mkdir(parents=True)
    rows = []
    for order, enabled in BASE_ORDER:
        label = f"{order}-{name}"
        cell_dir = scenario_dir / order
        configure_scenario(cell_dir, value_bytes, ratio, pipeline, image)
        for resource in (p.SERVER, p.CLIENT, p.PROFILER, p.NETWORK):
            p.require_absent(resource)
        try:
            c.start(enabled)
            health(label, "before-preload")
            p.preload()
            health(label, "after-preload")
            warmup = p.run_load(f"{order}-warmup", 10)
            check_row(warmup, f"{label} warmup")
            health(label, "after-warmup")
            row = p.run_load(order, 60) | {"enabled": enabled, "scenario": name, "order": order, "value_bytes": value_bytes, "ratio": ratio, "pipeline": pipeline, "binary_sha256": SHA}
            check_row(row, label)
            health(label, "after")
            rows.append(row)
            write(scenario_dir / "checkpoint.json", {"scenario": name, "rows": rows, "last_cell": order})
            print("DONE", label, f"{row['ops_per_sec']:,.0f} ops/s", f"p99={row['p99_ms']:.3f}ms", flush=True)
        except Exception as exc:
            write(scenario_dir / "analysis.json", {"scenario": name, "rows": rows, "status": "stopped", "reason": str(exc)})
            raise
        finally:
            c.cleanup()
    gates = regression_gates(rows)
    signal = c.compare(rows) if name == "mixed-256-p1" else None
    analysis = {"scenario": name, "workload": {"value_bytes": value_bytes, "ratio": ratio, "pipeline": pipeline, "keys": BASE_KEYS, "warmup_seconds": 10, "sample_seconds": 60}, "rows": rows, "regression_gates": gates, "efficacy_signal": signal, "status": "complete"}
    write(scenario_dir / "analysis.json", analysis)
    if not gates["passed"]:
        raise StopExperiment(f"regression gate failed: {gates}")
    if signal is not None and not signal["signal_pass"]:
        raise StopExperiment(f"efficacy gate failed: {signal}")
    return analysis


def run(root, image):
    assert not root.exists() or not any(root.iterdir()), f"refusing to overwrite existing output: {root}"
    root.mkdir(parents=True, exist_ok=True)
    write(root / ".gitignore", "*\n!.gitignore\n")
    provenance = source_provenance(image)
    locked_image = provenance["image_id"]
    protocol = {"status": "planned", "scenarios": [{"name": name, "value_bytes": value_bytes, "ratio": ratio, "pipeline": pipeline, "keys": BASE_KEYS, "key_minimum": BASE_MIN, "key_maximum": BASE_MAX} for name, value_bytes, ratio, pipeline in SCENARIOS], "ab_order": BASE_ORDER, "timed_seconds": 60, "warmup_seconds": 10, "throughput_regression_gate": .02, "cpu_per_op_regression_gate": .02, "p99_regression_gate": .05, "rss_regression_gate": .02, "efficacy": {"scenario": "mixed-256-p1", "gain_gate": .03, "lower_cpu_per_op": True}, "provenance": provenance | {"locked_image_id": locked_image}}
    write(root / "protocol.json", protocol)
    completed = []
    try:
        for scenario in SCENARIOS:
            print("START SCENARIO", scenario[0], flush=True)
            completed.append(run_scenario(root, *scenario, locked_image))
            write(root / "summary.json", {"status": "running", "completed_scenarios": [item["scenario"] for item in completed]})
    except Exception as exc:
        summary = {"status": "stopped", "reason": str(exc), "completed_scenarios": [item["scenario"] for item in completed]}
        write(root / "summary.json", summary)
        raise
    write(root / "summary.json", {"status": "complete", "completed_scenarios": [item["scenario"] for item in completed], "analyses": completed})


def self_test():
    base = {"server_identity": "x", "operations": 1, "p99_ms": 1, "server_rss_bytes": 100, "server_cpu_seconds_per_op": 1, "constrained": False}
    rows = [base | {"cell": label, "enabled": enabled, "ops_per_sec": 100 if not enabled else 101} for label, enabled in BASE_ORDER]
    assert regression_gates(rows)["passed"]
    rows[1]["ops_per_sec"] = 97
    rows[2]["ops_per_sec"] = 95
    assert not regression_gates(rows)["passed"]
    rows[1]["ops_per_sec"], rows[2]["ops_per_sec"] = 97, 105
    assert not regression_gates(rows)["passed"]
    print("Self-test passed: ABBA regression gates")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=pathlib.Path, default=OUT)
    ap.add_argument("--image", default=IMAGE)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
    else:
        run(args.output.resolve(), args.image)


if __name__ == "__main__":
    main()
