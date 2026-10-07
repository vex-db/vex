//! Redis key semantics, specified before the cross-store lifecycle repair.
//! Both handler backends run the same contract. This does not exercise Worker
//! fast dispatch or prove concurrent linearizability; those need network tests.
const std = @import("std");
const CommandHandler = @import("../../../src/command/handler.zig").CommandHandler;
const KVStore = @import("../../../src/engine/kv/kv.zig").KVStore;
const ConcurrentKV = @import("../../../src/engine/kv/concurrent_kv.zig").ConcurrentKV;
const GraphEngine = @import("../../../src/engine/graph/graph.zig").GraphEngine;
const HashStore = @import("../../../src/engine/types/hash.zig").HashStore;
const ListStore = @import("../../../src/engine/types/list.zig").ListStore;
const SetStore = @import("../../../src/engine/types/set.zig").SetStore;
const SortedSetStore = @import("../../../src/engine/types/sorted_set.zig").SortedSetStore;
const a = std.testing.allocator;
const Args = []const []const u8;
const Backend = enum { legacy, concurrent };
const backends = [_]Backend{ .legacy, .concurrent };
const Kind = enum { string, hash, list, set, zset };
const kinds = [_]Kind{ .string, .hash, .list, .set, .zset };
const Step = struct { args: Args, reply: []const u8 };

