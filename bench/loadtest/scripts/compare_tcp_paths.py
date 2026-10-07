#!/usr/bin/env python3
"""Bounded same-binary bridge TCP versus shared-network-namespace TCP diagnostic."""
import argparse
import json
import pathlib
import re
import statistics

import compare_sq_head_cache as health
import profile_local_docker as p


ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = ROOT / "bench" / "loadtest" / "runs" / "2026-09-24-tcp-path"
NETWORK = "vex-tcp-path-20260924-network"
TASK_PREFIX = "vex-tcp-path-20260924"
ORDER = (("bridge-1", "bridge"), ("loopback-1", "loopback"), ("loopback-2", "loopback"), ("bridge-2", "bridge"))
EXPECTED_CLIENT_IMAGE = "sha256:0cd87ec181c3570ee499a2768c02d8becfa87d2f13a3b4ab9facee043fbf83e7"


def configure(output, server_image, client_image, sha, mode):
    p.OUT = pathlib.Path(output)
    p.OUT.mkdir(parents=True, exist_ok=True)
    p.NETWORK = NETWORK
    p.SERVER, p.CLIENT, p.PROFILER = (TASK_PREFIX + "-" + x for x in ("server", "client", "profiler"))
    p.SERVER_IMAGE, p.CLIENT_IMAGE, p.EXPECTED_HASH = server_image, client_image, sha
    p.WORKERS, p.SERVER_CPUSET, p.SERVER_QUOTA = 2, "0-1", 2
    p.CLIENT_NETWORK = f"container:{p.SERVER}" if mode == "loopback" else p.NETWORK
    p.TCP_HOST, p.TRANSPORT, p.UNIX_SOCKET, p.SOCKET_VOLUME = ("127.0.0.1" if mode == "loopback" else None), "tcp", None, None
    p.KEYS, p.KEY_MINIMUM, p.KEY_MAXIMUM = 1_000_000, 1_000_000, 1_999_999
    p.VALUE_BYTES, p.THREADS, p.CONNECTIONS, p.PIPELINE, p.RATIO = 256, 32, 128, 1, "1:4"
    p.SAMPLE_SECONDS, p.WARMUP_SECONDS = 60, 10
    p.REQUIRE_MONOTONIC_CLOCK = True


def resources():
    return p.SERVER, p.CLIENT, p.PROFILER, p.NETWORK


def require_clean():
    for name in resources():
        p.require_absent(name)


def cleanup():
    p.docker("rm", "-f", p.PROFILER, p.CLIENT, p.SERVER, check=False)
    p.docker("network", "rm", p.NETWORK, check=False)


def namespace_inode(container):
    return p.dex(container, "readlink", "/proc/self/ns/net").strip()


def validate_namespace_identities(mode, identities):
    assert mode in ("bridge", "loopback"), mode
    for role in ("server", "client", "profiler"):
        inode = identities.get(role)
        assert isinstance(inode, str) and re.fullmatch(r"net:\[\d+\]", inode), identities
    if mode == "loopback":
        assert identities["server"] == identities["client"], identities
    else:
        assert identities["server"] != identities["client"], identities


def save_topology(mode):
    identities = {role: namespace_inode(container) for role, container in (("server", p.SERVER), ("client", p.CLIENT), ("profiler", p.PROFILER))}
    validate_namespace_identities(mode, identities)
    p.save("network-namespaces.json", {"mode": mode, "identities": identities})
    settings = json.loads((p.OUT / "settings.json").read_text())
    settings.update({"topology": mode, "namespace_identities": identities, "actual_destination": p.TCP_HOST or p.SERVER, "actual_client_network": p.CLIENT_NETWORK or p.NETWORK})
    p.save("settings.json", settings)
    return identities


def combined_cpu(row):
    return row["server_cpu_seconds_per_op"] + row["client_cpu_seconds_per_op"]


def stable(rows, mode):
    selected = [row["ops_per_sec"] for row in rows if row["topology"] == mode]
    assert len(selected) == 2
    return (max(selected) - min(selected)) / statistics.median(selected)


