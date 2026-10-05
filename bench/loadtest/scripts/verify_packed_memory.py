#!/usr/bin/env python3
"""Socket-level packed-entry memory verification using the lifecycle protocol.

This intentionally keeps the workload client and RESP checks from
measure_memory_lifecycle.py.  It only changes the image inventory, the four
extra map-growth points, and the small-value cells needed by this experiment.
"""
import argparse
import json
import pathlib
import tempfile
import time

import measure_memory_lifecycle as base

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[3]
INVENTORY = ROOT / "bench/loadtest/runs/2026-09-25-packed-growth/ci-engines.json"
OUT = ROOT / "bench/loadtest/runs/2026-09-25-packed-growth/socket-memory"
ENGINES = ("control", "candidate", "redis", "dragonfly")
POINTS = (200_000, 400_000, 600_000, 750_000, 800_000, 825_000, 850_000,
          900_000, 1_000_000, 1_200_000, 1_225_000, 1_250_000, 1_275_000,
          1_300_000, 1_500_000, 1_600_000)


def engines():
    data = json.loads(INVENTORY.read_text())
    assert set(data) == set(ENGINES), (set(data), ENGINES)
    for name, row in data.items():
        assert all(row.get(key) for key in ("image", "command", "process_name", "binary_sha256")), (name, row)
    for name in ("control", "candidate"):
        assert "@sha256:" in data[name]["image"], (name, data[name]["image"])
    return data


def sample(names, engine, hashed=False):
    row = json.loads(base.sh([
        "exec", names["sampler"], "python3", "-c", "import sys;" + base.SAMPLER,
        engines()[engine]["process_name"], "hash" if hashed else "light",
    ]))
    if hashed:
        assert row["exe_sha256"] == engines()[engine]["binary_sha256"], row
    return row


def stop(names):
    for key in ("sampler", "client", "server", "net"):
        base.sh(["rm", "-f", names[key]] if key != "net" else ["network", "rm", names[key]], check=False)


def start(engine, tag):
    """Base start protocol with its pinned Python helper image as both sidecars."""
    names = {key: f"packed-memory-{tag}-{key}" for key in ("net", "server", "client", "sampler")}
    for name in names.values():
        base.absent(name)
    try:
        config, sidecar = engines()[engine], base.VEX_SAMPLER_IMAGE
        assert base.image_arch(config["image"]) == base.image_arch(sidecar) == "arm64"
        base.sh(["network", "create", names["net"]])
        command = config["command"]
        base.sh([
            "run", "-d", "--name", names["server"], "--network", names["net"], "--network-alias", "server",
            "--cpus", "6", "--memory", "4g", "--memory-swap", "4g", "--security-opt", "seccomp=unconfined",
            "--entrypoint", command[0], config["image"], *command[1:],
        ])
        base.sh(["run", "-d", "--name", names["client"], "--network", names["net"], "--entrypoint", "sleep", sidecar, "3600"])
        # No shared volume: SYS_PTRACE lets the helper inspect a non-root server PID namespace.
        base.sh(["run", "-d", "--name", names["sampler"], "--pid", f"container:{names['server']}", "--cap-add", "SYS_PTRACE", "--entrypoint", "sleep", sidecar, "3600"])
        for _ in range(60):
            try:
                base.sh(["exec", names["client"], "python3", "-c", "import socket;s=socket.create_connection(('server',6379),1);s.close()"], timeout=5)
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("server did not become ready")
        return names, sample(names, engine, True)
    except Exception:
        stop(names)
        raise


def install_base_adapters():
    """The reused workload calls these module globals, so bind them once."""
    base.engines = engines
    base.sample = sample
    base.POINTS = POINTS


