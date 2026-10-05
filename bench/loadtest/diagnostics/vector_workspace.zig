//! Query-only HNSW workspace screen.
//!
//! Compile through a temporary root shim exporting HnswIndex, SearchWorkspace,
//! and VectorStore. Usage:
//!   vector-workspace control|workspace COUNT DIM QUERIES timed|accounting [STRIDE] [TOTAL_QUERIES]
//!
//! The graph uses deterministic write-buffer vectors. Correctness and exact
//! recall run before timing on one immutable graph and query set. TOTAL_QUERIES
//! repeats that fixed set for steady-state samples.
const std = @import("std");
const root = @import("root");
const HnswIndex = root.HnswIndex;
const SearchWorkspace = root.SearchWorkspace;
const VectorStore = root.VectorStore;
const Allocator = std.mem.Allocator;
const Alignment = std.mem.Alignment;

extern "c" fn clock_gettime(clock_id: c_int, timestamp: *Timespec) c_int;
extern "c" fn open(path: [*:0]const u8, flags: c_int) c_int;
extern "c" fn read(fd: c_int, buffer: [*]u8, len: usize) isize;
extern "c" fn close(fd: c_int) c_int;
const Timespec = extern struct { sec: i64, nsec: i64 };

const K: u32 = 10;
const Ef: u16 = 50;

const Stats = struct { alloc_calls: u64, free_calls: u64, live_bytes: usize, peak_bytes: usize };
const Counted = struct {
    child: Allocator,
    alloc_calls: u64 = 0,
    free_calls: u64 = 0,
    live_bytes: usize = 0,
    peak_bytes: usize = 0,

    fn allocator(self: *@This()) Allocator {
        return .{ .ptr = self, .vtable = &.{ .alloc = alloc, .resize = resize, .remap = remap, .free = free } };
    }
    fn snapshot(self: *const @This()) Stats {
        return .{ .alloc_calls = self.alloc_calls, .free_calls = self.free_calls, .live_bytes = self.live_bytes, .peak_bytes = self.peak_bytes };
    }
    fn resetStats(self: *@This()) void {
        self.alloc_calls = 0;
        self.free_calls = 0;
        self.peak_bytes = self.live_bytes;
    }
    fn alloc(ctx: *anyopaque, len: usize, alignment: Alignment, ra: usize) ?[*]u8 {
        const self: *@This() = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawAlloc(len, alignment, ra) orelse return null;
        self.alloc_calls += 1;
        self.live_bytes += len;
        self.peak_bytes = @max(self.peak_bytes, self.live_bytes);
        return ptr;
    }
    fn resize(ctx: *anyopaque, memory: []u8, alignment: Alignment, len: usize, ra: usize) bool {
        const self: *@This() = @ptrCast(@alignCast(ctx));
        if (!self.child.rawResize(memory, alignment, len, ra)) return false;
        self.live_bytes = self.live_bytes - memory.len + len;
        self.peak_bytes = @max(self.peak_bytes, self.live_bytes);
        return true;
    }
    fn remap(ctx: *anyopaque, memory: []u8, alignment: Alignment, len: usize, ra: usize) ?[*]u8 {
        const self: *@This() = @ptrCast(@alignCast(ctx));
        const ptr = self.child.rawRemap(memory, alignment, len, ra) orelse return null;
        self.live_bytes = self.live_bytes - memory.len + len;
        self.peak_bytes = @max(self.peak_bytes, self.live_bytes);
        return ptr;
    }
    fn free(ctx: *anyopaque, memory: []u8, alignment: Alignment, ra: usize) void {
        const self: *@This() = @ptrCast(@alignCast(ctx));
        self.child.rawFree(memory, alignment, ra);
        self.free_calls += 1;
        self.live_bytes -= memory.len;
    }
};

fn monotonicNs() !u64 {
    var now: Timespec = undefined;
    if (clock_gettime(1, &now) != 0 or now.sec < 0 or now.nsec < 0 or now.nsec >= 1_000_000_000) return error.ClockFailed;
    return @as(u64, @intCast(now.sec)) * 1_000_000_000 + @as(u64, @intCast(now.nsec));
}

