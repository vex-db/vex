#!/usr/bin/env python3
"""Bounded same-binary TCP versus Unix-domain-socket diagnostic."""
import argparse
import json
import pathlib
import statistics

import compare_sq_head_cache as health_helpers
import profile_local_docker as p


ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / "bench" / "loadtest" / "runs" / "2026-09-22-local-transports"
NETWORK = "vex-transport-20260922"
ORDER = (("worker1-tcp-1", 1, "tcp"), ("worker1-uds-1", 1, "uds"), ("worker1-uds-2", 1, "uds"), ("worker1-tcp-2", 1, "tcp"), ("worker2-uds-1", 2, "uds"), ("worker2-tcp-1", 2, "tcp"), ("worker2-tcp-2", 2, "tcp"), ("worker2-uds-2", 2, "uds"))
EXPECTED_SHA = "59ff67f50b15852d69a25cec41cd8ab9bc692dd94287ec93ac2126543c0c9b9f"


def configure(output, image, client_image, sha, workers, transport):
    p.OUT = pathlib.Path(output)
    p.OUT.mkdir(parents=True, exist_ok=True)
    p.NETWORK = NETWORK
    p.SERVER, p.CLIENT, p.PROFILER = (NETWORK + "-" + x for x in ("server", "client", "profiler"))
    p.SERVER_IMAGE, p.CLIENT_IMAGE, p.EXPECTED_HASH = image, client_image, sha
    p.WORKERS, p.SERVER_CPUSET, p.SERVER_QUOTA = workers, "0" if workers == 1 else "0-1", workers
    p.KEYS, p.KEY_MINIMUM, p.KEY_MAXIMUM = 1_000_000, 1_000_000, 1_999_999
    p.VALUE_BYTES, p.THREADS, p.CONNECTIONS, p.PIPELINE, p.RATIO = 256, 32, 128, 1, "1:4"
    p.SAMPLE_SECONDS, p.WARMUP_SECONDS = 60, 10
    p.TRANSPORT = transport
    # Keep the server's passive UDS listener identical in TCP and UDS cells;
    # only the client destination changes.
    p.UNIX_SOCKET = "/bench-sockets/vex.sock"
    # Keep one task-owned volume across both modes; UDS uses the mounted path,
    # while TCP simply carries the same container definition and cleanup scope.
    p.SOCKET_VOLUME = NETWORK + "-sockets"
    p.REQUIRE_MONOTONIC_CLOCK = True


def resources():
    return (p.SERVER, p.CLIENT, p.PROFILER, p.NETWORK, p.SOCKET_VOLUME)


def cleanup():
    p.docker("rm", "-f", p.PROFILER, p.CLIENT, p.SERVER, check=False)
    p.docker("network", "rm", p.NETWORK, check=False)
    if p.SOCKET_VOLUME:
        p.docker("volume", "rm", p.SOCKET_VOLUME, check=False)


def require_clean():
    for name in resources():
        if name:
            p.require_absent(name)


def stable(rows, workers, transport):
    selected = [row for row in rows if row["workers"] == workers and row["transport"] == transport]
    if len(selected) < 2:
        return None
    median = statistics.median(row["ops_per_sec"] for row in selected)
    variation = (max(row["ops_per_sec"] for row in selected) - min(row["ops_per_sec"] for row in selected)) / median
    if variation > 0.05:
        raise RuntimeError(f"inconclusive: {workers}-worker {transport} variation {variation:.3%} exceeds 5%")
    return variation


