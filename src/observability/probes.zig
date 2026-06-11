//! Hot-path timing probes. Per-worker counters of (sum_ns, count) for the
//! suspected cost centers under multi-worker load. Default-off via a global
//! flag so default builds pay nothing; flipped on for diagnostic runs.
//!
//! Each `record()` call adds 2 clock_gettime invocations (~60ns total) to
//! the path it wraps. Acceptable for analysis builds; not for production.

const std = @import("std");
const c = std.c;

/// Master switch. Atomic so vex.conf / CONFIG SET can flip it at runtime
/// without rebuild. Defaults off.
pub var enabled: std.atomic.Value(bool) = std.atomic.Value(bool).init(false);

pub inline fn isEnabled() bool {
    return enabled.load(.monotonic);
}

inline fn nowNs() u64 {
    var ts: c.timespec = undefined;
    _ = c.clock_gettime(c.CLOCK.MONOTONIC, &ts);
    return @as(u64, @intCast(ts.sec)) * 1_000_000_000 + @as(u64, @intCast(ts.nsec));
}

pub const Probe = struct {
    sum_ns: u64 = 0,
    count: u64 = 0,
    max_ns: u64 = 0,

    pub inline fn record(self: *Probe, ns: u64) void {
        self.sum_ns +%= ns;
        self.count +%= 1;
        if (ns > self.max_ns) self.max_ns = ns;
    }
};

/// Scoped timer — start() returns a token, finish(token) records elapsed.
/// Use only when isEnabled(); callers must check.
pub inline fn start() u64 {
    return nowNs();
}

pub inline fn finish(probe: *Probe, started: u64) void {
    const now = nowNs();
    if (now >= started) probe.record(now - started);
}

/// Elapsed ns since a start() token, for sites that record one interval
/// into more than one probe.
pub inline fn sinceNs(started: u64) u64 {
    const now = nowNs();
    return if (now >= started) now - started else 0;
}

/// Which event-loop backend won at init. Set once at startup by the event
/// loop; read by DEBUG PROBES so diagnostic runs know the actual syscall
/// pattern (SQPOLL init silently falls back to plain io_uring, then epoll).
/// 0=unknown 1=epoll/kqueue 2=io_uring 3=io_uring+sqpoll
pub var ring_mode: std.atomic.Value(u8) = std.atomic.Value(u8).init(0);

pub fn ringModeName() []const u8 {
    return switch (ring_mode.load(.monotonic)) {
        1 => "epoll/kqueue",
        2 => "io_uring",
        3 => "io_uring+sqpoll",
        else => "unknown",
    };
}

