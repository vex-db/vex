//! Bounded, single-process comparison of the production direct map and the
//! pinned std.StringArrayHashMap layout.  Compile with a root shim:
//!   pub const ConcurrentKV = @import("src/engine/kv/concurrent_kv.zig").ConcurrentKV;
//!   pub const main = @import("bench/loadtest/diagnostics/table_screen.zig").main;
//! Usage: table-screen direct|dense COUNT VALUE_BYTES timed|accounting|selftest [OPS]
//!
//! The diagnostic deliberately uses the real 64-byte ConcurrentKV.Entry and
//! owns every 16-byte key separately.  It is a storage diagnostic, not a
//! concurrent server benchmark: there are no stripe locks or TCP operations.
const std = @import("std");
const ConcurrentKV = @import("root").ConcurrentKV;
const Entry = ConcurrentKV.Entry;
const Allocator = std.mem.Allocator;
const Alignment = std.mem.Alignment;
const DenseMap = std.array_hash_map.String(Entry);

const STRIPES = 256;
const KEY_BYTES = 16;
const DEFAULT_OPS = 1_000_000;
const TRACE_LIMIT = 1_000_000;
const INLINE_BYTES = ConcurrentKV.INLINE_BUF_SIZE;

const Timespec = extern struct { sec: isize, nsec: isize };
extern "c" fn clock_gettime(clock_id: c_int, timestamp: *Timespec) c_int;
extern "c" fn open(path: [*:0]const u8, flags: c_int, ...) c_int;
extern "c" fn read(fd: c_int, buffer: [*]u8, len: usize) isize;
extern "c" fn close(fd: c_int) c_int;

const Mode = enum { direct, dense };
const RunMode = enum { timed, accounting, selftest };

const Counter = struct {
    child: Allocator,
    live_requested: usize = 0,
    peak_requested: usize = 0,
    alloc_calls: usize = 0,

    fn allocator(self: *Counter) Allocator {
        return .{ .ptr = self, .vtable = &.{ .alloc = alloc, .resize = resize, .remap = remap, .free = free } };
    }
    fn add(self: *Counter, n: usize) void {
        self.live_requested += n;
        self.peak_requested = @max(self.peak_requested, self.live_requested);
        self.alloc_calls += 1;
    }
    fn move(self: *Counter, old: usize, new: usize) void {
        self.live_requested -= old;
        self.live_requested += new;
        self.peak_requested = @max(self.peak_requested, self.live_requested);
    }
    fn alloc(ctx: *anyopaque, n: usize, alignment: Alignment, ra: usize) ?[*]u8 {
        const self: *Counter = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawAlloc(n, alignment, ra) orelse return null;
        self.add(n);
        return ptr;
    }
    fn resize(ctx: *anyopaque, memory: []u8, alignment: Alignment, n: usize, ra: usize) bool {
        const self: *Counter = @ptrCast(@alignCast(ctx));
        if (!self.child.rawResize(memory, alignment, n, ra)) return false;
        self.move(memory.len, n);
        return true;
    }
    fn remap(ctx: *anyopaque, memory: []u8, alignment: Alignment, n: usize, ra: usize) ?[*]u8 {
        const self: *Counter = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawRemap(memory, alignment, n, ra) orelse return null;
        self.move(memory.len, n);
        return ptr;
    }
    fn free(ctx: *anyopaque, memory: []u8, alignment: Alignment, ra: usize) void {
        const self: *Counter = @ptrCast(@alignCast(ctx));
        self.child.rawFree(memory, alignment, ra);
        self.live_requested -= memory.len;
    }
};

fn monotonicNs() !u64 {
    var now: Timespec = undefined;
    if (clock_gettime(1, &now) != 0 or now.sec < 0 or now.nsec < 0 or now.nsec >= 1_000_000_000) return error.ClockFailed;
    return @as(u64, @intCast(now.sec)) * 1_000_000_000 + @as(u64, @intCast(now.nsec));
}

fn processCpuNs() !u64 {
    var now: Timespec = undefined;
    if (clock_gettime(2, &now) != 0 or now.sec < 0 or now.nsec < 0 or now.nsec >= 1_000_000_000) return error.ClockFailed;
    return @as(u64, @intCast(now.sec)) * 1_000_000_000 + @as(u64, @intCast(now.nsec));
}