def compare(rows):
    result = {"transport_pairs": {}, "scaling": {}, "interpretation": "diagnostic transport attribution; no engine promotion claim"}
    for workers in (1, 2):
        tcp = [r for r in rows if r["workers"] == workers and r["transport"] == "tcp"]
        uds = [r for r in rows if r["workers"] == workers and r["transport"] == "uds"]
        assert len(tcp) == len(uds) == 2
        pairs = []
        for suffix in ("1", "2"):
            tcp_row = next(r for r in tcp if r["cell"] == f"worker{workers}-tcp-{suffix}")
            uds_row = next(r for r in uds if r["cell"] == f"worker{workers}-uds-{suffix}")
            pairs.append({"tcp_ops_per_sec": tcp_row["ops_per_sec"], "uds_ops_per_sec": uds_row["ops_per_sec"], "uds_vs_tcp": uds_row["ops_per_sec"] / tcp_row["ops_per_sec"] - 1, "tcp_cpu_per_op": tcp_row["server_cpu_seconds_per_op"], "uds_cpu_per_op": uds_row["server_cpu_seconds_per_op"], "tcp_p99_ms": tcp_row["p99_ms"], "uds_p99_ms": uds_row["p99_ms"], "tcp_rss_bytes": tcp_row["server_rss_bytes"], "uds_rss_bytes": uds_row["server_rss_bytes"], "cpu_direction_supported": uds_row["server_cpu_seconds_per_op"] <= tcp_row["server_cpu_seconds_per_op"]})
        tcp_ops, uds_ops = statistics.median(r["ops_per_sec"] for r in tcp), statistics.median(r["ops_per_sec"] for r in uds)
        result["transport_pairs"][str(workers)] = {"pairs": pairs, "tcp_ops_median": tcp_ops, "uds_ops_median": uds_ops, "uds_vs_tcp": uds_ops / tcp_ops - 1, "difference_gate_pass": all(pair["uds_vs_tcp"] >= 0.05 for pair in pairs), "cpu_direction_supported": all(pair["cpu_direction_supported"] for pair in pairs), "inconclusive": not all(pair["uds_vs_tcp"] >= 0.05 and pair["cpu_direction_supported"] for pair in pairs), "tcp_cpu_per_op": statistics.median(r["server_cpu_seconds_per_op"] for r in tcp), "uds_cpu_per_op": statistics.median(r["server_cpu_seconds_per_op"] for r in uds), "tcp_p99_ms": statistics.median(r["p99_ms"] for r in tcp), "uds_p99_ms": statistics.median(r["p99_ms"] for r in uds), "tcp_rss_bytes": statistics.median(r["server_rss_bytes"] for r in tcp), "uds_rss_bytes": statistics.median(r["server_rss_bytes"] for r in uds)}
        stable(rows, workers, "tcp")
        stable(rows, workers, "uds")
    for transport in ("tcp", "uds"):
        one = result["transport_pairs"]["1"][f"{transport}_ops_median"]
        two = result["transport_pairs"]["2"][f"{transport}_ops_median"]
        result["scaling"][transport] = {"two_over_one": two / one}
    result["transport_difference_gate_pass"] = all(pair["difference_gate_pass"] for pair in result["transport_pairs"].values())
    result["cpu_direction_supported"] = all(pair["cpu_direction_supported"] for pair in result["transport_pairs"].values())
    result["inconclusive"] = not (result["transport_difference_gate_pass"] and result["cpu_direction_supported"])
    return result


