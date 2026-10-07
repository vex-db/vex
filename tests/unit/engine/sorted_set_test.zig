// Migrated unit tests for src/engine/types/sorted_set.zig.

const std = @import("std");
const SortedSetStore = @import("../../../src/engine/types/sorted_set.zig").SortedSetStore;

test "ZADD and ZSCORE" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();

    const added = try store.zadd("lb", &[_][]const u8{ "10", "alice", "20", "bob", "15", "carol" });
    try std.testing.expectEqual(@as(usize, 3), added);
    try std.testing.expectEqual(@as(f64, 10.0), store.zscore("lb", "alice").?);
    try std.testing.expectEqual(@as(f64, 20.0), store.zscore("lb", "bob").?);
    try std.testing.expect(store.zscore("lb", "missing") == null);
}

test "ZADD updates score" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();

    _ = try store.zadd("z", &[_][]const u8{ "10", "a" });
    const added = try store.zadd("z", &[_][]const u8{ "99", "a" });
    try std.testing.expectEqual(@as(usize, 0), added); // not new
    try std.testing.expectEqual(@as(f64, 99.0), store.zscore("z", "a").?);
}

test "ZREM" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();

    _ = try store.zadd("z", &[_][]const u8{ "1", "a", "2", "b", "3", "c" });
    const removed = store.zrem("z", &[_][]const u8{ "a", "x" });
    try std.testing.expectEqual(@as(usize, 1), removed);
    try std.testing.expectEqual(@as(usize, 2), store.zcard("z"));
}

test "ZRANGE" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();

    _ = try store.zadd("z", &[_][]const u8{ "30", "c", "10", "a", "20", "b" });
    const range = try store.zrange("z", 0, -1, std.testing.allocator);
    defer std.testing.allocator.free(range);
    try std.testing.expectEqual(@as(usize, 3), range.len);
    try std.testing.expectEqualStrings("a", range[0].member); // score 10
    try std.testing.expectEqualStrings("b", range[1].member); // score 20
    try std.testing.expectEqualStrings("c", range[2].member); // score 30

    // Sub-range
    const sub = try store.zrange("z", 0, 1, std.testing.allocator);
    defer std.testing.allocator.free(sub);
    try std.testing.expectEqual(@as(usize, 2), sub.len);
}

test "ZRANK" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();

    _ = try store.zadd("z", &[_][]const u8{ "30", "c", "10", "a", "20", "b" });
    const rank_a = store.zrank("z", "a");
    try std.testing.expectEqual(@as(usize, 0), rank_a.?);
    const rank_c = store.zrank("z", "c");
    try std.testing.expectEqual(@as(usize, 2), rank_c.?);
    const rank_x = store.zrank("z", "missing");
    try std.testing.expect(rank_x == null);
}

test "ZINCRBY" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();

    const s1 = try store.zincrby("z", 5.0, "player");
    try std.testing.expectEqual(@as(f64, 5.0), s1);
    const s2 = try store.zincrby("z", 3.0, "player");
    try std.testing.expectEqual(@as(f64, 8.0), s2);
}

test "ZCOUNT" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();

    _ = try store.zadd("z", &[_][]const u8{ "1", "a", "5", "b", "10", "c", "15", "d" });
    try std.testing.expectEqual(@as(usize, 2), store.zcount("z", 5, 10));
    try std.testing.expectEqual(@as(usize, 4), store.zcount("z", 0, 100));
    try std.testing.expectEqual(@as(usize, 0), store.zcount("z", 50, 100));
}

test "empty after ZREM auto-deletes" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();

    _ = try store.zadd("tmp", &[_][]const u8{ "1", "x" });
    _ = store.zrem("tmp", &[_][]const u8{"x"});
    try std.testing.expect(!store.exists("tmp"));
}

fn checkIndex(node: anytype) !void {
    const n = node orelse return;
    try checkIndex(n.left);
    try checkIndex(n.right);
    const left_height: i32 = if (n.left) |left| left.height else 0;
    const right_height: i32 = if (n.right) |right| right.height else 0;
    const left_size: usize = if (n.left) |left| left.size else 0;
    const right_size: usize = if (n.right) |right| right.size else 0;
    try std.testing.expect(@abs(left_height - right_height) <= 1);
    try std.testing.expectEqual(1 + @max(left_height, right_height), n.height);
    try std.testing.expectEqual(1 + left_size + right_size, n.size);
}

