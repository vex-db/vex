// Migrated unit tests for src/engine/vector/hnsw.zig.

const std = @import("std");
const hnsw = @import("../../../src/engine/vector/hnsw.zig");
const VectorStore = @import("../../../src/engine/vector/vector_store.zig").VectorStore;

const HnswIndex = hnsw.HnswIndex;
const SearchWorkspace = hnsw.SearchWorkspace;
const MinHeap = hnsw.MinHeap;
const SortedCandidates = hnsw.SortedCandidates;
const Alignment = std.mem.Alignment;

const ToggleAllocator = struct {
    child: std.mem.Allocator,
    fail: bool = false,
    remaining: ?usize = null,

    fn allocator(self: *@This()) std.mem.Allocator {
        return .{ .ptr = self, .vtable = &.{ .alloc = alloc, .resize = resize, .remap = remap, .free = free } };
    }

    fn alloc(ctx: *anyopaque, len: usize, alignment: Alignment, ra: usize) ?[*]u8 {
        const self: *@This() = @ptrCast(@alignCast(ctx));
        if (self.fail) return null;
        if (self.remaining) |*remaining| {
            if (remaining.* == 0) return null;
            remaining.* -= 1;
        }
        return self.child.rawAlloc(len, alignment, ra);
    }

    fn resize(ctx: *anyopaque, memory: []u8, alignment: Alignment, len: usize, ra: usize) bool {
        const self: *@This() = @ptrCast(@alignCast(ctx));
        if (self.fail) return false;
        return self.child.rawResize(memory, alignment, len, ra);
    }

    fn remap(ctx: *anyopaque, memory: []u8, alignment: Alignment, len: usize, ra: usize) ?[*]u8 {
        const self: *@This() = @ptrCast(@alignCast(ctx));
        if (self.fail) return null;
        return self.child.rawRemap(memory, alignment, len, ra);
    }

    fn free(ctx: *anyopaque, memory: []u8, alignment: Alignment, ra: usize) void {
        const self: *@This() = @ptrCast(@alignCast(ctx));
        self.child.rawFree(memory, alignment, ra);
    }
};

fn expectWorkspaceParity(index: *const HnswIndex, workspace: *SearchWorkspace, query: []const f32, k: u32) !void {
    const control = try index.search(query, k, null);
    defer index.allocator.free(control);
    const candidate = try index.searchWithWorkspace(workspace, query, k, null);
    defer index.allocator.free(candidate);
    try std.testing.expectEqual(control.len, candidate.len);
    for (control, candidate) |left, right| {
        try std.testing.expectEqual(left.node_id, right.node_id);
        try std.testing.expectEqual(@as(u32, @bitCast(left.distance)), @as(u32, @bitCast(right.distance)));
    }
}

test "hnsw basic insert and search" {
    const allocator = std.testing.allocator;
    var vs = VectorStore.init(allocator);
    defer vs.deinit();

    const vecs = [_][3]f32{
        .{ 1.0, 0.0, 0.0 }, .{ 0.9, 0.1, 0.0 }, .{ 0.0, 1.0, 0.0 },
        .{ 0.0, 0.0, 1.0 }, .{ 0.5, 0.5, 0.0 }, .{ 0.7, 0.7, 0.0 },
        .{ 0.1, 0.9, 0.0 }, .{ 0.0, 0.1, 0.9 }, .{ 0.8, 0.2, 0.0 },
        .{ 0.3, 0.3, 0.3 },
    };
    for (vecs, 0..) |v, i| try vs.set(@intCast(i), "emb", &v);

    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    for (0..10) |i| try idx.insert(@intCast(i));

    var query = [_]f32{ 1.0, 0.0, 0.0 };
    VectorStore.normalize(&query);
    const results = try idx.search(&query, 3, null);
    defer allocator.free(results);

    try std.testing.expect(results.len >= 1);
    try std.testing.expectEqual(@as(u32, 0), results[0].node_id);
    try std.testing.expect(results[0].distance < 0.1);
}

