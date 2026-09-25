const std = @import("std");
const ConcurrentKV = @import("../src/engine/kv/concurrent_kv.zig").ConcurrentKV;

fn expectValue(store: *ConcurrentKV, key: []const u8, expected: []const u8) !void {
    const value = store.get(key) orelse return error.TestUnexpectedResult;
    defer value.deinit();
    try std.testing.expectEqualStrings(expected, value.data);
}

fn expectCombined(store: *ConcurrentKV, key: []const u8, expected: bool) !void {
    const stripe = store.getStripePublic(key);
    store.readLockStripePublic(stripe);
    defer store.readUnlockStripePublic(stripe);
    const entry = stripe.map.getPtr(key) orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(expected, entry.isCombined());
}

test "packed heap owns key and value through growth, shrink, and delete" {
    const alloc = std.testing.allocator;
    var store = ConcurrentKV.init(alloc, std.testing.io);
    store.initStripes();
    defer store.deinit();
    var value: [25]u8 = @splat('v');
    try store.set("packed", &value);
    try expectCombined(&store, "packed", true);

    var failing = std.testing.FailingAllocator.init(alloc, .{ .fail_index = 0 });
    {
        store.allocator = failing.allocator();
        defer store.allocator = alloc;
        try store.set("packed", &value); // Equal-size overwrite reuses the value capacity.
    }
    try expectValue(&store, "packed", &value);

    failing = std.testing.FailingAllocator.init(alloc, .{ .fail_index = 0 });
    {
        store.allocator = failing.allocator();
        defer store.allocator = alloc;
        try std.testing.expectError(error.OutOfMemory, store.set("packed", "short"));
    }
    try expectCombined(&store, "packed", true);
    try expectValue(&store, "packed", &value);

    try store.set("packed", "short");
    try expectCombined(&store, "packed", false);
    try store.set("packed", &value);
    try expectCombined(&store, "packed", true);
    try std.testing.expect(store.delete("packed"));
    try std.testing.expectEqual(@as(usize, 0), store.dbsize());
}

test "packed TTL expiry invalidates then releases one combined allocation" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);
    var value: [25]u8 = @splat('t');
    try store.setInternal("ttl-packed", &value, 1500);
    try expectCombined(&store, "ttl-packed", true);
    var cursor: ConcurrentKV.SweepCursor = .{};
    var removed: [512]ConcurrentKV.Expired = undefined;
    const count = store.sweepExpired(2000, &cursor, &removed);
    try std.testing.expectEqual(@as(usize, 1), count);
    store.freeExpired(removed[0]);
    try std.testing.expectEqual(@as(usize, 0), store.dbsize());
}

test "packed transitions preserve TTL and fail before touching direct heap bytes" {
    const alloc = std.testing.allocator;
    var store = ConcurrentKV.init(alloc, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);
    const old: [25]u8 = @splat('o');
    const replacement: [25]u8 = @splat('n');
    try store.set("same-size", &old);
    var failing = std.testing.FailingAllocator.init(alloc, .{ .fail_index = 0 });
    {
        store.allocator = failing.allocator();
        defer store.allocator = alloc;
        try std.testing.expectError(error.OutOfMemory, store.setInternal("same-size", &replacement, 5000));
    }
    try expectValue(&store, "same-size", &old);
    try std.testing.expectEqual(@as(?i64, -1), store.ttl("same-size"));

    try store.setInternal("ttl-shrink", &old, 5000);
    try expectCombined(&store, "ttl-shrink", true);
    try store.setInternal("ttl-shrink", "tiny", 5000);
    try expectCombined(&store, "ttl-shrink", false);
    try expectValue(&store, "ttl-shrink", "tiny");
    try std.testing.expectEqual(@as(?i64, 4), store.ttl("ttl-shrink"));
}

test "packed entry replaced by adopted storage returns one stale block" {
    const alloc = std.testing.allocator;
    var store = ConcurrentKV.init(alloc, std.testing.io);
    store.initStripes();
    defer store.deinit();
    var value: [25]u8 = @splat('p');
    try store.set("copy", &value);
    const key = try alloc.dupe(u8, "copy");
    const adopted = try alloc.dupe(u8, "adopted");
    const stale = store.setPrealloc("copy", key, adopted, 0);
    try std.testing.expect(stale.stale_key == null);
    try std.testing.expectEqual(@as(usize, "copy".len + value.len), stale.stale_val.?.len);
    alloc.free(stale.stale_val.?);
    try expectCombined(&store, "copy", false);
    try expectValue(&store, "copy", "adopted");
}