const Fixture = struct {
    kv: KVStore,
    ckv: ConcurrentKV,
    graph: GraphEngine,
    hashes: HashStore,
    lists: ListStore,
    sets: SetStore,
    zsets: SortedSetStore,
    db: std.atomic.Value(u8),
    handler: CommandHandler,
    backend: Backend,
    failures: usize = 0,

    fn create(backend: Backend) !*Fixture {
        return createWithPartitionCount(backend, SortedSetStore.default_partition_count);
    }

    fn createWithPartitionCount(backend: Backend, count: usize) !*Fixture {
        // Locks and handler pointers must stay at their final addresses.
        const f = try a.create(Fixture);
        errdefer a.destroy(f);
        var zsets = try SortedSetStore.initWithPartitionCount(a, count);
        errdefer zsets.deinit();
        var lists = try ListStore.initWithPartitionCount(a, count);
        errdefer lists.deinit();
        var sets = try SetStore.initWithPartitionCount(a, count);
        errdefer sets.deinit();
        f.* = .{
            .kv = KVStore.init(a, std.testing.io),
            .ckv = ConcurrentKV.init(a, std.testing.io),
            .graph = GraphEngine.init(a),
            .hashes = HashStore.init(a),
            .lists = lists,
            .sets = sets,
            .zsets = zsets,
            .db = std.atomic.Value(u8).init(0),
            .handler = undefined,
            .backend = backend,
        };
        f.ckv.initStripes();
        f.hashes.initStripes();
        f.handler = CommandHandler.init(a, std.testing.io, &f.kv, &f.graph, null, &f.db, .strict);
        f.handler.list_store = &f.lists;
        f.handler.hash_store = &f.hashes;
        f.handler.set_store = &f.sets;
        f.handler.sorted_set_store = &f.zsets;
        if (backend == .concurrent) f.handler.ckv = &f.ckv;
        return f;
    }

    fn destroy(f: *Fixture) void {
        f.handler.kvGetCleanup();
        f.zsets.deinit();
        f.sets.deinit();
        f.lists.deinit();
        f.hashes.deinit();
        f.graph.deinit();
        f.ckv.deinit();
        f.kv.deinit();
        a.destroy(f);
    }

    fn exec(f: *Fixture, args: Args) ![]u8 {
        var list: std.ArrayList(u8) = .empty;
        var out = std.Io.Writer.Allocating.fromArrayList(a, &list);
        defer out.deinit();
        defer f.handler.kvGetCleanup();
        try f.handler.execute(args, &out.writer);
        return a.dupe(u8, out.written());
    }

    fn check(f: *Fixture, args: Args, expected: []const u8) !void {
        const actual = try f.exec(args);
        defer a.free(actual);
        // Error wording can differ; the RESP error class cannot.
        const ok = if (std.mem.eql(u8, expected, "-ERR ") or std.mem.eql(u8, expected, "-WRONGTYPE "))
            std.mem.startsWith(u8, actual, expected)
        else
            std.mem.eql(u8, expected, actual);
        if (!ok) {
            f.failures += 1;
            const command = try std.mem.join(a, " ", args);
            defer a.free(command);
            std.debug.print("\n[{s}] {s}: expected {s}, got {s}\n", .{ @tagName(f.backend), command, expected, actual });
        }
    }

    fn seed(f: *Fixture, kind: Kind) !void {
        const step: Step = switch (kind) {
            .string => .{ .args = &.{ "SET", "k", "7" }, .reply = "+OK\r\n" },
            .hash => .{ .args = &.{ "HSET", "k", "f", "7" }, .reply = ":1\r\n" },
            .list => .{ .args = &.{ "RPUSH", "k", "7" }, .reply = ":1\r\n" },
            .set => .{ .args = &.{ "SADD", "k", "7" }, .reply = ":1\r\n" },
            .zset => .{ .args = &.{ "ZADD", "k", "7", "m" }, .reply = ":1\r\n" },
        };
        try f.check(step.args, step.reply);
    }

    fn unchanged(f: *Fixture, kind: Kind) !void {
        switch (kind) {
            .string => try f.check(&.{ "GET", "k" }, "$1\r\n7\r\n"),
            .hash => try f.check(&.{ "HGETALL", "k" }, "*2\r\n$1\r\nf\r\n$1\r\n7\r\n"),
            .list => try f.check(&.{ "LRANGE", "k", "0", "-1" }, "*1\r\n$1\r\n7\r\n"),
            .set => try f.check(&.{ "SMEMBERS", "k" }, "*1\r\n$1\r\n7\r\n"),
            .zset => {
                try f.check(&.{ "ZRANGE", "k", "0", "-1" }, "*1\r\n$1\r\nm\r\n");
                try f.check(&.{ "ZCOUNT", "k", "7", "7" }, ":1\r\n");
            },
        }
    }

    fn onlyStore(f: *Fixture, expected: Kind) void {
        // A correct error must not leave an invisible entry in another store.
        // Wrong-type cases run in DB 0, using the internal db:0: namespace.
        const present = [_]bool{
            if (f.backend == .concurrent) f.ckv.exists("db:0:k") else f.kv.exists("db:0:k"),
            f.hashes.exists("db:0:k"),
            f.lists.exists("db:0:k"),
            f.sets.exists("db:0:k"),
            f.zsets.exists("db:0:k"),
        };
        for (present, kinds) |exists, kind| {
            if (exists != (kind == expected)) {
                f.failures += 1;
                std.debug.print("\n[{s}] key should occupy only {s}; {s} presence is {}\n", .{
                    @tagName(f.backend), @tagName(expected), @tagName(kind), exists,
                });
            }
        }
    }

    fn absent(f: *Fixture) !void {
        try f.check(&.{ "TYPE", "k" }, "+none\r\n");
        try f.check(&.{ "EXISTS", "k" }, ":0\r\n");
        try f.check(&.{ "TTL", "k" }, ":-2\r\n");
        try f.check(&.{ "PTTL", "k" }, ":-2\r\n");
        // Check every store through public commands: TYPE alone can hide ghosts.
        try f.check(&.{ "GET", "k" }, "$-1\r\n");
        try f.check(&.{ "HLEN", "k" }, ":0\r\n");
        try f.check(&.{ "LLEN", "k" }, ":0\r\n");
        try f.check(&.{ "SCARD", "k" }, ":0\r\n");
        try f.check(&.{ "ZCARD", "k" }, ":0\r\n");
    }
};