fn rssBytes() !usize {
    const fd = open("/proc/self/status", 0);
    if (fd < 0) return error.StatusOpenFailed;
    defer _ = close(fd);
    var buf: [16384]u8 = undefined;
    const n = read(fd, &buf, buf.len);
    if (n <= 0) return error.StatusReadFailed;
    var lines = std.mem.splitScalar(u8, buf[0..@intCast(n)], '\n');
    while (lines.next()) |line| {
        if (!std.mem.startsWith(u8, line, "VmRSS:")) continue;
        var words = std.mem.tokenizeAny(u8, line, " \t");
        _ = words.next();
        return (try std.fmt.parseInt(usize, words.next() orelse return error.MissingRss, 10)) * 1024;
    }
    return error.MissingRss;
}

fn stripeIndex(key: []const u8) usize {
    return @as(usize, @intCast(std.hash.Wyhash.hash(1, key) & (STRIPES - 1)));
}

fn freeEntry(entry: *const Entry, allocator: Allocator) void {
    if (!entry.flags.is_inline) allocator.free(entry.storage.heap.ptr[0..entry.storage.heap.capacity]);
}

fn initEntry(allocator: Allocator, value: []const u8) !Entry {
    var entry: Entry = .{};
    if (value.len <= INLINE_BYTES) {
        entry.storage = .{ .inline_buf = undefined };
        @memcpy(entry.storage.inline_buf[0..value.len], value);
        entry.inline_len = @intCast(value.len);
        entry.flags.is_inline = true;
        return entry;
    }
    const owned = try allocator.dupe(u8, value);
    entry.storage = .{ .heap = .{ .ptr = owned.ptr, .len = owned.len, .capacity = owned.len } };
    entry.flags.is_inline = false;
    return entry;
}

fn replaceEntry(entry: *Entry, allocator: Allocator, value: []const u8) !void {
    if (value.len <= INLINE_BYTES) {
        var copy: [INLINE_BYTES]u8 = undefined;
        @memcpy(copy[0..value.len], value);
        freeEntry(entry, allocator);
        entry.storage = .{ .inline_buf = copy };
        entry.inline_len = @intCast(value.len);
        entry.flags.is_inline = true;
        return;
    }
    if (!entry.flags.is_inline and entry.storage.heap.capacity >= value.len and entry.storage.heap.capacity / 2 <= value.len) {
        const dest = @constCast(entry.storage.heap.ptr)[0..value.len];
        @memcpy(dest, value);
        entry.storage.heap.len = value.len;
        return;
    }
    const replacement = try allocator.dupe(u8, value);
    freeEntry(entry, allocator);
    entry.storage = .{ .heap = .{ .ptr = replacement.ptr, .len = replacement.len, .capacity = replacement.len } };
    entry.flags.is_inline = false;
}

fn entryBytes(entry: *const Entry) []const u8 {
    return entry.bytes();
}