test "entry32 layout keeps boundary values intact" {
    try std.testing.expectEqual(@as(usize, 32), @sizeOf(ConcurrentKV.Entry));
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();

    var bytes: [33]u8 = undefined;
    for (&bytes, 0..) |*byte, i| byte.* = @intCast('a' + i % 26);
    for ([_]usize{ 24, 25, 32, 33 }) |len| {
        const key = try std.fmt.allocPrint(std.testing.allocator, "boundary-{d}", .{len});
        defer std.testing.allocator.free(key);
        try store.set(key, bytes[0..len]);
        try expectValue(&store, key, bytes[0..len]);
    }
    try std.testing.expectEqual(@as(usize, 0), store.metadataRequestedBytes());
}

test "entry32 metadata transitions preserve ownership and expiry" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.cached_now_ms.store(1000, .release);

    try store.set("number", "7");
    try std.testing.expectEqual(@as(i64, 8), try store.incrBy("number", 1));
    try expectValue(&store, "number", "8");
    try store.set("number", "plain");
    try expectValue(&store, "number", "plain");
    try std.testing.expectEqual(@as(i64, 1), try store.incrBy("brand-new", 1));
    try expectValue(&store, "brand-new", "1");

    try store.setInternal("ttl", "value", 1500);
    var cursor: ConcurrentKV.SweepCursor = .{};
    var removed: [512]ConcurrentKV.Expired = undefined;
    const count = store.sweepExpired(2000, &cursor, &removed);
    for (removed[0..count]) |stale| store.freeExpired(stale);
    try std.testing.expectEqual(@as(usize, 1), count);
    try std.testing.expectEqual(@as(usize, 2), store.dbsize());
}

test "entry32 lru entries and equal-size overwrites remain valid" {
    var store = ConcurrentKV.init(std.testing.allocator, std.testing.io);
    store.initStripes();
    defer store.deinit();
    store.eviction_policy = .allkeys_lru;

    var value: [256]u8 = @splat('x');
    try store.set("lru", &value);
    try std.testing.expectEqual(@as(usize, 32), store.metadataRequestedBytes());
    var failing = std.testing.FailingAllocator.init(std.testing.allocator, .{ .fail_index = 0 });
    {
        store.allocator = failing.allocator();
        defer store.allocator = std.testing.allocator;
        try store.set("lru", &value);
    }
    try expectValue(&store, "lru", &value);
}

test "entry32 metadata allocation failure leaves live and preallocated values untouched" {
    const alloc = std.testing.allocator;
    var store = ConcurrentKV.init(alloc, std.testing.io);
    store.initStripes();
    defer store.deinit();
    try store.set("k", "old");

    var failing = std.testing.FailingAllocator.init(alloc, .{ .fail_index = 0 });
    store.allocator = failing.allocator();
    try std.testing.expectError(error.OutOfMemory, store.setInternal("k", "replacement", 5000));
    store.allocator = alloc;
    try expectValue(&store, "k", "old");
    try std.testing.expectEqual(@as(?i64, -1), store.ttl("k"));

    try store.set("k2", "old");
    const before_growth_failure = store.total_bytes.load(.monotonic);
    var large: [256]u8 = @splat('x');
    failing = std.testing.FailingAllocator.init(alloc, .{ .fail_index = 1 });
    store.allocator = failing.allocator();
    try std.testing.expectError(error.OutOfMemory, store.setInternal("k2", &large, 5000));
    store.allocator = alloc;
    try expectValue(&store, "k2", "old");
    try std.testing.expectEqual(@as(?i64, -1), store.ttl("k2"));
    try std.testing.expectEqual(before_growth_failure, store.total_bytes.load(.monotonic));

    const key = try alloc.dupe(u8, "k");
    const value = try alloc.dupe(u8, "copy");
    failing = std.testing.FailingAllocator.init(alloc, .{ .fail_index = 0 });
    store.allocator = failing.allocator();
    const stale = store.setPrealloc("k", key, value, 5000);
    store.allocator = alloc;
    alloc.free(stale.stale_key.?);
    alloc.free(stale.stale_val.?);
    try expectValue(&store, "k", "old");
}