def run(output, image, client_image, sha):
    output.mkdir(parents=True, exist_ok=True)
    (output / ".gitignore").write_text("*\n!.gitignore\n")
    assert not (output / "protocol.json").exists() and not (output / "summary.json").exists(), "refusing to mix an existing transport comparison"
    assert not any((output / label).exists() for label, _, _ in ORDER), "refusing to mix an existing transport cell"
    server_info = json.loads(p.docker("image", "inspect", image))[0]
    client_info = json.loads(p.docker("image", "inspect", client_image))[0]
    assert server_info["Architecture"] == client_info["Architecture"] == "arm64"
    server_id, client_id = server_info["Id"], client_info["Id"]
    protocol = {"network": NETWORK, "server_image_id": server_id, "client_image_id": client_id, "server_repo_digests": server_info.get("RepoDigests", []), "client_repo_digests": client_info.get("RepoDigests", []), "binary_sha256": sha, "order": ORDER, "workers": [1, 2], "transports": ["tcp", "uds"], "warmup_seconds": 10, "timed_seconds": 60, "stability_gate": 0.05, "transport_difference_gate": 0.05, "configuration": {"keys": 1_000_000, "key_minimum": 1_000_000, "key_maximum": 1_999_999, "key_bytes": 16, "value_bytes": 256, "threads": 32, "connections": 128, "pipeline": 1, "ratio": "1:4", "server_cpus": {"1": "0", "2": "0-1"}, "server_quota": {"1": 1, "2": 2}, "client_cpus": "2-5", "client_quota": 4, "socket_path": "/bench-sockets/vex.sock", "socket_volume": NETWORK + "-sockets"}, "clock_metadata": {"required": True, "clock_source": p.CLOCK_SOURCE, "source_tag": p.CLOCK_TAG, "source_commit": p.CLOCK_COMMIT}}
    protocol["docker_version"] = p.docker("--version").strip()
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    rows = []
    try:
        for label, workers, transport in ORDER:
            cell = output / label
            configure(cell, server_id, client_id, sha, workers, transport)
            require_clean()
            try:
                print("START", label, flush=True)
                p.setup()
                backend = [line for line in p.logs(p.SERVER).splitlines() if "io_uring backend active" in line]
                assert len(backend) == workers, f"expected {workers} backend log lines, got {len(backend)}"
                p.save("container-definitions.json", json.loads(p.docker("inspect", p.SERVER, p.CLIENT, p.PROFILER)))
                health_helpers.health(label, "before-preload")
                p.preload()
                health_helpers.health(label, "after-preload")
                warmup = p.run_load("warmup", 10)
                assert not warmup["constrained"], f"inconclusive: constrained warmup {label}"
                health_helpers.health(label, "after-warmup")
                row = p.run_load(label, 60) | {"workers": workers, "transport": transport, "binary_sha256": sha, "server_image_id": server_id, "client_image_id": client_id}
                assert not row["constrained"], f"inconclusive: constrained timed cell {label}"
                health_helpers.health(label, "after")
                rows.append(row)
                (output / "cells.json").write_text(json.dumps(rows, indent=2) + "\n")
                stable(rows, workers, transport)
                print("DONE", label, round(row["ops_per_sec"]), "ops/s", flush=True)
            finally:
                cleanup()
    except Exception as exc:
        (output / "summary.json").write_text(json.dumps({"status": "inconclusive", "reason": str(exc), "cells": rows}, indent=2) + "\n")
        raise
    comparison = compare(rows)
    summary = {"status": "complete", "interpretation_status": "inconclusive" if comparison["inconclusive"] else "diagnostic_gate_pass", "cells": rows, "comparison": comparison}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary["comparison"], indent=2), flush=True)


def self_test():
    rows = []
    for label, workers, transport in ORDER:
        base = 100.0 if transport == "tcp" else 108.0
        rows.append({"cell": label, "workers": workers, "transport": transport, "ops_per_sec": base + (1 if label.endswith("-2") else 0), "server_cpu_seconds_per_op": 1.0, "p99_ms": 1.0, "server_rss_bytes": 1})
    result = compare(rows)
    assert result["transport_pairs"]["1"]["difference_gate_pass"]
    assert not result["transport_pairs"]["1"]["inconclusive"]
    assert result["transport_difference_gate_pass"] and not result["inconclusive"]
    assert result["scaling"]["tcp"]["two_over_one"] == 1
    masked = [dict(row) for row in rows]
    next(row for row in masked if row["cell"] == "worker1-uds-1")["ops_per_sec"] = 104
    assert not compare(masked)["transport_pairs"]["1"]["difference_gate_pass"]
    cpu_bad = [dict(row) for row in rows]
    next(row for row in cpu_bad if row["cell"] == "worker1-uds-1")["server_cpu_seconds_per_op"] = 2
    assert compare(cpu_bad)["transport_pairs"]["1"]["inconclusive"]
    print("Self-test passed: transport matrix, stability, and diagnostic gates")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=pathlib.Path, default=OUT)
    ap.add_argument("--image", default="vex-perf-local:arm64")
    ap.add_argument("--client-image", default=p.CLIENT_IMAGE)
    ap.add_argument("--sha", default=EXPECTED_SHA)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
    else:
        assert len(args.sha) == 64, "supply the native candidate SHA256"
        run(args.output.resolve(), args.image, args.client_image, args.sha)
