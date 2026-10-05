#!/usr/bin/env python3
"""Verify the packed-entry candidate against Vex control and two engines.

This is intentionally a private AWS runner.  It reuses the existing pod,
identity, monotonic-clock, cgroup, memtier, value-sampling, and resource
guards from compare_aws_engines.py, but owns the 24-cell packed-growth matrix
and its result classification.
"""
import argparse
import json
import pathlib
import statistics
import sys

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent))
import compare_aws_engines as shared

BASE = ROOT / "bench/loadtest/runs/2026-09-25-packed-growth-aws"
NS = "vex-release-20260925"
VALUE_BYTES = (256, 32)
ENGINES = ("control", "candidate", "redis", "dragonfly")
ORDERS = (
    ("control", "candidate", "redis", "dragonfly"),
    ("redis", "dragonfly", "control", "candidate"),
    ("candidate", "redis", "dragonfly", "control"),
)
CELLS = tuple(
    (f"value{value}-round{round_no}-{engine}", engine, value)
    for value in VALUE_BYTES
    for round_no, order in enumerate(ORDERS, 1)
    for engine in order
)


def configure(value):
    shared.BASE = BASE
    shared.NS = NS
    shared.K = ["kubectl", "--context", shared.CTX, "-n", NS]
    shared.KEYS = 1_000_000
    shared.VALUE_BYTES = value
    shared.PIPELINE = 1
    shared.CONNECTIONS = 128
    shared.WARMUP_SECONDS = 10
    shared.MEASURE_SECONDS = 60
    shared.SERVER_CPU = 6
    shared.CLIENT_CPU = 24
    # compare_aws_engines exposes the AWS profile as ``aws.local``.
    local = shared.aws.local
    local.KEYS = shared.KEYS
    local.VALUE_BYTES = value
    local.KEY_MINIMUM = 1_000_000
    local.KEY_MAXIMUM = 1_999_999
    local.REQUIRE_MONOTONIC_CLOCK = True
    # compare_aws_engines.measure validates its memtier JSON through the
    # imported saturation helper, which has its own value-size global.
    sat = shared.aws.sat
    sat.VALUE_BYTES = value
    sat.KEYS = shared.KEYS
    sat.KEY_MINIMUM = local.KEY_MINIMUM
    sat.KEY_MAXIMUM = local.KEY_MAXIMUM
    sat.KEY_PREFIX = local.KEY_PREFIX
    shared.HERE = HERE


def inventory():
    raw = json.loads((BASE / "engines.json").read_text())
    engines = raw.get("engines", raw)
    assert set(engines) == set(ENGINES), sorted(engines)
    for name, item in engines.items():
        for key in ("image", "process_name", "binary_sha256", "command"):
            assert key in item, (name, key)
        assert "@sha256:" in item["image"] and len(item["binary_sha256"]) == 64, item
        assert isinstance(item["command"], list), item
    return engines


shared.engine_inventory = inventory
_postload_checks = shared.postload_checks


def backend_name(name):
    return "vex" if name in {"control", "candidate"} else name


def postload_checks(name, host, out, artifact):
    # Redis has extra INFO checks; both Vex variants use the Vex probe check.
    return _postload_checks(backend_name(name), host, out, artifact)


shared.postload_checks = postload_checks


def append_progress(event):
    path = BASE / "progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True) + "\n")
        stream.flush()


def run_one(label, engine_name, value):
    configure(value)
    row = shared.run_cell(label, engine_name)
    row = normalize_measurement_row(row)
    row.update(value_bytes=value, matrix_engine=engine_name)
    out = BASE / "cells" / label
    shared.save(out, "matrix-row.json", row)
    return row


def normalize_measurement_row(row):
    """Give aggregation one stable name for shared runner throughput."""
    normalized = dict(row)
    if "ops_per_sec" not in normalized and "ops" in normalized:
        normalized["ops_per_sec"] = normalized["ops"]
    if "ops_per_sec" not in normalized:
        raise ValueError("shared measurement row has no ops/ops_per_sec field")
    return normalized


