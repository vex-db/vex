const std = @import("std");
const fractional = @import("../src/engine/kv/fractional_map.zig");

const CollisionContext = struct {
    pub fn hash(_: @This(), _: u32) u64 {
        // Starts at the last bucket for both mask and multiply reduction.
        return 0xffff_ffff;
    }
    pub fn eql(_: @This(), a: u32, b: u32) bool {
        return a == b;
    }
};
const CollisionMap = fractional.HashMap(u32, u32, CollisionContext, 80);

test "fractional map keeps 80 percent load through 4096 6144 8192" {
    var map = fractional.StringHashMap(u32).init(std.testing.allocator);
    defer map.deinit();
    var names: [4916][16]u8 = undefined;
    for (&names, 0..) |*buffer, i| {
        const key = try std.fmt.bufPrint(buffer, "key-{d}", .{i});
        try map.put(key, @intCast(i));
        if (i == 3275) try std.testing.expectEqual(@as(u32, 4096), map.capacity());
        if (i == 3276 or i == 4914) try std.testing.expectEqual(@as(u32, 6144), map.capacity());
        if (i == 4915) try std.testing.expectEqual(@as(u32, 8192), map.capacity());
    }
    for (&names, 0..) |*buffer, i| {
        const key = try std.fmt.bufPrint(buffer, "key-{d}", .{i});
        try std.testing.expectEqual(@as(u32, @intCast(i)), map.getPtr(key).?.*);
    }
    try std.testing.expectEqual(@as(u32, 4916), map.count());
}

test "fractional collision cluster wraps tombstones rehash and bucket iteration" {
    var map = CollisionMap.init(std.testing.allocator);
    defer map.deinit();
    try map.ensureTotalCapacity(9);
    try std.testing.expectEqual(@as(u32, 12), map.capacity());
    for (0..9) |i| try map.put(@intCast(i), @intCast(i * 10));
    for ([_]u32{ 0, 4, 8 }) |key| {
        const removed = map.fetchRemove(key).?;
        try std.testing.expectEqual(key, removed.key);
        try std.testing.expectEqual(key * 10, removed.value);
    }
    try std.testing.expect(map.getPtr(99) == null);
    for (9..12) |i| try map.put(@intCast(i), @intCast(i * 10));
    map.unmanaged.rehash(map.ctx);
    for ([_]u32{ 1, 2, 3, 5, 6, 7, 9, 10, 11 }) |key| {
        try std.testing.expectEqual(key * 10, map.getPtr(key).?.*);
    }
    for ([_]u32{ 0, 4, 8, 99 }) |key| try std.testing.expect(map.getPtr(key) == null);

    var seen: [12]bool = @splat(false);
    var visited: u32 = 0;
    for (0..map.capacity()) |bucket| {
        if (!map.unmanaged.metadata.?[bucket].isUsed()) continue;
        var iterator = map.iterator();
        iterator.index = @intCast(bucket);
        const entry = iterator.next().?;
        const key = entry.key_ptr.*;
        try std.testing.expect(!seen[key]);
        seen[key] = true;
        try std.testing.expectEqual(key * 10, entry.value_ptr.*);
        visited += 1;
    }
    try std.testing.expectEqual(map.count(), visited);
    try std.testing.expectEqual(@as(u32, 9), visited);

    const existing = try map.getOrPut(5);
    try std.testing.expect(existing.found_existing);
    existing.value_ptr.* = 55;
    try std.testing.expectEqual(@as(u32, 55), map.getPtr(5).?.*);
    const added = try map.getOrPut(12);
    try std.testing.expect(!added.found_existing);
    added.value_ptr.* = 120;
    try std.testing.expectEqual(@as(u32, 10), map.count());
    try std.testing.expectEqual(@as(u32, 16), map.capacity());
}

test "fractional failed allocation preserves old table and permits existing overwrite" {
    const allocator = std.testing.allocator;
    var map = CollisionMap.init(allocator);
    defer map.deinit();
    try map.ensureTotalCapacity(9);
    for (0..9) |i| try map.put(@intCast(i), @intCast(i * 10));
    const old_metadata = map.unmanaged.metadata.?;
    var failing = std.testing.FailingAllocator.init(allocator, .{ .fail_index = 0 });
    {
        map.allocator = failing.allocator();
        defer map.allocator = allocator;
        try std.testing.expectError(error.OutOfMemory, map.put(9, 90));
        try std.testing.expectError(error.OutOfMemory, map.ensureTotalCapacity(100));
        try map.put(5, 55);
    }
    try std.testing.expectEqual(old_metadata, map.unmanaged.metadata.?);
    try std.testing.expectEqual(@as(u32, 12), map.capacity());
    try std.testing.expectEqual(@as(u32, 9), map.count());
    for (0..9) |i| {
        const expected: u32 = if (i == 5) 55 else @intCast(i * 10);
        try std.testing.expectEqual(expected, map.getPtr(@intCast(i)).?.*);
    }
    try std.testing.expect(map.getPtr(9) == null);

    failing = std.testing.FailingAllocator.init(allocator, .{ .fail_index = 0 });
    var empty = CollisionMap.init(failing.allocator());
    defer empty.deinit();
    try std.testing.expectError(error.OutOfMemory, empty.put(1, 1));
    try std.testing.expectEqual(@as(u32, 0), empty.count());
    try std.testing.expectEqual(@as(u32, 0), empty.capacity());
}

test "fractional deterministic churn matches a reference through growth and rehash" {
    var map = fractional.AutoHashMap(u32, u32).init(std.testing.allocator);
    defer map.deinit();
    var expected: [256]?u32 = @splat(null);
    var random: u32 = 0x8b36d921;
    for (0..4096) |i| {
        random = random *% 1664525 +% 1013904223;
        const key = (random >> 16) & 255;
        switch (random % 3) {
            0 => {
                const value: u32 = @intCast(i);
                try map.put(key, value);
                expected[key] = value;
            },
            1 => {
                const removed = map.fetchRemove(key);
                try std.testing.expectEqual(expected[key], if (removed) |entry| entry.value else null);
                expected[key] = null;
            },
            else => try std.testing.expectEqual(expected[key], if (map.getPtr(key)) |value| value.* else null),
        }
        if (i % 97 == 0 and map.capacity() != 0) map.unmanaged.rehash(map.ctx);
    }
    var count: u32 = 0;
    for (expected, 0..) |value, key| {
        try std.testing.expectEqual(value, if (map.getPtr(@intCast(key))) |actual| actual.* else null);
        if (value != null) count += 1;
    }
    try std.testing.expectEqual(count, map.count());
}