const Table = struct {
    mode: Mode,
    allocator: Allocator,
    direct: [STRIPES]std.StringHashMap(Entry),
    dense: [STRIPES]DenseMap,

    fn init(mode: Mode, allocator: Allocator) Table {
        var table: Table = .{ .mode = mode, .allocator = allocator, .direct = undefined, .dense = undefined };
        for (&table.direct) |*map| map.* = std.StringHashMap(Entry).init(allocator);
        for (&table.dense) |*map| map.* = .empty;
        return table;
    }
    fn getPtr(self: *Table, key: []const u8) ?*Entry {
        const i = stripeIndex(key);
        return switch (self.mode) {
            .direct => self.direct[i].getPtr(key),
            .dense => self.dense[i].getPtr(key),
        };
    }
    fn putNew(self: *Table, key: []const u8, value: []const u8) !void {
        const i = stripeIndex(key);
        const owned_key = try self.allocator.dupe(u8, key);
        errdefer self.allocator.free(owned_key);
        var entry = try initEntry(self.allocator, value);
        errdefer freeEntry(&entry, self.allocator);
        switch (self.mode) {
            .direct => try self.direct[i].put(owned_key, entry),
            .dense => try self.dense[i].put(self.allocator, owned_key, entry),
        }
    }
    fn overwrite(self: *Table, key: []const u8, value: []const u8) !void {
        const ptr = self.getPtr(key) orelse return error.MissingKey;
        try replaceEntry(ptr, self.allocator, value);
    }
    fn remove(self: *Table, key: []const u8) !bool {
        const i = stripeIndex(key);
        switch (self.mode) {
            .direct => if (self.direct[i].fetchRemove(key)) |pair| {
                freeEntry(&pair.value, self.allocator);
                self.allocator.free(pair.key);
                return true;
            },
            .dense => if (self.dense[i].fetchSwapRemove(key)) |pair| {
                freeEntry(&pair.value, self.allocator);
                self.allocator.free(pair.key);
                return true;
            },
        }
        return false;
    }
    fn count(self: *const Table) usize {
        var n: usize = 0;
        for (self.direct, self.dense) |direct_map, dense_map| n += if (self.mode == .direct) direct_map.count() else dense_map.count();
        return n;
    }
    fn capacities(self: *const Table) usize {
        var n: usize = 0;
        for (self.direct, self.dense) |direct_map, dense_map| n += if (self.mode == .direct) direct_map.capacity() else dense_map.entries.capacity;
        return n;
    }
    fn deinit(self: *Table) void {
        for (&self.direct, &self.dense) |*direct_map, *dense_map| {
            if (self.mode == .direct) {
                var it = direct_map.iterator();
                while (it.next()) |pair| {
                    freeEntry(pair.value_ptr, self.allocator);
                    self.allocator.free(pair.key_ptr.*);
                }
                direct_map.deinit();
            } else {
                var it = dense_map.iterator();
                while (it.next()) |pair| {
                    freeEntry(pair.value_ptr, self.allocator);
                    self.allocator.free(pair.key_ptr.*);
                }
                dense_map.deinit(self.allocator);
            }
        }
    }
};

fn keyByte(index: usize, byte: usize) u8 {
    var x: u64 = @as(u64, @intCast(index)) *% 0x9e3779b97f4a7c15 +% 0xd1b54a32d192ed03;
    x ^= x >> 29;
    x *%= 0xbf58476d1ce4e5b9;
    x ^= x >> 32;
    return @truncate(x >> @intCast((byte & 7) * 8));
}

fn makeKeys(allocator: Allocator, count: usize) ![][]u8 {
    const keys = try allocator.alloc([]u8, count);
    var made: usize = 0;
    errdefer {
        for (keys[0..made]) |key| allocator.free(key);
        allocator.free(keys);
    }
    for (keys, 0..) |*slot, i| {
        slot.* = try allocator.alloc(u8, KEY_BYTES);
        for (slot.*, 0..) |*byte, j| byte.* = keyByte(i, j);
        made += 1;
    }
    return keys;
}

fn makeMissKeys(allocator: Allocator, count: usize) ![][]u8 {
    const keys = try allocator.alloc([]u8, count);
    var made: usize = 0;
    errdefer {
        for (keys[0..made]) |key| allocator.free(key);
        allocator.free(keys);
    }
    for (keys, 0..) |*slot, i| {
        slot.* = try allocator.alloc(u8, KEY_BYTES);
        for (slot.*, 0..) |*byte, j| byte.* = keyByte(i + 0x7fffffff, j) ^ 0xa5;
        made += 1;
    }
    return keys;
}

fn freeKeys(allocator: Allocator, keys: [][]u8) void {
    for (keys) |key| allocator.free(key);
    allocator.free(keys);
}

fn nextTrace(state: *u64) u32 {
    state.* = state.* *% 6364136223846793005 +% 1442695040888963407;
    return @truncate(state.* >> 16);
}

fn resetLoaded(table: *Table, keys: [][]u8, value: []const u8) !void {
    for (keys) |key| _ = try table.remove(key);
    for (keys) |key| try table.putNew(key, value);
}