test "hnsw empty search" {
    const allocator = std.testing.allocator;
    var vs = VectorStore.init(allocator);
    defer vs.deinit();
    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    const results = try idx.search(&[_]f32{ 1.0, 0.0, 0.0 }, 5, null);
    defer allocator.free(results);
    try std.testing.expectEqual(@as(usize, 0), results.len);
}

test "hnsw single vector" {
    const allocator = std.testing.allocator;
    var vs = VectorStore.init(allocator);
    defer vs.deinit();
    try vs.set(42, "emb", &[_]f32{ 0.5, 0.5, 0.0 });
    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    try idx.insert(42);
    var query = [_]f32{ 1.0, 0.0, 0.0 };
    VectorStore.normalize(&query);
    const results = try idx.search(&query, 1, null);
    defer allocator.free(results);
    try std.testing.expectEqual(@as(usize, 1), results.len);
    try std.testing.expectEqual(@as(u32, 42), results[0].node_id);
}

test "hnsw node alive filtering" {
    const allocator = std.testing.allocator;
    var vs = VectorStore.init(allocator);
    defer vs.deinit();
    try vs.set(0, "emb", &[_]f32{ 1.0, 0.0, 0.0 });
    try vs.set(1, "emb", &[_]f32{ 0.9, 0.1, 0.0 });
    try vs.set(2, "emb", &[_]f32{ 0.0, 1.0, 0.0 });

    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    for (0..3) |i| try idx.insert(@intCast(i));

    var alive = try std.DynamicBitSet.initFull(allocator, 3);
    defer alive.deinit();
    alive.unset(0);

    var query = [_]f32{ 1.0, 0.0, 0.0 };
    VectorStore.normalize(&query);
    const results = try idx.search(&query, 3, &alive);
    defer allocator.free(results);

    for (results) |r| try std.testing.expect(r.node_id != 0);
    if (results.len > 0) try std.testing.expectEqual(@as(u32, 1), results[0].node_id);
}

test "hnsw workspace matches control and retains returned results" {
    const allocator = std.testing.allocator;
    var vs = VectorStore.init(allocator);
    defer vs.deinit();

    const vecs = [_][3]f32{
        .{ 1.0, 0.0, 0.0 }, .{ 0.9, 0.1, 0.0 }, .{ 0.0, 1.0, 0.0 },
        .{ 0.0, 0.0, 1.0 }, .{ 0.5, 0.5, 0.0 }, .{ 0.7, 0.7, 0.0 },
    };
    for (vecs, 0..) |v, i| try vs.set(@intCast(i), "emb", &v);

    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    for (0..vecs.len) |i| try idx.insert(@intCast(i));

    var query_a = [_]f32{ 1.0, 0.0, 0.0 };
    var query_b = [_]f32{ 0.0, 1.0, 0.0 };
    VectorStore.normalize(&query_a);
    VectorStore.normalize(&query_b);
    var workspace = SearchWorkspace.init(allocator);
    defer workspace.deinit();

    const first = try idx.searchWithWorkspace(&workspace, &query_a, 3, null);
    defer allocator.free(first);
    const control_a = try idx.search(&query_a, 3, null);
    defer allocator.free(control_a);
    try std.testing.expectEqual(control_a.len, first.len);
    for (control_a, first) |control, candidate| {
        try std.testing.expectEqual(control.node_id, candidate.node_id);
        try std.testing.expectEqual(@as(u32, @bitCast(control.distance)), @as(u32, @bitCast(candidate.distance)));
    }
    var first_ids: [3]u32 = undefined;
    var first_distance_bits: [3]u32 = undefined;
    for (first, 0..) |result, i| {
        first_ids[i] = result.node_id;
        first_distance_bits[i] = @bitCast(result.distance);
    }

    const second = try idx.searchWithWorkspace(&workspace, &query_b, 3, null);
    defer allocator.free(second);
    const control_b = try idx.search(&query_b, 3, null);
    defer allocator.free(control_b);
    try std.testing.expectEqual(control_b.len, second.len);
    for (control_b, second) |control, candidate| {
        try std.testing.expectEqual(control.node_id, candidate.node_id);
        try std.testing.expectEqual(@as(u32, @bitCast(control.distance)), @as(u32, @bitCast(candidate.distance)));
    }
    for (first, 0..) |result, i| {
        try std.testing.expectEqual(first_ids[i], result.node_id);
        try std.testing.expectEqual(first_distance_bits[i], @as(u32, @bitCast(result.distance)));
    }

    var workspace_two = SearchWorkspace.init(allocator);
    defer workspace_two.deinit();
    const independent = try idx.searchWithWorkspace(&workspace_two, &query_a, 3, null);
    defer allocator.free(independent);
    try std.testing.expectEqual(first.len, independent.len);
    for (first, independent) |left, right| try std.testing.expectEqual(left.node_id, right.node_id);
}

