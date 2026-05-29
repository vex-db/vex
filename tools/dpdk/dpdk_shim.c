/*
 * dpdk_shim — non-inline wrappers around DPDK's static-inline /
 * thread-local symbols so Zig's extern fn can reach them.
 *
 * DPDK keeps `rte_lcore_id`, `rte_get_tsc_cycles`, and similar in
 * headers as `static inline` functions (or in `rte_errno`'s case, a
 * per-thread variable accessed via macro). They never end up as
 * exported symbols in librte_*.so. Zig's `extern fn` then can't
 * find them at link time.
 *
 * We could fix this in-Zig by replicating the inline bodies (rdtsc
 * inline asm + thread-local extern), but a 30-line C shim is less
 * code, less arch-specific, and easier to audit.
 *
 * Each `vex_*` here is the only entry point hello_lcore.zig calls;
 * the eventual src/server/net/dpdk.zig will extend this set.
 */

#include <rte_eal.h>
#include <rte_lcore.h>
#include <rte_cycles.h>
#include <rte_errno.h>

unsigned vex_dpdk_lcore_id(void)       { return rte_lcore_id(); }
int      vex_dpdk_socket_id(void)      { return rte_socket_id(); }
uint64_t vex_dpdk_get_tsc_cycles(void) { return rte_get_tsc_cycles(); }
int      vex_dpdk_get_errno(void)      { return rte_errno; }
