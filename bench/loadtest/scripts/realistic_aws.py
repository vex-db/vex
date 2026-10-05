#!/usr/bin/env python3
"""Run realistic profiles on dedicated nodes cloned from a saved AWS benchmark.

Owns only the new namespace/NodePools. Always requests cleanup, including on failure.
Existing fixed-workload measurement validators are not used for TTL/miss workloads.
"""
import argparse
import json
import pathlib
import subprocess
import time

import compare_aws_engines as shared

HERE = pathlib.Path(__file__).resolve().parent


def save(path, value):
    shared.write(path, value)


def engine_order(names, round_no, profile_index):
    offset = (round_no - 1 + profile_index) % len(names)
    return names[offset:] + names[:offset]


def upload(name):
    remote = "/opt/vex/bench/loadtest/scripts/" + name
    code = "import pathlib,sys;p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(sys.stdin.read())"
    subprocess.run(shared.K + ["exec", "-i", "client", "-c", "memtier", "--", "python3", "-c", code, remote],
                   input=(HERE / name).read_text(), text=True, check=True, timeout=60)


def client_snapshot():
    code = ("import json,pathlib,time;P=pathlib.Path;print(json.dumps({"
            "'monotonic':time.monotonic(),'boot':P('/proc/sys/kernel/random/boot_id').read_text().strip(),"
            "'node':{'vmstat':P('/proc/vmstat').read_text()}}))")
    point = json.loads(shared.ex("client", "sampler", "python3", "-c", code, read_only=True))
    point["cgroup"] = shared.primary_cgroup("client", "memtier")
    return point


def start_server(o, engine, cell):
    shared.kub("delete", "pod", "server", "--ignore-not-found", "--wait=true", "--timeout=60s")
    shared.kub("create", "-f", str(o.output / f"server-{engine}.manifest.json"))
    shared.kub("wait", "--for=condition=Ready", "pod/server", "--timeout=180s", timeout=190)
    nodes = json.loads(shared.kub("get", "nodes", "-l", f"bench.vex.dev/run={o.namespace}", "-o", "json"))
    initial_nodes = json.loads((o.output / "nodes-active.json").read_text())
    assert {n["metadata"]["uid"] for n in nodes["items"]} == {n["metadata"]["uid"] for n in initial_nodes["items"]}, "benchmark nodes changed between cells"
    server, client = shared.validate_pods(o.engines[engine], nodes)
    save(cell / "server-pod.json", server)
    save(cell / "client-pod.json", client)
    save(cell / "nodes.json", nodes)
    identity = shared.snapshot_server(o.engines[engine]["process_name"])
    assert identity["exe_sha256"] == o.engines[engine]["binary_sha256"]
    topology = shared.live_topology(server, client, identity)
    save(cell / "topology-before.json", topology)
    save(cell / "identity-before.json", identity)
    assert shared.memtier_hash() == shared.MEMTIER_SHA256
    return server["status"]["podIP"], topology