fn validateValues(table: *Table, keys: [][]u8, value: []const u8, marker: u8) !void {
    for (keys) |key| {
        const entry = table.getPtr(key) orelse return error.UnexpectedMiss;
        const bytes = entryBytes(entry);
        if (bytes.len != value.len or !std.mem.allEqual(u8, bytes, marker)) return error.ValueMismatch;
    }
}

fn observeGet(table: *Table, key: []const u8, checksum: *u64, expected: bool, observe: bool) !void {
    const found = table.getPtr(key);
    if (expected) {
        const ptr = found orelse return error.UnexpectedMiss;
        if (observe) {
            const bytes = entryBytes(ptr);
            checksum.* = checksum.* *% 131 +% @as(u64, @intCast(bytes.len));
            if (bytes.len > 0) checksum.* = checksum.* *% 131 +% bytes[0];
            if (bytes.len > 1) checksum.* = checksum.* *% 131 +% bytes[bytes.len - 1];
            checksum.* = checksum.* *% 131 +% @as(u64, @intCast(@intFromBool(ptr.flags.is_inline)));
        }
    } else if (found != null) return error.UnexpectedHit else if (observe) checksum.* +%= 0xfeed;
}

const Phase = enum { randomhit, miss, hotset, equaloverwrite, mixed_80_20, delete_reinsert };

fn runOperation(table: *Table, phase: Phase, op: usize, trace: []const u32, keys: [][]u8, miss_keys: [][]u8, value: []const u8, overwrite_value: []const u8, checksum: *u64, observe: bool) !void {
    const number = @as(usize, trace[op % trace.len]);
    switch (phase) {
        .randomhit => try observeGet(table, keys[number % keys.len], checksum, true, observe),
        .miss => try observeGet(table, miss_keys[number % miss_keys.len], checksum, false, observe),
        .hotset => try observeGet(table, keys[number % @min(keys.len, 1024)], checksum, true, observe),
        .equaloverwrite => {
            try table.overwrite(keys[number % keys.len], overwrite_value);
            if (observe) checksum.* = checksum.* *% 131 +% @as(u64, @intCast(overwrite_value[0]));
        },
        .mixed_80_20 => if (op % 5 == 0) {
            try table.overwrite(keys[number % keys.len], overwrite_value);
            if (observe) checksum.* = checksum.* *% 131 +% @as(u64, @intCast(overwrite_value[0]));
        } else try observeGet(table, keys[number % keys.len], checksum, true, observe),
        .delete_reinsert => {
            const slot = (op / 10) % keys.len;
            if (op % 10 == 0) {
                if (!try table.remove(keys[slot])) return error.DeleteFailed;
                if (observe) checksum.* +%= 0xdead;
            } else if (op % 10 == 1) {
                try table.putNew(keys[slot], value);
                if (observe) checksum.* +%= 0xbeef;
            } else try observeGet(table, keys[slot], checksum, true, observe);
        },
    }
}

fn denseIndexBytes(table: *const Table) struct { slots: usize, bytes: usize, max_width: usize } {
    var slots_total: usize = 0;
    var bytes_total: usize = 0;
    var max_width: usize = 0;
    if (table.mode == .dense) for (table.dense) |map| if (map.index_header) |header| {
        const slots = @as(usize, 1) << @intCast(header.bit_index);
        const width: usize = if (header.bit_index <= 8) 2 else if (header.bit_index <= 16) 4 else 8;
        slots_total += slots;
        bytes_total += @sizeOf(@TypeOf(header.*)) + slots * width;
        max_width = @max(max_width, width);
    };
    return .{ .slots = slots_total, .bytes = bytes_total, .max_width = max_width };
}

