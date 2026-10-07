// Migrated unit tests for src/engine/kv/concurrent_kv.zig.

const std = @import("std");
const ConcurrentKV = @import("../../../src/engine/kv/concurrent_kv.zig").ConcurrentKV;
const KVStore = @import("../../../src/engine/kv/kv.zig").KVStore;
const obs_stats = @import("../../../src/observability/stats.zig");

test "concurrent_kv basic set/get" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    try store.set("name", "vex");
    const val = store.get("name") orelse return error.TestUnexpectedResult;
    defer val.deinit();
    try std.testing.expectEqualStrings("vex", val.data);
}

test "concurrent_kv delete" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    try store.set("key1", "val1");
    try std.testing.expect(store.delete("key1"));
    try std.testing.expect(store.get("key1") == null);
    try std.testing.expect(!store.delete("nonexistent"));
}

test "concurrent_kv overwrite" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    try store.set("k", "v1");
    try store.set("k", "v2");
    const val = store.get("k") orelse return error.TestUnexpectedResult;
    defer val.deinit();
    try std.testing.expectEqualStrings("v2", val.data);
}

test "concurrent_kv exists" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    try store.set("present", "yes");
    try std.testing.expect(store.exists("present"));
    try std.testing.expect(!store.exists("absent"));
}

test "concurrent_kv flushdb and dbsize" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    try store.set("a", "1");
    try store.set("b", "2");
    try store.set("c", "3");
    try std.testing.expectEqual(@as(usize, 3), store.dbsize());
    store.flushdb();
    try std.testing.expectEqual(@as(usize, 0), store.dbsize());
}

test "concurrent_kv multi-thread stress" {
    // Skip in debug: Zig's HashMap pointer_stability check conflicts with
    // external rwlock synchronization. Passes in ReleaseFast.
    if (@import("builtin").mode == .Debug) return error.SkipZigTest;
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    const num_threads = 8;
    const ops_per_thread = 1000;

    const Worker = struct {
        fn run(s: *ConcurrentKV, thread_id: usize) void {
            var i: usize = 0;
            while (i < ops_per_thread) : (i += 1) {
                var key_buf: [32]u8 = undefined;
                const key = std.fmt.bufPrint(&key_buf, "t{d}:k{d}", .{ thread_id, i }) catch continue;
                var val_buf: [32]u8 = undefined;
                const val = std.fmt.bufPrint(&val_buf, "v{d}", .{i}) catch continue;

                s.set(key, val) catch continue;
                if (s.get(key)) |v| v.deinit();
                _ = s.exists(key);
                _ = s.delete(key);
            }
        }
    };

    var threads: [num_threads]std.Thread = undefined;
    for (0..num_threads) |t| {
        threads[t] = try std.Thread.spawn(.{}, Worker.run, .{ &store, t });
    }
    for (&threads) |thread| {
        thread.join();
    }

    // Should not crash or leak (testing allocator checks leaks on deinit)
}

test "concurrent_kv maxmemory + allkeys_lru evicts on overflow" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    const target_stripe: usize = ConcurrentKV.stripeIndex("a");
    var key_buf: [32]u8 = undefined;
    var second_key: []const u8 = undefined;
    var candidate: usize = 0;
    while (candidate < 100000) : (candidate += 1) {
        second_key = try std.fmt.bufPrint(&key_buf, "eviction-{d}", .{candidate});
        if (ConcurrentKV.stripeIndex(second_key) == target_stripe) break;
    }
    try std.testing.expect(candidate < 100000);

    store.maxmemory = second_key.len + 1; // fits either entry; both require eviction
    store.eviction_policy = .allkeys_lru;

    const before = obs_stats.evicted_keys.load(.monotonic);

    store.cached_now_ms.store(1000, .release);
    try store.set("a", "x");
    store.cached_now_ms.store(2000, .release);
    try store.set(second_key, "y"); // triggers eviction of "a"

    try std.testing.expect(store.get("a") == null);
    const v = store.get(second_key) orelse return error.TestUnexpectedResult;
    defer v.deinit();
    try std.testing.expectEqualStrings("y", v.data);

    const after = obs_stats.evicted_keys.load(.monotonic);
    try std.testing.expect(after > before);
}

