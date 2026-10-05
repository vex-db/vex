//! Read-only accounting output around real ConcurrentKV insert/overwrite/free.
//! Compile in a temporary root shim (no production source changes):
//!   pub const ConcurrentKV = @import("src/engine/kv/concurrent_kv.zig").ConcurrentKV;
//!   pub const main = @import("bench/loadtest/diagnostics/memory_density.zig").main;
//! Usage: memory-density KEY_COUNT VALUE_BYTES [native|smp] [churn] or
//!        memory-density curve [native]. Curve is fixed to 16-byte keys and 256-byte values.
//! Run every case in a fresh process. Churn deletes all keys, reloads alternating
//! VALUE_BYTES / VALUE_BYTES+1 sizes, then flushes, exposing retained size classes.
//! JSON lines go to stderr. This is a single-thread allocation diagnostic, not
//! a reactor throughput benchmark. Modeled SMP bytes exclude retained slabs.
const std = @import("std");
const ConcurrentKV = @import("root").ConcurrentKV;
const Entry = ConcurrentKV.Entry;
const StoredEntry = if (@hasDecl(ConcurrentKV, "StoredEntry")) ConcurrentKV.StoredEntry else Entry;
const Allocator = std.mem.Allocator;
const Alignment = std.mem.Alignment;

extern "c" fn open(path: [*:0]const u8, flags: c_int, ...) c_int;
extern "c" fn read(fd: c_int, buffer: [*]u8, len: usize) isize;
extern "c" fn close(fd: c_int) c_int;
extern "c" fn mincore(address: *anyopaque, len: usize, vector: [*]u8) c_int;
extern "c" fn malloc_usable_size(pointer: *anyopaque) usize;
extern "c" fn malloc_trim(pad: usize) c_int;
const Timespec = extern struct { sec: isize, nsec: isize };
extern "c" fn clock_gettime(clock_id: c_int, timestamp: *Timespec) c_int;

fn monotonicNs() !u64 {
    var now: Timespec = undefined;
    if (clock_gettime(1, &now) != 0 or now.sec < 0 or now.nsec < 0 or now.nsec >= 1_000_000_000) return error.ClockFailed;
    return @as(u64, @intCast(now.sec)) * 1_000_000_000 + @as(u64, @intCast(now.nsec));
}

fn reportTiming(phase: []const u8, elapsed: u64, operations: usize) void {
    std.debug.print("{{\"phase\":\"{s}\",\"clock\":\"CLOCK_MONOTONIC\",\"elapsed_ns\":{d},\"operations\":{d}}}\n", .{ phase, elapsed, operations });
}