test "hnsw workspace grows for sparse node IDs" {
    const allocator = std.testing.allocator;
    var vs = VectorStore.init(allocator);
    defer vs.deinit();
    try vs.set(3, "emb", &[_]f32{ 1.0, 0.0, 0.0 });
    try vs.set(1000, "emb", &[_]f32{ 0.0, 1.0, 0.0 });

    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    try idx.insert(3);
    var workspace = SearchWorkspace.init(allocator);
    defer workspace.deinit();
    var query = [_]f32{ 1.0, 0.0, 0.0 };
    VectorStore.normalize(&query);
    const first = try idx.searchWithWorkspace(&workspace, &query, 1, null);
    defer allocator.free(first);
    try std.testing.expectEqual(@as(u32, 3), first[0].node_id);
    try std.testing.expectEqual(@as(u32, 4), workspace.visitedCapacity());

    try idx.insert(1000);
    const second = try idx.searchWithWorkspace(&workspace, &query, 2, null);
    defer allocator.free(second);
    try std.testing.expectEqual(@as(u32, 1001), workspace.visitedCapacity());
    try std.testing.expectEqual(@as(u32, 3), second[0].node_id);
}

test "hnsw workspace and scratch recover from allocation failures" {
    const allocator = std.testing.allocator;
    var vs = VectorStore.init(allocator);
    defer vs.deinit();
    try vs.set(0, "emb", &[_]f32{ 1.0, 0.0, 0.0 });
    try vs.set(1, "emb", &[_]f32{ 0.0, 1.0, 0.0 });
    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    try idx.insert(0);
    try idx.insert(1);
    var query = [_]f32{ 1.0, 0.0, 0.0 };
    VectorStore.normalize(&query);

    var visited_fault = ToggleAllocator{ .child = allocator, .fail = true };
    var visited_workspace = SearchWorkspace.init(visited_fault.allocator());
    defer visited_workspace.deinit();
    try std.testing.expectError(error.OutOfMemory, idx.searchWithWorkspace(&visited_workspace, &query, 1, null));
    visited_fault.fail = false;
    const recovered = try idx.searchWithWorkspace(&visited_workspace, &query, 1, null);
    defer allocator.free(recovered);
    try std.testing.expectEqual(@as(u32, 0), recovered[0].node_id);

    var candidate_fault = ToggleAllocator{ .child = allocator, .fail = true };
    var candidates = SortedCandidates.init();
    defer candidates.deinit(candidate_fault.allocator());
    try std.testing.expectError(error.OutOfMemory, candidates.add(candidate_fault.allocator(), .{ .node_id = 0, .distance = 0.0 }));
    candidate_fault.fail = false;
    try candidates.add(candidate_fault.allocator(), .{ .node_id = 0, .distance = 0.0 });
    candidates.reset();

    var heap_fault = ToggleAllocator{ .child = allocator, .fail = true };
    var heap = MinHeap.init(heap_fault.allocator());
    defer heap.deinit();
    try std.testing.expectError(error.OutOfMemory, heap.push(.{ .node_id = 0, .distance = 0.0 }));
    heap_fault.fail = false;
    try heap.push(.{ .node_id = 0, .distance = 0.0 });
    heap.reset();

    var result_fault = ToggleAllocator{ .child = allocator, .fail = true };
    var result_workspace = SearchWorkspace.init(allocator);
    defer result_workspace.deinit();
    const warm = try idx.searchWithWorkspace(&result_workspace, &query, 1, null);
    defer allocator.free(warm);
    idx.allocator = result_fault.allocator();
    try std.testing.expectError(error.OutOfMemory, idx.searchWithWorkspace(&result_workspace, &query, 1, null));
    idx.allocator = allocator;
    result_fault.fail = false;
    const result_recovered = try idx.searchWithWorkspace(&result_workspace, &query, 1, null);
    defer allocator.free(result_recovered);
    try std.testing.expectEqual(@as(u32, 0), result_recovered[0].node_id);

    var fail_at: usize = 0;
    while (fail_at < 12) : (fail_at += 1) {
        var mid_search_fault = ToggleAllocator{ .child = allocator, .remaining = fail_at };
        var mid_search_workspace = SearchWorkspace.init(mid_search_fault.allocator());
        const attempt = idx.searchWithWorkspace(&mid_search_workspace, &query, 1, null) catch null;
        if (attempt) |results| allocator.free(results);
        mid_search_fault.remaining = null;
        const recovered_again = try idx.searchWithWorkspace(&mid_search_workspace, &query, 1, null);
        defer allocator.free(recovered_again);
        mid_search_workspace.deinit();
    }
}

