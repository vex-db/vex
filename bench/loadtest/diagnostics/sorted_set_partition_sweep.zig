//! Contention microbenchmark, not server QPS: no sockets, parser, or pipelining.
//! Compile with the current `sorted_set` module; CSV is written to stderr.
const std = @import("std");
const Store = @import("sorted_set").SortedSetStore;
const Atomic = std.atomic.Value;

fn now() i64 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
    return @as(i64, ts.sec) * 1_000_000_000 + ts.nsec;
}

const Job = struct {
    store: *Store,
    keys: []const []const u8,
    members: []const []const u8,
    ready: *Atomic(usize),
    start: *Atomic(i64),
    seed: u64,
    operations: usize = 0,
    writes: usize = 0,
    elapsed_ns: i64 = 0,

    fn run(self: *Job) void {
        var random = std.Random.DefaultPrng.init(self.seed);
        _ = self.ready.fetchAdd(1, .release);
        var begin: i64 = 0;
        while (begin == 0) {
            begin = self.start.load(.acquire);
            std.atomic.spinLoopHint();
        }
        const deadline = begin + 1_000_000_000;
        while (now() < deadline) {
            for (0..128) |_| {
                const key = self.keys[random.random().uintLessThan(usize, self.keys.len)];
                const member = self.members[random.random().uintLessThan(usize, self.members.len)];
                const lock = self.store.lockKey(key);
                if (self.operations % 5 == 4) {
                    if (self.store.zrank(key, member) == null) @panic("missing member");
                } else {
                    _ = self.store.zincrby(key, 1, member) catch @panic("update failed");
                    self.writes += 1;
                }
                self.store.unlockKey(lock);
                self.operations += 1;
            }
        }
        self.elapsed_ns = now() - begin;
    }
};

fn trial(partitions: usize, workers: usize, key_count: usize, member_count: usize, repeat: usize) !void {
    const allocator = std.heap.c_allocator;
    var store = try Store.initWithPartitionCount(allocator, partitions);
    defer store.deinit();
    var names_arena = std.heap.ArenaAllocator.init(allocator);
    defer names_arena.deinit();
    const names = names_arena.allocator();
    const keys = try names.alloc([]const u8, key_count);
    const members = try names.alloc([]const u8, member_count);
    for (keys, 0..) |*key, i| key.* = try std.fmt.allocPrint(names, "z:{d}", .{i});
    for (members, 0..) |*member, i| member.* = try std.fmt.allocPrint(names, "m:{d}", .{i});
    for (keys) |key| for (members) |member| {
        _ = try store.zincrby(key, 0, member);
    };
    for (keys) |key| _ = store.zrank(key, members[0]);
    var ready = Atomic(usize).init(0);
    var start = Atomic(i64).init(0);
    var jobs: [8]Job = undefined;
    var threads: [8]std.Thread = undefined;
    var spawned: usize = 0;
    errdefer {
        start.store(now(), .release);
        for (threads[0..spawned]) |thread| thread.join();
    }
    for (0..workers) |i| {
        jobs[i] = .{ .store = &store, .keys = keys, .members = members, .ready = &ready, .start = &start, .seed = 20260928 + i };
        threads[i] = try std.Thread.spawn(.{}, Job.run, .{&jobs[i]});
        spawned += 1;
    }
    while (ready.load(.acquire) != workers) std.atomic.spinLoopHint();
    start.store(now(), .release);
    for (threads[0..workers]) |thread| thread.join();
    spawned = 0;
    var operations: usize = 0;
    var writes: usize = 0;
    var elapsed: i64 = 0;
    for (jobs[0..workers]) |job| {
        operations += job.operations;
        writes += job.writes;
        elapsed = @max(elapsed, job.elapsed_ns);
    }
    var score_total: f64 = 0;
    for (keys) |key| for (members) |member| {
        score_total += store.zscore(key, member).?;
    };
    if (score_total != @as(f64, @floatFromInt(writes))) return error.LostUpdates;
    const rate = @as(f64, @floatFromInt(operations)) * 1e9 / @as(f64, @floatFromInt(elapsed));
    std.debug.print("{d},{d},{d},{d},{d},{d},{d:.0},true\n", .{ partitions, workers, key_count, member_count, repeat, operations, rate });
}

pub fn main() !void {
    std.debug.print("partitions,workers,keys,members,repeat,operations,ops_per_second,verified\n", .{});
    // Reverse counts and worker order on alternate repetitions to reduce order bias.
    for (0..3) |repeat| {
        for ([_][2]usize{ .{ 64, 4096 }, .{ 1024, 256 }, .{ 1, 4096 } }) |shape| {
            for (0..3) |wi| {
                const workers = ([_]usize{ 1, 4, 8 })[if (repeat % 2 == 0) wi else 2 - wi];
                for (0..3) |pi| {
                    const partitions = ([_]usize{ 64, 256, 1024 })[if (repeat % 2 == 0) pi else 2 - pi];
                    try trial(partitions, workers, shape[0], shape[1], repeat + 1);
                }
            }
        }
    }
}