// Compare against a deliberately simple independent model after mixed writes,
// removals, and ties. Sorted insertions first exercise worst-case input order.
test "sorted set indexed ranks match a reference model after mixed mutations" {
    const alloc = std.testing.allocator;
    var store = try SortedSetStore.init(alloc);
    defer store.deinit();
    var names: [128][8]u8 = undefined;
    var members: [128][]const u8 = undefined;
    var scores: [128]?f64 = @splat(null);
    for (&members, 0..) |*member, i| {
        member.* = try std.fmt.bufPrint(&names[i], "m{d:0>4}", .{i});
        scores[i] = try store.zincrby("z", @floatFromInt(i), member.*);
    }
    var random = std.Random.DefaultPrng.init(20260927);
    for (0..2000) |step| {
        const i = random.random().uintLessThan(usize, members.len);
        if (step % 4 == 0) {
            _ = store.zrem("z", &.{members[i]});
            scores[i] = null;
        } else {
            const delta: f64 = @floatFromInt(@as(i32, @intCast(random.random().uintLessThan(u32, 33))) - 16);
            const expected = (scores[i] orelse 0) + delta;
            try std.testing.expectEqual(expected, try store.zincrby("z", delta, members[i]));
            scores[i] = expected;
        }
        // Exercise ZADD replacements independently of the increment path.
        if (step % 11 == 0) {
            _ = try store.zadd("z", &.{ "0", members[i] });
            scores[i] = 0;
        }
        if (step % 17 != 0 and step != 1999) continue;
        try checkIndex(store.partitions[store.partitionIndex("z")].map.getPtr("z").?.root);
        var reference: [128]SortedSetStore.Entry = undefined;
        var count: usize = 0;
        var in_range: usize = 0;
        for (scores, members) |score, member| {
            if (score) |value| {
                reference[count] = .{ .score = value, .member = member };
                count += 1;
                if (value >= -5 and value <= 20) in_range += 1;
            } else try std.testing.expectEqual(@as(?usize, null), store.zrank("z", member));
        }
        std.mem.sort(SortedSetStore.Entry, reference[0..count], {}, struct {
            fn less(_: void, a: SortedSetStore.Entry, b: SortedSetStore.Entry) bool {
                return a.score < b.score or (a.score == b.score and std.mem.order(u8, a.member, b.member) == .lt);
            }
        }.less);
        try std.testing.expectEqual(count, store.zcard("z"));
        try std.testing.expectEqual(in_range, store.zcount("z", -5, 20));
        const actual = try store.zrange("z", 0, -1, alloc);
        defer alloc.free(actual);
        try std.testing.expectEqual(count, actual.len);
        for (reference[0..count], actual, 0..) |expected, entry, rank| {
            try std.testing.expectEqualStrings(expected.member, entry.member);
            try std.testing.expectEqual(expected.score, entry.score);
            try std.testing.expectEqual(rank, store.zrank("z", entry.member).?);
        }
        const tail = try store.zrange("z", -3, -1, alloc);
        defer alloc.free(tail);
        for (tail, 0..) |entry, offset| try std.testing.expectEqualStrings(reference[count - tail.len + offset].member, entry.member);
    }
    for (members) |member| _ = store.zrem("z", &.{member});
    try std.testing.expect(!store.exists("z"));
    _ = try store.zadd("z", &.{ "1", "new" });
    store.flush();
    try std.testing.expect(!store.exists("z"));
}

test "sorted set rejects NaN without corrupting ordering" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();
    _ = try store.zadd("z", &.{ "1", "a", "inf", "b" });
    try std.testing.expectError(error.InvalidScore, store.zadd("z", &.{ "2", "a", "nan", "c" }));
    try std.testing.expectEqual(@as(f64, 1), store.zscore("z", "a").?);
    try std.testing.expectError(error.InvalidScore, store.zincrby("z", -std.math.inf(f64), "b"));
    try std.testing.expectEqual(@as(usize, 1), store.zrank("z", "b").?);
    try std.testing.expectEqual(@as(usize, 2), store.zcount("z", -std.math.inf(f64), std.math.inf(f64)));
}

fn allocationExercise(allocator: std.mem.Allocator) !void {
    var store = try SortedSetStore.init(allocator);
    defer store.deinit();
    _ = try store.zadd("z", &.{ "1", "a", "2", "b" });
    _ = try store.zincrby("z", 3, "c");
    _ = try store.zincrby("z", 5, "a");
    try std.testing.expectEqual(@as(usize, 2), store.zrank("z", "a").?);
    const entries = try store.zrange("z", 0, -1, allocator);
    defer allocator.free(entries);
    _ = store.zrem("z", &.{"b"});
}

test "sorted set allocation failures leave owned indexes safe to destroy" {
    try std.testing.checkAllAllocationFailures(std.testing.allocator, allocationExercise, .{});
}

