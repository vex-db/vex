// Migrated unit tests for src/config.zig.

const std = @import("std");
const ConfigFile = @import("../../src/config.zig").ConfigFile;

test "parse config file" {
    const allocator = std.testing.allocator;
    const data =
        \\# Vex configuration
        \\port 6380
        \\requirepass secret
        \\maxmemory 256mb
        \\
        \\# Empty lines and comments ignored
        \\reactor
        \\maxclients 5000
    ;

    var cfg = try ConfigFile.parse(allocator, data);
    defer cfg.deinit();

    try std.testing.expectEqualStrings("6380", cfg.get("port").?);
    try std.testing.expectEqualStrings("secret", cfg.get("requirepass").?);
    try std.testing.expectEqualStrings("256mb", cfg.get("maxmemory").?);
    try std.testing.expectEqualStrings("5000", cfg.get("maxclients").?);
    try std.testing.expectEqualStrings("", cfg.get("reactor").?); // boolean flag
    try std.testing.expect(cfg.get("nonexistent") == null);
}

test "parse empty config" {
    const allocator = std.testing.allocator;
    var cfg = try ConfigFile.parse(allocator, "");
    defer cfg.deinit();
    try std.testing.expectEqual(@as(usize, 0), cfg.entries.count());
}

test "parse config with comments only" {
    const allocator = std.testing.allocator;
    const data = "# just a comment\n# another\n";
    var cfg = try ConfigFile.parse(allocator, data);
    defer cfg.deinit();
    try std.testing.expectEqual(@as(usize, 0), cfg.entries.count());
}

test "sorted set partition count validates startup tuning" {
    const parse = @import("../../src/config.zig").parseSortedSetPartitions;
    for ([_][]const u8{ "1", "64", "256", "4096" }) |value| {
        try std.testing.expectEqual(try std.fmt.parseInt(usize, value, 10), try parse(value));
    }
    for ([_][]const u8{ "", "0", "3", "8192", "-1", "many", "99999999999999999999999999999" }) |value| {
        try std.testing.expectError(error.InvalidPartitionCount, parse(value));
    }
}
