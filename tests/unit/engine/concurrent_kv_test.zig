// Migrated unit tests for src/engine/kv/concurrent_kv.zig.

const std = @import("std");
const ConcurrentKV = @import("../../../src/engine/kv/concurrent_kv.zig").ConcurrentKV;
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
    var second_key: [1]u8 = .{'b'};
    var b: u8 = 'b';
    while (b <= 'z') : (b += 1) {
        second_key[0] = b;
        if (ConcurrentKV.stripeIndex(&second_key) == target_stripe) break;
    }
    if (b > 'z') return error.SkipZigTest; // unable to find a same-stripe pair

    store.maxmemory = 3; // fits one 2-byte entry; second triggers eviction
    store.eviction_policy = .allkeys_lru;

    const before = obs_stats.evicted_keys.load(.monotonic);

    store.cached_now_ms = 1000;
    try store.set("a", "x");
    store.cached_now_ms = 2000;
    try store.set(&second_key, "y"); // triggers eviction of "a"

    try std.testing.expect(store.get("a") == null);
    const v = store.get(&second_key) orelse return error.TestUnexpectedResult;
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

// The compact layout must retain ownership of the full allocation even when
// its logical value shrinks. std.testing.allocator checks every free and leak.
test "concurrent_kv compact buffers reuse capacity and release large shrinks" {
    try std.testing.expect(@sizeOf(ConcurrentKV.Entry) <= 104);
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
    const original = stripe.map.getPtr("k").?.value.ptr;
    try store.set("k", &medium);
    try std.testing.expectEqual(original, stripe.map.getPtr("k").?.value.ptr);
    try std.testing.expectEqual(@as(usize, 1024), stripe.map.getPtr("k").?.value_capacity);
    try store.set("k", &large);
    try std.testing.expectEqual(original, stripe.map.getPtr("k").?.value.ptr);
    try store.set("k", &small);
    try std.testing.expectEqual(@as(usize, 256), stripe.map.getPtr("k").?.value_capacity);
    try store.set("k", "42");
    try std.testing.expect(stripe.map.getPtr("k").?.flags.is_inline);
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

test "concurrent_kv failed buffer growth preserves existing value and TTL" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms = 1000;
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
    try std.testing.expectEqual(@as(usize, 1024), stale.stale_val.?.len);
    alloc.free(stale.stale_val.?);
    alloc.free(stale.stale_key.?);
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
            var bytes: [1024]u8 = undefined;
            @memset(&bytes, @intCast('a' + id));
            for (0..500) |iteration| {
                const sizes = [_]usize{ 16, 32, 33, 128, 256, 257, 768, 1024 };
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