test "concurrent_kv maxmemory + noeviction returns error" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    store.maxmemory = 3;
    store.eviction_policy = .noeviction;

    try store.set("k", "v");

    const result = store.set("kk", "vv");
    try std.testing.expectError(error.MaxMemoryReached, result);

    const v = store.get("k") orelse return error.TestUnexpectedResult;
    defer v.deinit();
    try std.testing.expectEqualStrings("v", v.data);
}

test "concurrent_kv total_bytes decrements on delete" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    try std.testing.expectEqual(@as(u64, 0), store.total_bytes.load(.monotonic));

    try store.set("hello", "world");
    try std.testing.expectEqual(@as(u64, "hello".len + "world".len), store.total_bytes.load(.monotonic));

    try std.testing.expect(store.delete("hello"));
    try std.testing.expectEqual(@as(u64, 0), store.total_bytes.load(.monotonic));
}

test "concurrent_kv bounded sweep reclaims expired heap and inline entries once" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);
    var heap: [128]u8 = @splat('h');
    try store.setInternal("expired-heap", &heap, 1500);
    try store.setInternal("expired-inline", "small", 1500);
    try store.setInternal("future", "live", 3000);
    const before = obs_stats.expired_keys.load(.monotonic);
    var cursor: ConcurrentKV.SweepCursor = .{};
    var removed: [512]ConcurrentKV.Expired = undefined;
    const count = store.sweepExpired(2000, &cursor, &removed);
    try std.testing.expectEqual(@as(usize, 2), count);
    for (removed[0..count]) |stale| store.freeExpired(stale);
    try std.testing.expect(store.get("expired-heap") == null);
    try std.testing.expect(store.get("expired-inline") == null);
    const future = store.get("future") orelse return error.TestUnexpectedResult;
    defer future.deinit();
    try std.testing.expectEqualStrings("live", future.data);
    try std.testing.expectEqual(before + 2, obs_stats.expired_keys.load(.monotonic));
    try std.testing.expectEqual(@as(u64, "future".len + "live".len), store.total_bytes.load(.monotonic));
    try std.testing.expectEqual(@as(usize, 0), store.sweepExpired(2000, &cursor, &removed));
}

test "concurrent_kv refreshed TTL survives sweep and import skips deleted source entries" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);
    try store.setInternal("refresh", "old", 1500);
    try store.set("refresh", "live");
    var cursor: ConcurrentKV.SweepCursor = .{};
    var removed: [512]ConcurrentKV.Expired = undefined;
    try std.testing.expectEqual(@as(usize, 0), store.sweepExpired(2000, &cursor, &removed));
    const refreshed = store.get("refresh") orelse return error.TestUnexpectedResult;
    defer refreshed.deinit();
    try std.testing.expectEqualStrings("live", refreshed.data);

    var source = KVStore.init(std.testing.allocator, std.testing.io);
    defer source.deinit();
    try source.set("deleted", "source-value");
    _ = source.delete("deleted");
    var imported = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    imported.initStripes();
    defer imported.deinit();
    try imported.importFrom(&source);
    try std.testing.expectEqual(@as(usize, 0), imported.dbsize());
}

test "concurrent_kv preallocated TTL enables a fresh store sweep" {
    const alloc = std.testing.allocator;
    var store = ConcurrentKV.init(alloc, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);
    const key = try alloc.dupe(u8, "prealloc-ttl");
    const value = try alloc.dupe(u8, "value");
    const stale = store.setPrealloc("prealloc-ttl", key, value, 1500);
    try std.testing.expect(stale.stale_key == null and stale.stale_val == null);
    var cursor: ConcurrentKV.SweepCursor = .{};
    var removed: [512]ConcurrentKV.Expired = undefined;
    const count = store.sweepExpired(2000, &cursor, &removed);
    try std.testing.expectEqual(@as(usize, 1), count);
    for (removed[0..count]) |item| store.freeExpired(item);
}