fn processCpuNs() !u64 {
    var now: Timespec = undefined;
    if (clock_gettime(2, &now) != 0 or now.sec < 0 or now.nsec < 0) return error.ClockFailed;
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

fn nextRandom(state: *u64) f32 {
    state.* ^= state.* << 13;
    state.* ^= state.* >> 7;
    state.* ^= state.* << 17;
    return @as(f32, @floatFromInt(state.* & 0xFFFF)) / 65535.0 - 0.5;
}

fn checksum(results: []const HnswIndex.SearchResult) u64 {
    var sum: u64 = 0;
    for (results) |result| {
        sum = sum *% 1_000_003 +% result.node_id;
        sum = sum *% 1_000_003 +% @as(u64, @as(u32, @bitCast(result.distance)));
    }
    return sum;
}

fn sameResults(a: []const HnswIndex.SearchResult, b: []const HnswIndex.SearchResult) bool {
    if (a.len != b.len) return false;
    for (a, b) |left, right| {
        if (left.node_id != right.node_id or @as(u32, @bitCast(left.distance)) != @as(u32, @bitCast(right.distance))) return false;
    }
    return true;
}

fn exactTopK(allocator: Allocator, vectors: *const VectorStore, field_id: u16, query: []const f32, count: u32, stride: u32) ![]HnswIndex.SearchResult {
    var exact = std.array_list.Managed(HnswIndex.SearchResult).init(allocator);
    defer exact.deinit();
    for (0..count) |i| {
        const id: u32 = @intCast(i * stride);
        const vector = vectors.getById(id, field_id) orelse continue;
        const candidate = HnswIndex.SearchResult{ .node_id = id, .distance = VectorStore.cosineDistance(vector, query) };
        var pos: usize = 0;
        while (pos < exact.items.len and exact.items[pos].distance <= candidate.distance) : (pos += 1) {}
        try exact.insert(pos, candidate);
        if (exact.items.len > K) exact.items.len = K;
    }
    return exact.toOwnedSlice();
}

fn runQuery(index: *const HnswIndex, workspace: *SearchWorkspace, mode: []const u8, query: []const f32) ![]HnswIndex.SearchResult {
    return if (std.mem.eql(u8, mode, "workspace"))
        index.searchWithWorkspace(workspace, query, K, null)
    else
        index.search(query, K, null);
}

fn percentile(samples: []u64, p: usize) u64 {
    std.sort.pdq(u64, samples, {}, std.sort.asc(u64));
    return samples[@min(samples.len - 1, (samples.len * p) / 100)];
}

pub fn main(init: std.process.Init) !void {
    var args = std.process.Args.Iterator.init(init.minimal.args);
    defer args.deinit();
    _ = args.skip();
    const mode = std.mem.sliceTo(args.next() orelse return error.ModeRequired, 0);
    const count = try std.fmt.parseInt(u32, std.mem.sliceTo(args.next() orelse return error.CountRequired, 0), 10);
    const dim = try std.fmt.parseInt(u32, std.mem.sliceTo(args.next() orelse return error.DimensionRequired, 0), 10);
    const fixed_queries = try std.fmt.parseInt(usize, std.mem.sliceTo(args.next() orelse return error.QueryCountRequired, 0), 10);
    const phase = std.mem.sliceTo(args.next() orelse return error.PhaseRequired, 0);
    const stride = if (args.next()) |arg| try std.fmt.parseInt(u32, std.mem.sliceTo(arg, 0), 10) else 1;
    const total_queries = if (args.next()) |arg| try std.fmt.parseInt(usize, std.mem.sliceTo(arg, 0), 10) else fixed_queries;
    const accounting = std.mem.eql(u8, phase, "accounting");
    if ((!std.mem.eql(u8, mode, "control") and !std.mem.eql(u8, mode, "workspace")) or
        (!std.mem.eql(u8, phase, "timed") and !std.mem.eql(u8, phase, "accounting")) or
        count < K or dim == 0 or fixed_queries == 0 or total_queries == 0 or stride == 0 or args.next() != null) return error.InvalidArguments;

    const core = std.heap.c_allocator;
    var core_counted = Counted{ .child = core };
    var scratch_counted = Counted{ .child = core };
    const core_allocator: Allocator = if (accounting) core_counted.allocator() else core;
    const scratch_allocator: Allocator = if (accounting) scratch_counted.allocator() else core;
    const rss_before = rssBytes() catch 0;
    var vs = VectorStore.init(core_allocator);
    var vs_live = true;
    defer if (vs_live) vs.deinit();
    const field = "workspace_fixture";
    var state: u64 = 0x4d595df4d0f33173;
    const dim_usize = @as(usize, dim);
    const vector = try core_allocator.alloc(f32, dim_usize);
    var vector_live = true;
    defer if (vector_live) core_allocator.free(vector);
    for (0..count) |i| {
        for (vector) |*value| value.* = nextRandom(&state);
        try vs.set(@intCast(i * stride), field, vector);
    }

    var index = HnswIndex.init(core_allocator, dim, &vs, 0);
    var index_live = true;
    defer if (index_live) index.deinit();
    index.ef_search = Ef;
    for (0..count) |i| try index.insert(@intCast(i * stride));
    const rss_loaded = rssBytes() catch 0;

    var queries = try core_allocator.alloc(f32, fixed_queries * dim_usize);
    var queries_live = true;
    defer if (queries_live) core_allocator.free(queries);
    for (0..fixed_queries) |qi| {
        const offset = qi * dim_usize;
        for (queries[offset..][0..dim_usize]) |*value| value.* = nextRandom(&state);
        VectorStore.normalize(queries[offset..][0..dim_usize]);
    }

    var correctness_workspace = SearchWorkspace.init(scratch_allocator);
    var correctness_live = true;
    defer if (correctness_live) correctness_workspace.deinit();
    var recall_hits: usize = 0;
    var recall_total: usize = 0;
    var correctness_checksum: u64 = 0;
    for (0..fixed_queries) |qi| {
        const query = queries[qi * dim_usize ..][0..dim_usize];
        const control = try index.search(query, K, null);
        defer core_allocator.free(control);
        const candidate = try index.searchWithWorkspace(&correctness_workspace, query, K, null);
        defer core_allocator.free(candidate);
        if (!sameResults(control, candidate)) return error.SearchMismatch;
        correctness_checksum +%= checksum(candidate);
        const exact = try exactTopK(core_allocator, &vs, 0, query, count, stride);
        defer core_allocator.free(exact);
        recall_total += exact.len;
        for (candidate) |result| for (exact) |truth| if (result.node_id == truth.node_id) {
            recall_hits += 1;
            break;
        };
    }
    correctness_workspace.deinit();
    correctness_live = false;
    if (accounting) {
        core_counted.resetStats();
        scratch_counted.resetStats();
    }
    {
        var timed_workspace = SearchWorkspace.init(scratch_allocator);
        var timed_workspace_live = true;
        defer if (timed_workspace_live) timed_workspace.deinit();
        const cold_before_core = if (accounting) core_counted.snapshot() else Stats{ .alloc_calls = 0, .free_calls = 0, .live_bytes = 0, .peak_bytes = 0 };
        const cold_before_scratch = if (accounting) scratch_counted.snapshot() else Stats{ .alloc_calls = 0, .free_calls = 0, .live_bytes = 0, .peak_bytes = 0 };
        const cold_started = try monotonicNs();
        const cold = try runQuery(&index, &timed_workspace, mode, queries[0..dim_usize]);
        const cold_checksum = checksum(cold);
        core_allocator.free(cold);
        const cold_elapsed = (try monotonicNs()) - cold_started;
        const cold_after_core = if (accounting) core_counted.snapshot() else cold_before_core;
        const cold_after_scratch = if (accounting) scratch_counted.snapshot() else cold_before_scratch;
        for (0..fixed_queries) |qi| {
            const warm = try runQuery(&index, &timed_workspace, mode, queries[qi * dim_usize ..][0..dim_usize]);
            core_allocator.free(warm);
        }
        const samples = try core_allocator.alloc(u64, total_queries);
        defer core_allocator.free(samples);
        const before_core = if (accounting) core_counted.snapshot() else Stats{ .alloc_calls = 0, .free_calls = 0, .live_bytes = 0, .peak_bytes = 0 };
        const before_scratch = if (accounting) scratch_counted.snapshot() else Stats{ .alloc_calls = 0, .free_calls = 0, .live_bytes = 0, .peak_bytes = 0 };
        const cpu_started = try processCpuNs();
        const started = try monotonicNs();
        var timed_checksum: u64 = 0;
        for (0..total_queries) |qi| {
            const one_started = try monotonicNs();
            const query = queries[(qi % fixed_queries) * dim_usize ..][0..dim_usize];
            const results = try runQuery(&index, &timed_workspace, mode, query);
            timed_checksum +%= checksum(results);
            core_allocator.free(results);
            samples[qi] = (try monotonicNs()) - one_started;
        }
        const elapsed = (try monotonicNs()) - started;
        const cpu_elapsed = (try processCpuNs()) - cpu_started;
        const after_core = if (accounting) core_counted.snapshot() else before_core;
        const after_scratch = if (accounting) scratch_counted.snapshot() else before_scratch;
        const visited_bits: usize = timed_workspace.visitedCapacity();
        const visited_bytes = if (visited_bits == 0) 0 else ((visited_bits + @bitSizeOf(usize) - 1) / @bitSizeOf(usize) + 1) * @sizeOf(usize);
        const scratch_expected_bytes = visited_bytes + (timed_workspace.candidateCapacity() + timed_workspace.heapCapacity()) * @sizeOf(HnswIndex.SearchResult);
        if (accounting and after_scratch.live_bytes != scratch_expected_bytes) return error.ScratchAccountingMismatch;
        if (accounting and (after_core.live_bytes != before_core.live_bytes or after_scratch.live_bytes != before_scratch.live_bytes)) return error.SteadyAllocationGrowth;
        std.debug.print("{{\"phase\":\"workspace_capacity\",\"visited_bits\":{d},\"candidate_capacity\":{d},\"heap_capacity\":{d},\"expected_scratch_bytes\":{d},\"rss_after_queries\":{d}}}\n", .{ visited_bits, timed_workspace.candidateCapacity(), timed_workspace.heapCapacity(), scratch_expected_bytes, try rssBytes() });
        const p50 = percentile(samples, 50);
        const p99 = percentile(samples, 99);
        const qps = @as(u64, @intCast(@divFloor(@as(u128, total_queries) * @as(u128, std.time.ns_per_s), @max(@as(u128, elapsed), 1))));
        if (accounting) {
            std.debug.print("{{\"phase\":\"accounting\",\"allocation_counting\":true,\"mode\":\"{s}\",\"count\":{d},\"dim\":{d},\"stride\":{d},\"fixed_queries\":{d},\"total_queries\":{d},\"k\":{d},\"ef\":{d},\"cold_elapsed_ns\":{d},\"cold_checksum\":{d},\"cold_core_alloc_delta\":{d},\"cold_core_free_delta\":{d},\"cold_scratch_alloc_delta\":{d},\"cold_scratch_free_delta\":{d},\"elapsed_ns\":{d},\"qps\":{d},\"cpu_ns_per_query\":{d},\"p50_ns\":{d},\"p99_ns\":{d},\"checksum\":{d},\"correctness_checksum\":{d},\"recall\":{d:.6},\"rss_before\":{d},\"rss_loaded\":{d},\"core_alloc_delta\":{d},\"core_free_delta\":{d},\"scratch_alloc_delta\":{d},\"scratch_free_delta\":{d},\"scratch_live_bytes\":{d},\"scratch_peak_bytes\":{d}}}\n", .{ mode, count, dim, stride, fixed_queries, total_queries, K, Ef, cold_elapsed, cold_checksum, cold_after_core.alloc_calls - cold_before_core.alloc_calls, cold_after_core.free_calls - cold_before_core.free_calls, cold_after_scratch.alloc_calls - cold_before_scratch.alloc_calls, cold_after_scratch.free_calls - cold_before_scratch.free_calls, elapsed, qps, @divFloor(cpu_elapsed, @max(total_queries, 1)), p50, p99, timed_checksum, correctness_checksum, @as(f64, @floatFromInt(recall_hits)) / @as(f64, @floatFromInt(recall_total)), rss_before, rss_loaded, after_core.alloc_calls - before_core.alloc_calls, after_core.free_calls - before_core.free_calls, after_scratch.alloc_calls - before_scratch.alloc_calls, after_scratch.free_calls - before_scratch.free_calls, after_scratch.live_bytes, after_scratch.peak_bytes });
        } else {
            std.debug.print("{{\"phase\":\"timed\",\"allocation_counting\":false,\"mode\":\"{s}\",\"count\":{d},\"dim\":{d},\"stride\":{d},\"fixed_queries\":{d},\"total_queries\":{d},\"k\":{d},\"ef\":{d},\"cold_elapsed_ns\":{d},\"cold_checksum\":{d},\"elapsed_ns\":{d},\"qps\":{d},\"cpu_ns_per_query\":{d},\"p50_ns\":{d},\"p99_ns\":{d},\"checksum\":{d},\"correctness_checksum\":{d},\"recall\":{d:.6},\"rss_before\":{d},\"rss_loaded\":{d},\"core_alloc_delta\":null,\"core_free_delta\":null,\"scratch_alloc_delta\":null,\"scratch_free_delta\":null,\"scratch_live_bytes\":null,\"scratch_peak_bytes\":null}}\n", .{ mode, count, dim, stride, fixed_queries, total_queries, K, Ef, cold_elapsed, cold_checksum, elapsed, qps, @divFloor(cpu_elapsed, @max(total_queries, 1)), p50, p99, timed_checksum, correctness_checksum, @as(f64, @floatFromInt(recall_hits)) / @as(f64, @floatFromInt(recall_total)), rss_before, rss_loaded });
        }
        timed_workspace.deinit();
        timed_workspace_live = false;
    }
    index.deinit();
    index_live = false;
    vs.deinit();
    vs_live = false;
    core_allocator.free(queries);
    queries_live = false;
    core_allocator.free(vector);
    vector_live = false;
    if (accounting and (core_counted.live_bytes != 0 or scratch_counted.live_bytes != 0)) return error.CountedAllocatorLeak;
    if (accounting) {
        std.debug.print("{{\"phase\":\"teardown\",\"allocation_counting\":true,\"core_live_bytes\":{d},\"scratch_live_bytes\":{d},\"rss_complete\":{d}}}\n", .{ core_counted.live_bytes, scratch_counted.live_bytes, rssBytes() catch 0 });
    } else {
        std.debug.print("{{\"phase\":\"teardown\",\"allocation_counting\":false,\"core_live_bytes\":null,\"scratch_live_bytes\":null,\"rss_complete\":{d}}}\n", .{ rssBytes() catch 0 });
    }
}