test "hnsw workspace parity matrix and cross-index reuse" {
    const allocator = std.testing.allocator;
    var vs = VectorStore.init(allocator);
    defer vs.deinit();
    try vs.set(1, "emb", &[_]f32{ 1.0, 0.0, 0.0 });
    try vs.set(7, "emb", &[_]f32{ 1.0, 0.0, 0.0 });

    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    try idx.insert(1);
    try idx.insert(7);
    var empty = HnswIndex.init(allocator, 3, &vs, 0);
    defer empty.deinit();
    var workspace = SearchWorkspace.init(allocator);
    defer workspace.deinit();
    var query = [_]f32{ 1.0, 0.0, 0.0 };
    VectorStore.normalize(&query);

    var alive = try std.DynamicBitSet.initFull(allocator, 8);
    defer alive.deinit();
    alive.unset(1);
    for ([_]u32{ 0, 1, 10 }) |k| {
        for ([_]u16{ 1, 50 }) |ef| {
            idx.ef_search = ef;
            const control = try idx.search(&query, k, &alive);
            defer allocator.free(control);
            const candidate = try idx.searchWithWorkspace(&workspace, &query, k, &alive);
            defer allocator.free(candidate);
            try std.testing.expectEqual(control.len, candidate.len);
            for (control, candidate) |left, right| {
                try std.testing.expectEqual(left.node_id, right.node_id);
                try std.testing.expectEqual(@as(u32, @bitCast(left.distance)), @as(u32, @bitCast(right.distance)));
            }
        }
    }

    const empty_control = try empty.search(&query, 10, null);
    defer allocator.free(empty_control);
    const empty_candidate = try empty.searchWithWorkspace(&workspace, &query, 10, null);
    defer allocator.free(empty_candidate);
    try std.testing.expectEqual(@as(usize, 0), empty_candidate.len);
    idx.entry_point = null;
    const deleted_control = try idx.search(&query, 10, null);
    defer allocator.free(deleted_control);
    const deleted_candidate = try idx.searchWithWorkspace(&workspace, &query, 10, null);
    defer allocator.free(deleted_candidate);
    try std.testing.expectEqual(deleted_control.len, deleted_candidate.len);
}