def run_standard(engine, case):
    out = OUT / engine / case
    assert not out.exists(), f"refusing duplicate run: {out}"
    out.mkdir(parents=True)
    names = None
    try:
        base.write(out, "engine.json", engines()[engine])
        base.write(out, "method.json", {
            "memory_only": True, "protocol": "one persistent RESP connection", "batch_size": base.BATCH,
            "key_prefix": base.PREFIX, "default_value_bytes": 256, "idle_after_growth_seconds": 2,
            "no_host_ports": True, "sampler": "pinned Python helper image, server PID namespace, SYS_PTRACE, no shared volume",
        })
        names, initial = start(engine, f"{engine}-{case}-20260925")
        base.write(out, "initial-process.json", initial)
        startup = base.sh(["logs", names["server"]])
        base.write(out, "startup.log", startup)
        if engine in ("control", "candidate"):
            assert "io_uring backend active" in startup, startup
        proc = base.driver(names)
        {"grow": base.grow, "overwrite": base.overwrite, "ttl": base.ttl}[case](out, names, engine, proc)
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
        final = sample(names, engine, True)
        assert {key: final[key] for key in ("pid", "start", "exe_sha256")} == {key: initial[key] for key in ("pid", "start", "exe_sha256")}
        base.write(out, "final-process.json", final)
    except Exception as exc:
        if not (out / "failure.json").exists():
            base.write(out, "failure.json", {"engine": engine, "case": case, "reason": str(exc)})
        raise
    finally:
        if names:
            stop(names)


def run_small(engine):
    out = OUT / engine / "small"
    assert not out.exists(), f"refusing duplicate run: {out}"
    out.mkdir(parents=True)
    base.write(out, "engine.json", engines()[engine])
    base.write(out, "method.json", {"fresh_server_per_value_size": True, "value_sizes": [25, 32], "points": [800_000, 1_000_000], "sampled_value_checks": True, "no_host_ports": True, "sampler": "pinned Python helper image, server PID namespace, SYS_PTRACE, no shared volume"})
    for size in (25, 32):
        cell = out / f"{size}B"
        cell.mkdir()
        names = None
        try:
            names, initial = start(engine, f"{engine}-small-{size}b-20260925")
            base.write(cell, "initial-process.json", initial)
            startup = base.sh(["logs", names["server"]])
            base.write(cell, "startup.log", startup)
            if engine in ("control", "candidate"):
                assert "io_uring backend active" in startup, startup
            proc = base.driver(names)
            current = 0
            for point in (800_000, 1_000_000):
                base.ask(proc, {"op": "set", "from": current, "to": point, "size": size})
                current = point
                row = base.snapshot(cell, f"grow-{point}", names, engine, proc)
                assert row["info"]["dbsize"] == point
                base.spotcheck(cell, f"grow-{point}", proc, [0, point // 2, point - 1], size)
            proc.stdin.close()
            assert proc.wait(timeout=30) == 0
            final = sample(names, engine, True)
            assert {key: final[key] for key in ("pid", "start", "exe_sha256")} == {key: initial[key] for key in ("pid", "start", "exe_sha256")}
            base.write(cell, "final-process.json", final)
        except Exception as exc:
            if not (cell / "failure.json").exists():
                base.write(cell, "failure.json", {"engine": engine, "case": "small", "size": size, "reason": str(exc)})
            raise
        finally:
            if names:
                stop(names)


def self_test():
    install_base_adapters()
    compile(base.DRIVER, "driver", "exec")
    compile("import sys;" + base.SAMPLER, "sampler", "exec")
    assert POINTS == tuple(sorted(POINTS)) and POINTS[9:14] == (1_200_000, 1_225_000, 1_250_000, 1_275_000, 1_300_000)
    assert all(len(base.PREFIX + str(base.FIRST + n)) == 16 for n in (0, 999_999))
    source = HERE.read_text()
    assert '"--cap-add", "SYS_PTRACE"' in source and '"--network-alias", "server"' in source
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory)
        base.write(path, "raw.json", {"ok": True})
        assert json.loads((path / "raw.json").read_text())["ok"]
    print("packed memory socket runner self-test passed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=[*ENGINES, "all"], default="all")
    parser.add_argument("--case", choices=["grow", "overwrite", "ttl", "small", "all"], default="all")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    install_base_adapters()
    for engine in (ENGINES if args.engine == "all" else (args.engine,)):
        for case in (("grow", "overwrite", "ttl", "small") if args.case == "all" else (args.case,)):
            run_small(engine) if case == "small" else run_standard(engine, case)


if __name__ == "__main__":
    main()
