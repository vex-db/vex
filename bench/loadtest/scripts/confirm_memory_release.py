#!/usr/bin/env python3
"""Six-cell AWS confirmation that bounded expiry reclamation does not regress Vex."""
import argparse
import json
import pathlib
import statistics
import sys

HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
import compare_aws_engines as c

BASE = c.ROOT / "bench/loadtest/runs/2026-09-25-memory-release-perf"
NS = "vex-release-20260925"
CELLS = (("round1-control", "control"), ("round1-candidate", "candidate"),
         ("round2-candidate", "candidate"), ("round2-control", "control"),
         ("round3-control", "control"), ("round3-candidate", "candidate"))


def write(path, value):
    assert not path.exists(), f"refusing to overwrite {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")


def inventory():
    raw = json.loads((BASE / "engines.json").read_text())
    engines = raw.get("engines", raw)
    assert set(engines) == {"control", "candidate"}, engines
    for name, item in engines.items():
        for field in ("image", "binary_sha256", "process_name", "command"):
            assert field in item, (name, field)
        assert item["process_name"] == "vex" and "@sha256:" in item["image"] and len(item["binary_sha256"]) == 64, item
        assert isinstance(item["command"], list) and item["command"], item
    return engines


def metric(row, key):
    return row["ops"] if key == "ops" else row[key]


def summarize(rows):
    groups = {name: [row for row in rows if row["engine"] == name] for name in ("control", "candidate")}
    report = {}
    for name, points in groups.items():
        assert len(points) == 3, (name, len(points))
        report[name] = {"cells": [x["cell"] for x in points],
                        "median_ops": statistics.median(metric(x, "ops") for x in points),
                        "median_p99_ms": statistics.median(metric(x, "p99_ms") for x in points),
                        "median_peak_rss_bytes": statistics.median(metric(x, "sampled_peak_rss_bytes") for x in points),
                        "ops_spread": (max(metric(x, "ops") for x in points) - min(metric(x, "ops") for x in points)) / statistics.median(metric(x, "ops") for x in points),
                        "measurement_valid": all(x.get("measurement_valid") for x in points)}
    ratios = {"throughput_candidate_control": report["candidate"]["median_ops"] / report["control"]["median_ops"],
              "p99_candidate_control": report["candidate"]["median_p99_ms"] / report["control"]["median_p99_ms"],
              "peak_rss_candidate_control": report["candidate"]["median_peak_rss_bytes"] / report["control"]["median_peak_rss_bytes"]}
    reasons = []
    if not all(x["measurement_valid"] for x in report.values()): reasons.append("invalid cell")
    if any(x["ops_spread"] > .05 for x in report.values()): reasons.append("variant throughput spread exceeds 5%")
    if ratios["throughput_candidate_control"] < .98: reasons.append("candidate throughput median below 98% of control")
    if ratios["p99_candidate_control"] > 1.10: reasons.append("candidate p99 median exceeds 110% of control")
    if ratios["peak_rss_candidate_control"] > 1.02: reasons.append("candidate peak RSS median exceeds 102% of control")
    return {"cells": rows, "variants": report, "ratios": ratios, "gates_pass": not reasons,
            "inconclusive_reasons": reasons, "reruns": "none; predeclared six cells only"}


def configure():
    c.BASE, c.NS = BASE, NS
    c.K = ["kubectl", "--context", c.CTX, "-n", NS]
    c.engine_inventory = inventory
    original = c.postload_checks
    c.postload_checks = lambda ignored, host, out, artifact: original("vex", host, out, artifact)


def run():
    configure()
    assert not (BASE / "comparison.json").exists(), "refusing existing comparison"
    rows = []
    for cell, engine in CELLS:
        row = c.run_cell(cell, engine)
        rows.append(row)
        write(BASE / "progress" / f"{cell}.json", rows)
        write(BASE / "cells" / cell / "confirm_memory_release.py", HERE.read_text())
    write(BASE / "comparison.json", summarize(rows))


def self_test():
    rows = []
    for engine, ops, p99, rss in (("control", 100, 1, 100), ("candidate", 99, 1.1, 102)):
        rows += [{"engine": engine, "cell": f"{engine}-{i}", "ops": ops + i, "p99_ms": p99, "sampled_peak_rss_bytes": rss,
                  "measurement_valid": True} for i in range(3)]
    report = summarize(rows)
    assert report["gates_pass"] and report["ratios"]["throughput_candidate_control"] >= .98
    rows[-1]["ops"] = 80
    assert not summarize(rows)["gates_pass"]
    print("memory release confirmation aggregation passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    self_test() if args.self_test else run()
