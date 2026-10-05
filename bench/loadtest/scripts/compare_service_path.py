#!/usr/bin/env python3
"""Bounded Service ClusterIP versus direct Pod-IP comparison.

The namespace, client pod, Service and holder are provisioned by the parent
runner. Importing this module performs no cluster work. A run owns one fresh
server pod and exactly four timed cells in ABBA order.
"""
import argparse
import hashlib
import json
import pathlib
import statistics
import sys

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent))
import profile_saturation as p  # noqa: E402


CTX = "scrum-fresh"
NS = "vex-service-path-20260922"
OUT = ROOT / "bench" / "loadtest" / "runs" / "2026-09-22-service-path"
SERVICE_NAME = "benchmark-server"
PORT = 6379
CLIENT = "loadgen-large"
BINARY_SHA256 = "200015d348ba5b5ce642fa3dd502d8249e3bb02c229f3bfef43a260f01778f0e"
CELLS = (("A1-service", "A"), ("B1-pod", "B"), ("B2-pod", "B"), ("A2-service", "A"))


def configure(namespace=NS, output=OUT, context=CTX):
    p.configure(namespace, output, context, SERVICE_NAME, PORT)


def save(name, value):
    p.save(name, value)


def service_state(server_pod):
    service = json.loads(p.kub("get", "service", SERVICE_NAME, "-o", "json"))
    cluster_ip = service.get("spec", {}).get("clusterIP")
    assert cluster_ip and cluster_ip != "None", service
    ports = service.get("spec", {}).get("ports", [])
    port = next((x for x in ports if int(x.get("port", -1)) == PORT), None)
    assert port and int(port.get("targetPort", PORT)) == PORT, service
    slices = json.loads(p.kub(
        "get", "endpointslice", "-l", f"kubernetes.io/service-name={SERVICE_NAME}", "-o", "json"
    ))
    ready = []
    for item in slices.get("items", []):
        for endpoint in item.get("endpoints", []):
            conditions = endpoint.get("conditions", {})
            if conditions.get("ready") is not True or conditions.get("serving", True) is not True:
                continue
            target = endpoint.get("targetRef", {}) or {}
            for address in endpoint.get("addresses", []):
                ready.append({"address": address, "uid": target.get("uid")})
    pod_ip = server_pod.get("status", {}).get("podIP")
    pod_uid = server_pod["metadata"]["uid"]
    assert len(ready) == 1, ready
    assert ready[0]["address"] == pod_ip, (ready, pod_ip)
    assert ready[0]["uid"] == pod_uid, (ready, pod_uid)
    return {
        "service": service,
        "endpoint_slices": slices,
        "cluster_ip": cluster_ip,
        "pod_ip": pod_ip,
        "pod_uid": pod_uid,
        "ready_endpoint": ready[0],
    }


def verify_dns(cluster_ip):
    code = (
        "import json,socket; "
        f"a=sorted({{x[4][0] for x in socket.getaddrinfo({SERVICE_NAME!r},{PORT},socket.AF_INET,socket.SOCK_STREAM)}}); "
        "print(json.dumps(a))"
    )
    addresses = json.loads(p.execpod(CLIENT, "sampler", "python3", "-c", code))
    assert cluster_ip in addresses, (cluster_ip, addresses)
    save("service-dns.json", {"name": SERVICE_NAME, "port": PORT, "addresses": addresses, "cluster_ip": cluster_ip})
    return addresses


def assert_topology(server_pod, expected):
    current = service_state(server_pod)
    assert current["cluster_ip"] == expected["cluster_ip"]
    assert current["pod_ip"] == expected["pod_ip"]
    assert current["pod_uid"] == expected["pod_uid"]
    return current


