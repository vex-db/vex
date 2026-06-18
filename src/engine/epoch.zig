//! Quiescent-State-Based Reclamation (QSBR) for lock-free reads of the
//! striped KV HashMaps.
//!
//! WHY: a GET on the hot path takes the stripe `pthread_rwlock_rdlock` purely
//! to stop a concurrent SET's HashMap rehash from freeing the bucket array
//! while `getPtr` walks it. That read-lock is an atomic RMW on a *shared*
//! cacheline, so at high core counts every stripe's lock line ping-pongs
//! between chiplets and per-core throughput collapses (~100k/core at 16c ->
//! ~34k/core at 32c, measured 2026-06-17).
//!
//! The fix is to let readers walk the bucket array WITHOUT a shared write, and
//! instead *defer* freeing a rehashed-away bucket array until no reader can
//! still hold a pointer into it. QSBR is ideal here because each reactor worker
//! is a single thread that holds no KV pointer between commands — so "between
//! event-loop ticks" is a natural quiescent state.
//!
//! MODEL: a monotonic global epoch. Each worker records the global epoch when
//! it quiesces (once per loop tick). Memory retired at epoch R is reclaimable
//! once every online worker has quiesced at an epoch > R (so any reference it
//! obtained at or before R has been dropped). A parked/idle worker marks itself
//! offline so it doesn't hold reclamation back.
//!
//! This module is standalone and carries no dependency on the KV/worker code;
//! integration (retiring rehashed bucket arrays, dropping the rdlock) lands in
//! later pieces.

const std = @import("std");

/// Hard cap on participating workers (vex caps --workers well under this).
pub const MAX_WORKERS = 64;

/// Cacheline-padded atomic so per-worker epoch slots never false-share.
const Slot = struct {
    epoch: std.atomic.Value(u64) align(64) = std.atomic.Value(u64).init(0),
    /// Online workers participate in reclamation; offline (parked/idle) ones
    /// are treated as fully caught up so a quiet worker can't stall frees.
    online: std.atomic.Value(bool) = std.atomic.Value(bool).init(true),
    _pad: [64 - @sizeOf(std.atomic.Value(u64)) - @sizeOf(std.atomic.Value(bool))]u8 = undefined,
};

pub const EpochGC = struct {
    /// Monotonic global epoch. Starts at 1 so 0 can mean "never quiesced".
    global: std.atomic.Value(u64) align(64) = std.atomic.Value(u64).init(1),
    n_workers: u32 = 1,
    local: [MAX_WORKERS]Slot = @splat(.{}),

    pub fn init(n_workers: u32) EpochGC {
        std.debug.assert(n_workers >= 1 and n_workers <= MAX_WORKERS);
        var gc = EpochGC{ .n_workers = n_workers };
        var i: u32 = 0;
        while (i < n_workers) : (i += 1) {
            gc.local[i].epoch.store(1, .monotonic);
            gc.local[i].online.store(true, .monotonic);
        }
        return gc;
    }

    pub fn currentEpoch(self: *EpochGC) u64 {
        return self.global.load(.acquire);
    }

    /// Worker `wid` reports a quiescent state — it holds no pointer into any
    /// reclaimable structure. Records the current global epoch and, if every
    /// online worker has now reached it, advances the global epoch so retired
    /// memory can age out. Call once per event-loop tick.
    pub fn quiesce(self: *EpochGC, wid: u32) void {
        const g = self.global.load(.acquire);
        self.local[wid].epoch.store(g, .release);
        self.local[wid].online.store(true, .release);

        // Advance only if all online workers have caught up to g.
        var i: u32 = 0;
        while (i < self.n_workers) : (i += 1) {
            if (!self.local[i].online.load(.acquire)) continue;
            if (self.local[i].epoch.load(.acquire) < g) return; // someone behind
        }
        // CAS so concurrent advancers don't double-bump.
        _ = self.global.cmpxchgStrong(g, g + 1, .monotonic, .monotonic);
    }

    /// Mark a worker offline before it parks with no work (it holds nothing, so
    /// it can't pin any retired memory). Offline workers are ignored by
    /// `safeEpoch`, so one idle worker can't freeze reclamation.
    pub fn goOffline(self: *EpochGC, wid: u32) void {
        self.local[wid].online.store(false, .release);
    }

    /// Lowest epoch any ONLINE worker might still be observing. Memory retired
    /// at an epoch strictly less than this is safe to free. Offline workers are
    /// skipped (treated as caught up to global).
    pub fn safeEpoch(self: *EpochGC) u64 {
        var min: u64 = self.global.load(.acquire);
        var i: u32 = 0;
        while (i < self.n_workers) : (i += 1) {
            if (!self.local[i].online.load(.acquire)) continue;
            const e = self.local[i].epoch.load(.acquire);
            if (e < min) min = e;
        }
        return min;
    }
};

