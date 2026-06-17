#!/usr/bin/env python3
"""Turn dragonfly-results.csv (cores,server,cmd,pipeline,rps) into a scaling
comparison: vex vs dragonfly at each core count, plus ops/s-per-core
(scaling efficiency), and a crossover read."""
import csv, sys, collections

PATH = sys.argv[1] if len(sys.argv) > 1 else "bench-logs/dragonfly-results.csv"
rows = []
# Positional parse (col5 = throughput, named rps or ops depending on tool);
# tolerates a header line and any ERR rows.
with open(PATH) as f:
    for line in f:
        parts = line.strip().split(",")
        if len(parts) < 5:
            continue
        try:
            rows.append((int(parts[0]), parts[1], parts[2].lower(), int(parts[3]), float(parts[4])))
        except ValueError:
            continue  # header row or ERR cell

# index: (cmd, pipeline, cores, server) -> rps
idx = {}
cores_set, servers = set(), set()
for c, s, cmd, p, rps in rows:
    idx[(cmd, p, c, s)] = rps
    if s in ("vex", "dragonfly"):
        cores_set.add(c)
    servers.add(s)
cores = sorted(cores_set)

def grid(cmd, p, label):
    print(f"\n## {label}  (SET={cmd=='set'}, P={p})\n")
    print("| cores | vex rps | dragonfly rps | vex Δ | vex/core | df/core |")
    print("|---|---|---|---|---|---|")
    for c in cores:
        v = idx.get((cmd, p, c, "vex"))
        d = idx.get((cmd, p, c, "dragonfly"))
        if v is None or d is None:
            print(f"| {c} | {v or '·'} | {d or '·'} | · | · | · |")
            continue
        delta = 100 * (v - d) / d
        print(f"| {c} | {v:,.0f} | {d:,.0f} | {delta:+.0f}% | {v/c:,.0f} | {d/c:,.0f} |")
    # redis baseline (single thread)
    rb = idx.get((cmd, p, 1, "redis"))
    if rb:
        print(f"\nredis (1 thread) baseline: {rb:,.0f} rps")

for cmd in ("set", "get"):
    for p in (1, 50):
        lbl = f"{cmd.upper()} {'pipelined -P50' if p==50 else 'unpipelined'}"
        grid(cmd, p, lbl)

# crossover read on SET unpipelined + pipelined
print("\n## scaling read")
for p in (1, 50):
    vs = [(c, idx.get(("set", p, c, "vex")), idx.get(("set", p, c, "dragonfly"))) for c in cores]
    vs = [(c, v, d) for c, v, d in vs if v and d]
    if not vs:
        continue
    lead_lo = "vex" if vs[0][1] > vs[0][2] else "dragonfly"
    lead_hi = "vex" if vs[-1][1] > vs[-1][2] else "dragonfly"
    cross = next((c for c, v, d in vs if (v > d) != (vs[0][1] > vs[0][2])), None)
    tag = "pipelined" if p == 50 else "unpipelined"
    print(f"- SET {tag}: at {vs[0][0]} cores {lead_lo} leads; at {vs[-1][0]} cores {lead_hi} leads"
          + (f"; crossover near {cross} cores" if cross else "; no crossover in range"))