def group(rows, engine, value):
    return [row for row in rows if row.get("matrix_engine") == engine and row.get("value_bytes") == value]


def summarize(rows, invalid):
    if invalid:
        return {
            "status": "measurement_invalid",
            "reason": "one or more hard measurement guards failed",
            "invalid_cells": invalid,
            "valid_cells": rows,
        }

    inconclusive = []
    reasons = []
    stability = {}
    for engine in ENGINES:
        for value in VALUE_BYTES:
            points = group(rows, engine, value)
            if len(points) != 3:
                inconclusive.append(f"missing repeated cells: {engine}/{value}")
                continue
            rates = [float(row["ops_per_sec"]) for row in points]
            median = statistics.median(rates)
            spread = (max(rates) - min(rates)) / median if median else float("inf")
            stability[f"{engine}/{value}"] = {"ops_per_sec": rates, "median": median, "spread": spread, "stable": spread <= .05}
            if spread > .05:
                inconclusive.append(f"{engine}/{value} spread exceeds 5%")

    comparisons = []
    for value in VALUE_BYTES:
        control = group(rows, "control", value)
        candidate = group(rows, "candidate", value)
        redis = group(rows, "redis", value)
        dragonfly = group(rows, "dragonfly", value)
        if not all((control, candidate, redis, dragonfly)):
            continue
        c = {"throughput": statistics.median(x["ops_per_sec"] for x in control), "p99": statistics.median(x["p99_ms"] for x in control), "rss": statistics.median(x["sampled_peak_rss_bytes"] for x in control)}
        n = {"throughput": statistics.median(x["ops_per_sec"] for x in candidate), "p99": statistics.median(x["p99_ms"] for x in candidate), "rss": statistics.median(x["sampled_peak_rss_bytes"] for x in candidate)}
        r = statistics.median(x["ops_per_sec"] for x in redis)
        d = statistics.median(x["ops_per_sec"] for x in dragonfly)
        item = {
            "value_bytes": value,
            "control": c,
            "candidate": n,
            "candidate_control_throughput_ratio": n["throughput"] / c["throughput"],
            "candidate_p99_ratio": n["p99"] / c["p99"],
            "candidate_rss_le_control": n["rss"] <= c["rss"],
            "candidate_ahead_redis": n["throughput"] >= 1.05 * r,
            "candidate_ahead_dragonfly": n["throughput"] >= 1.05 * d,
            "required_competitor_lead_fraction": 0.05,
            "redis_median_throughput": r,
            "dragonfly_median_throughput": d,
        }
        comparisons.append(item)
        if item["candidate_control_throughput_ratio"] < .98:
            reasons.append(f"{value}B candidate/control throughput below 98%")
        if item["candidate_p99_ratio"] > 1.10:
            reasons.append(f"{value}B candidate p99 above 110% of control")
        if not item["candidate_rss_le_control"]:
            reasons.append(f"{value}B candidate RSS exceeds control")
        if not item["candidate_ahead_redis"] or not item["candidate_ahead_dragonfly"]:
            reasons.append(f"{value}B candidate does not retain a 5% lead over both comparison engines")

    if inconclusive:
        return {
            "status": "measurement_inconclusive",
            "reasons": inconclusive,
            "stability": stability,
            "comparisons": comparisons,
            "cells": rows,
            "matrix": {"keys": 1_000_000, "values": list(VALUE_BYTES), "engines": list(ENGINES), "pipeline": 1, "connections": 128, "server_cpu": 6, "warmup_seconds": 10, "measure_seconds": 60, "rounds_per_value": 3, "orders": ORDERS},
        }
    return {
        "status": "accepted" if not reasons else "performance_rejection",
        "reasons": reasons,
        "stability": stability,
        "comparisons": comparisons,
        "cells": rows,
        "matrix": {"keys": 1_000_000, "values": list(VALUE_BYTES), "engines": list(ENGINES), "pipeline": 1, "connections": 128, "server_cpu": 6, "warmup_seconds": 10, "measure_seconds": 60, "rounds_per_value": 3, "orders": ORDERS},
    }


