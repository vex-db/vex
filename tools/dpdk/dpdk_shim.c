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
#include <rte_ethdev.h>
#include <rte_mbuf.h>
#include <rte_mempool.h>

unsigned vex_dpdk_lcore_id(void)       { return rte_lcore_id(); }
int      vex_dpdk_socket_id(void)      { return rte_socket_id(); }
uint64_t vex_dpdk_get_tsc_cycles(void) { return rte_get_tsc_cycles(); }
int      vex_dpdk_get_errno(void)      { return rte_errno; }

/*
 * Port setup — wraps the rte_eth_dev_configure / rx_queue_setup /
 * dev_start handshake plus the rte_eth_conf struct (whose layout has
 * evolved several times across DPDK releases — replicating it in
 * Zig means tracking that drift). Returns 0 on success, negative on
 * failure (caller can pass to rte_strerror for diagnostics).
 *
 * `rx_ring_size` is a power of 2 the PMD must support — 1024 is the
 * safe default on every PMD we care about (ena, virtio, null).
 */
int vex_dpdk_port_setup(uint16_t port_id,
                        uint16_t rx_ring_size,
                        struct rte_mempool *pool)
{
    struct rte_eth_conf port_conf = { 0 };
    int ret;

    ret = rte_eth_dev_configure(port_id, /* nb_rx_q */ 1, /* nb_tx_q */ 0,
                                &port_conf);
    if (ret < 0) return ret;

    ret = rte_eth_rx_queue_setup(port_id, /* queue_id */ 0, rx_ring_size,
                                 rte_eth_dev_socket_id(port_id),
                                 /* default conf */ NULL,
                                 pool);
    if (ret < 0) return ret;

    return rte_eth_dev_start(port_id);
}

/*
 * RX burst — rte_eth_rx_burst is an always-inline (it's the hot path,
 * shouldn't pay for a function call). Expose it through the shim.
 * `pkts` is a caller-owned array; the function fills it and returns
 * how many were filled (0 means "no packets ready right now").
 */
uint16_t vex_dpdk_rx_burst(uint16_t port_id, uint16_t queue_id,
                           struct rte_mbuf **pkts, uint16_t nb_pkts)
{
    return rte_eth_rx_burst(port_id, queue_id, pkts, nb_pkts);
}

/* rte_pktmbuf_free is also inline. */
void vex_dpdk_pktmbuf_free(struct rte_mbuf *m)
{
    rte_pktmbuf_free(m);
}

/* Per-packet length lookup — `pkt_len` lives in a union inside
 * rte_mbuf and crossing that boundary from Zig is fragile. */
uint32_t vex_dpdk_pktmbuf_len(struct rte_mbuf *m)
{
    return m->pkt_len;
}