const Bucket = struct { live_count: usize = 0, live_requested: usize = 0, live_modeled: usize = 0, allocations: usize = 0 };
const Counted = struct {
    child: Allocator,
    buckets: [64]Bucket = @splat(.{}),
    live_requested: usize = 0,
    peak_requested: usize = 0,
    alloc_calls: usize = 0,

    fn allocator(self: *Counted) Allocator {
        return .{ .ptr = self, .vtable = &.{ .alloc = alloc, .resize = resize, .remap = remap, .free = free } };
    }
    fn index(len: usize, alignment: Alignment) usize {
        return @max(@bitSizeOf(usize) - @clz(len - 1), @intFromEnum(alignment), 3);
    }
    fn modeled(len: usize, alignment: Alignment) usize {
        const slot = @as(usize, 1) << @intCast(index(len, alignment));
        const slab = @max(std.heap.page_size_max, 64 * 1024);
        return if (slot < slab) slot else std.mem.alignForward(usize, len, std.heap.pageSize());
    }
    fn add(self: *Counted, len: usize, alignment: Alignment, new_allocation: bool) void {
        const b = &self.buckets[index(len, alignment)];
        b.live_count += 1;
        b.live_requested += len;
        b.live_modeled += modeled(len, alignment);
        self.live_requested += len;
        self.peak_requested = @max(self.peak_requested, self.live_requested);
        if (new_allocation) { b.allocations += 1; self.alloc_calls += 1; }
    }
    fn remove(self: *Counted, len: usize, alignment: Alignment) void {
        const b = &self.buckets[index(len, alignment)];
        b.live_count -= 1;
        b.live_requested -= len;
        b.live_modeled -= modeled(len, alignment);
        self.live_requested -= len;
    }
    fn alloc(ctx: *anyopaque, len: usize, alignment: Alignment, ra: usize) ?[*]u8 {
        const self: *Counted = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawAlloc(len, alignment, ra) orelse return null;
        self.add(len, alignment, true);
        return ptr;
    }
    fn resize(ctx: *anyopaque, memory: []u8, alignment: Alignment, len: usize, ra: usize) bool {
        const self: *Counted = @ptrCast(@alignCast(ctx));
        if (!self.child.rawResize(memory, alignment, len, ra)) return false;
        self.remove(memory.len, alignment);
        self.add(len, alignment, false);
        return true;
    }
    fn remap(ctx: *anyopaque, memory: []u8, alignment: Alignment, len: usize, ra: usize) ?[*]u8 {
        const self: *Counted = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawRemap(memory, alignment, len, ra) orelse return null;
        self.remove(memory.len, alignment);
        self.add(len, alignment, false);
        return ptr;
    }
    fn free(ctx: *anyopaque, memory: []u8, alignment: Alignment, ra: usize) void {
        const self: *Counted = @ptrCast(@alignCast(ctx));
        self.child.rawFree(memory, alignment, ra);
        self.remove(memory.len, alignment);
    }
};

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

// Exact pinned std.hash_map header/layout; not an allocator-residency estimate.
const MapHeader = struct { values: [*]StoredEntry, keys: [*][]const u8, capacity: u32 };
fn mapRequested(capacity: usize) usize {
    if (capacity == 0) return 0;
    const keys_start = std.mem.alignForward(usize, @sizeOf(MapHeader) + capacity, @alignOf([]const u8));
    const values_start = std.mem.alignForward(usize, keys_start + capacity * @sizeOf([]const u8), @alignOf(StoredEntry));
    return std.mem.alignForward(usize, values_start + capacity * @sizeOf(StoredEntry), @max(@alignOf(MapHeader), @alignOf(StoredEntry)));
}

fn entryPointer(value: *StoredEntry) *Entry {
    return if (StoredEntry == Entry) value else value.*;
}

fn residentPages(address: usize, len: usize) !usize {
    const page = std.heap.pageSize();
    const begin = std.mem.alignBackward(usize, address, page);
    const span = std.mem.alignForward(usize, address + len - begin, page);
    var vector: [8192]u8 = undefined;
    const count = span / page;
    if (count > vector.len) return error.ResidencyRangeTooLarge;
    if (mincore(@ptrFromInt(begin), span, &vector) != 0) return error.MincoreFailed;
    var resident: usize = 0;
    for (vector[0..count]) |bits| { if (bits & 1 != 0) resident += page; }
    return resident;
}

fn valueBytes(entry: *const Entry) []const u8 {
    if (@hasDecl(Entry, "bytes")) {
        return entry.bytes();
    } else {
        return if (entry.flags.is_inline) entry.inline_buf[0..entry.inline_len] else entry.value;
    }
}