def compare(rows):
    by_name = {row["cell"]: row for row in rows}
    invalid_reasons = []
    signal_reasons = []
    if set(by_name) != {name for name, _ in CELLS}:
        invalid_reasons.append("incomplete ABBA cells")
    for row in rows:
        if row.get("connection_errors", 0) or row.get("misses_per_sec", 0) or row.get("constrained") or row.get("client_headroom_constrained"):
            invalid_reasons.append(f"constraint or error in {row.get('cell')}")
        if row.get("server_process_identity_before") != row.get("server_process_identity_after"):
            invalid_reasons.append(f"server process changed in {row.get('cell')}")
        if row.get("key_count_before") != p.KEYS or row.get("key_count_after") != p.KEYS:
            invalid_reasons.append(f"key count changed in {row.get('cell')}")

    pair_specs = (("A1-service", "B1-pod"), ("A2-service", "B2-pod"))
    pairs = []
    winners = []
    cpu_directions = []
    for service_name, pod_name in pair_specs:
        if service_name not in by_name or pod_name not in by_name:
            continue
        a, b = by_name[service_name], by_name[pod_name]
        winner = "service" if a["ops_per_sec"] >= b["ops_per_sec"] else "pod"
        winner_row, loser_row = (a, b) if winner == "service" else (b, a)
        gap = (winner_row["ops_per_sec"] / loser_row["ops_per_sec"]) - 1.0
        cpu_direction = (a["server_cgroup_cpu_seconds_per_op"] > b["server_cgroup_cpu_seconds_per_op"]) - (a["server_cgroup_cpu_seconds_per_op"] < b["server_cgroup_cpu_seconds_per_op"])
        if cpu_direction == 0:
            signal_reasons.append(f"CPU/op did not move in {service_name}/{pod_name}")
        cpu_directions.append(cpu_direction)
        if winner_row["p99_ms"] > loser_row["p99_ms"] * 1.10:
            signal_reasons.append(f"p99 regression over 10% in {service_name}/{pod_name}")
        if winner_row["server_cgroup_cpu_seconds_per_op"] > loser_row["server_cgroup_cpu_seconds_per_op"]:
            signal_reasons.append(f"winner used more server CPU/op in {service_name}/{pod_name}")
        winners.append(winner)
        pairs.append({"service_cell": service_name, "pod_cell": pod_name, "winner": winner, "throughput_gap": gap,
                      "service_ops_per_sec": a["ops_per_sec"], "pod_ops_per_sec": b["ops_per_sec"],
                      "service_p99_ms": a["p99_ms"], "pod_p99_ms": b["p99_ms"],
                      "service_cpu_seconds_per_op": a["server_cgroup_cpu_seconds_per_op"],
                      "pod_cpu_seconds_per_op": b["server_cgroup_cpu_seconds_per_op"]})
    if len(winners) == 2 and winners[0] != winners[1]:
        signal_reasons.append("paired comparisons disagree on winner")
    if len(cpu_directions) == 2 and cpu_directions[0] != cpu_directions[1]:
        signal_reasons.append("CPU/op movement is inconsistent across pairs")
    if len(pairs) == 2 and statistics.median([x["throughput_gap"] for x in pairs]) < 0.05:
        signal_reasons.append("median throughput gap below 5%")
    identities = {row.get("server_process_identity_before") for row in rows} | {row.get("server_process_identity_after") for row in rows}
    if len(identities) != 1:
        invalid_reasons.append("server process identity changed across cells")
    server_uids = {row.get("server_pod_uid") for row in rows}
    endpoint_uids = {row.get("endpoint_target_uid") for row in rows}
    client_uids = {row.get("client_pod_uid") for row in rows}
    if len(server_uids) != 1 or len(endpoint_uids) != 1 or endpoint_uids != server_uids:
        invalid_reasons.append("server endpoint or Pod UID changed across cells")
    if len(client_uids) != 1:
        invalid_reasons.append("client Pod UID changed across cells")
    return {"pairs": pairs, "winner": winners[0] if len(winners) == 2 and winners[0] == winners[1] else None,
            "median_throughput_gap": statistics.median([x["throughput_gap"] for x in pairs]) if pairs else None,
            "measurement_valid": not invalid_reasons, "signal_pass": not invalid_reasons and not signal_reasons,
            "invalid_reasons": invalid_reasons, "signal_reasons": signal_reasons}