def compare(rows):
    by_cell = {row.get("cell"): row for row in rows}
    reasons = []
    if set(by_cell) != {name for name, _ in ORDER}:
        reasons.append("incomplete ABBA cells")
    for row in rows:
        if row.get("constrained"):
            reasons.append(f"constrained cell: {row.get('cell')}")
        if row.get("connection_errors", 0) or row.get("misses_per_sec", 0):
            reasons.append(f"benchmark errors or misses: {row.get('cell')}")
    variations = {}
    for mode in ("bridge", "loopback"):
        if sum(row.get("topology") == mode for row in rows) == 2:
            variations[mode] = stable(rows, mode)
            if variations[mode] > 0.05:
                reasons.append(f"{mode} repeat variation exceeds 5%")

    pairs = []
    for bridge_name, loopback_name in (("bridge-1", "loopback-1"), ("bridge-2", "loopback-2")):
        bridge, loopback = by_cell.get(bridge_name), by_cell.get(loopback_name)
        if not bridge or not loopback:
            continue
        throughput_gain = loopback["ops_per_sec"] / bridge["ops_per_sec"] - 1
        server_cpu_gain = bridge["server_cpu_seconds_per_op"] / loopback["server_cpu_seconds_per_op"] - 1
        combined_bridge, combined_loopback = combined_cpu(bridge), combined_cpu(loopback)
        combined_cpu_gain = combined_bridge / combined_loopback - 1
        pair = {"bridge_cell": bridge_name, "loopback_cell": loopback_name, "throughput_gain": throughput_gain, "server_cpu_gain": server_cpu_gain, "combined_cpu_gain": combined_cpu_gain, "bridge_ops_per_sec": bridge["ops_per_sec"], "loopback_ops_per_sec": loopback["ops_per_sec"], "bridge_server_cpu_seconds_per_op": bridge["server_cpu_seconds_per_op"], "loopback_server_cpu_seconds_per_op": loopback["server_cpu_seconds_per_op"], "bridge_combined_cpu_seconds_per_op": combined_bridge, "loopback_combined_cpu_seconds_per_op": combined_loopback, "bridge_p99_ms": bridge["p99_ms"], "loopback_p99_ms": loopback["p99_ms"], "bridge_server_rss_bytes": bridge["server_rss_bytes"], "loopback_server_rss_bytes": loopback["server_rss_bytes"]}
        pairs.append(pair)
        if throughput_gain < 0.10:
            reasons.append(f"paired throughput gain below 10%: {bridge_name}/{loopback_name}")
        if loopback["server_cpu_seconds_per_op"] >= bridge["server_cpu_seconds_per_op"]:
            reasons.append(f"loopback server CPU/op did not decrease: {bridge_name}/{loopback_name}")
        if combined_loopback >= combined_bridge:
            reasons.append(f"loopback combined CPU/op did not decrease: {bridge_name}/{loopback_name}")
        if loopback["p99_ms"] > bridge["p99_ms"] * 1.05:
            reasons.append(f"loopback p99 regression over 5%: {bridge_name}/{loopback_name}")
        if loopback["server_rss_bytes"] > bridge["server_rss_bytes"] * 1.02:
            reasons.append(f"loopback RSS regression over 2%: {bridge_name}/{loopback_name}")
    if len(pairs) != 2:
        reasons.append("missing paired cells")
    bridge_ops = [row["ops_per_sec"] for row in rows if row.get("topology") == "bridge"]
    loopback_ops = [row["ops_per_sec"] for row in rows if row.get("topology") == "loopback"]
    median_gain = statistics.median(loopback_ops) / statistics.median(bridge_ops) - 1 if len(bridge_ops) == len(loopback_ops) == 2 else None
    if median_gain is None or median_gain < 0.10:
        reasons.append("median throughput gain below 10%")
    measurement_valid = (not any(row.get("constrained") or row.get("connection_errors", 0) or row.get("misses_per_sec", 0) for row in rows)
                         and len(pairs) == 2 and all(value <= 0.05 for value in variations.values()))
    return {"measurement_valid": measurement_valid, "signal_pass": measurement_valid and not reasons, "median_throughput_gain": median_gain, "repeat_variation": variations, "pairs": pairs, "reasons": reasons, "interpretation": "local topology diagnostic/deployment lever; no server-code or remote-TCP improvement claim"}