fn report(phase: []const u8, store: ?*ConcurrentKV, counted: *const Counted) !void {
    var capacity: usize = 0;
    var entries: usize = 0;
    var map_bytes: usize = 0;
    var map_resident: usize = 0;
    var large_maps: usize = 0;
    var key_bytes: usize = 0;
    var heap_value_bytes: usize = 0;
    var inline_values: usize = 0;
    var capacity_hist: [32]usize = @splat(0);
    if (store) |s| for (&s.stripes) |*stripe| {
        const cap: usize = stripe.map.capacity();
        const bytes = mapRequested(cap);
        capacity += cap;
        entries += stripe.map.count();
        map_bytes += bytes;
        if (cap > 0) {
            capacity_hist[std.math.log2_int(usize, cap)] += 1;
            // Page coverage, not exclusive map RSS: libc allocation boundaries
            // can share pages, so this sum may count a boundary page twice.
            if (bytes > @max(std.heap.page_size_max, 64 * 1024) / 2) {
                const begin = @intFromPtr(stripe.map.unmanaged.metadata.?) - @sizeOf(MapHeader);
                map_resident += try residentPages(begin, bytes);
                large_maps += 1;
            }
        }
        var iterator = stripe.map.iterator();
        while (iterator.next()) |entry| {
            const value = entryPointer(entry.value_ptr);
            key_bytes += entry.key_ptr.len;
            if (value.flags.is_inline) { inline_values += 1; } else {
                heap_value_bytes += if (@hasField(Entry, "storage")) value.storage.heap.capacity else value.value.len;
            }
        }
    };
    const entry_object_bytes = if (StoredEntry == Entry) 0 else entries * @sizeOf(Entry);
    if (counted.live_requested != map_bytes + key_bytes + heap_value_bytes + entry_object_bytes) return error.AccountingMismatch;
    std.debug.print("{{\"phase\":\"{s}\",\"entry_bytes\":{d},\"stored_entry_bytes\":{d},\"entry_object_requested_bytes\":{d},\"entries\":{d},\"capacity\":{d},\"map_requested_bytes\":{d},\"large_map_count\":{d},\"large_map_resident_page_bytes\":{d},\"key_bytes\":{d},\"inline_values\":{d},\"heap_value_capacity_bytes\":{d},\"tracked_live_requested\":{d},\"tracked_peak_requested\":{d},\"alloc_calls\":{d},\"rss_bytes\":{d},\"capacity_histogram\":[", .{ phase, @sizeOf(Entry), @sizeOf(StoredEntry), entry_object_bytes, entries, capacity, map_bytes, large_maps, map_resident, key_bytes, inline_values, heap_value_bytes, counted.live_requested, counted.peak_requested, counted.alloc_calls, try rssBytes() });
    var comma = false;
    for (capacity_hist, 0..) |n, power| if (n > 0) {
        std.debug.print("{s}{{\"capacity\":{d},\"stripes\":{d}}}", .{ if (comma) "," else "", @as(usize, 1) << @intCast(power), n });
        comma = true;
    };
    std.debug.print("],\"allocation_histogram\":[", .{});
    comma = false;
    for (counted.buckets, 0..) |b, power| if (b.allocations > 0) {
        std.debug.print("{s}{{\"power2_class\":{d},\"live_count\":{d},\"live_requested\":{d},\"live_modeled_smp_bytes\":{d},\"total_allocations\":{d}}}", .{ if (comma) "," else "", @as(usize, 1) << @intCast(power), b.live_count, b.live_requested, b.live_modeled, b.allocations });
        comma = true;
    };
    std.debug.print("]}}\n", .{});
}

fn curveKey(buffer: *[16]u8, index: usize) ![]const u8 {
    const key = try std.fmt.bufPrint(buffer, "vex:key::{d}", .{1_000_000 + index});
    if (key.len != 16) return error.KeyLengthMismatch;
    return key;
}