fn printCapacityHistogram(table: *const Table) void {
    var capacities: [STRIPES]usize = @splat(0);
    var counts: [STRIPES]usize = @splat(0);
    var length: usize = 0;
    if (table.mode == .direct) {
      for (table.direct) |map| {
        const capacity = map.capacity();
        var found = false;
        for (capacities[0..length], counts[0..length]) |*known, *count| if (known.* == capacity) { count.* += 1; found = true; break; };
        if (!found) { capacities[length] = capacity; counts[length] = 1; length += 1; }
      }
    } else {
      for (table.dense) |map| {
        const capacity = map.entries.capacity;
        var found = false;
        for (capacities[0..length], counts[0..length]) |*known, *count| if (known.* == capacity) { count.* += 1; found = true; break; };
        if (!found) { capacities[length] = capacity; counts[length] = 1; length += 1; }
      }
    }
    std.debug.print(",\"capacity_histogram\":[", .{});
    var comma = false;
    for (capacities[0..length], counts[0..length]) |capacity, n| {
        std.debug.print("{s}{{\"capacity\":{d},\"stripes\":{d}}}", .{ if (comma) "," else "", capacity, n });
        comma = true;
    }
    std.debug.print("]", .{});
}

fn printAccounting(phase: []const u8, table: *Table, counter: *const Counter, before_allocs: usize, checksum: u64) !void {
    const capacity = table.capacities();
    const dense_index = denseIndexBytes(table);
    const index_slots = dense_index.slots;
    const index_width = dense_index.max_width;
    const index_bytes = dense_index.bytes;
    const data_bytes = if (table.mode == .dense) capacity * (4 + @sizeOf([]const u8) + @sizeOf(Entry)) else 0;
    std.debug.print("{{\"phase\":\"{s}\",\"mode\":\"{s}\",\"entries\":{d},\"capacity\":{d},\"dense_index_slots_estimate\":{d},\"dense_index_width\":{d},\"dense_index_bytes_estimate\":{d},\"dense_data_bytes_nominal\":{d},\"live_requested_bytes\":{d},\"peak_requested_bytes\":{d},\"alloc_calls_delta\":{d},\"rss_bytes\":{d},\"checksum\":{d}", .{ phase, @tagName(table.mode), table.count(), capacity, index_slots, index_width, index_bytes, data_bytes, counter.live_requested, counter.peak_requested, counter.alloc_calls - before_allocs, try rssBytes(), checksum });
    printCapacityHistogram(table);
    std.debug.print("}}\n", .{});
}

fn runPhase(table: *Table, phase: Phase, trace: []const u32, operations: usize, keys: [][]u8, miss_keys: [][]u8, value: []const u8, overwrite_value: []const u8, run_mode: RunMode, counter: *Counter) !void {
    try resetLoaded(table, keys, value);
    const raw_warm = @min(trace.len, 1024);
    const warm = if (phase == .delete_reinsert) ((raw_warm + 9) / 10) * 10 else raw_warm;
    const phase_ops = if (phase == .delete_reinsert) ((operations + 9) / 10) * 10 else operations;
    const measured = measurement: {
        // The reference trace is prepared outside timing and never repairs the
        // table under test. Free this oracle before reporting table accounting.
        const expected = try table.allocator.alloc(u8, keys.len);
        defer table.allocator.free(expected);
        @memset(expected, 'a');
        if (phase == .equaloverwrite or phase == .mixed_80_20) {
            for ([_]usize{ warm, phase_ops }) |n| {
                for (0..n) |i| {
                    if (phase == .equaloverwrite or i % 5 == 0)
                        expected[@as(usize, trace[i % trace.len]) % keys.len] = 'b';
                }
            }
        }
        var ignored: u64 = 0;
        for (0..warm) |i| try runOperation(table, phase, i, trace, keys, miss_keys, value, overwrite_value, &ignored, false);
        var checksum: u64 = 0;
        const before_allocs = counter.alloc_calls;
        const started = try monotonicNs();
        const cpu_started = try processCpuNs();
        for (0..phase_ops) |i| try runOperation(table, phase, i, trace, keys, miss_keys, value, overwrite_value, &checksum, true);
        const cpu_elapsed = (try processCpuNs()) - cpu_started;
        const elapsed = (try monotonicNs()) - started;
        for (keys, expected) |key, marker| {
            const entry = table.getPtr(key) orelse return error.UnexpectedMiss;
            const bytes = entryBytes(entry);
            if (bytes.len != value.len or !std.mem.allEqual(u8, bytes, marker)) return error.ValueMismatch;
        }
        break :measurement .{ .elapsed = elapsed, .cpu_elapsed = cpu_elapsed, .checksum = checksum, .before_allocs = before_allocs };
    };
    if (run_mode == .timed) {
        std.debug.print("{{\"phase\":\"{s}\",\"mode\":\"{s}\",\"elapsed_ns\":{d},\"cpu_elapsed_ns\":{d},\"operations\":{d},\"checksum\":{d},\"entries\":{d},\"capacity\":{d}", .{ @tagName(phase), @tagName(table.mode), measured.elapsed, measured.cpu_elapsed, phase_ops, measured.checksum, table.count(), table.capacities() });
        printCapacityHistogram(table);
        std.debug.print("}}\n", .{});
    } else if (run_mode == .accounting) {
        try printAccounting(@tagName(phase), table, counter, measured.before_allocs, measured.checksum);
    }
}

