//! port_probe — second-step DPDK toolchain probe.
//!
//! Where hello_lcore only initializes the EAL, this one walks the
//! full single-port single-queue setup that vex's eventual DPDK
//! driver will rely on:
//!
//!   1. rte_eal_init                          (CLI passes EAL args)
//!   2. rte_pktmbuf_pool_create                (one mbuf pool)
//!   3. rte_eth_dev_configure                  (port 0, 1 RX queue)
//!   4. rte_eth_rx_queue_setup
//!   5. rte_eth_dev_start
//!   6. rx_burst poll loop for DURATION seconds
//!   7. tear everything down
//!
//! Run on **a host with no NIC** by using the software null PMD:
//!
//!   docker run --rm vex-dpdk-probe:port \
//!     /usr/local/bin/port_probe \
//!     -l 0-1 --no-pci --no-huge --vdev=net_null0 -- --duration=2
//!
//! Run on a **c5n perf box** (bound ENA on PCI):
//!
//!   ./port_probe -l 0-3 -a <ENA-PCI-BDF> -- --duration=10
//!
//! Stats printed at the end: packets, bytes, packets/sec.

const std = @import("std");

// Opaque pointers to DPDK C structs. Each `opaque {}` is a distinct
// type in Zig, so we declare them once at module scope and reuse the
// same type everywhere it appears in a signature — otherwise the
// pool we hand to vex_dpdk_port_setup would be a different type than
// the one rte_pktmbuf_pool_create produced.
const Mempool = opaque {};
const Mbuf = opaque {};

const dpdk = struct {
    // EAL lifecycle.
    extern "c" fn rte_eal_init(argc: c_int, argv: [*c][*c]u8) c_int;
    extern "c" fn rte_eal_cleanup() c_int;

    // Topology.
    extern "c" fn rte_socket_count() c_uint;

    // Ports.
    extern "c" fn rte_eth_dev_count_avail() u16;
    extern "c" fn rte_eth_dev_stop(port_id: u16) c_int;
    extern "c" fn rte_eth_dev_close(port_id: u16) c_int;

    // mbuf pool. Opaque to Zig — only the shim creates / destroys it.
    extern "c" fn rte_pktmbuf_pool_create(
        name: [*c]const u8,
        n: c_uint,
        cache_size: c_uint,
        priv_size: u16,
        data_room_size: u16,
        socket_id: c_int,
    ) ?*Mempool;
    extern "c" fn rte_mempool_free(mp: ?*Mempool) void;

    // Shim entry points (see dpdk_shim.c).
    extern "c" fn vex_dpdk_get_errno() c_int;
    extern "c" fn vex_dpdk_get_tsc_cycles() u64;
    extern "c" fn vex_dpdk_port_setup(
        port_id: u16,
        rx_ring_size: u16,
        pool: ?*Mempool,
    ) c_int;
    extern "c" fn vex_dpdk_rx_burst(
        port_id: u16,
        queue_id: u16,
        pkts: [*c]?*Mbuf,
        nb_pkts: u16,
    ) u16;
    extern "c" fn vex_dpdk_pktmbuf_free(m: ?*Mbuf) void;
    extern "c" fn vex_dpdk_pktmbuf_len(m: ?*Mbuf) u32;

    // Diagnostics.
    extern "c" fn rte_strerror(errnum: c_int) [*c]const u8;
    extern "c" fn rte_version() [*c]const u8;
};

// libc.
extern "c" fn printf(fmt: [*c]const u8, ...) c_int;
extern "c" fn fprintf(stream: ?*anyopaque, fmt: [*c]const u8, ...) c_int;
extern "c" var stderr: ?*anyopaque;

// Tunables. App-side flags (everything after `--` in argv) parse into
// these. EAL flags (everything before `--`) are consumed by
// rte_eal_init and don't reach us.
const Cfg = struct {
    duration_secs: u32 = 5,
    burst: u16 = 32,
};

fn parseAppArgs(args: []const [*:0]const u8) Cfg {
    var cfg = Cfg{};
    var i: usize = 0;
    while (i < args.len) : (i += 1) {
        const arg = std.mem.span(args[i]);
        if (std.mem.startsWith(u8, arg, "--duration=")) {
            const v = arg["--duration=".len..];
            cfg.duration_secs = std.fmt.parseInt(u32, v, 10) catch cfg.duration_secs;
        } else if (std.mem.startsWith(u8, arg, "--burst=")) {
            const v = arg["--burst=".len..];
            cfg.burst = std.fmt.parseInt(u16, v, 10) catch cfg.burst;
        }
    }
    return cfg;
}

