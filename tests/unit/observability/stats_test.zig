// Migrated unit tests for src/observability/stats.zig.

const std = @import("std");
const stats = @import("../../../src/observability/stats.zig");
const cmd_table = @import("../../../src/observability/cmd_table.zig");

const WorkerStats = stats.WorkerStats;
const N_CMDS = stats.N_CMDS;
const register = stats.register;
const unregister = stats.unregister;
const resetForTest = stats.resetForTest;
const aggregateCmdCalls = stats.aggregateCmdCalls;
const totalCommands = stats.totalCommands;

test "RSS reporting reads current residency on supported platforms" {
    const os = @import("builtin").os.tag;
    if (os != .linux and os != .macos) return error.SkipZigTest;
    const resident = stats.currentRssBytes(std.testing.io) orelse return error.TestUnexpectedResult;
    try std.testing.expect(resident > 0);
    var usage: std.c.rusage = std.mem.zeroes(std.c.rusage);
    usage.maxrss = 123;
    try std.testing.expectEqual(@as(u64, if (os == .macos) 123 else 123 * 1024), stats.peakRssBytes(usage));
}

test "register and aggregate" {
    resetForTest();
    var s1 = WorkerStats.init();
    var s2 = WorkerStats.init();
    try std.testing.expect(register(&s1));
    try std.testing.expect(register(&s2));

    const get_idx = cmd_table.lookup("GET");
    const set_idx = cmd_table.lookup("SET");

    s1.recordCall(get_idx);
    s1.recordCall(get_idx);
    s1.recordCall(set_idx);
    s2.recordCall(get_idx);

    var totals: [N_CMDS]u64 = undefined;
    aggregateCmdCalls(&totals);
    try std.testing.expectEqual(@as(u64, 3), totals[get_idx]);
    try std.testing.expectEqual(@as(u64, 1), totals[set_idx]);
    try std.testing.expectEqual(@as(u64, 4), totalCommands());

    resetForTest();
}

test "register is idempotent" {
    resetForTest();
    var s = WorkerStats.init();
    try std.testing.expect(register(&s));
    try std.testing.expect(register(&s));
    s.recordCall(cmd_table.lookup("GET"));
    try std.testing.expectEqual(@as(u64, 1), totalCommands());
    resetForTest();
}

test "unregister" {
    resetForTest();
    var s1 = WorkerStats.init();
    var s2 = WorkerStats.init();
    _ = register(&s1);
    _ = register(&s2);
    s1.recordCall(0);
    s2.recordCall(0);
    try std.testing.expectEqual(@as(u64, 2), totalCommands());
    unregister(&s1);
    try std.testing.expectEqual(@as(u64, 1), totalCommands());
    resetForTest();
}

test "unknown command bucketed to OTHER" {
    resetForTest();
    var s = WorkerStats.init();
    _ = register(&s);
    s.recordCall(cmd_table.lookup("DEFINITELY_NOT_A_COMMAND"));
    var totals: [N_CMDS]u64 = undefined;
    aggregateCmdCalls(&totals);
    try std.testing.expectEqual(@as(u64, 1), totals[cmd_table.OTHER_IDX]);
    resetForTest();
}