def inspect_images(server_ref, client_ref):
    server_info = json.loads(p.docker("image", "inspect", server_ref))[0]
    client_info = json.loads(p.docker("image", "inspect", client_ref))[0]
    assert server_info["Architecture"] == client_info["Architecture"] == "arm64"
    assert client_info["Id"] == EXPECTED_CLIENT_IMAGE, "the repaired client image is mandatory"
    return server_info, client_info


def run(output, server_ref, client_ref, sha):
    output = pathlib.Path(output).resolve()
    assert not output.exists(), "refusing to reuse an existing output directory"
    assert len(sha) == 64, "supply the expected native server binary SHA256"
    server_info, client_info = inspect_images(server_ref, client_ref)
    server_id, client_id = server_info["Id"], client_info["Id"]
    output.mkdir(parents=True)
    (output / ".gitignore").write_text("*\n!.gitignore\n")
    protocol = {"network": NETWORK, "task_prefix": TASK_PREFIX, "server_image_ref": server_ref, "client_image_ref": client_ref, "server_image_id": server_id, "client_image_id": client_id, "server_repo_digests": server_info.get("RepoDigests", []), "client_repo_digests": client_info.get("RepoDigests", []), "binary_sha256": sha, "order": ORDER, "warmup_seconds": 10, "timed_seconds": 60, "repeat_variation_gate": 0.05, "throughput_gate": 0.10, "p99_gate": 0.05, "rss_gate": 0.02, "clock_metadata": {"required": True, "clock_source": p.CLOCK_SOURCE, "source_tag": p.CLOCK_TAG, "source_commit": p.CLOCK_COMMIT}, "configuration": {"keys": 1_000_000, "key_bytes": 16, "value_bytes": 256, "threads": 32, "connections": 128, "pipeline": 1, "ratio": "1:4", "server_cpus": "0-1", "server_quota": 2, "client_cpus": "2-5", "client_quota": 4, "profiler_cpu": "6", "profiler_memory": "256m", "server_memory": "1g", "client_memory": "512m", "sq_head_cache": 0}}
    protocol["docker_version"] = p.docker("--version").strip()
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    rows = []
    try:
        for label, mode in ORDER:
            cell = output / label
            configure(cell, server_id, client_id, sha, mode)
            require_clean()
            try:
                print("START", label, flush=True)
                p.setup(server_env={"VEX_SQ_HEAD_CACHE": "0"})
                startup = p.logs(p.SERVER)
                assert startup.count("io_uring backend active") == 2, startup[-3000:]
                assert "sq_head_cache=true" not in startup, startup[-3000:]
                p.save("startup.log", startup)
                p.save("experiment-env.json", {"VEX_SQ_HEAD_CACHE": "0"})
                identities = save_topology(mode)
                p.save("container-definitions.json", json.loads(p.docker("inspect", p.SERVER, p.CLIENT, p.PROFILER)))
                health.health(label, "before-preload")
                p.preload()
                health.health(label, "after-preload")
                warmup = p.run_load("warmup", 10)
                assert not warmup["constrained"], f"inconclusive: constrained warmup {label}"
                health.health(label, "after-warmup")
                row = p.run_load(label, 60) | {"topology": mode, "namespace_identities": identities, "server_image_id": server_id, "client_image_id": client_id, "binary_sha256": sha}
                assert not row["constrained"], f"inconclusive: constrained timed cell {label}"
                row["combined_cpu_seconds_per_op"] = combined_cpu(row)
                health.health(label, "after")
                rows.append(row)
                (output / "cells.json").write_text(json.dumps(rows, indent=2) + "\n")
                if sum(item.get("topology") == mode for item in rows) == 2:
                    assert stable(rows, mode) <= 0.05, f"inconclusive: {mode} repeat variation exceeds 5%"
                print("DONE", label, round(row["ops_per_sec"]), "ops/s", flush=True)
            finally:
                cleanup()
    except Exception as exc:
        (output / "summary.json").write_text(json.dumps({"status": "inconclusive", "reason": str(exc), "cells": rows}, indent=2) + "\n")
        raise
    comparison = compare(rows)
    summary = {"status": "complete", "interpretation_status": "diagnostic_gate_pass" if comparison["signal_pass"] else "inconclusive", "cells": rows, "comparison": comparison}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(comparison, indent=2), flush=True)