pub fn main(init: std.process.Init.Minimal) u8 {
    // The OS argv goes wholesale to rte_eal_init. DPDK swallows its
    // own flags and leaves everything after `--` to the application.
    // We pass the WHOLE argv to EAL — it walks until it sees `--` (or
    // an unrecognized flag) and returns the offset, which we use to
    // find our slice.
    const args_vec = init.args.vector;
    const argc: c_int = @intCast(args_vec.len);
    const argv: [*c][*c]u8 = @ptrCast(@constCast(args_vec.ptr));

    const eal_consumed = dpdk.rte_eal_init(argc, argv);
    if (eal_consumed < 0) {
        const errno = dpdk.vex_dpdk_get_errno();
        _ = fprintf(stderr, "rte_eal_init failed: %s\n", dpdk.rte_strerror(errno));
        return 1;
    }

    // Slice out the app-side args.
    const consumed: usize = @intCast(eal_consumed);
    const app_args: []const [*:0]const u8 = if (consumed < args_vec.len)
        args_vec[consumed..]
    else
        &[_][*:0]const u8{};
    const cfg = parseAppArgs(app_args);

    _ = printf("DPDK %s\n", dpdk.rte_version());

    // ── Port discovery ───────────────────────────────────────────
    const n_ports = dpdk.rte_eth_dev_count_avail();
    _ = printf("Available ports: %u\n", @as(c_uint, n_ports));
    if (n_ports == 0) {
        _ = fprintf(stderr,
            "no ports available — pass `--vdev=net_null0` to use the software null PMD,\n" ++
            "or `-a <PCI-BDF>` after binding an ENA to vfio-pci on a c5n perf box.\n");
        _ = dpdk.rte_eal_cleanup();
        return 1;
    }

    // ── Mempool ──────────────────────────────────────────────────
    // 8192 mbufs is more than 1024 (the RX ring size) by enough to
    // absorb a couple of burst overshoot iterations. 256-mbuf cache
    // is the DPDK-recommended default.
    const pool = dpdk.rte_pktmbuf_pool_create(
        "vex_port_pool",
        8192,
        256,
        0,
        2048 + 128, // RTE_MBUF_DEFAULT_BUF_SIZE
        -1, // SOCKET_ID_ANY
    ) orelse {
        _ = fprintf(stderr, "rte_pktmbuf_pool_create failed: %s\n",
            dpdk.rte_strerror(dpdk.vex_dpdk_get_errno()));
        _ = dpdk.rte_eal_cleanup();
        return 1;
    };

    // ── Port 0 setup ─────────────────────────────────────────────
    const port_id: u16 = 0;
    const ret = dpdk.vex_dpdk_port_setup(port_id, 1024, pool);
    if (ret < 0) {
        _ = fprintf(stderr, "port %u setup failed: %s\n",
            @as(c_uint, port_id), dpdk.rte_strerror(-ret));
        dpdk.rte_mempool_free(pool);
        _ = dpdk.rte_eal_cleanup();
        return 1;
    }

    // ── Poll loop ────────────────────────────────────────────────
    _ = printf("Polling for %u seconds, burst=%u\n",
        @as(c_uint, cfg.duration_secs), @as(c_uint, cfg.burst));

    var pkts_buf: [128]?*Mbuf = undefined;
    const burst: u16 = @min(cfg.burst, pkts_buf.len);

    var total_packets: u64 = 0;
    var total_bytes: u64 = 0;

    // wall-clock loop bound via clock_gettime(CLOCK_MONOTONIC) — we
    // already link libc for the printf/fprintf shim above.
    const monoNanos = struct {
        fn read() i64 {
            var ts: std.c.timespec = undefined;
            _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
            return @as(i64, @intCast(ts.sec)) * std.time.ns_per_s +
                @as(i64, @intCast(ts.nsec));
        }
    }.read;
    const start_ns: i64 = monoNanos();
    const deadline_ns: i64 = start_ns + @as(i64, cfg.duration_secs) * std.time.ns_per_s;

    while (monoNanos() < deadline_ns) {
        const n = dpdk.vex_dpdk_rx_burst(port_id, 0, &pkts_buf, burst);
        if (n > 0) {
            var i: usize = 0;
            while (i < n) : (i += 1) {
                total_bytes += dpdk.vex_dpdk_pktmbuf_len(pkts_buf[i]);
                dpdk.vex_dpdk_pktmbuf_free(pkts_buf[i]);
            }
            total_packets += n;
        }
    }

    const pps: u64 = total_packets / cfg.duration_secs;
    const bps: u64 = total_bytes / cfg.duration_secs;
    _ = printf(
        "Received %lu packets (%lu bytes) in %u seconds\n" ++
            "  -> %lu pkt/s, %lu bytes/s\n",
        total_packets, total_bytes, @as(c_uint, cfg.duration_secs), pps, bps,
    );

    // ── Teardown ─────────────────────────────────────────────────
    _ = dpdk.rte_eth_dev_stop(port_id);
    _ = dpdk.rte_eth_dev_close(port_id);
    dpdk.rte_mempool_free(pool);
    if (dpdk.rte_eal_cleanup() != 0) {
        _ = fprintf(stderr, "rte_eal_cleanup failed\n");
        return 1;
    }
    return 0;
}