def run():
    binary = p.OUT / p.BINARY_NAME
    assert binary.exists(), binary
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == BINARY_SHA256
    assert not (p.OUT / "cells.json").exists(), "refusing to mix a prior comparison"
    assert not (p.OUT / "comparison.json").exists(), "refusing to mix a prior comparison"
    p.kub("wait", "--for=condition=Ready", f"pod/{CLIENT}", "--timeout=300s", timeout=320)
    p.SAMPLE_SECONDS = 60
    client = json.loads(p.kub("get", "pod", CLIENT, "-o", "json"))
    client_uid, client_node = client["metadata"]["uid"], client["spec"]["nodeName"]
    save("client-pod.json", client)
    server_pod, backend = p.start_server("comparison", binary)
    assert server_pod["spec"]["nodeName"] != client_node
    topology = service_state(server_pod)
    save("service-definition.json", topology["service"])
    save("endpoint-slices-definition.json", topology["endpoint_slices"])
    verify_dns(topology["cluster_ip"])
    save("settings.json", {"namespace": NS, "context": CTX, "binary_sha256": BINARY_SHA256,
                            "service": SERVICE_NAME, "cluster_ip": topology["cluster_ip"], "pod_ip": topology["pod_ip"],
                            "pod_uid": topology["pod_uid"], "port": PORT, "order": [x[0] for x in CELLS],
                            "pipeline": 1, "connections": 128, "threads": 32, "clients": 4,
                            "keys": p.KEYS, "key_bytes": 16, "value_bytes": p.VALUE_BYTES, "ratio": p.RATIO,
                            "warmup_seconds": 10, "sample_seconds": 60, "backend": backend})
    p.DESTINATION_HOST, p.DESTINATION_PORT = topology["cluster_ip"], PORT
    p.run_memtier("comparison-preload", 1, 32, 0, preload=True)
    assert int(p.bench.cli("DBSIZE")) == p.KEYS
    rows = []
    for name, destination in CELLS:
        current = assert_topology(server_pod, topology)
        save(f"{name}-topology.json", current)
        p.DESTINATION_HOST = topology["cluster_ip"] if destination == "A" else topology["pod_ip"]
        pre = p.verify_sampled_values(p.DESTINATION_HOST, PORT)
        save(f"{name}-pre-sampled-values.json", pre)
        row = p.run_cell((name, 1, 128), server_pod, backend, client_uid, client_node, pre)
        post = assert_topology(server_pod, topology)
        save(f"{name}-post-topology.json", post)
        row["destination_path"] = destination
        row["service_cluster_ip"] = topology["cluster_ip"]
        row["server_pod_ip"] = topology["pod_ip"]
        row["endpoint_target_uid"] = post["ready_endpoint"]["uid"]
        rows.append(row)
        save("cells.json", rows)
        print("DONE", name, f"{row['ops_per_sec']:,.0f} ops/s", f"p99={row['p99_ms']:.3f}ms", flush=True)
    result = compare(rows)
    save("comparison.json", result)
    print(json.dumps(result, indent=2), flush=True)


def self_test():
    fixture = ROOT / "bench" / "loadtest" / "runs" / "2026-09-22-saturation" / "forward-p1-c128.json"
    assert fixture.exists(), fixture
    data = json.loads(fixture.read_text())
    p.validate_raw(data, 1, 128, 30, "benchmark-server", PORT)
    broken = json.loads(fixture.read_text())
    broken["configuration"]["server"] = "10.0.0.4"
    try:
        p.validate_raw(broken, 1, 128, 30, "benchmark-server", PORT)
    except AssertionError:
        print("Self-test passed: destination is validated in raw memtier output")
    else:
        raise AssertionError("validator accepted a mismatched destination")


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
    assert not (options.output / "cells.json").exists(), "refusing to mix a prior comparison"
    assert not (options.output / "comparison.json").exists(), "refusing to mix a prior comparison"
    configure(options.namespace, options.output, options.context)
    run()


if __name__ == "__main__":
    main()