def self_test():
    assert p.transport_args() == ["-s", p.SERVER, "-p", str(p.PORT)]
    assert p.expected_load_config(60)["server"] == p.SERVER
    old_host, old_network = p.TCP_HOST, p.CLIENT_NETWORK
    try:
        p.TCP_HOST, p.CLIENT_NETWORK = "127.0.0.1", "container:server"
        assert p.transport_args() == ["-s", "127.0.0.1", "-p", str(p.PORT)]
        assert p.expected_load_config(60)["server"] == "127.0.0.1"
    finally:
        p.TCP_HOST, p.CLIENT_NETWORK = old_host, old_network
    old_clock_required = p.REQUIRE_MONOTONIC_CLOCK
    try:
        p.REQUIRE_MONOTONIC_CLOCK = True
        try:
            p.parse_clock_metadata("")
        except AssertionError as exc:
            assert "missing" in str(exc)
        else:
            raise AssertionError("strict clock validation accepted missing metadata")
    finally:
        p.REQUIRE_MONOTONIC_CLOCK = old_clock_required
    base = {"server_cpu_seconds_per_op": 1.0, "client_cpu_seconds_per_op": 1.0, "p99_ms": 1.0, "server_rss_bytes": 100, "constrained": False}
    rows = [base | {"cell": cell, "topology": mode, "ops_per_sec": 100.0 if mode == "bridge" else 112.0, "server_cpu_seconds_per_op": 1.0 if mode == "bridge" else .8} for cell, mode in ORDER]
    result = compare(rows)
    assert result["signal_pass"] and result["median_throughput_gain"] >= .10
    constrained = [dict(row) for row in rows]
    constrained[0]["constrained"] = True
    assert not compare(constrained)["measurement_valid"]
    noisy = [dict(row) for row in rows]
    noisy[3]["ops_per_sec"] = 106.0
    assert not compare(noisy)["signal_pass"]
    assert not compare(noisy)["measurement_valid"]
    cost = [dict(row) for row in rows]
    cost[1]["client_cpu_seconds_per_op"] = 2.5
    assert not compare(cost)["signal_pass"]
    ns = {"server": "net:[1]", "client": "net:[2]", "profiler": "net:[3]"}
    validate_namespace_identities("bridge", ns)
    ns["client"] = ns["server"]
    validate_namespace_identities("loopback", ns)
    for invalid in ({"server": "", "client": "net:[2]", "profiler": "net:[3]"}, {"server": "net:[1]", "client": "net:[2]"}):
        try:
            validate_namespace_identities("bridge", invalid)
        except AssertionError:
            pass
        else:
            raise AssertionError("invalid namespace identities were accepted")
    print("Self-test passed: destination defaults, namespace modes, client constraints, variance, and CPU-transfer gates")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=pathlib.Path, default=OUT)
    ap.add_argument("--image", default="vex-perf-local:arm64")
    ap.add_argument("--client-image", default=EXPECTED_CLIENT_IMAGE)
    ap.add_argument("--sha")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
    else:
        assert args.sha and len(args.sha) == 64, "supply the expected native server binary SHA256"
        run(args.output, args.image, args.client_image, args.sha)