fn runCurve(init: std.process.Init) !void {
    const child = std.heap.c_allocator;
    var counted = Counted{ .child = child };
    std.debug.print("{{\"phase\":\"configuration\",\"mode\":\"curve\",\"allocator\":\"native\",\"key_bytes\":16,\"value_bytes\":256,\"max_keys\":1600000,\"native_gpa_is_c\":{},\"trim_is_standalone_only\":true}}\n", .{init.gpa.vtable == std.heap.c_allocator.vtable});
    try report("curve-before", null, &counted);
    var store = ConcurrentKV.init(counted.allocator(), init.io);
    store.initStripes();
    store.updateClock();
    var live = true;
    defer if (live) store.deinit();
    var key_buf: [16]u8 = undefined;
    var value: [256]u8 = undefined;
    @memset(&value, 'a');
    const targets = [_]usize{ 200_000, 400_000, 600_000, 750_000, 800_000, 825_000, 850_000, 900_000, 1_000_000, 1_200_000, 1_500_000, 1_600_000 };
    var loaded: usize = 0;
    for (targets) |target| {
        for (loaded..target) |i| try store.set(try curveKey(&key_buf, i), &value);
        loaded = target;
        if (store.dbsize() != target) return error.KeyCountMismatch;
        for ([_]usize{ 0, target / 2, target - 1 }) |i| {
            const key = try curveKey(&key_buf, i);
            const entry = store.getStripePublic(key).map.getPtr(key) orelse return error.KeyCountMismatch;
            if (!std.mem.eql(u8, entry.bytes(), &value)) return error.ValueMismatch;
        }
        var phase_buf: [32]u8 = undefined;
        const phase = try std.fmt.bufPrint(&phase_buf, "curve-grow-{d}", .{loaded});
        try report(phase, &store, &counted);
    }
    for ([_]usize{ 800_000, 200_000, 0 }) |target| {
        while (loaded > target) {
            loaded -= 1;
            if (!store.delete(try curveKey(&key_buf, loaded))) return error.DeleteFailed;
        }
        var phase_buf: [32]u8 = undefined;
        const phase = try std.fmt.bufPrint(&phase_buf, "curve-delete-{d}", .{loaded});
        try report(phase, &store, &counted);
    }
    @memset(&value, 'b');
    for (0..200_000) |i| try store.set(try curveKey(&key_buf, i), &value);
    if (store.dbsize() != 200_000) return error.KeyCountMismatch;
    try report("curve-refill-200000", &store, &counted);
    store.flushdb();
    if (store.dbsize() != 0 or counted.live_requested != 0) return error.TrackedLeak;
    try report("curve-flushdb-before-trim", &store, &counted);
    const trim_result = malloc_trim(0);
    std.debug.print("{{\"phase\":\"curve-malloc-trim\",\"result\":{d},\"rss_bytes\":{d},\"tracked_live_requested\":{d}}}\n", .{ trim_result, try rssBytes(), counted.live_requested });
    try report("curve-flushdb-after-trim", &store, &counted);
    store.deinit();
    live = false;
    if (counted.live_requested != 0) return error.TrackedLeak;
    try report("curve-deinitialized", null, &counted);
}