def self_test():
    assert len(CELLS) == 24
    assert len({label for label, _, _ in CELLS}) == 24
    assert all(len([x for x in CELLS if x[2] == value and x[1] == engine]) == 3 for value in VALUE_BYTES for engine in ENGINES)
    configure(32)
    assert shared.aws.local.VALUE_BYTES == shared.aws.sat.VALUE_BYTES == 32
    configure(256)
    assert shared.aws.local.VALUE_BYTES == shared.aws.sat.VALUE_BYTES == 256
    assert backend_name("control") == backend_name("candidate") == "vex"
    assert backend_name("redis") == "redis"
    shared_shape = {"ops": 12.5, "p99_ms": 1.0, "operation_count": 100, "runtime_ms": 1000, "connection_errors": 0, "misses_per_sec": 0}
    assert normalize_measurement_row(shared_shape)["ops_per_sec"] == 12.5
    assert normalize_measurement_row({"ops_per_sec": 13.5})["ops_per_sec"] == 13.5
    try:
        normalize_measurement_row({"p99_ms": 1.0})
    except ValueError:
        pass
    else:
        raise AssertionError("missing throughput field was accepted")
    rows = []
    for value in VALUE_BYTES:
        for engine, rate, rss in (("control", 100.0, 100), ("candidate", 99.0, 90), ("redis", 80.0, 120), ("dragonfly", 70.0, 130)):
            for round_no in range(3):
                rows.append({"matrix_engine": engine, "value_bytes": value, "ops_per_sec": rate, "p99_ms": 1.0, "sampled_peak_rss_bytes": rss})
    result = summarize(rows, [])
    assert result["status"] == "accepted", result
    bad = [dict(row) for row in rows]
    for row in bad:
        if row["matrix_engine"] == "candidate":
            row["sampled_peak_rss_bytes"] = 101
    assert summarize(bad, [])["status"] == "performance_rejection"
    unstable = [dict(row) for row in rows]
    unstable[0]["ops_per_sec"] = 120.0
    assert summarize(unstable, [])["status"] == "measurement_inconclusive"
    invalid = summarize(rows[:3], [{"cell": "broken", "reason": "clock"}])
    assert invalid["status"] == "measurement_invalid"
    print("packed-growth runner self-test passed")


def prepare_run_directory():
    """Keep root-provided inputs, but reject any prior measurement output."""
    if not BASE.exists():
        BASE.mkdir(parents=True)
        return
    artifacts = {
        "cells",
        "comparison.json",
        "matrix.json",
        "progress.jsonl",
    }
    prior = sorted(path.name for path in BASE.iterdir() if path.name in artifacts)
    if prior:
        raise RuntimeError(f"refusing existing measurement artifacts in {BASE}: {prior}")


def run_matrix(selected=None):
    prepare_run_directory()
    inventory()
    (BASE / "matrix.json").write_text(json.dumps({"cells": CELLS, "orders": ORDERS}, indent=2) + "\n")
    rows, invalid = [], []
    for label, engine, value in CELLS:
        if selected and label != selected:
            continue
        try:
            row = run_one(label, engine, value)
            rows.append(row)
            append_progress({"status": "complete", "cell": label, "engine": engine, "value_bytes": value, "ops_per_sec": row["ops_per_sec"]})
        except Exception as exc:
            event = {"status": "measurement_invalid", "cell": label, "engine": engine, "value_bytes": value, "reason": str(exc)}
            invalid.append(event)
            append_progress(event)
            if selected:
                raise
    summary = summarize(rows, invalid)
    (BASE / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cell", choices=[x[0] for x in CELLS])
    parser.add_argument("--run", choices=["all24"])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if bool(args.cell) == bool(args.run):
        parser.error("choose exactly one of --cell or --run all24")
    run_matrix(args.cell)


if __name__ == "__main__":
    main()