def run_cell(o, engine, profile, round_no):
    label = f"r{round_no}-{profile}-{engine}"
    cell = o.output / "cells" / label
    cell.mkdir(parents=True)
    print("START", label, flush=True)
    host, topology = start_server(o, engine, cell)
    backend = "vex" if engine in ("control", "candidate") else engine
    shared.postload_checks(backend, host, cell, "engine-checks-before.json")
    client_before = client_snapshot()
    save(cell / "client-before.json", client_before)
    sampler = shared.SNAPSHOT + "\n" + (
        f"pid=locate({o.engines[engine]['process_name']!r}).name\n"
        "while not P('/results/realistic-stop').exists():\n"
        " print(json.dumps({'monotonic':time.monotonic(),'process':light(pid)}),flush=True);time.sleep(.5)\n")
    with (cell / "server-process.jsonl").open("w") as sample_log:
        proc = subprocess.Popen(shared.K + ["exec", "server", "-c", "sampler", "--", "python3", "-c", sampler],
                                stdout=sample_log, stderr=subprocess.PIPE, text=True)
        try:
            if profile == "locust":
                remote = f"/results/{label}"
                shared.ex("client", "memtier", "mkdir", remote)
                env = [f"REDIS_URL=redis://{host}:6379", f"CACHE_PREFIX=vex-realistic:{o.namespace}:{label}:",
                       "CACHE_KEYS=10000", f"CACHE_STAGE_SECONDS={o.seconds}", "CACHE_USERS=32", "CACHE_BACKEND_MS=5"]
                command = ["env", *env, "locust", "-f", "/opt/vex/bench/loadtest/scripts/cache_locust.py",
                           "--headless", "--only-summary", "--csv", remote + "/cache", "--csv-full-history", "--stop-timeout", "5"]
                timeout = o.seconds * 3 + 120
            else:
                seconds = o.soak_seconds if profile == "soak" else o.seconds
                command = ["python3", "/opt/vex/bench/loadtest/scripts/realistic_workloads.py",
                           "--host", host, "--profile", profile, "--output", f"/results/{label}",
                           "--keys", str(o.keys), "--rate", str(o.rate), "--seconds", str(seconds)]
                timeout = seconds * (3 if profile == "burst" else 1) + 1200
            save(cell / "command.json", command)
            with (cell / "client.log").open("w") as log:
                subprocess.run(shared.K + ["exec", "client", "-c", "memtier", "--", *command],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)
        finally:
            shared.ex("server", "engine", "touch", "/results/realistic-stop")
            _, err = proc.communicate(timeout=30)
            save(cell / "sampler.stderr", err)
            # Export partial output on errors too; it must never become a completed cell.
            result = shared.run(shared.K + ["cp", "-c", "memtier", f"client:/results/{label}", str(cell / "raw")], timeout=120, check=False)
            save(cell / "export.log", result.stdout + result.stderr)
    assert proc.returncode == 0 and result.returncode == 0
    final, identity = shared.assert_final_topology(topology, o.engines[engine])
    save(cell / "topology-after.json", final)
    save(cell / "identity-after.json", identity)
    before = json.loads((cell / "identity-before.json").read_text())
    shared.assert_memory(before, identity)
    client_after = client_snapshot()
    save(cell / "client-after.json", client_after)
    shared.assert_memory(client_before, client_after)
    assert client_before["boot"] == client_after["boot"]
    assert shared.memtier_hash() == shared.MEMTIER_SHA256
    shared.postload_checks(backend, host, cell, "engine-checks-after.json")
    samples = [json.loads(line) for line in (cell / "server-process.jsonl").read_text().splitlines()]
    assert len(samples) >= o.seconds
    row = {"label": label, "engine": engine, "profile": profile, "round": round_no,
           "peak_process_rss_mib": max(x["process"]["rss_bytes"] for x in samples) / 2**20,
           "rss_scope": "entire profile including preload/warmup; not phase-specific",
           "server_cpu_seconds": samples[-1]["process"]["cpu_seconds"] - samples[0]["process"]["cpu_seconds"]}
    if profile != "locust":
        row["phases"] = json.loads((cell / "raw" / "summary.json").read_text())
        row["client_headroom_ok"] = all(
            max(t["cores_used"] for t in phase["client_cpu"]["Per Thread"].values()) < .9
            and phase["client_cpu"]["cpu_cores_used"] < 24 * .8
            for phase in row["phases"])
    save(cell / "complete.json", row)
    print("COMPLETE", label, flush=True)
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-run", type=pathlib.Path, required=True)
    p.add_argument("--output", type=pathlib.Path, required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--image-overrides", type=pathlib.Path,
                   help="JSON mapping engine to {original_image, image}; e.g. a private ECR mirror")
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--soak-seconds", type=int, default=1800)
    p.add_argument("--keys", type=int, default=1_000_000)
    p.add_argument("--rate", type=int, default=128000)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--engines", dest="engine_names", nargs="+",
                   choices=("control", "candidate", "redis", "dragonfly"), default=["control", "candidate"])
    p.add_argument("--profiles", nargs="+", choices=("baseline", "hot", "mixed", "churn", "burst", "soak", "locust"),
                   default=["baseline", "hot", "mixed", "churn", "burst", "soak", "locust"])
    o = p.parse_args()
    if not o.namespace.startswith("vex-realistic-") or min(o.seconds, o.soak_seconds, o.keys, o.rate, o.rounds) <= 0:
        p.error("use a new vex-realistic-* namespace and positive counts/durations/rate")
    if o.rate % 128:
        p.error("rate must be divisible by 128 connections")
    if len(set(o.engine_names)) != len(o.engine_names):
        p.error("engine names must be unique")
    o.output = o.output.resolve()
    o.output.mkdir(parents=True, exist_ok=False)
    shared.BASE, shared.NS = o.output, o.namespace
    shared.K = ["kubectl", "--context", shared.CTX, "-n", o.namespace]
    old = json.loads((o.source_run / "resources.json").read_text())["items"][0]["metadata"]["name"]
    old_label = json.loads((o.source_run / "resources.json").read_text())["items"][0]["metadata"]["labels"]["bench.vex.dev/run"]
    o.engines = {k: v for k, v in json.loads((o.source_run / "engines.json").read_text()).items() if k in o.engine_names}
    assert set(o.engines) == set(o.engine_names)
    overrides = json.loads(o.image_overrides.read_text()) if o.image_overrides else {}
    for engine, override in overrides.items():
        if engine not in o.engines:
            continue
        assert override["original_image"] == o.engines[engine]["image"]
        assert "@sha256:" in override["image"]
        o.engines[engine]["original_image"] = override["original_image"]
        o.engines[engine]["image"] = override["image"]
    shared.engine_inventory = lambda: o.engines
    save(o.output / "engines.json", o.engines)
    save(o.output / "protocol.json", {**vars(o), "output": str(o.output), "source_run": str(o.source_run),
         "image_overrides": str(o.image_overrides) if o.image_overrides else None,
         "classification": "pilot" if o.rounds == 1 else "repeated comparison", "started_unix": time.time()})
    for name in ["resources.json", "client.manifest.json", "holder.manifest.json", *[f"server-{engine}.manifest.json" for engine in o.engine_names]]:
        data = (o.source_run / name).read_text().replace(old, o.namespace).replace(old_label, o.namespace)
        if name.startswith("server-"):
            engine = name.removeprefix("server-").removesuffix(".manifest.json")
            if engine in overrides:
                manifest = json.loads(data)
                manifest["spec"]["containers"][0]["image"] = overrides[engine]["image"]
                manifest["spec"].pop("imagePullSecrets", None)
                data = json.dumps(manifest, indent=2)
            elif json.loads(data)["spec"].get("imagePullSecrets"):
                raise ValueError("saved manifest requires a namespace pull secret; provide accessible image overrides before provisioning")
        save(o.output / name, data)
    for kind, name in (("namespace", o.namespace), ("nodepool", o.namespace + "-server"), ("nodepool", o.namespace + "-client")):
        assert not shared.kub("get", kind, name, "--ignore-not-found", "-o", "name").strip(), f"resource exists: {name}"
    try:
        shared.kub("create", "-f", str(o.output / "resources.json"))
        shared.kub("create", "-f", str(o.output / "holder.manifest.json"))
        shared.kub("create", "-f", str(o.output / "client.manifest.json"))
        print("Waiting for dedicated AWS nodes", flush=True)
        shared.kub("wait", "--for=condition=Ready", "pod/client", "pod/server-holder", "--timeout=600s", timeout=610)
        save(o.output / "nodes-active.json", json.loads(shared.kub("get", "nodes", "-l", f"bench.vex.dev/run={o.namespace}", "-o", "json")))
        for name in ("realistic_workloads.py", "profile_local_docker.py", "cache_locust.py"):
            upload(name)
            save(o.output / "source" / name, (HERE / name).read_text())
        for name in ("realistic_aws.py", "compare_aws_engines.py", "profile_aws_request_path.py", "profile_saturation.py"):
            save(o.output / "source" / name, (HERE / name).read_text())
        if "locust" in o.profiles:
            print("Installing pinned Locust dependencies on the temporary client", flush=True)
            setup = shared.ex("client", "memtier", "sh", "-ec", "apt-get update -qq && apt-get install -y -qq python3-pip && python3 -m pip install locust==2.43.3 redis==6.4.0", timeout=600)
            save(o.output / "client-setup.log", setup)
            save(o.output / "python-freeze.txt", shared.ex("client", "memtier", "python3", "-m", "pip", "freeze"))
        rows = []
        for round_no in range(1, o.rounds + 1):
            for index, profile in enumerate(o.profiles):
                for engine in engine_order(o.engine_names, round_no, index):
                    rows.append(run_cell(o, engine, profile, round_no))
        save(o.output / "summary.json", rows)
    finally:
        result = shared.run(shared.K + ["delete", "-f", str(o.output / "resources.json"), "--ignore-not-found", "--wait=false"], check=False)
        save(o.output / "cleanup-request.log", result.stdout + result.stderr)
        print("Cleanup requested; verify EC2 termination separately", flush=True)


if __name__ == "__main__":
    main()