pub fn main(init: std.process.Init) !void {
    var args = std.process.Args.Iterator.init(init.minimal.args);
    defer args.deinit();
    _ = args.skip();
    const first = std.mem.sliceTo(args.next() orelse return error.KeyCountRequired, 0);
    if (std.mem.eql(u8, first, "curve")) {
        if (args.next()) |allocator_name| if (!std.mem.eql(u8, std.mem.sliceTo(allocator_name, 0), "native")) return error.InvalidAllocator;
        if (args.next() != null) return error.InvalidArguments;
        return runCurve(init);
    }
    const keys = try std.fmt.parseInt(usize, first, 10);
    const value_len = try std.fmt.parseInt(usize, std.mem.sliceTo(args.next() orelse return error.ValueBytesRequired, 0), 10);
    const allocator_name = if (args.next()) |arg| std.mem.sliceTo(arg, 0) else "native";
    const child = if (std.mem.eql(u8, allocator_name, "native")) init.gpa else if (std.mem.eql(u8, allocator_name, "smp")) std.heap.smp_allocator else return error.InvalidAllocator;
    const churn = if (args.next()) |arg| if (std.mem.eql(u8, std.mem.sliceTo(arg, 0), "churn")) true else return error.InvalidArguments else false;
    if (keys == 0 or keys > 1_000_000 or value_len == 0 or value_len > 4096 or args.next() != null) return error.InvalidArguments;
    var counted = Counted{ .child = child };
    std.debug.print("{{\"phase\":\"configuration\",\"keys\":{d},\"value_bytes\":{d},\"key_bytes\":16,\"allocator\":\"{s}\",\"churn\":{},\"native_gpa_is_c\":{},\"native_gpa_is_smp\":{},\"selected_is_c\":{},\"selected_is_smp\":{},\"page_bytes\":{d},\"modeled_smp_slab_bytes\":{d},\"map_residency_is_page_coverage_sum\":true}}\n", .{ keys, value_len, allocator_name, churn, init.gpa.vtable == std.heap.c_allocator.vtable, init.gpa.vtable == std.heap.smp_allocator.vtable, child.vtable == std.heap.c_allocator.vtable, child.vtable == std.heap.smp_allocator.vtable, std.heap.pageSize(), @max(std.heap.page_size_max, 64 * 1024) });
    // Representative usable bytes exclude malloc's hidden chunk metadata and
    // are not RSS. Never call this libc API on an SMP-owned pointer.
    if (child.vtable == std.heap.c_allocator.vtable) for ([_]usize{ 16, 32, 64, 256, 257, 4096 }) |size| {
        const allocation = try child.alloc(u8, size);
        defer child.free(allocation);
        std.debug.print("{{\"phase\":\"libc_usable_sample\",\"requested\":{d},\"usable\":{d}}}\n", .{ size, malloc_usable_size(@ptrCast(allocation.ptr)) });
    };
    try report("before", null, &counted);
    var store = ConcurrentKV.init(counted.allocator(), init.io);
    store.initStripes();
    store.updateClock();
    var live = true;
    defer if (live) store.deinit();
    var key_buf: [16]u8 = undefined;
    var value_buf: [4097]u8 = undefined;
    const value = value_buf[0..value_len];
    @memset(value, 'a');
    const insert_started = try monotonicNs();
    for (0..keys) |i| {
        const key = try std.fmt.bufPrint(&key_buf, "vex:key::{d}", .{1_000_000 + i});
        std.debug.assert(key.len == 16);
        try store.set(key, value);
    }
    reportTiming("insert_timing", (try monotonicNs()) - insert_started, keys);
    try report("loaded", &store, &counted);
    @memset(value, 'b');
    const overwrite_started = try monotonicNs();
    for (0..keys) |i| {
        const key = try std.fmt.bufPrint(&key_buf, "vex:key::{d}", .{1_000_000 + i});
        try store.set(key, value);
    }
    reportTiming("overwrite_timing", (try monotonicNs()) - overwrite_started, keys);
    // No allocating GET calls: they would alter the allocation histogram.
    var found: usize = 0;
    for (&store.stripes) |*stripe| {
        var it = stripe.map.iterator();
        while (it.next()) |entry| {
            const bytes = valueBytes(entryPointer(entry.value_ptr));
            if (bytes.len != value_len or !std.mem.allEqual(u8, bytes, 'b')) return error.ValueMismatch;
            found += 1;
        }
    }
    if (found != keys) return error.KeyCountMismatch;
    try report("overwritten", &store, &counted);
    if (churn) {
        for (0..keys) |i| {
            const key = try std.fmt.bufPrint(&key_buf, "vex:key::{d}", .{1_000_000 + i});
            if (!store.delete(key)) return error.DeleteFailed;
        }
        if (store.dbsize() != 0) return error.KeyCountMismatch;
        try report("deleted", &store, &counted);
        @memset(&value_buf, 'c');
        for (0..keys) |i| {
            const key = try std.fmt.bufPrint(&key_buf, "vex:key::{d}", .{1_000_000 + i});
            try store.set(key, value_buf[0 .. value_len + (i % 2)]);
        }
        if (store.dbsize() != keys) return error.KeyCountMismatch;
        for (&store.stripes) |*stripe| {
            var it = stripe.map.iterator();
            while (it.next()) |entry| {
                const index = (try std.fmt.parseInt(usize, entry.key_ptr.*[9..], 10)) - 1_000_000;
                const bytes = valueBytes(entryPointer(entry.value_ptr));
                if (bytes.len != value_len + (index % 2) or !std.mem.allEqual(u8, bytes, 'c')) return error.ValueMismatch;
            }
        }
        try report("mixed_reloaded", &store, &counted);
        store.flushdb();
        if (store.dbsize() != 0 or counted.live_requested != 0) return error.TrackedLeak;
        try report("flushed", &store, &counted);
    }
    store.deinit();
    live = false;
    if (counted.live_requested != 0) return error.TrackedLeak;
    try report("deinitialized", null, &counted);
}