test "sorted set existing-member updates and ranks do not allocate" {
    var failing = std.testing.FailingAllocator.init(std.testing.allocator, .{});
    var store = try SortedSetStore.init(failing.allocator());
    defer store.deinit();
    _ = try store.zadd("z", &.{ "1", "a", "2", "b" });
    failing.fail_index = failing.alloc_index;
    failing.resize_fail_index = failing.resize_index;
    _ = try store.zincrby("z", 2, "a");
    try std.testing.expectEqual(@as(usize, 1), store.zrank("z", "a").?);
    _ = try store.zadd("z", &.{ "0", "a" });
    try std.testing.expectEqual(@as(usize, 0), store.zrank("z", "a").?);
}

test "sorted set bulk index rebuild and allocation-free fallback agree" {
    for ([_]bool{ false, true }) |fail_scratch| {
        var failing = std.testing.FailingAllocator.init(std.testing.allocator, .{});
        var store = try SortedSetStore.init(failing.allocator());
        defer store.deinit();
        var buf: [32]u8 = undefined;
        for (0..4096) |i| {
            const member = try std.fmt.bufPrint(&buf, "m{d:0>5}", .{i});
            _ = try store.zincrby("z", @floatFromInt(i), member);
        }
        for (0..4096) |i| {
            const member = try std.fmt.bufPrint(&buf, "m{d:0>5}", .{i});
            _ = try store.zincrby("z", -1 - 2 * @as(f64, @floatFromInt(i)), member);
        }
        if (fail_scratch) failing.fail_index = failing.alloc_index;
        try std.testing.expectEqual(@as(usize, 4095), store.zrank("z", "m00000").?);
        if (fail_scratch) try std.testing.expect(failing.has_induced_failure);
        try checkIndex(store.partitions[store.partitionIndex("z")].map.getPtr("z").?.root);
        for (0..4096) |i| {
            const member = try std.fmt.bufPrint(&buf, "m{d:0>5}", .{i});
            try std.testing.expectEqual(4095 - i, store.zrank("z", member).?);
            try std.testing.expectEqual(-1 - @as(f64, @floatFromInt(i)), store.zscore("z", member).?);
        }
        // Nodes can be updated/removed again after either synchronization path.
        _ = store.zrem("z", &.{"m00000"});
        _ = try store.zincrby("z", 8192, "m00001");
        try std.testing.expectEqual(@as(usize, 4094), store.zrank("z", "m00001").?);
        try checkIndex(store.partitions[store.partitionIndex("z")].map.getPtr("z").?.root);
    }
}

test "sorted set database flush preserves other databases across partitions" {
    var store = try SortedSetStore.init(std.testing.allocator);
    defer store.deinit();
    for (0..128) |i| {
        var buf: [32]u8 = undefined;
        _ = try store.zadd(try std.fmt.bufPrint(&buf, "db:0:z{d}", .{i}), &.{ "1", "m" });
        _ = try store.zadd(try std.fmt.bufPrint(&buf, "db:1:z{d}", .{i}), &.{ "2", "m" });
    }
    store.lockAll();
    store.flushDb(0);
    store.flushDb(0);
    store.unlockAll();
    for (0..128) |i| {
        var buf: [32]u8 = undefined;
        try std.testing.expect(!store.exists(try std.fmt.bufPrint(&buf, "db:0:z{d}", .{i})));
        try std.testing.expectEqual(@as(?f64, 2), store.zscore(try std.fmt.bufPrint(&buf, "db:1:z{d}", .{i}), "m"));
    }
}

test "runtime partition counts preserve lookup locking and flush" {
    for ([_]usize{ 1, 64, 256, 1024, 4096 }) |count| {
        var store = try SortedSetStore.initWithPartitionCount(std.testing.allocator, count);
        defer store.deinit();
        try std.testing.expectEqual(count, store.partitions.len);
        var key_buf: [32]u8 = undefined;
        for (0..128) |i| {
            const key = try std.fmt.bufPrint(&key_buf, "db:{d}:z:{d}", .{ i % 2, i });
            const index = store.lockKey(key);
            defer store.unlockKey(index);
            try std.testing.expectEqual(std.hash.Wyhash.hash(0, key) % count, index);
            _ = try store.zincrby(key, 2, "member");
            try std.testing.expectEqual(@as(f64, 2), store.zscore(key, "member").?);
        }
        store.lockAll();
        defer store.unlockAll();
        store.flushDb(0);
        try std.testing.expect(!store.exists("db:0:z:0"));
        try std.testing.expectEqual(@as(f64, 2), store.zscore("db:1:z:1", "member").?);
        store.flush();
        try std.testing.expect(!store.exists("db:1:z:1"));
    }
    for ([_]usize{ 0, 3, 8192 }) |count| {
        try std.testing.expectError(error.InvalidPartitionCount, SortedSetStore.initWithPartitionCount(std.testing.allocator, count));
    }
}