fn selfTest(mode: Mode, allocator: Allocator, counter: *Counter) !void {
    const count = 97;
    const keys = try makeKeys(allocator, count);
    const misses = try makeMissKeys(allocator, count);
    // Concentrate the fixture in one stripe so the randomized run grows an
    // index and exercises dense swap-remove/backshift movement.
    for (keys, 0..) |key, i| {
        var attempt: usize = i;
        while (true) : (attempt += 1) {
            for (key, 0..) |*byte, j| byte.* = keyByte(i * 0x10000 + attempt, j);
            var duplicate = false;
            for (keys[0..i]) |prior| if (std.mem.eql(u8, prior, key)) { duplicate = true; break; };
            if (!duplicate and stripeIndex(key) == 0) break;
        }
    }
    var value_a: [32]u8 = undefined;
    var value_b: [32]u8 = undefined;
    @memset(&value_a, 'a');
    @memset(&value_b, 'b');
    var table = Table.init(mode, allocator);
    var present: [count]bool = @splat(false);
    var is_b: [count]bool = @splat(false);
    var state: u64 = 1;
    var checksum: u64 = 0;
    for (0..5000) |step| {
        const index = @as(usize, @intCast(nextTrace(&state))) % count;
        switch (nextTrace(&state) % 4) {
            0 => if (!present[index]) { try table.putNew(keys[index], &value_a); present[index] = true; is_b[index] = false; },
            1 => if (present[index]) {
                const calls = counter.alloc_calls;
                try table.overwrite(keys[index], &value_b);
                if (counter.alloc_calls != calls) return error.OverwriteAllocated;
                is_b[index] = true;
            },
            2 => if (present[index]) { if (!try table.remove(keys[index])) return error.DeleteFailed; present[index] = false; is_b[index] = false; },
            else => try observeGet(&table, if (step % 7 == 0) misses[index % misses.len] else keys[index], &checksum, if (step % 7 == 0) false else present[index], true),
        }
    }
    for (keys, 0..) |key, i| if (present[i]) {
        const ptr = table.getPtr(key) orelse return error.UnexpectedMiss;
        const bytes = entryBytes(ptr);
        if (bytes.len != value_a.len or !std.mem.allEqual(u8, bytes, if (is_b[i]) 'b' else 'a')) return error.ValueMismatch;
        try observeGet(&table, key, &checksum, true, true);
    };
    table.deinit();
    if (counter.live_requested != keys.len * KEY_BYTES + misses.len * KEY_BYTES + @sizeOf([]u8) * (keys.len + misses.len)) return error.AccountingMismatch;
    freeKeys(allocator, keys);
    freeKeys(allocator, misses);
    if (counter.live_requested != 0) return error.TrackedLeak;
    std.debug.print("{{\"phase\":\"selftest\",\"mode\":\"{s}\",\"result\":\"ok\",\"checksum\":{d}}}\n", .{ @tagName(mode), checksum });
}