/// Per-worker deferred-free list. Single-owner (the worker that retires also
/// reclaims), so no locking. Holds raw allocations tagged with the epoch at
/// which they were retired; `reclaim` frees those the GC says are safe.
pub const RetireList = struct {
    const Item = struct {
        memory: []u8,
        alignment: std.mem.Alignment,
        epoch: u64,
    };

    backing: std.mem.Allocator,
    gc: *EpochGC,
    items: std.array_list.Managed(Item),
    /// Total bytes currently deferred (for observability / backpressure).
    pending_bytes: usize = 0,

    pub fn init(backing: std.mem.Allocator, gc: *EpochGC) RetireList {
        return .{ .backing = backing, .gc = gc, .items = std.array_list.Managed(Item).init(backing) };
    }

    /// Defer freeing `memory` (allocated with `alignment`) until safe. The
    /// caller must not access `memory` after this returns.
    pub fn retire(self: *RetireList, memory: []u8, alignment: std.mem.Alignment) !void {
        try self.items.append(.{ .memory = memory, .alignment = alignment, .epoch = self.gc.currentEpoch() });
        self.pending_bytes += memory.len;
    }

    /// Free every retired allocation whose epoch is strictly older than the
    /// current safe epoch. Returns the number reclaimed.
    pub fn reclaim(self: *RetireList) usize {
        const safe = self.gc.safeEpoch();
        var freed: usize = 0;
        var i: usize = 0;
        while (i < self.items.items.len) {
            const it = self.items.items[i];
            if (it.epoch < safe) {
                self.backing.rawFree(it.memory, it.alignment, @returnAddress());
                self.pending_bytes -= it.memory.len;
                _ = self.items.swapRemove(i);
                freed += 1;
            } else {
                i += 1;
            }
        }
        return freed;
    }

    /// Free everything unconditionally (shutdown only — no readers remain).
    pub fn deinit(self: *RetireList) void {
        for (self.items.items) |it| self.backing.rawFree(it.memory, it.alignment, @returnAddress());
        self.items.deinit();
        self.pending_bytes = 0;
    }
};

// ───────────────────────────── tests ─────────────────────────────

test "epoch advances only when all online workers quiesce" {
    var gc = EpochGC.init(3);
    try std.testing.expectEqual(@as(u64, 1), gc.currentEpoch());
    gc.quiesce(0); // worker 0 at 1; others still 1 -> all caught up -> advance to 2
    try std.testing.expectEqual(@as(u64, 2), gc.currentEpoch());
    gc.quiesce(0); // worker 0 -> 2; workers 1,2 still at 1 -> behind -> no advance
    try std.testing.expectEqual(@as(u64, 2), gc.currentEpoch());
    gc.quiesce(1);
    try std.testing.expectEqual(@as(u64, 2), gc.currentEpoch());
    gc.quiesce(2); // now all at 2 -> advance to 3
    try std.testing.expectEqual(@as(u64, 3), gc.currentEpoch());
}