test "concurrent_kv INCR-created TTL reconciles bytes after expiry" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);
    try std.testing.expectEqual(@as(i64, 1), try store.incrBy("count", 1));
    try store.setInternal("count", "1", 1500);
    try std.testing.expectEqual(@as(u64, "count".len + 1), store.total_bytes.load(.monotonic));
    var cursor: ConcurrentKV.SweepCursor = .{};
    var removed: [512]ConcurrentKV.Expired = undefined;
    const count = store.sweepExpired(2000, &cursor, &removed);
    for (removed[0..count]) |item| store.freeExpired(item);
    try std.testing.expectEqual(@as(u64, 0), store.total_bytes.load(.monotonic));
}

test "concurrent_kv expiry cursor crosses sparse prefix after growth and frees empty stripe" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);
    const target = ConcurrentKV.stripeIndex("cursor-anchor");
    var key_buf: [48]u8 = undefined;
    var expired: usize = 0;
    var permanent: usize = 0;
    var candidate: usize = 0;
    while (expired < 96 or permanent < 96) : (candidate += 1) {
        const key = try std.fmt.bufPrint(&key_buf, "cursor-{d}", .{candidate});
        if (ConcurrentKV.stripeIndex(key) != target) continue;
        if (expired < 96) {
            try store.setInternal(key, "gone", 1500);
            expired += 1;
        } else {
            try store.set(key, "keep");
            permanent += 1;
        }
    }
    var cursor: ConcurrentKV.SweepCursor = .{};
    var removed: [512]ConcurrentKV.Expired = undefined;
    var reclaimed: usize = 0;
    for (0..2) |_| {
        const count = store.sweepExpired(2000, &cursor, &removed);
        try std.testing.expect(count <= removed.len);
        reclaimed += count;
        for (removed[0..count]) |item| store.freeExpired(item);
    }
    // Rehash between cursor passes; the saved physical cursor must clamp and
    // continue rather than repeatedly scanning the first sparse prefix.
    while (permanent < 192) : (candidate += 1) {
        const key = try std.fmt.bufPrint(&key_buf, "cursor-grow-{d}", .{candidate});
        if (ConcurrentKV.stripeIndex(key) != target) continue;
        try store.set(key, "keep");
        permanent += 1;
    }
    for (0..32) |_| {
        if (reclaimed == expired) break;
        const count = store.sweepExpired(2000, &cursor, &removed);
        try std.testing.expect(count <= removed.len);
        reclaimed += count;
        for (removed[0..count]) |item| store.freeExpired(item);
    }
    try std.testing.expectEqual(expired, reclaimed);
    try std.testing.expectEqual(permanent, store.dbsize());

    var empty = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    empty.initStripes();
    defer empty.deinit();
    empty.cached_now_ms.store(1000, .release);
    try empty.setInternal("empty-stripe", "x", 1500);
    var empty_cursor: ConcurrentKV.SweepCursor = .{};
    var empty_removed: [512]ConcurrentKV.Expired = undefined;
    const count = empty.sweepExpired(2000, &empty_cursor, &empty_removed);
    for (empty_removed[0..count]) |item| empty.freeExpired(item);
    try std.testing.expectEqual(@as(u32, 0), empty.getStripePublic("empty-stripe").map.capacity());
}

// The compact layout must retain ownership of the full allocation even when
// its logical value shrinks. std.testing.allocator checks every free and leak.
test "concurrent_kv compact buffers reuse capacity and release large shrinks" {
    try std.testing.expect(@sizeOf(ConcurrentKV.Entry) == 32);
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    var large: [1024]u8 = undefined;
    @memset(&large, 'a');
    var medium: [768]u8 = undefined;
    @memset(&medium, 'b');
    var small: [256]u8 = undefined;
    @memset(&small, 'c');
    try store.set("k", &large);
    const stripe = store.getStripePublic("k");
    const original = stripe.map.getPtr("k").?.bytes().ptr;
    try store.set("k", &medium);
    try std.testing.expectEqual(original, stripe.map.getPtr("k").?.bytes().ptr);
    try std.testing.expectEqual(@as(usize, 1024), stripe.map.getPtr("k").?.storage.heap.capacity);
    try store.set("k", &large);
    try std.testing.expectEqual(original, stripe.map.getPtr("k").?.bytes().ptr);
    try store.set("k", &small);
    try std.testing.expectEqual(@as(usize, 256), stripe.map.getPtr("k").?.storage.heap.capacity);
    try store.set("k", "42");
    try std.testing.expect(stripe.map.getPtr("k").?.isInline());
    try std.testing.expectEqual(@as(i64, 43), try store.incrBy("k", 1));
    try store.set("k", &medium);
    const got = store.get("k") orelse return error.TestUnexpectedResult;
    defer got.deinit();
    try std.testing.expectEqualSlices(u8, &medium, got.data);
    try std.testing.expectEqual(@as(u64, 1 + medium.len), store.total_bytes.load(.monotonic));
    try std.testing.expect(store.delete("k"));
    try std.testing.expectEqual(@as(u64, 0), store.total_bytes.load(.monotonic));
    try store.set("flush", &large);
    try store.set("flush", &medium);
    store.flushdb();
}