test "hnsw retained workspace growth failures preserve reuse and tie order" {
    const allocator = std.testing.allocator;
    const query = [_]f32{ 1.0, 0.0, 0.0 };
    var vs = VectorStore.init(allocator);
    defer vs.deinit();
    for (0..32) |id| try vs.set(@intCast(id), "emb", &query);

    var index = HnswIndex.init(allocator, 3, &vs, 0);
    defer index.deinit();
    try index.insert(0);
    var visited_fault = ToggleAllocator{ .child = allocator };
    var workspace = SearchWorkspace.init(visited_fault.allocator());
    defer workspace.deinit();
    try expectWorkspaceParity(&index, &workspace, &query, 1);
    const original_capacity = workspace.visitedCapacity();

    for (1..32) |id| try index.insert(@intCast(id));
    visited_fault.fail = true;
    try std.testing.expectError(error.OutOfMemory, index.searchWithWorkspace(&workspace, &query, 32, null));
    try std.testing.expectEqual(original_capacity, workspace.visitedCapacity());
    visited_fault.fail = false;
    try expectWorkspaceParity(&index, &workspace, &query, 32);
    try std.testing.expectEqual(@as(u32, 32), workspace.visitedCapacity());

    // Nonempty small -> large -> small -> large reuse must clear old visits.
    var small = HnswIndex.init(allocator, 3, &vs, 0);
    defer small.deinit();
    try small.insert(0);
    try small.insert(1);
    try expectWorkspaceParity(&small, &workspace, &query, 32);
    try std.testing.expectEqual(@as(u32, 32), workspace.visitedCapacity());
    try expectWorkspaceParity(&index, &workspace, &query, 32);

    // All 32 equal vectors remain unfiltered, exposing exact tie ordering.
    const tied = try index.searchWithWorkspace(&workspace, &query, 32, null);
    defer allocator.free(tied);
    try std.testing.expectEqual(@as(usize, 32), tied.len);

    for ([_]bool{ true, false }) |fail_candidates| {
        var growth_fault = ToggleAllocator{ .child = allocator };
        var retained = SearchWorkspace.init(allocator);
        defer retained.deinit();
        index.ef_search = 1;
        try expectWorkspaceParity(&index, &retained, &query, 1);
        const original_backing_capacity = if (fail_candidates) retained.candidateCapacity() else retained.heapCapacity();
        try std.testing.expect(original_backing_capacity > 0 and original_backing_capacity < 31);

        // Only the selected already-populated backing buffer can fail. Both
        // alloc and resize/remap fail, so allocator in-place growth cannot hide it.
        if (fail_candidates) {
            retained.candidates.buf.allocator = growth_fault.allocator();
        } else {
            retained.working.buf.allocator = growth_fault.allocator();
        }
        growth_fault.fail = true;
        index.ef_search = 50;
        try std.testing.expectError(error.OutOfMemory, index.searchWithWorkspace(&retained, &query, 32, null));
        try std.testing.expectEqual(@as(usize, 0), retained.candidates.len);
        try std.testing.expectEqual(@as(usize, 0), retained.working.len);
        const failed_capacity = if (fail_candidates) retained.candidateCapacity() else retained.heapCapacity();
        try std.testing.expectEqual(original_backing_capacity, failed_capacity);

        growth_fault.fail = false;
        try expectWorkspaceParity(&index, &retained, &query, 32);
        const recovered_capacity = if (fail_candidates) retained.candidateCapacity() else retained.heapCapacity();
        try std.testing.expect(recovered_capacity > original_backing_capacity);
    }
}

test "min heap push pop order" {
    const allocator = std.testing.allocator;
    var heap = MinHeap.init(allocator);
    defer heap.deinit();

    try heap.push(.{ .node_id = 3, .distance = 0.5 });
    try heap.push(.{ .node_id = 1, .distance = 0.1 });
    try heap.push(.{ .node_id = 2, .distance = 0.3 });

    // Should pop in ascending distance order
    try std.testing.expectApproxEqAbs(@as(f32, 0.1), heap.pop().distance, 0.001);
    try std.testing.expectApproxEqAbs(@as(f32, 0.3), heap.pop().distance, 0.001);
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), heap.pop().distance, 0.001);
}