test "retired memory is not reclaimed until all workers pass its epoch" {
    const a = std.testing.allocator;
    var gc = EpochGC.init(2);
    var rl = RetireList.init(a, &gc);
    defer rl.deinit();

    // Both workers start at epoch 1. Retire a buffer at epoch 1.
    const buf = try a.alloc(u8, 128);
    try rl.retire(buf, .@"1");
    try std.testing.expectEqual(@as(usize, 1), rl.items.items.len);

    // safeEpoch == 1 (both workers at 1) -> nothing with epoch < 1 -> no free.
    try std.testing.expectEqual(@as(usize, 0), rl.reclaim());
    try std.testing.expectEqual(@as(usize, 1), rl.items.items.len);

    // Worker 0 quiesces (->2 attempt; worker 1 still 1, no global advance, but
    // worker 0 local=... ) — advance needs BOTH. Quiesce both to push safe>1.
    gc.quiesce(0);
    gc.quiesce(1); // global -> 2, both locals now reflect >= ... requiesce:
    gc.quiesce(0);
    gc.quiesce(1);
    // Now both locals are 2 -> safeEpoch == 2 -> buffer (epoch 1 < 2) reclaimed.
    try std.testing.expectEqual(@as(usize, 1), rl.reclaim());
    try std.testing.expectEqual(@as(usize, 0), rl.items.items.len);
}

test "offline worker does not stall reclamation" {
    const a = std.testing.allocator;
    var gc = EpochGC.init(3);
    var rl = RetireList.init(a, &gc);
    defer rl.deinit();

    const buf = try a.alloc(u8, 64);
    try rl.retire(buf, .@"1"); // retired at epoch 1

    // Worker 2 goes idle/offline and never quiesces again.
    gc.goOffline(2);
    // Workers 0 and 1 quiesce enough to advance past epoch 1.
    gc.quiesce(0);
    gc.quiesce(1); // online set {0,1} both at 1 -> advance to 2
    gc.quiesce(0);
    gc.quiesce(1); // both at 2 -> safeEpoch (ignoring offline 2) == 2
    try std.testing.expectEqual(@as(usize, 2), gc.safeEpoch());
    try std.testing.expectEqual(@as(usize, 1), rl.reclaim()); // freed despite worker 2 idle
}

test "concurrent retire + quiesce never frees a pinned buffer" {
    const a = std.testing.allocator;
    const N = 4;
    var gc = EpochGC.init(N);

    const Poison = struct {
        // A worker thread that repeatedly: quiesces, "reads" a shared buffer
        // (checking a sentinel), and quiesces again. The reclaimer only frees
        // via safeEpoch, so the sentinel must never be observed freed mid-read.
        fn worker(g: *EpochGC, wid: u32, shared: *std.atomic.Value(?*[256]u8), stop: *std.atomic.Value(bool)) void {
            while (!stop.load(.acquire)) {
                g.quiesce(wid);
                // critical section: hold a pointer obtained after quiesce
                if (shared.load(.acquire)) |p| {
                    // touch it; if reclamation were premature this would be UAF
                    std.mem.doNotOptimizeAway(p[0]);
                }
                g.quiesce(wid);
            }
            g.goOffline(wid);
        }
    };

    var shared = std.atomic.Value(?*[256]u8).init(null);
    var stop = std.atomic.Value(bool).init(false);
    var rl = RetireList.init(a, &gc);
    defer rl.deinit();

    var threads: [N - 1]std.Thread = undefined;
    for (0..N - 1) |i| threads[i] = try std.Thread.spawn(.{}, Poison.worker, .{ &gc, @as(u32, @intCast(i + 1)), &shared, &stop });

    // main thread (wid 0) churns buffers: publish, retire-old, reclaim.
    var round: usize = 0;
    while (round < 2000) : (round += 1) {
        const blk = try a.create([256]u8);
        @memset(blk, 0xAB);
        const old = shared.swap(blk, .acq_rel);
        if (old) |o| try rl.retire(std.mem.asBytes(o), .of([256]u8));
        gc.quiesce(0);
        _ = rl.reclaim();
    }
    stop.store(true, .release);
    for (threads) |t| t.join();
    // drain
    if (shared.load(.acquire)) |o| try rl.retire(std.mem.asBytes(o), .of([256]u8));
}
