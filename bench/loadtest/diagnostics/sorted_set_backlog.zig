//! Measure the first ordered read after every member changes, separately from
//! steady-state rank queries. Compile with the selected `sorted_set` module.
const std = @import("std");
const Store = @import("sorted_set").SortedSetStore;
fn now() i128 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
    return @as(i128, ts.sec) * 1_000_000_000 + ts.nsec;
}
pub fn main() !void {
    const allocator = std.heap.c_allocator;
    for ([_]usize{ 4096, 65536, 262144 }) |size| {
        var store = try Store.init(allocator);
        defer store.deinit();
        const buffers = try allocator.alloc([16]u8, size);
        defer allocator.free(buffers);
        const members = try allocator.alloc([]const u8, size);
        defer allocator.free(members);
        for (members, 0..) |*member, i| {
            member.* = try std.fmt.bufPrint(&buffers[i], "m{d:0>8}", .{i});
            _ = try store.zincrby("z", @floatFromInt(i), member.*);
        }
        _ = store.zrank("z", members[0]);
        // Reverse the ordering, forcing every changed member to move.
        for (members, 0..) |member, i| _ = try store.zincrby("z", -2 * @as(f64, @floatFromInt(i)), member);
        const start = now();
        const rank = store.zrank("z", members[0]).?;
        const first_ns = now() - start;
        std.debug.assert(rank == size - 1);
        const steady_start = now();
        var checksum: u64 = 0;
        for (0..10000) |i| checksum += store.zrank("z", members[i % size]).?;
        const steady_ns = now() - steady_start;
        std.debug.print("{{\"members\":{d},\"first_rank_ms\":{d:.3},\"steady_rank_ns\":{d:.3},\"checksum\":{d}}}\n", .{ size, @as(f64, @floatFromInt(first_ns)) / 1000000, @as(f64, @floatFromInt(steady_ns)) / 10000, checksum });
    }
}