/// Set of probes attached to each Worker. Single-owner writes (the worker's
/// thread), no atomics needed within the struct itself.
pub const WorkerProbes = struct {
    /// Time spent in handleRecvCompletion for a single recv batch.
    recv_batch: Probe = .{},
    /// Time per dispatched command — top-level cost per op.
    cmd_dispatch: Probe = .{},
    /// Time in DsStripeLocks.acquire + releaseAll across the recv batch.
    stripe_lock: Probe = .{},
    /// Time in the actual storage operation (CKV/hash/list/set/zset).
    storage_op: Probe = .{},
    /// Time loading shared atomics on the hot path (persistence_broken etc).
    shared_atomics: Probe = .{},
    /// Time formatting and writing the RESP reply.
    resp_write: Probe = .{},
    /// Time per io_uring submit (recv re-arm + send).
    io_submit: Probe = .{},

    // ── GET substeps (executeHotFast GET path) ──
    /// readLockStripe / readUnlockStripe pair.
    get_stripe_lock: Probe = .{},
    /// stripe.map.getPtr — hashmap bucket walk under rdlock.
    get_hashmap_lookup: Probe = .{},
    /// Copying the value out (inline buf or large value).
    get_value_copy: Probe = .{},
    /// bufPrint + appendSliceAssumeCapacity for the bulk-string reply.
    get_resp_format: Probe = .{},

    // ── SET substeps (executeHotFast SET path) ──
    set_stripe_lock: Probe = .{},
    /// stripe.map.getOrPut for the key.
    set_hashmap_op: Probe = .{},
    /// Value memcpy (inline or alloc+memcpy).
    set_value_copy: Probe = .{},
    /// seqlock bump on the entry.
    set_seqlock: Probe = .{},

    // ── HSET substeps (dispatched through hash.zig) ──
    /// HashStore.getOrCreate — outer StringHashMap lookup + (if new) FieldMap init.
    hset_get_or_create: Probe = .{},
    /// FieldMap.fields.getOrPut — inner StringHashMap lookup.
    hset_fieldmap_lookup: Probe = .{},
    /// Combined field+value buffer alloc + 2× memcpy.
    hset_alloc_copy: Probe = .{},
    /// Free old value (update-only path).
    hset_old_free: Probe = .{},

    // ── Per-command frame probes (chase the "unaccounted" gap) ──
    /// nsKey() — db-prefix memcpy for the key. Called per command.
    nskey: Probe = .{},
    /// dsl.acquire() — stripe lease lock acquire.
    dsl_acquire: Probe = .{},
    /// Entire hs.hset() call (HSET only).
    hset_total: Probe = .{},
    /// bumpWatchVersion() — load active_watches + (if 0) early return.
    bump_watch: Probe = .{},
    /// writeIntTo() / appendSlice() for command reply.
    reply_write: Probe = .{},

    // ── Event-loop wait-path probes (the segment NO CPU-path probe covers:
    //    everything between "reply submitted" and "next recv batch starts") ──
    /// Full duration of submit_and_wait(1) in pollIoUring, including time
    /// asleep waiting for traffic. count = wait-enter syscalls.
    wait_enter: Probe = .{},
    /// Subset of wait_enter that exceeded ~2µs, i.e. the thread genuinely
    /// blocked in the kernel and paid a scheduler sleep/wake cycle.
    /// wait_blocked.count / ops = sleeps per op.
    wait_blocked: Probe = .{},
    /// CQEs harvested per wakeup. NOT nanoseconds: the avg column is avg
    /// completions per wake (amortization factor), max is the largest batch.
    cqes_per_wake: Probe = .{},
    /// Per-wake CQE counts by type. NOT nanoseconds: sum = total CQEs of
    /// that type, count = poll ticks. Separates recv traffic from send
    /// completions and (wasteful) poll_add wakeups.
    cqe_recv: Probe = .{},
    cqe_send: Probe = .{},
    cqe_poll: Probe = .{},
    /// One flushSqes() ring.submit() syscall. After the eager-flush removal
    /// the only hot caller left is the AOF group-commit in the loop tail.
    flush_enter: Probe = .{},
};

/// Thread-local pointer to the current worker's probes. Set by the worker
/// before entering engine code (hash.zig / concurrent_kv.zig). Engine code
/// reads this to record probes without taking a probe ptr in every signature.
pub threadlocal var current: ?*WorkerProbes = null;