pub fn main(init: std.process.Init) !void {
    var args = std.process.Args.Iterator.init(init.minimal.args);
    defer args.deinit();
    _ = args.skip();
    const mode_name = std.mem.sliceTo(args.next() orelse return error.ModeRequired, 0);
    const mode = if (std.mem.eql(u8, mode_name, "direct")) Mode.direct else if (std.mem.eql(u8, mode_name, "dense")) Mode.dense else return error.InvalidMode;
    const count = try std.fmt.parseInt(usize, std.mem.sliceTo(args.next() orelse return error.CountRequired, 0), 10);
    const value_len = try std.fmt.parseInt(usize, std.mem.sliceTo(args.next() orelse return error.ValueBytesRequired, 0), 10);
    const run_name = std.mem.sliceTo(args.next() orelse return error.RunModeRequired, 0);
    const run_mode = if (std.mem.eql(u8, run_name, "timed")) RunMode.timed else if (std.mem.eql(u8, run_name, "accounting")) RunMode.accounting else if (std.mem.eql(u8, run_name, "selftest")) RunMode.selftest else return error.InvalidRunMode;
    const ops = if (args.next()) |arg| try std.fmt.parseInt(usize, std.mem.sliceTo(arg, 0), 10) else DEFAULT_OPS;
    if (args.next() != null or count == 0 or count > 1_000_000 or value_len == 0 or value_len > 4096 or ops == 0 or ops > 10_000_000) return error.InvalidArguments;
    var counter = Counter{ .child = std.heap.c_allocator };
    const allocator = if (run_mode == .timed) std.heap.c_allocator else counter.allocator();
    std.debug.print("{{\"phase\":\"configuration\",\"mode\":\"{s}\",\"count\":{d},\"value_bytes\":{d},\"key_bytes\":16,\"stripes\":256,\"entry_bytes\":{d},\"ops\":{d},\"run\":\"{s}\",\"hash\":\"Wyhash seed1 stripe, seed0 map\",\"cache_line_bytes\":{d}}}\n", .{ mode_name, count, value_len, @sizeOf(Entry), ops, run_name, std.atomic.cache_line });
    if (run_mode == .selftest) {
        try selfTest(mode, allocator, &counter);
        return;
    }
    const keys = try makeKeys(allocator, count);
    const misses = try makeMissKeys(allocator, count);
    const trace = try allocator.alloc(u32, @min(ops, TRACE_LIMIT));

    var state: u64 = 0x123456789abcdef0;
    for (trace) |*entry| entry.* = nextTrace(&state);
    const value = try allocator.alloc(u8, value_len);
    const overwrite_value = try allocator.alloc(u8, value_len);
    @memset(value, 'a');
    @memset(overwrite_value, 'b');
    var table = Table.init(mode, allocator);
    try resetLoaded(&table, keys, value);
    std.debug.print("{{\"phase\":\"loaded\",\"mode\":\"{s}\",\"entries\":{d},\"capacity\":{d},\"rss_bytes\":{d}}}\n", .{ mode_name, table.count(), table.capacities(), try rssBytes() });
    const phases = [_]Phase{ .randomhit, .miss, .hotset, .equaloverwrite, .mixed_80_20, .delete_reinsert };
    for (phases) |phase| try runPhase(&table, phase, trace, ops, keys, misses, value, overwrite_value, run_mode, &counter);
    table.deinit();
    if (run_mode == .accounting and counter.live_requested != keys.len * KEY_BYTES + misses.len * KEY_BYTES + @sizeOf([]u8) * (keys.len + misses.len) + @sizeOf(u8) * value_len * 2 + @sizeOf(u32) * trace.len) return error.AccountingMismatch;
    std.debug.print("{{\"phase\":\"post_flush\",\"mode\":\"{s}\",\"live_requested_bytes\":{d},\"rss_bytes\":{d}}}\n", .{ mode_name, counter.live_requested, try rssBytes() });
    freeKeys(allocator, keys);
    freeKeys(allocator, misses);
    allocator.free(trace);
    allocator.free(value);
    allocator.free(overwrite_value);
    if (run_mode == .accounting and counter.live_requested != 0) return error.TrackedLeak;
    std.debug.print("{{\"phase\":\"completion\",\"mode\":\"{s}\",\"live_requested_bytes\":{d},\"peak_requested_bytes\":{d},\"alloc_calls\":{d},\"rss_bytes\":{d}}}\n", .{ mode_name, counter.live_requested, counter.peak_requested, counter.alloc_calls, try rssBytes() });
}
