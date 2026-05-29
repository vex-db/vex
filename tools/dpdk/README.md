# tools/dpdk/

Build + run scaffolding for the DPDK kernel-bypass work tracked in
[docs/dpdk.md](../../docs/dpdk.md). Nothing here ships with the
production `vex` binary — it's all out-of-band tooling for the perf
branch.

## What's here

| File | What it does |
|---|---|
| `Dockerfile.dpdk` | Build image with DPDK 22.11 + Zig nightly installed. Two stages: Zig compiles each probe to a `.o` via `build-obj`; system gcc links against the DPDK library line (dodges Zig's bundled lld not finding `/usr/lib/x86_64-linux-gnu/libmd`). |
| `hello_lcore.zig` | EAL init + per-lcore hello. Smallest probe — confirms the toolchain works end-to-end. |
| `port_probe.zig` | Adds mbuf-pool create, port configure / rx_queue_setup / start, and an `rte_eth_rx_burst` polling loop with packets/sec stats. Works against the software null PMD on any Linux box; ready for a real ENA NIC once the c5n perf box has the secondary ENI bound to vfio-pci. |
| `dpdk_shim.c` | Thin C shims for DPDK symbols that live as `static inline` in headers and therefore aren't exported from `librte_*.so` (rte_lcore_id, rte_get_tsc_cycles, rte_errno, rte_eth_rx_burst, rte_pktmbuf_free, rte_pktmbuf_len, plus a port-setup convenience wrapper). |
| `aws-perf-up.sh` | One-command bring-up of a `c5n.18xlarge` (override via env: `INSTANCE_TYPE`, `AWS_REGION`, `KEY_NAME`, `SECURITY_GROUP_ID`, `SUBNET_ID`). User-data installs DPDK + reserves hugepages. Prints SSH instructions + the `aws-perf-down` command. |
| `aws-perf-down.sh` | Terminate the perf instance. Pairs with the up-script. |

Future commits add:

- F-Stack install on top of DPDK (userspace TCP stack — see
  [docs/dpdk.md](../../docs/dpdk.md)).
- ENA → vfio-pci binding script that runs on the c5n perf box once
  it has a secondary ENI attached.
- A `vex-dpdk` Zig build wiring (`-Ddpdk=true` build option in
  `build.zig`) that links F-Stack + DPDK + the vex command path.

## Build the hello-lcore image

From the repo root:

```bash
docker buildx build \
    --platform=linux/amd64 \
    -f tools/dpdk/Dockerfile.dpdk \
    -t vex-dpdk-probe:hello \
    --load .
```

Cross-platform from macOS works because the C source is portable; the
resulting binary is Linux/x86_64 and requires a Linux host to execute.

## Run

The default `CMD` uses `-l 0-3 --no-pci --no-huge`, which means:

- `-l 0-3` — use lcores 0 through 3 (no NUMA pinning yet)
- `--no-pci` — skip the PCI device scan (no NIC required for the probe)
- `--no-huge` — use anonymous memory instead of hugepages (lets the
  binary run on any Linux box without sysctl tuning)

```bash
docker run --rm vex-dpdk-probe:hello
```

Expected output (lcore order may vary):

```
EAL: Detected CPU lcores: 4
EAL: Detected NUMA nodes: 1
...
DPDK 22.11.5
Main lcore: 0
Total lcores: 4
NUMA sockets: 1
[lcore 1] running on NUMA socket 0, tsc=...
[lcore 2] running on NUMA socket 0, tsc=...
[lcore 3] running on NUMA socket 0, tsc=...
[lcore 0] running on NUMA socket 0, tsc=...
```

## Run with hugepages (closer to production)

A real perf run needs hugepages allocated on the host and mounted into
the container. On the AWS `c5n.18xlarge` perf target this is:

```bash
# Once per boot, on the host:
sudo mkdir -p /dev/hugepages
sudo mount -t hugetlbfs nodev /dev/hugepages
echo 1024 | sudo tee /sys/kernel/mm/hugepages/hugepages-2048kB/nr_hugepages

# Run the probe with hugepages:
docker run --rm --privileged \
    -v /dev/hugepages:/dev/hugepages \
    vex-dpdk-probe:hello \
    /usr/local/bin/hello_lcore -l 0-3 --no-pci
```

(`--no-pci` stays until we add an NIC port and an mbuf pool.)

## Run the port_probe (mempool + rx_burst loop, no NIC required)

The port_probe walks the full single-port single-queue setup chain
and runs `rte_eth_rx_burst` for a few seconds. With the **null PMD**
it doesn't actually receive anything from the wire — but it does
prove the pool/ethdev/poll plumbing works, which is what we need
before plugging in a real NIC:

```bash
docker run --rm vex-dpdk-probe:hello \
    /usr/local/bin/port_probe \
    -l 0-1 --no-pci --no-huge \
    --vdev=net_null0 -- --duration=2
```

`--` separates EAL flags from app flags; only `--duration=N` and
`--burst=N` are recognized today. On Docker Desktop / Apple Silicon
emulation we see ~104M packets/sec through the null PMD — that's the
software cost ceiling, not a real network number.

## Bring up an AWS perf box

```bash
# One-time: have an SSH key + a security group that allows your IP
# inbound on port 22.
export KEY_NAME=vex-dpdk
export SECURITY_GROUP_ID=sg-xxxxxxxx
export AWS_REGION=ap-south-1

# Launch:
./tools/dpdk/aws-perf-up.sh
# Prints: instance_id, public_ip, the ssh command, and the tear-down command.

# When done:
INSTANCE_ID=i-xxxxxxxxxxxxxxxxx ./tools/dpdk/aws-perf-down.sh
```

Defaults to `c5n.18xlarge` (~$3.88/hr on-demand; ~$1.50/hr spot). Set
`INSTANCE_TYPE=c5n.metal` to escalate to true bare-metal if a
Nitro-virtualization edge bites us.

The script's user-data installs DPDK + libdpdk-dev + libmd-dev,
reserves 1024 × 2MB hugepages on NUMA node 0, and mounts hugetlbfs at
`/mnt/huge`. What it does **NOT** do: bind a secondary ENA NIC to
vfio-pci. That's a manual step (it varies enough by distro/kernel
that auto-binding inside user-data is fragile); we'll add a guided
script in the next commit on this branch.

## Why no execution test on macOS

Docker Desktop's Linux VM doesn't expose hugepages by default, and
the emulated path makes the DPDK PMD numbers meaningless even with
`--no-pci`. Both probes build cleanly on macOS via buildx, and the
null-PMD `port_probe` runs fine inside the container, but real
validation happens on the AWS c5n box. See
[docs/dpdk.md §Test plan](../../docs/dpdk.md) for the full plan.
