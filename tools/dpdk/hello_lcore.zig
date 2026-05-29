//! hello_lcore — minimal DPDK toolchain probe in Zig.
//!
//! Calls into DPDK via hand-rolled `extern fn` declarations (Zig
//! 0.17-dev removed `@cImport`; the project standard for C interop is
//! now explicit extern blocks). No networking yet — we initialize
//! the EAL, launch a function on every lcore that prints its id +
//! NUMA socket + tsc, and tear down. Confirms the build chain
//! (libdpdk-dev + pkg-config + Zig's clang frontend) is wired
//! correctly end-to-end.
//!
//! Run example (inside the Dockerfile.dpdk container):
//!   ./hello_lcore -l 0-3 --no-pci --no-huge
//!
//! Flag rationale:
//!   -l 0-3       use lcores 0 through 3
//!   --no-pci     skip PCI device scan (no NIC required for this probe)
//!   --no-huge    use anonymous mmap instead of hugepages; lets the
//!                binary run on any Linux box without sysctl tuning.

const std = @import("std");

// DPDK C symbols — only what this probe uses. The eventual
// src/server/net/dpdk.zig will extend this set with rte_ethdev /
// rte_mbuf / etc.
//
// A handful of DPDK's "always-inline" or per-thread symbols can't be
// reached by linking against librte_*.so (they live only in headers
// as `static inline` or `__thread` declarations). We expose them
// through `vex_dpdk_*` shims in dpdk_shim.c rather than replicating
// the inline bodies in Zig — see dpdk_shim.c's header for the
// rationale. The shim functions are marked with `_shim` suffixes
// here so the call sites make the indirection obvious.
const dpdk = struct {
    // EAL lifecycle
    extern "c" fn rte_eal_init(argc: c_int, argv: [*c][*c]u8) c_int;
    extern "c" fn rte_eal_cleanup() c_int;

    // Per-lcore launch / wait
    extern "c" fn rte_eal_mp_remote_launch(
        f: *const fn (arg: ?*anyopaque) callconv(.c) c_int,
        arg: ?*anyopaque,
        call_main: c_int,
    ) c_int;
    extern "c" fn rte_eal_mp_wait_lcore() c_int;

    // Topology / IDs — main_lcore and lcore_count are real exported
    // symbols; lcore_id / socket_id / tsc come through the shim.
    extern "c" fn rte_get_main_lcore() c_uint;
    extern "c" fn rte_lcore_count() c_uint;
    extern "c" fn rte_socket_count() c_uint;
    extern "c" fn vex_dpdk_lcore_id() c_uint;
    extern "c" fn vex_dpdk_socket_id() c_int;
    extern "c" fn vex_dpdk_get_tsc_cycles() u64;

    // Errors / version. rte_errno is per-thread; reach it through the shim.
    extern "c" fn vex_dpdk_get_errno() c_int;
    extern "c" fn rte_strerror(errnum: c_int) [*c]const u8;
    extern "c" fn rte_version() [*c]const u8;

    // rte_rmt_call_main_t enum — only the value we need.
    const SKIP_MAIN: c_int = 0;
};

// libc bits we use for output. Cheaper than wrapping std.Io for a
// 50-line probe.
extern "c" fn printf(fmt: [*c]const u8, ...) c_int;
extern "c" fn fprintf(stream: ?*anyopaque, fmt: [*c]const u8, ...) c_int;
extern "c" var stderr: ?*anyopaque;

// Invoked on each lcore via rte_eal_mp_remote_launch. Side-effect:
// one printf per lcore. Returns 0 because lcore launch propagates
// non-zero returns out of rte_eal_mp_wait_lcore.
//
// `callconv(.c)` matches the function-pointer signature DPDK expects;
// the runtime calls this directly from a C-allocated worker thread.
fn helloFromLcore(_: ?*anyopaque) callconv(.c) c_int {
    const lcore_id = dpdk.vex_dpdk_lcore_id();
    const socket_id = dpdk.vex_dpdk_socket_id();
    const tsc = dpdk.vex_dpdk_get_tsc_cycles();
    _ = printf(
        "[lcore %u] running on NUMA socket %d, tsc=%llu\n",
        lcore_id,
        socket_id,
        tsc,
    );
    return 0;
}

pub fn main(init: std.process.Init.Minimal) u8 {
    // DPDK takes (argc, argv) in C-canonical form. Init.Minimal hands
    // us a [][*:0]const u8 (Zig sees argv as immutable from the
    // program's point of view); DPDK's signature wants char**, so we
    // @constCast. DPDK does modify argv in place when it consumes its
    // own EAL flags — but we're past that point and the OS-owned argv
    // backing memory is safely mutable.
    const args_vec = init.args.vector;
    const argc: c_int = @intCast(args_vec.len);
    const argv: [*c][*c]u8 = @ptrCast(@constCast(args_vec.ptr));

    const ret = dpdk.rte_eal_init(argc, argv);
    if (ret < 0) {
        const errno = dpdk.vex_dpdk_get_errno();
        _ = fprintf(stderr, "rte_eal_init failed: %s\n", dpdk.rte_strerror(errno));
        return 1;
    }

    _ = printf("DPDK %s\n", dpdk.rte_version());
    _ = printf("Main lcore: %u\n", dpdk.rte_get_main_lcore());
    _ = printf("Total lcores: %u\n", dpdk.rte_lcore_count());
    _ = printf("NUMA sockets: %u\n", dpdk.rte_socket_count());

    // Run on every non-main lcore in parallel, then on main.
    _ = dpdk.rte_eal_mp_remote_launch(&helloFromLcore, null, dpdk.SKIP_MAIN);
    _ = helloFromLcore(null);
    _ = dpdk.rte_eal_mp_wait_lcore();

    if (dpdk.rte_eal_cleanup() != 0) {
        _ = fprintf(stderr, "rte_eal_cleanup failed\n");
        return 1;
    }
    return 0;
}