/// Format the probe set into an array_list as a human-readable text block.
pub fn formatInto(buf: *std.array_list.Managed(u8), probes: *const WorkerProbes, worker_id: u32) !void {
    var line: [256]u8 = undefined;
    const labels = [_][]const u8{
        "recv_batch          ", "cmd_dispatch        ", "stripe_lock         ",
        "storage_op          ", "shared_atomics      ", "resp_write          ",
        "io_submit           ",
        "get_stripe_lock     ", "get_hashmap_lookup  ", "get_value_copy      ",
        "get_resp_format     ",
        "set_stripe_lock     ", "set_hashmap_op      ", "set_value_copy      ",
        "set_seqlock         ",
        "hset_get_or_create  ", "hset_fieldmap_lookup", "hset_alloc_copy     ",
        "hset_old_free       ",
        "nskey               ", "dsl_acquire         ", "hset_total          ",
        "bump_watch          ", "reply_write         ",
        "wait_enter          ", "wait_blocked        ", "cqes_per_wake       ",
        "cqe_recv            ", "cqe_send            ", "cqe_poll            ",
        "flush_enter         ",
    };
    const pset = [_]Probe{
        probes.recv_batch,     probes.cmd_dispatch,   probes.stripe_lock,
        probes.storage_op,     probes.shared_atomics, probes.resp_write,
        probes.io_submit,
        probes.get_stripe_lock, probes.get_hashmap_lookup, probes.get_value_copy,
        probes.get_resp_format,
        probes.set_stripe_lock, probes.set_hashmap_op, probes.set_value_copy,
        probes.set_seqlock,
        probes.hset_get_or_create, probes.hset_fieldmap_lookup, probes.hset_alloc_copy,
        probes.hset_old_free,
        probes.nskey, probes.dsl_acquire, probes.hset_total,
        probes.bump_watch, probes.reply_write,
        probes.wait_enter, probes.wait_blocked, probes.cqes_per_wake,
        probes.cqe_recv, probes.cqe_send, probes.cqe_poll,
        probes.flush_enter,
    };
    const hdr = try std.fmt.bufPrint(&line, "worker {d}\n", .{worker_id});
    try buf.appendSlice(hdr);
    for (labels, pset) |lbl, p| {
        if (p.count == 0) continue;  // skip uncollected probes
        const s = try std.fmt.bufPrint(&line, "  {s}  n={d:>10}  avg_ns={d:>6}  max_ns={d:>8}\n", .{
            lbl, p.count, avgNs(p), p.max_ns,
        });
        try buf.appendSlice(s);
    }
}

fn avgNs(p: Probe) u64 {
    if (p.count == 0) return 0;
    return p.sum_ns / p.count;
}

pub fn reset(probes: *WorkerProbes) void {
    probes.* = .{};
}

/// Global registry mirroring stats_mod.register pattern. Each worker
/// registers its `probes` pointer at startup; DEBUG PROBES aggregates
/// across all registered workers.
const MAX_WORKERS: usize = 64;
var probes_buf: [MAX_WORKERS]*WorkerProbes = undefined;
var probes_len: usize = 0;
var probes_mutex: std.c.pthread_mutex_t = std.c.PTHREAD_MUTEX_INITIALIZER;

pub fn register(p: *WorkerProbes) void {
    _ = std.c.pthread_mutex_lock(&probes_mutex);
    defer _ = std.c.pthread_mutex_unlock(&probes_mutex);
    for (probes_buf[0..probes_len]) |existing| if (existing == p) return;
    if (probes_len >= MAX_WORKERS) return;
    probes_buf[probes_len] = p;
    probes_len += 1;
}

pub fn unregister(p: *WorkerProbes) void {
    _ = std.c.pthread_mutex_lock(&probes_mutex);
    defer _ = std.c.pthread_mutex_unlock(&probes_mutex);
    var i: usize = 0;
    while (i < probes_len) : (i += 1) {
        if (probes_buf[i] == p) {
            probes_buf[i] = probes_buf[probes_len - 1];
            probes_len -= 1;
            return;
        }
    }
}

pub fn forEach(ctx: anytype, comptime cb: fn (@TypeOf(ctx), worker_id: u32, *const WorkerProbes) anyerror!void) !void {
    _ = std.c.pthread_mutex_lock(&probes_mutex);
    defer _ = std.c.pthread_mutex_unlock(&probes_mutex);
    var i: u32 = 0;
    while (i < probes_len) : (i += 1) {
        try cb(ctx, i, probes_buf[i]);
    }
}

pub fn resetAll() void {
    _ = std.c.pthread_mutex_lock(&probes_mutex);
    defer _ = std.c.pthread_mutex_unlock(&probes_mutex);
    var i: usize = 0;
    while (i < probes_len) : (i += 1) reset(probes_buf[i]);
}
