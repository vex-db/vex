//! Standalone diagnostic: compile with a `sorted_set` module pointing at either
//! revision. Same seeded sequence, checksum, and allocator for both versions.
const std = @import("std");
const Store = @import("sorted_set").SortedSetStore;

fn now() i128 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
    return @as(i128, ts.sec) * 1_000_000_000 + ts.nsec;
}

pub fn main() !void {
    const allocator = std.heap.c_allocator;
    for ([_]usize{ 4096, 65536 }) |size| {
        var store = try Store.init(allocator);
        defer store.deinit();
        const members = try allocator.alloc([16]u8, size);
        defer allocator.free(members);
        const names = try allocator.alloc([]const u8, size);
        defer allocator.free(names);
        for (names, 0..) |*name, i| {
            name.* = try std.fmt.bufPrint(&members[i], "member{d:0>8}", .{i});
            _ = try store.zincrby("z", @floatFromInt(i % 257), name.*);
        }
        // Exercise both write-only cost and the problematic update/rank mix.
        for ([_]bool{ false, true }) |mixed| {
            var prng = std.Random.DefaultPrng.init(20260927);
            var checksum: u64 = 0;
            const operations: usize = if (size == 4096) 100000 else 10000;
            const start = now();
            for (0..operations) |i| {
                const member = names[prng.random().uintLessThan(usize, size)];
                if (mixed and i % 5 == 4) {
                    checksum += store.zrank("z", member).?;
                } else {
                    checksum += @intFromFloat(try store.zincrby("z", 1, member));
                }
            }
            const ns: f64 = @floatFromInt(now() - start);
            std.debug.print("{{\"members\":{d},\"mixed\":{},\"operations\":{d},\"ns_per_op\":{d:.3},\"checksum\":{d}}}\n", .{ size, mixed, operations, ns / @as(f64, @floatFromInt(operations)), checksum });
        }
    }
}