test "concurrent_kv entry32 inline boundary is 24 bytes" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    var at_24: [24]u8 = @splat('a');
    var at_25: [25]u8 = @splat('b');
    var at_32: [32]u8 = @splat('c');
    try store.set("boundary", &at_24);
    try std.testing.expect(store.getStripePublic("boundary").map.getPtr("boundary").?.isInline());
    try store.set("boundary", &at_25);
    try std.testing.expect(!store.getStripePublic("boundary").map.getPtr("boundary").?.isInline());
    try std.testing.expect(store.getStripePublic("boundary").map.getPtr("boundary").?.isCombined());
    try store.set("boundary", &at_32);
    try std.testing.expect(!store.getStripePublic("boundary").map.getPtr("boundary").?.isInline());
    try std.testing.expect(store.getStripePublic("boundary").map.getPtr("boundary").?.isCombined());
    try store.set("boundary", &at_24);
    try std.testing.expect(store.getStripePublic("boundary").map.getPtr("boundary").?.isInline());
    try std.testing.expect(!store.getStripePublic("boundary").map.getPtr("boundary").?.isCombined());
}

test "concurrent_kv failed buffer growth preserves existing value and TTL" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);
    var old: [256]u8 = undefined;
    @memset(&old, 'o');
    var larger: [1024]u8 = undefined;
    @memset(&larger, 'n');
    try store.setInternal("k", &old, 5000);
    var failing = std.testing.FailingAllocator.init(std.testing.allocator, .{ .fail_index = 0 });
    store.allocator = failing.allocator();
    const result = store.set("k", &larger);
    store.allocator = std.testing.allocator;
    try std.testing.expectError(error.OutOfMemory, result);
    const value = store.get("k") orelse return error.TestUnexpectedResult;
    defer value.deinit();
    try std.testing.expectEqualSlices(u8, &old, value.data);
    try std.testing.expectEqual(@as(?i64, 4), store.ttl("k"));
    try std.testing.expectEqual(@as(u64, 257), store.total_bytes.load(.monotonic));
}

test "concurrent_kv preallocated replacement returns old allocation" {
    const alloc = std.testing.allocator;
    var store = ConcurrentKV.init(alloc, std.testing.io);
    store.initStripes();
    defer store.deinit();
    var initial: [1024]u8 = undefined;
    @memset(&initial, 'a');
    var shrunk: [768]u8 = undefined;
    @memset(&shrunk, 'b');
    try store.set("k", &initial);
    try store.set("k", &shrunk);
    const key = try alloc.dupe(u8, "k");
    const replacement = try alloc.dupe(u8, "new");
    const stale = store.setPrealloc("k", key, replacement, 0);
    try std.testing.expect(stale.stale_key == null);
    try std.testing.expectEqual(@as(usize, 1 + 1024), stale.stale_val.?.len);
    alloc.free(stale.stale_val.?);
    const value = store.get("k") orelse return error.TestUnexpectedResult;
    defer value.deinit();
    try std.testing.expectEqualStrings("new", value.data);
    try std.testing.expectEqual(@as(u64, 4), store.total_bytes.load(.monotonic));
    try store.set("k", "inline again");
}