test "sorted candidates binary search insert" {
    const allocator = std.testing.allocator;
    var sc = SortedCandidates.init();
    defer sc.deinit(allocator);

    try sc.add(allocator, .{ .node_id = 3, .distance = 0.5 });
    try sc.add(allocator, .{ .node_id = 1, .distance = 0.1 });
    try sc.add(allocator, .{ .node_id = 2, .distance = 0.3 });

    // Should be sorted ascending
    try std.testing.expectEqual(@as(u32, 1), sc.items[0].node_id);
    try std.testing.expectEqual(@as(u32, 2), sc.items[1].node_id);
    try std.testing.expectEqual(@as(u32, 3), sc.items[2].node_id);
}

test "hnsw serialize deserialize round-trip" {
    const allocator = std.testing.allocator;

    // Clean up test files
    defer {
        _ = std.c.unlink("/tmp/vex_hnsw_test/emb.vhi");
        _ = std.c.unlink("/tmp/vex_hnsw_test/emb.vhi.tmp");
        _ = std.c.rmdir("/tmp/vex_hnsw_test");
    }
    _ = std.c.mkdir("/tmp/vex_hnsw_test", 0o755);

    var vs = VectorStore.init(allocator);
    defer vs.deinit();

    const vecs = [_][3]f32{
        .{ 1.0, 0.0, 0.0 }, .{ 0.9, 0.1, 0.0 }, .{ 0.0, 1.0, 0.0 },
        .{ 0.0, 0.0, 1.0 }, .{ 0.5, 0.5, 0.0 }, .{ 0.7, 0.7, 0.0 },
        .{ 0.1, 0.9, 0.0 }, .{ 0.0, 0.1, 0.9 }, .{ 0.8, 0.2, 0.0 },
        .{ 0.3, 0.3, 0.3 },
    };
    for (vecs, 0..) |v, i| try vs.set(@intCast(i), "emb", &v);

    // Build original index
    var idx = HnswIndex.init(allocator, 3, &vs, 0);
    defer idx.deinit();
    for (0..10) |i| try idx.insert(@intCast(i));

    // Search on original
    var query = [_]f32{ 1.0, 0.0, 0.0 };
    VectorStore.normalize(&query);
    const orig_results = try idx.search(&query, 3, null);
    defer allocator.free(orig_results);

    // Serialize
    try idx.serialize("/tmp/vex_hnsw_test", "emb");

    // Deserialize into a new index
    var idx2 = try HnswIndex.deserialize(allocator, "/tmp/vex_hnsw_test", "emb", &vs, 0);
    defer idx2.deinit();

    // Verify metadata
    try std.testing.expectEqual(idx.dim, idx2.dim);
    try std.testing.expectEqual(idx.node_count, idx2.node_count);
    try std.testing.expectEqual(idx.capacity, idx2.capacity);
    try std.testing.expectEqual(idx.max_level, idx2.max_level);
    try std.testing.expectEqual(idx.entry_point, idx2.entry_point);
    try std.testing.expectEqual(idx.M, idx2.M);
    try std.testing.expectEqual(idx.M_max0, idx2.M_max0);
    try std.testing.expectEqual(idx.rng_state, idx2.rng_state);

    // Search on deserialized — should return same results
    const deser_results = try idx2.search(&query, 3, null);
    defer allocator.free(deser_results);

    try std.testing.expectEqual(orig_results.len, deser_results.len);
    for (0..orig_results.len) |i| {
        try std.testing.expectEqual(orig_results[i].node_id, deser_results[i].node_id);
        try std.testing.expectApproxEqAbs(orig_results[i].distance, deser_results[i].distance, 0.001);
    }
}
