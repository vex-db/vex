# Memory reclamation

The reactor stores each table entry in 32 bytes, with values up to 24 bytes
inline. Larger strings share one allocation with their key and reuse that
allocation for equal-size writes. TTL, integer and LRU metadata is allocated
only when needed. Tables keep the 80% load limit but grow through intermediate
capacities (for example, 4,096 → 6,144 → 8,192 slots) to reduce resize jumps. Independent stripe hashing avoids concentrating every
stripe's keys in the same subset of its table buckets. GET copies values into
the response while holding the stripe read lock.

Expired keys are now removed during idle time by bounded background scans.
Each pass inspects at most 8,192 physical buckets and removes at most 512
entries, skipping busy stripes. Expiry invalidates watched keys before their
owned allocations are released. Stores that have never accepted a TTL skip
these scans. Refreshed values are checked under the stripe lock and survive
an old expiration deadline.

`INFO used_memory_rss` reports current resident memory, with
`used_memory_rss_available` indicating whether it could be read. The lifetime
peak is reported separately as `used_memory_rss_peak`.

Explicit `FLUSHDB` releases all stores and, when using Linux glibc's C
allocator, requests that fully freed pages be returned to the operating
system. This is synchronous and can increase FLUSHDB latency. Ordinary
deletes and smaller overwrites can leave reusable table capacity and
allocator pages resident. Expiry reclamation likewise does not guarantee a
particular RSS floor.

The 0.8.1 release candidates are used for matched throughput and memory
validation before promotion. The release image is built by the repository's
tag workflow; benchmark results must identify that image's digest.