fn scenario(steps: []const Step) !void {
    var failures: usize = 0;
    for (backends) |backend| {
        const f = try Fixture.create(backend);
        defer f.destroy();
        for (steps) |step| try f.check(step.args, step.reply);
        failures += f.failures;
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

fn wrongTypes(wanted: Kind, commands: []const Args) !void {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |existing| {
            if (existing == wanted) continue;
            for (commands) |args| {
                const f = try Fixture.create(backend);
                defer f.destroy();
                try f.seed(existing);
                try f.check(args, "-WRONGTYPE ");
                // Continue even if the response was wrong, to expose mutation.
                try f.unchanged(existing);
                f.onlyStore(existing);
                failures += f.failures;
            }
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: string commands reject every non-string type without mutation" {
    try wrongTypes(.string, &.{
        &.{ "GET", "k" },         &.{ "INCR", "k" },                &.{ "DECR", "k" },
        &.{ "INCRBY", "k", "2" }, &.{ "DECRBY", "k", "2" },         &.{ "APPEND", "k", "x" },
        &.{ "STRLEN", "k" },      &.{ "GETRANGE", "k", "0", "-1" }, &.{ "SETRANGE", "k", "0", "x" },
    });
}

test "key contract: hash commands reject every non-hash type without mutation" {
    try wrongTypes(.hash, &.{
        &.{ "HSET", "k", "f", "9" },        &.{ "HGET", "k", "f" },
        &.{ "HDEL", "k", "f" },             &.{ "HEXISTS", "k", "f" },
        &.{ "HLEN", "k" },                  &.{ "HGETALL", "k" },
        &.{ "HKEYS", "k" },                 &.{ "HVALS", "k" },
        &.{ "HMGET", "k", "f", "missing" }, &.{ "HMSET", "k", "f", "9" },
        &.{ "HINCRBY", "k", "f", "1" },
    });
}

test "key contract: list commands reject every non-list type without mutation" {
    try wrongTypes(.list, &.{
        &.{ "LPUSH", "k", "9" },      &.{ "RPUSH", "k", "9" },
        &.{ "LPOP", "k" },            &.{ "RPOP", "k" },
        &.{ "LLEN", "k" },            &.{ "LRANGE", "k", "0", "-1" },
        &.{ "LINDEX", "k", "0" },     &.{ "LSET", "k", "0", "9" },
        &.{ "LTRIM", "k", "0", "0" },
    });
}

test "key contract: set commands reject every non-set type without mutation" {
    try wrongTypes(.set, &.{
        &.{ "SADD", "k", "9" },         &.{ "SREM", "k", "7" },
        &.{ "SISMEMBER", "k", "7" },    &.{ "SCARD", "k" },
        &.{ "SMEMBERS", "k" },          &.{ "SUNION", "missing", "k" },
        &.{ "SINTER", "missing", "k" }, &.{ "SDIFF", "missing", "k" },
    });
}

test "key contract: sorted-set commands reject every other type without mutation" {
    try wrongTypes(.zset, &.{
        &.{ "ZADD", "k", "9", "m" },         &.{ "ZREM", "k", "m" },
        &.{ "ZINCRBY", "k", "2", "m" },      &.{ "ZSCORE", "k", "m" },
        &.{ "ZRANK", "k", "m" },             &.{ "ZREVRANK", "k", "m" },
        &.{ "ZCARD", "k" },                  &.{ "ZRANGE", "k", "0", "-1" },
        &.{ "ZCOUNT", "k", "-inf", "+inf" },
    });
}

test "key contract: TYPE EXISTS DBSIZE and KEYS recognize each live type" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |kind| {
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            const reply = try std.fmt.allocPrint(a, "+{s}\r\n", .{@tagName(kind)});
            defer a.free(reply);
            try f.check(&.{ "TYPE", "k" }, reply);
            try f.check(&.{ "EXISTS", "k", "k", "missing" }, ":2\r\n");
            try f.check(&.{"DBSIZE"}, ":1\r\n");
            try f.check(&.{ "KEYS", "*" }, "*1\r\n$1\r\nk\r\n");
            try f.check(&.{ "TTL", "k" }, ":-1\r\n");
            try f.check(&.{ "PTTL", "k" }, ":-1\r\n");
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: DEL counts keys once clears every store and permits every replacement type" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |before| {
            for (kinds) |after| {
                const f = try Fixture.create(backend);
                defer f.destroy();
                try f.seed(before);
                try f.check(&.{ "DEL", "missing", "k", "k" }, ":1\r\n");
                try f.absent();
                try f.check(&.{ "DEL", "k" }, ":0\r\n");
                try f.seed(after);
                try f.unchanged(after);
                failures += f.failures;
            }
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: SET replaces each type and discarded collections never reappear" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |kind| {
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            try f.check(&.{ "SET", "k", "replacement" }, "+OK\r\n");
            try f.check(&.{ "TYPE", "k" }, "+string\r\n");
            try f.check(&.{ "GET", "k" }, "$11\r\nreplacement\r\n");
            try f.check(&.{ "DEL", "k" }, ":1\r\n");
            try f.absent();
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: SET NX and XX consider keys in every store" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |kind| {
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            try f.check(&.{ "SET", "k", "9", "NX" }, "$-1\r\n");
            try f.unchanged(kind);
            try f.check(&.{ "SET", "k", "9", "XX" }, "+OK\r\n");
            try f.check(&.{ "GET", "k" }, "$1\r\n9\r\n");
            try f.check(&.{ "SET", "missing", "9", "XX" }, "$-1\r\n");
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: MGET returns nil for non-string keys" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |kind| {
            if (kind == .string) continue;
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            try f.check(&.{ "SET", "s", "v" }, "+OK\r\n");
            try f.check(&.{ "MGET", "k", "s", "missing" }, "*3\r\n$-1\r\n$1\r\nv\r\n$-1\r\n");
            try f.unchanged(kind);
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: immediate expiry removes every type synchronously" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |kind| {
            for ([_][]const u8{ "EXPIRE", "PEXPIRE" }) |cmd| {
                for ([_][]const u8{ "0", "-1" }) |ttl| {
                    const f = try Fixture.create(backend);
                    defer f.destroy();
                    try f.seed(kind);
                    try f.check(&.{ cmd, "k", ttl }, ":1\r\n");
                    try f.absent();
                    try f.check(&.{ cmd, "k", ttl }, ":0\r\n");
                    try f.seed(kind);
                    try f.check(&.{ "PTTL", "k" }, ":-1\r\n");
                    failures += f.failures;
                }
            }
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

fn liveTtl(f: *Fixture) !i64 {
    const reply = try f.exec(&.{ "PTTL", "k" });
    defer a.free(reply);
    if (reply.len < 4 or reply[0] != ':') return error.InvalidTtlReply;
    const value = try std.fmt.parseInt(i64, reply[1 .. reply.len - 2], 10);
    if (value <= 0 or value > 60000) {
        f.failures += 1;
        std.debug.print("\n[{s}] expected live PTTL in (0, 60000], got {d}\n", .{ @tagName(f.backend), value });
    }
    return value;
}

test "key contract: expiry and PERSIST apply to every type without changing contents" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |kind| {
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            try f.check(&.{ "PERSIST", "k" }, ":0\r\n");
            try f.check(&.{ "PEXPIRE", "k", "60000" }, ":1\r\n");
            _ = try liveTtl(f);
            try f.unchanged(kind);
            try f.check(&.{ "PERSIST", "k" }, ":1\r\n");
            try f.check(&.{ "PTTL", "k" }, ":-1\r\n");
            try f.check(&.{ "PERSIST", "k" }, ":0\r\n");
            try f.unchanged(kind);
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: missing reads and removals never create collection keys" {
    try scenario(&.{
        .{ .args = &.{ "HGET", "k", "f" }, .reply = "$-1\r\n" },
        .{ .args = &.{ "HDEL", "k", "f" }, .reply = ":0\r\n" },
        .{ .args = &.{ "HGETALL", "k" }, .reply = "*0\r\n" },
        .{ .args = &.{ "HMGET", "k", "a", "b" }, .reply = "*2\r\n$-1\r\n$-1\r\n" },
        .{ .args = &.{ "LPOP", "k" }, .reply = "$-1\r\n" },
        .{ .args = &.{ "RPOP", "k" }, .reply = "$-1\r\n" },
        .{ .args = &.{ "LRANGE", "k", "0", "-1" }, .reply = "*0\r\n" },
        .{ .args = &.{ "LTRIM", "k", "0", "-1" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "SREM", "k", "m" }, .reply = ":0\r\n" },
        .{ .args = &.{ "SMEMBERS", "k" }, .reply = "*0\r\n" },
        .{ .args = &.{ "ZREM", "k", "m" }, .reply = ":0\r\n" },
        .{ .args = &.{ "ZSCORE", "k", "m" }, .reply = "$-1\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+none\r\n" },
        .{ .args = &.{ "EXISTS", "k" }, .reply = ":0\r\n" },
        .{ .args = &.{"DBSIZE"}, .reply = ":0\r\n" },
        .{ .args = &.{ "TTL", "k" }, .reply = ":-2\r\n" },
        .{ .args = &.{ "PTTL", "k" }, .reply = ":-2\r\n" },
        .{ .args = &.{ "PERSIST", "k" }, .reply = ":0\r\n" },
    });
}

test "key contract: HSET empty values and duplicate fields retain Redis return counts" {
    try scenario(&.{
        .{ .args = &.{ "HSET", "k", "f", "" }, .reply = ":1\r\n" },
        .{ .args = &.{ "HGET", "k", "f" }, .reply = "$0\r\n\r\n" },
        .{ .args = &.{ "HSET", "k", "f", "7" }, .reply = ":0\r\n" },
        .{ .args = &.{ "HSET", "k", "g", "1", "g", "2" }, .reply = ":1\r\n" },
        .{ .args = &.{ "HLEN", "k" }, .reply = ":2\r\n" },
        .{ .args = &.{ "HGET", "k", "g" }, .reply = "$1\r\n2\r\n" },
    });
}

test "key contract: HDEL removes the last field and allows a different key type" {
    try scenario(&.{
        .{ .args = &.{ "HSET", "k", "a", "1", "b", "2" }, .reply = ":2\r\n" },
        .{ .args = &.{ "HDEL", "k", "a", "a", "missing" }, .reply = ":1\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+hash\r\n" },
        .{ .args = &.{ "HGET", "k", "b" }, .reply = "$1\r\n2\r\n" },
        .{ .args = &.{ "HDEL", "k", "b" }, .reply = ":1\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+none\r\n" },
        .{ .args = &.{ "EXISTS", "k" }, .reply = ":0\r\n" },
        .{ .args = &.{ "INCR", "k" }, .reply = ":1\r\n" },
    });
}

test "key contract: LPOP removes the final element and allows another type" {
    try scenario(&.{
        .{ .args = &.{ "RPUSH", "k", "a", "b" }, .reply = ":2\r\n" },
        .{ .args = &.{ "LPOP", "k" }, .reply = "$1\r\na\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+list\r\n" },
        .{ .args = &.{ "LPOP", "k" }, .reply = "$1\r\nb\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+none\r\n" },
        .{ .args = &.{ "EXISTS", "k" }, .reply = ":0\r\n" },
        .{ .args = &.{ "SADD", "k", "m" }, .reply = ":1\r\n" },
    });
}

test "key contract: RPOP removes the final element and allows another type" {
    try scenario(&.{
        .{ .args = &.{ "RPUSH", "k", "a", "b" }, .reply = ":2\r\n" },
        .{ .args = &.{ "RPOP", "k" }, .reply = "$1\r\nb\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+list\r\n" },
        .{ .args = &.{ "RPOP", "k" }, .reply = "$1\r\na\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+none\r\n" },
        .{ .args = &.{ "EXISTS", "k" }, .reply = ":0\r\n" },
        .{ .args = &.{ "SADD", "k", "m" }, .reply = ":1\r\n" },
    });
}

test "key contract: LTRIM to an empty range deletes the list" {
    try scenario(&.{
        .{ .args = &.{ "RPUSH", "k", "a" }, .reply = ":1\r\n" },
        .{ .args = &.{ "LTRIM", "k", "1", "0" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+none\r\n" },
        .{ .args = &.{ "EXISTS", "k" }, .reply = ":0\r\n" },
        .{ .args = &.{ "HSET", "k", "f", "v" }, .reply = ":1\r\n" },
    });
}

test "key contract: SREM duplicates count once and last member deletes the set" {
    try scenario(&.{
        .{ .args = &.{ "SADD", "k", "a", "a", "b" }, .reply = ":2\r\n" },
        .{ .args = &.{ "SREM", "k", "a", "a", "missing" }, .reply = ":1\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+set\r\n" },
        .{ .args = &.{ "SISMEMBER", "k", "b" }, .reply = ":1\r\n" },
        .{ .args = &.{ "SREM", "k", "b" }, .reply = ":1\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+none\r\n" },
        .{ .args = &.{ "EXISTS", "k" }, .reply = ":0\r\n" },
        .{ .args = &.{ "RPUSH", "k", "v" }, .reply = ":1\r\n" },
    });
}

test "key contract: ZREM last member deletes the sorted set" {
    try scenario(&.{
        .{ .args = &.{ "ZADD", "k", "1", "a", "2", "b" }, .reply = ":2\r\n" },
        .{ .args = &.{ "ZREM", "k", "a", "a" }, .reply = ":1\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+zset\r\n" },
        .{ .args = &.{ "ZREM", "k", "b" }, .reply = ":1\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+none\r\n" },
        .{ .args = &.{ "EXISTS", "k" }, .reply = ":0\r\n" },
        .{ .args = &.{ "HSET", "k", "f", "v" }, .reply = ":1\r\n" },
    });
}

test "key contract: malformed multi-field HSET is atomic" {
    try scenario(&.{
        .{ .args = &.{ "HSET", "k", "f", "7" }, .reply = ":1\r\n" },
        .{ .args = &.{ "HSET", "k", "f", "9", "orphan" }, .reply = "-ERR " },
        .{ .args = &.{ "HGETALL", "k" }, .reply = "*2\r\n$1\r\nf\r\n$1\r\n7\r\n" },
    });
}

test "key contract: invalid list index never mutates the list" {
    try scenario(&.{
        .{ .args = &.{ "RPUSH", "k", "7" }, .reply = ":1\r\n" },
        .{ .args = &.{ "LSET", "k", "bad", "9" }, .reply = "-ERR " },
        .{ .args = &.{ "LSET", "k", "2", "9" }, .reply = "-ERR " },
        .{ .args = &.{ "LRANGE", "k", "0", "-1" }, .reply = "*1\r\n$1\r\n7\r\n" },
    });
}

test "key contract: integer boundary errors preserve stored strings" {
    try scenario(&.{
        .{ .args = &.{ "SET", "k", "9223372036854775807" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "INCR", "k" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$19\r\n9223372036854775807\r\n" },
        .{ .args = &.{ "SET", "k", "-9223372036854775808" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "DECR", "k" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$20\r\n-9223372036854775808\r\n" },
        .{ .args = &.{ "SET", "k", "9223372036854775807" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "INCRBY", "k", "1" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$19\r\n9223372036854775807\r\n" },
        .{ .args = &.{ "SET", "k", "-9223372036854775808" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "INCRBY", "k", "-1" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$20\r\n-9223372036854775808\r\n" },
        .{ .args = &.{ "SET", "k", "9223372036854775807" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "DECRBY", "k", "-1" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$19\r\n9223372036854775807\r\n" },
        .{ .args = &.{ "SET", "k", "-9223372036854775808" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "DECRBY", "k", "1" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$20\r\n-9223372036854775808\r\n" },
        .{ .args = &.{ "SET", "k", "0" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "DECRBY", "k", "-9223372036854775808" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n0\r\n" },
        .{ .args = &.{ "SET", "k", "text" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "INCR", "k" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$4\r\ntext\r\n" },
        .{ .args = &.{ "SET", "k", "7" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "INCRBY", "k", "bad" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{ "SET", "k", "7" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "INCRBY", "k", "9223372036854775808" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
    });
}

test "key contract: integer commands reject missing and extra arguments without mutation" {
    try scenario(&.{
        .{ .args = &.{ "SET", "k", "7" }, .reply = "+OK\r\n" },
        .{ .args = &.{"INCR"}, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{ "INCR", "k", "extra" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{"DECR"}, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{ "DECR", "k", "extra" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{"INCRBY"}, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{ "INCRBY", "k" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{ "INCRBY", "k", "1", "extra" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{"DECRBY"}, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{ "DECRBY", "k" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
        .{ .args = &.{ "DECRBY", "k", "1", "extra" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n7\r\n" },
    });
}

test "key contract: cached integer overflow does not mutate the maximum value" {
    try scenario(&.{
        .{ .args = &.{ "SET", "k", "9223372036854775806" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "INCR", "k" }, .reply = ":9223372036854775807\r\n" },
        .{ .args = &.{ "INCR", "k" }, .reply = "-ERR " },
        .{ .args = &.{ "GET", "k" }, .reply = "$19\r\n9223372036854775807\r\n" },
    });
}

test "key contract: MSET replaces collection keys without leaving ghosts" {
    try scenario(&.{
        .{ .args = &.{ "HSET", "h", "f", "7" }, .reply = ":1\r\n" },
        .{ .args = &.{ "SADD", "s", "m" }, .reply = ":1\r\n" },
        .{ .args = &.{ "MSET", "h", "9", "s", "8" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "GET", "h" }, .reply = "$1\r\n9\r\n" },
        .{ .args = &.{ "GET", "s" }, .reply = "$1\r\n8\r\n" },
        .{ .args = &.{ "DEL", "h", "s" }, .reply = ":2\r\n" },
        .{ .args = &.{ "HLEN", "h" }, .reply = ":0\r\n" },
        .{ .args = &.{ "SCARD", "s" }, .reply = ":0\r\n" },
    });
}

test "key contract: in-place updates preserve TTL and SET clears it" {
    const updates = [_]Args{
        &.{ "INCR", "k" },           &.{ "DECR", "k" },       &.{ "INCRBY", "k", "1" }, &.{ "DECRBY", "k", "1" },
        &.{ "HSET", "k", "f", "8" }, &.{ "RPUSH", "k", "8" }, &.{ "SADD", "k", "8" },   &.{ "ZADD", "k", "8", "n" },
    };
    const types = [_]Kind{ .string, .string, .string, .string, .hash, .list, .set, .zset };
    var failures: usize = 0;
    for (backends) |backend| {
        for (updates, types) |args, kind| {
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            try f.check(&.{ "PEXPIRE", "k", "60000" }, ":1\r\n");
            const before = try liveTtl(f);
            const reply = try f.exec(args);
            defer a.free(reply);
            if (reply.len == 0 or reply[0] == '-') f.failures += 1;
            const after = try liveTtl(f);
            if (after > before) f.failures += 1;
            try f.check(&.{ "SET", "k", "9" }, "+OK\r\n");
            try f.check(&.{ "PTTL", "k" }, ":-1\r\n");
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: SELECT isolates every type and DEL only affects selected DB" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |kind| {
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            try f.check(&.{ "SELECT", "1" }, "+OK\r\n");
            try f.absent();
            try f.seed(kind);
            try f.check(&.{ "DEL", "k" }, ":1\r\n");
            try f.absent();
            try f.check(&.{ "SELECT", "0" }, "+OK\r\n");
            try f.unchanged(kind);
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: FLUSHDB and FLUSHALL cover all types with correct DB scope" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds) |kind| {
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            try f.check(&.{ "SELECT", "1" }, "+OK\r\n");
            try f.seed(kind);
            try f.check(&.{"FLUSHDB"}, "+OK\r\n");
            try f.absent();
            try f.check(&.{ "SELECT", "0" }, "+OK\r\n");
            try f.unchanged(kind);
            try f.check(&.{ "SELECT", "1" }, "+OK\r\n");
            try f.seed(kind);
            try f.check(&.{"FLUSHALL"}, "+OK\r\n");
            try f.absent();
            try f.check(&.{ "SELECT", "0" }, "+OK\r\n");
            try f.absent();
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}

test "key contract: LTRIM keeps inclusive negative-index range and ZREVRANK reverses rank" {
    try scenario(&.{
        .{ .args = &.{ "RPUSH", "l", "a", "b", "c", "d" }, .reply = ":4\r\n" },
        .{ .args = &.{ "LTRIM", "l", "-3", "-2" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "LRANGE", "l", "0", "-1" }, .reply = "*2\r\n$1\r\nb\r\n$1\r\nc\r\n" },
        .{ .args = &.{ "LTRIM", "l", "bad", "0" }, .reply = "-ERR " },
        .{ .args = &.{ "LLEN", "l" }, .reply = ":2\r\n" },
        .{ .args = &.{ "ZADD", "z", "1", "a", "1", "b", "2", "c" }, .reply = ":3\r\n" },
        .{ .args = &.{ "ZREVRANK", "z", "a" }, .reply = ":2\r\n" },
        .{ .args = &.{ "ZREVRANK", "z", "c" }, .reply = ":0\r\n" },
        .{ .args = &.{ "ZREVRANK", "z", "missing" }, .reply = "$-1\r\n" },
    });
}

test "key contract: former reactor extensions share collection and namespace semantics" {
    try scenario(&.{
        .{ .args = &.{ "HSET", "h", "f", "v" }, .reply = ":1\r\n" },
        .{ .args = &.{ "MSETNX", "s", "1", "h", "2" }, .reply = ":0\r\n" },
        .{ .args = &.{ "EXISTS", "s" }, .reply = ":0\r\n" },
        .{ .args = &.{ "MEXISTS", "h", "h", "s" }, .reply = ":2\r\n" },
        .{ .args = &.{ "MSETEX", "h", "2", "60" }, .reply = "+OK\r\n" },
        .{ .args = &.{ "MGETDEL", "h", "s" }, .reply = "*2\r\n$1\r\n2\r\n$-1\r\n" },
        .{ .args = &.{ "HLEN", "h" }, .reply = ":0\r\n" },
        .{ .args = &.{ "RPUSH", "k", "a", "b" }, .reply = ":2\r\n" },
        .{ .args = &.{ "LPOPN", "k", "5" }, .reply = "*2\r\n$1\r\na\r\n$1\r\nb\r\n" },
        .{ .args = &.{ "TYPE", "k" }, .reply = "+none\r\n" },
        .{ .args = &.{ "INCRTTL", "k", "3", "EX", "60" }, .reply = ":3\r\n" },
        .{ .args = &.{ "GET", "k" }, .reply = "$1\r\n3\r\n" },
        .{ .args = &.{ "SCANGET", "0", "k", "COUNT", "1" }, .reply = "*2\r\n$1\r\n0\r\n*2\r\n$1\r\nk\r\n$1\r\n3\r\n" },
        .{ .args = &.{ "UNLINK", "k", "k" }, .reply = ":1\r\n" },
    });
}

test "key contract: SCAN and timed collection expiry use the shared namespace" {
    var failures: usize = 0;
    for (backends) |backend| {
        for (kinds[1..]) |kind| {
            const f = try Fixture.create(backend);
            defer f.destroy();
            try f.seed(kind);
            try f.check(&.{ "SCAN", "0", "MATCH", "k", "COUNT", "10" }, "*2\r\n$1\r\n0\r\n*1\r\n$1\r\nk\r\n");
            // Force an already reached absolute deadline; no timing sleeps.
            try f.zsets.setCollectionExpiry("db:0:k", 1);
            try f.absent();
            try f.seed(kind);
            try f.check(&.{ "PTTL", "k" }, ":-1\r\n");
            failures += f.failures;
        }
    }
    try std.testing.expectEqual(@as(usize, 0), failures);
}


const ParallelCommand = struct {
    handler: CommandHandler,
    args: Args,
    expected: []const u8,
    done: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
    passed: bool = false,

    fn run(job: *ParallelCommand) void {
        defer job.done.store(true, .release);
        defer job.handler.kvGetCleanup();
        var writer = std.Io.Writer.Allocating.init(a);
        defer writer.deinit();
        job.handler.execute(job.args, &writer.writer) catch return;
        job.passed = std.mem.eql(u8, job.expected, writer.written());
    }
};

test "single-key collections progress while an unrelated command partition is locked" {
    const commands = [_]Args{
        &.{ "hSeT", "free", "f", "v" }, &.{ "rpush", "free", "v" },
        &.{ "sAdD", "free", "v" }, &.{ "zadd", "free", "1", "v" },
    };
    for ([_][]const u8{ "SUNION", "sinter", "SDIFF", "EXEC", "FLUSHDB", "" }) |cmd|
        try std.testing.expect(!CommandHandler.isSingleKeyCollectionCommand(cmd));
    for (commands) |args| {
        try std.testing.expect(CommandHandler.isSingleKeyCollectionCommand(args[0]));
        const f = try Fixture.create(.concurrent);
        defer f.destroy();
        // Exercise reclamation of an expired collection of a different type.
        _ = try f.sets.sadd("db:0:free", &.{"expired"});
        try f.zsets.setCollectionExpiry("db:0:free", 0);
        var held_buf: [64]u8 = undefined;
        var id: usize = 0;
        var held: []const u8 = undefined;
        while (true) : (id += 1) {
            held = try std.fmt.bufPrint(&held_buf, "db:0:held:{d}", .{id});
            if (f.zsets.partitionIndex(held) != f.zsets.partitionIndex("db:0:free")) break;
        }
        var job = ParallelCommand{ .handler = f.handler, .args = args, .expected = ":1\r\n" };
        const partition = f.zsets.lockKey(held);
        var released = false;
        defer if (!released) f.zsets.unlockKey(partition);
        const thread = try std.Thread.spawn(.{}, ParallelCommand.run, .{&job});
        var attempts: usize = 0;
        while (!job.done.load(.acquire) and attempts < 2000) : (attempts += 1) {
            const delay = std.c.timespec{ .sec = 0, .nsec = 1_000_000 };
            var remaining: std.c.timespec = undefined;
            _ = std.c.nanosleep(&delay, &remaining);
        }
        const independent = job.done.load(.acquire);
        f.zsets.unlockKey(partition);
        released = true;
        thread.join();
        try std.testing.expect(independent);
        try std.testing.expect(job.passed);
    }
}

test "list and set partitions match the command coordinator across startup sizes" {
    for ([_]usize{ 1, 16, 256, 1024, 4096 }) |count| {
        const f = try Fixture.createWithPartitionCount(.concurrent, count);
        defer f.destroy();
        var buffer: [64]u8 = undefined;
        for (0..512) |i| {
            const key = try std.fmt.bufPrint(&buffer, "db:7:growth:{d}", .{i});
            try std.testing.expectEqual(f.zsets.partitionIndex(key), f.lists.partitionIndex(key));
            try std.testing.expectEqual(f.zsets.partitionIndex(key), f.sets.partitionIndex(key));
            _ = try f.lists.rpush(key, &.{"list-value"});
            _ = try f.sets.sadd(key, &.{"set-value"});
        }
        for (0..512) |i| {
            const key = try std.fmt.bufPrint(&buffer, "db:7:growth:{d}", .{i});
            try std.testing.expectEqualStrings("list-value", f.lists.lindex(key, 0).?);
            try std.testing.expect(f.sets.sismember(key, "set-value"));
            try std.testing.expect(f.lists.delete(key));
            try std.testing.expect(f.sets.delete(key));
        }
    }
    try std.testing.expectError(error.InvalidPartitionCount, ListStore.initWithPartitionCount(a, 3));
    try std.testing.expectError(error.InvalidPartitionCount, SetStore.initWithPartitionCount(a, 0));
}