test "concurrent_kv mixed buffer sizes remain intact under contention" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    const Runner = struct {
        fn run(s: *ConcurrentKV, id: usize) void {
            var bytes: [8192]u8 = undefined;
            @memset(&bytes, @intCast('a' + id));
            for (0..500) |iteration| {
                const sizes = [_]usize{ 16, 24, 25, 32, 33, 128, 256, 257, 768, 1024, 4096, 4097, 8192 };
                const size = sizes[iteration % sizes.len];
                s.set("contended", bytes[0..size]) catch @panic("SET failed");
                const value = s.get("contended") orelse @panic("value lost");
                defer value.deinit();
                for (value.data) |byte| {
                    if (byte != value.data[0]) @panic("torn value");
                }
            }
        }
    };
    var threads: [4]std.Thread = undefined;
    for (&threads, 0..) |*thread, id| thread.* = try std.Thread.spawn(.{}, Runner.run, .{ &store, id });
    for (&threads) |*thread| thread.join();
}

test "concurrent_kv compact inline and heap values survive table growth" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    const stripe = store.getStripePublic("seed");
    try std.testing.expectEqual(@as(u32, 0), stripe.map.capacity());
    try store.set("seed", "42");
    var large: [256]u8 = undefined;
    @memset(&large, 'v');
    var keys: [200][32]u8 = undefined;
    var lengths: [200]usize = undefined;
    var found: usize = 0;
    var candidate: usize = 0;
    while (found < keys.len) : (candidate += 1) {
        const key = try std.fmt.bufPrint(&keys[found], "grow:{d}", .{candidate});
        if (store.getStripePublic(key) != stripe) continue;
        lengths[found] = key.len;
        try store.set(key, if (found % 2 == 0) key else &large);
        found += 1;
    }
    for (&keys, lengths, 0..) |*buffer, len, index| {
        const key = buffer[0..len];
        const value = store.get(key) orelse return error.TestUnexpectedResult;
        defer value.deinit();
        try std.testing.expectEqualSlices(u8, if (index % 2 == 0) key else &large, value.data);
    }
    try std.testing.expectEqual(@as(i64, 43), try store.incrBy("seed", 1));
    store.flushdb();
    try std.testing.expectEqual(@as(u32, 0), stripe.map.capacity());
    try store.set("after", "flush");
    const value = store.get("after") orelse return error.TestUnexpectedResult;
    defer value.deinit();
    try std.testing.expectEqualStrings("flush", value.data);
}

// Stripe selection must not consume the hash bits used by table buckets or
// fingerprints; otherwise compact tables degenerate into long probe chains.
test "concurrent_kv stripe keys retain bucket and fingerprint diversity" {
    const target = ConcurrentKV.stripeIndex("distribution");
    var buckets: [256]bool = @splat(false);
    var fingerprints: [128]bool = @splat(false);
    var found: usize = 0;
    var candidate: usize = 0;
    var key_buf: [32]u8 = undefined;
    while (found < 64 and candidate < 100000) : (candidate += 1) {
        const key = try std.fmt.bufPrint(&key_buf, "distribution-{d}", .{candidate});
        if (ConcurrentKV.stripeIndex(key) != target) continue;
        const hash = std.hash_map.hashString(key);
        buckets[@intCast(hash & 255)] = true;
        fingerprints[@intCast(hash >> 57)] = true;
        found += 1;
    }
    try std.testing.expectEqual(@as(usize, 64), found);
    try std.testing.expect(std.mem.count(bool, &buckets, &.{true}) >= 32);
    try std.testing.expect(std.mem.count(bool, &fingerprints, &.{true}) >= 24);
}

// The large-value write benchmark must not allocate a replacement on every SET.
test "concurrent_kv repeated 4KiB overwrites succeed without allocation" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    var payload: [4096]u8 = @splat('a');
    try store.set("large", &payload);
    var failing = std.testing.FailingAllocator.init(std.testing.allocator, .{ .fail_index = 0 });
    {
        store.allocator = failing.allocator();
        defer store.allocator = std.testing.allocator;
        for (0..100) |i| {
            @memset(&payload, @intCast(i));
            try store.set("large", &payload);
        }
    }
    const value = store.get("large") orelse return error.TestUnexpectedResult;
    defer value.deinit();
    try std.testing.expectEqualSlices(u8, &payload, value.data);
    try std.testing.expectEqual(@as(u64, 4101), store.total_bytes.load(.monotonic));
}
