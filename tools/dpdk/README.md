# tools/dpdk/

Build + run scaffolding for the DPDK kernel-bypass work tracked in
[docs/dpdk.md](../../docs/dpdk.md). Nothing here ships with the
production `vex` binary — it's all out-of-band tooling for the perf
branch.

## What's here

| File | What it does |
|---|---|
| `Dockerfile.dpdk` | Build image with DPDK 22.11 + Zig nightly installed; today produces only the `hello_lcore` probe. |
| `hello_lcore.zig` | Small Zig program that uses `@cImport` to call into DPDK, initializes the EAL, and prints per-lcore info. Same path the eventual `src/server/net/dpdk.zig` will take — keeps the codebase single-language. |

Future commits add:

- `aws-perf-up.sh` — `aws ec2 run-instances` for `c5n.18xlarge` with
  the right AMI + user-data (hugepages, ENA → vfio-pci, DPDK install).
- F-Stack install on top of DPDK.
- Zig toolchain (mirroring `Dockerfile.vex`) and a `vex-dpdk` build
  step that links against both.

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

## Why no execution test on macOS

Docker Desktop's Linux VM doesn't expose hugepages by default, and the
emulated network path between the VM and the container makes the
DPDK PMD numbers meaningless even with `--no-pci`. The probe builds
on macOS via buildx (we routinely do this when developing on Apple
Silicon) but real validation happens on a Linux box. See
[docs/dpdk.md §Test plan](../../docs/dpdk.md) for the AWS path.
