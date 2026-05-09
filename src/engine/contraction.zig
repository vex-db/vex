const std = @import("std");
const Allocator = std.mem.Allocator;
const graph_mod = @import("graph.zig");
const NodeId = graph_mod.NodeId;
const CSR = graph_mod.CSR;

/// Contraction Hierarchies index for O(sqrt(N)) weighted shortest path queries.
/// Built during COMPACT, invalidated on mutation_seq change.
pub const CHIndex = struct {
    allocator: Allocator,

    /// rank[node_id] = contraction order (higher = more important, contracted later)
    rank: []u32,

    /// Upward overlay graph: only edges to higher-ranked neighbors.
    up_offsets: []u32, // [node_count + 1] outgoing
    up_targets: []NodeId,
    up_weights: []f64,
    up_middle: []NodeId, // INVALID_ID for original edges

    /// Upward incoming (reverse of up_out) for backward search.
    up_in_offsets: []u32,
    up_in_targets: []NodeId,
    up_in_edge_idx: []u32, // index into up_weights/up_middle

    node_count: u32,
    mutation_seq: u64,

    pub fn deinit(self: *CHIndex) void {
        self.allocator.free(self.rank);
        self.allocator.free(self.up_offsets);
        if (self.up_targets.len > 0) self.allocator.free(self.up_targets);
        if (self.up_weights.len > 0) self.allocator.free(self.up_weights);
        if (self.up_middle.len > 0) self.allocator.free(self.up_middle);
        self.allocator.free(self.up_in_offsets);
        if (self.up_in_targets.len > 0) self.allocator.free(self.up_in_targets);
        if (self.up_in_edge_idx.len > 0) self.allocator.free(self.up_in_edge_idx);
    }

    /// Outgoing upward neighbors of a node.
    pub fn upOutNeighbors(self: *const CHIndex, node: NodeId) []const NodeId {
        if (node + 1 >= self.up_offsets.len) return &.{};
        const s = self.up_offsets[node];
        const e = self.up_offsets[node + 1];
        if (s >= e) return &.{};
        return self.up_targets[s..e];
    }

    pub fn upOutWeights(self: *const CHIndex, node: NodeId) []const f64 {
        if (node + 1 >= self.up_offsets.len) return &.{};
        const s = self.up_offsets[node];
        const e = self.up_offsets[node + 1];
        if (s >= e) return &.{};
        return self.up_weights[s..e];
    }

    pub fn upOutMiddle(self: *const CHIndex, node: NodeId) []const NodeId {
        if (node + 1 >= self.up_offsets.len) return &.{};
        const s = self.up_offsets[node];
        const e = self.up_offsets[node + 1];
        if (s >= e) return &.{};
        return self.up_middle[s..e];
    }

    /// Incoming upward neighbors of a node (for backward search).
    pub fn upInNeighbors(self: *const CHIndex, node: NodeId) []const NodeId {
        if (node + 1 >= self.up_in_offsets.len) return &.{};
        const s = self.up_in_offsets[node];
        const e = self.up_in_offsets[node + 1];
        if (s >= e) return &.{};
        return self.up_in_targets[s..e];
    }

    pub fn upInEdgeIndices(self: *const CHIndex, node: NodeId) []const u32 {
        if (node + 1 >= self.up_in_offsets.len) return &.{};
        const s = self.up_in_offsets[node];
        const e = self.up_in_offsets[node + 1];
        if (s >= e) return &.{};
        return self.up_in_edge_idx[s..e];
    }
};

const INF: f64 = std.math.inf(f64);
const INVALID: NodeId = graph_mod.INVALID_ID;
const WITNESS_LIMIT: u32 = 20; // max nodes settled in witness search
const MAX_CONTRACT_DEGREE: usize = 10; // skip contracting nodes with higher degree

/// Build a Contraction Hierarchies index from the compacted graph.
pub fn build(
    allocator: Allocator,
    base_out: *const CSR,
    base_in: *const CSR,
    edge_weights: []const f64,
    node_alive: std.DynamicBitSet,
    edge_alive: std.DynamicBitSet,
    node_count: u32,
    mutation_seq: u64,
) !CHIndex {
    const nc: usize = node_count;

    // ── Phase 1: compute node ordering by importance ──
    var rank = try allocator.alloc(u32, nc);
    @memset(rank, 0);
    var contracted = try allocator.alloc(bool, nc);
    defer allocator.free(contracted);
    @memset(contracted, false);

    // Importance = edge_difference + contracted_neighbors
    // edge_difference = shortcuts_needed - edges_removed
    const ImportItem = struct {
        id: NodeId,
        importance: i32,

        fn order(ctx: void, a: @This(), b: @This()) std.math.Order {
            _ = ctx;
            // Min-heap: lower importance = contract first
            return std.math.order(a.importance, b.importance);
        }
    };
    var pq = std.PriorityQueue(ImportItem, void, ImportItem.order).initContext({});
    defer pq.deinit(allocator);

    // Initial importance for all live nodes
    for (0..nc) |i| {
        const nid: NodeId = @intCast(i);
        if (!node_alive.isSet(nid)) {
            contracted[i] = true;
            continue;
        }
        const imp = computeImportance(base_out, base_in, edge_weights, edge_alive, contracted, nid, node_count, allocator);
        try pq.push(allocator, .{ .id = nid, .importance = imp });
    }

    // Shortcut collection
    var sc_from = std.array_list.Managed(NodeId).init(allocator);
    defer sc_from.deinit();
    var sc_to = std.array_list.Managed(NodeId).init(allocator);
    defer sc_to.deinit();
    var sc_weight = std.array_list.Managed(f64).init(allocator);
    defer sc_weight.deinit();
    var sc_middle = std.array_list.Managed(NodeId).init(allocator);
    defer sc_middle.deinit();

    // Also track dynamic adjacency for shortcuts (so later contractions see them)
    // adj_out[u] = list of (target, weight, middle_node)
    var adj_out = try allocator.alloc(std.ArrayListUnmanaged(ShortcutEdge), nc);
    defer {
        for (adj_out) |*list| list.deinit(allocator);
        allocator.free(adj_out);
    }
    for (adj_out) |*list| list.* = .{ .items = &.{}, .capacity = 0 };

    var adj_in = try allocator.alloc(std.ArrayListUnmanaged(ShortcutEdge), nc);
    defer {
        for (adj_in) |*list| list.deinit(allocator);
        allocator.free(adj_in);
    }
    for (adj_in) |*list| list.* = .{ .items = &.{}, .capacity = 0 };

    // ── Phase 2: contract nodes in importance order ──
    var order: u32 = 0;
    while (pq.pop()) |item| {
        const v = item.id;
        if (contracted[v]) continue;

        // Lazy update: recompute importance; if it's worse than next in queue, re-insert
        const fresh_imp = computeImportance(base_out, base_in, edge_weights, edge_alive, contracted, v, node_count, allocator);
        if (pq.peek()) |next| {
            if (fresh_imp > next.importance) {
                try pq.push(allocator, .{ .id = v, .importance = fresh_imp });
                continue;
            }
        }

        rank[v] = order;
        order += 1;
        contracted[v] = true;

        // Get all non-contracted incoming/outgoing neighbors of v
        // (including shortcuts added by prior contractions)
        contractNode(
            base_out, base_in, edge_weights, edge_alive,
            contracted, v, allocator,
            &adj_out, &adj_in,
            &sc_from, &sc_to, &sc_weight, &sc_middle,
        ) catch {};
    }

    // ── Phase 3: build upward overlay CSR ──
    // Collect all upward edges: original edges (u→v where rank[u] < rank[v])
    // plus shortcuts (u→w where rank[u] < rank[w])
    var up_from = std.array_list.Managed(NodeId).init(allocator);
    defer up_from.deinit();
    var up_to_list = std.array_list.Managed(NodeId).init(allocator);
    defer up_to_list.deinit();
    var up_w = std.array_list.Managed(f64).init(allocator);
    defer up_w.deinit();
    var up_m = std.array_list.Managed(NodeId).init(allocator);
    defer up_m.deinit();

    // Original edges
    for (0..nc) |i| {
        const u: NodeId = @intCast(i);
        if (!node_alive.isSet(u)) continue;
        const targets = base_out.neighbors(u);
        const eidxs = base_out.edgeIndices(u);
        for (targets, eidxs) |v, eidx| {
            if (!edge_alive.isSet(eidx)) continue;
            if (!node_alive.isSet(v)) continue;
            if (rank[u] < rank[v]) {
                try up_from.append(u);
                try up_to_list.append(v);
                try up_w.append(edge_weights[eidx]);
                try up_m.append(INVALID);
            }
        }
    }

    // Shortcuts (directed: u→w)
    for (sc_from.items, sc_to.items, sc_weight.items, sc_middle.items) |u, w, weight, mid| {
        // Shortcuts are between non-contracted neighbors of the contracted node.
        // Both u and w have higher rank than the contracted middle node.
        // Add upward edge from the lower-ranked to the higher-ranked endpoint.
        if (rank[u] < rank[w]) {
            try up_from.append(u);
            try up_to_list.append(w);
            try up_w.append(weight);
            try up_m.append(mid);
        } else if (rank[w] < rank[u]) {
            // Reverse direction: add as w→u upward edge
            try up_from.append(w);
            try up_to_list.append(u);
            try up_w.append(weight);
            try up_m.append(mid);
        }
        // If equal rank (shouldn't happen), skip
    }

    const edge_count = up_from.items.len;

    // Build outgoing CSR
    var up_offsets = try allocator.alloc(u32, nc + 1);
    @memset(up_offsets, 0);

    // Count edges per source
    for (up_from.items) |u| up_offsets[u + 1] += 1;
    // Prefix sum
    for (1..nc + 1) |i| up_offsets[i] += up_offsets[i - 1];

    var up_targets = try allocator.alloc(NodeId, edge_count);
    var up_weights = try allocator.alloc(f64, edge_count);
    var up_middle_arr = try allocator.alloc(NodeId, edge_count);

    // Scatter edges into CSR
    var pos = try allocator.alloc(u32, nc);
    defer allocator.free(pos);
    @memcpy(pos, up_offsets[0..nc]);

    for (up_from.items, up_to_list.items, up_w.items, up_m.items) |u, v, w, m| {
        const p = pos[u];
        up_targets[p] = v;
        up_weights[p] = w;
        up_middle_arr[p] = m;
        pos[u] = p + 1;
    }

    // Build incoming CSR (reverse of upward graph)
    var up_in_offsets = try allocator.alloc(u32, nc + 1);
    @memset(up_in_offsets, 0);

    for (up_to_list.items) |v| up_in_offsets[v + 1] += 1;
    for (1..nc + 1) |i| up_in_offsets[i] += up_in_offsets[i - 1];

    var up_in_targets = try allocator.alloc(NodeId, edge_count);
    var up_in_edge_idx = try allocator.alloc(u32, edge_count);

    var pos2 = try allocator.alloc(u32, nc);
    defer allocator.free(pos2);
    @memcpy(pos2, up_in_offsets[0..nc]);

    for (up_from.items, up_to_list.items, 0..) |u, v, edge_i| {
        // Find where this edge is in the outgoing CSR to get its index
        const p = pos2[v];
        up_in_targets[p] = u;
        up_in_edge_idx[p] = @intCast(edge_i);
        pos2[v] = p + 1;
    }

    // Fix up_in_edge_idx: we need indices into up_weights/up_middle, which
    // are in CSR order. We stored the original edge list index above.
    // Since we scattered edges into CSR in order, the CSR position for edge i
    // from node u is at the position we wrote it. Let's build a mapping.
    // Actually, the up_weights/up_middle arrays are already in CSR order (built by scatter).
    // The up_in_edge_idx should point to the CSR position, not the original list index.
    // Let me fix: re-scatter with CSR positions.
    @memcpy(pos2, up_in_offsets[0..nc]);
    @memcpy(pos, up_offsets[0..nc]);

    // up_in_edge_idx stores index into up_weights/up_middle (which are in outgoing CSR order).
    // These arrays are in outgoing CSR order. For the incoming reverse, we need to find
    // which outgoing CSR slot an edge (u→v) landed in.
    // Since we process up_from in order and scatter sequentially, edge i in the list
    // went to position = initial_pos[u] + count_so_far_for_u.
    // Just track it during scatter:
    var edge_csr_pos = try allocator.alloc(u32, edge_count);
    defer allocator.free(edge_csr_pos);
    @memcpy(pos, up_offsets[0..nc]);
    for (up_from.items, 0..) |u, i| {
        edge_csr_pos[i] = pos[u];
        pos[u] += 1;
    }

    // Now re-build up_in with correct edge indices
    @memcpy(pos2, up_in_offsets[0..nc]);
    for (0..edge_count) |i| {
        const v = up_to_list.items[i];
        const p = pos2[v];
        up_in_targets[p] = up_from.items[i];
        up_in_edge_idx[p] = edge_csr_pos[i];
        pos2[v] = p + 1;
    }

    return CHIndex{
        .allocator = allocator,
        .rank = rank,
        .up_offsets = up_offsets,
        .up_targets = up_targets,
        .up_weights = up_weights,
        .up_middle = up_middle_arr,
        .up_in_offsets = up_in_offsets,
        .up_in_targets = up_in_targets,
        .up_in_edge_idx = up_in_edge_idx,
        .node_count = node_count,
        .mutation_seq = mutation_seq,
    };
}

fn computeImportance(
    base_out: *const CSR,
    base_in: *const CSR,
    edge_weights: []const f64,
    edge_alive: std.DynamicBitSet,
    contracted: []const bool,
    v: NodeId,
    node_count: u32,
    allocator: Allocator,
) i32 {
    _ = node_count;
    // Count non-contracted in/out neighbors
    var in_count: i32 = 0;
    var out_count: i32 = 0;
    var contracted_neighbors: i32 = 0;

    const in_targets = base_in.neighbors(v);
    const in_eidxs = base_in.edgeIndices(v);
    for (in_targets, in_eidxs) |u, eidx| {
        if (!edge_alive.isSet(eidx)) continue;
        if (contracted[u]) { contracted_neighbors += 1; continue; }
        in_count += 1;
    }

    const out_targets = base_out.neighbors(v);
    const out_eidxs = base_out.edgeIndices(v);
    for (out_targets, out_eidxs) |w, eidx| {
        if (!edge_alive.isSet(eidx)) continue;
        if (contracted[w]) { contracted_neighbors += 1; continue; }
        out_count += 1;
    }

    // Estimate shortcuts: for each (u, w) pair, would we need a shortcut?
    // Quick estimate: assume ~50% need shortcuts
    const shortcuts_est = @divTrunc(in_count * out_count, 2);
    const edges_removed = in_count + out_count;
    const edge_diff = shortcuts_est - edges_removed;

    _ = edge_weights;
    _ = allocator;
    return edge_diff + contracted_neighbors;
}

fn contractNode(
    base_out: *const CSR,
    base_in: *const CSR,
    edge_weights: []const f64,
    edge_alive: std.DynamicBitSet,
    contracted: []const bool,
    v: NodeId,
    allocator: Allocator,
    adj_out: *[]std.ArrayListUnmanaged(ShortcutEdge),
    adj_in: *[]std.ArrayListUnmanaged(ShortcutEdge),
    sc_from: *std.array_list.Managed(NodeId),
    sc_to: *std.array_list.Managed(NodeId),
    sc_weight: *std.array_list.Managed(f64),
    sc_middle: *std.array_list.Managed(NodeId),
) !void {
    // Skip high-degree nodes — too many witness searches needed
    const in_deg = base_in.neighbors(v).len + adj_in.*[v].items.len;
    const out_deg = base_out.neighbors(v).len + adj_out.*[v].items.len;
    if (in_deg > MAX_CONTRACT_DEGREE or out_deg > MAX_CONTRACT_DEGREE) return;

    // Collect incoming edges to v (non-contracted sources)
    const InEdge = struct { from: NodeId, weight: f64 };
    var in_edges: [128]InEdge = undefined;
    var in_count: usize = 0;

    const in_targets = base_in.neighbors(v);
    const in_eidxs = base_in.edgeIndices(v);
    for (in_targets, in_eidxs) |u, eidx| {
        if (!edge_alive.isSet(eidx)) continue;
        if (contracted[u]) continue;
        if (in_count < 128) {
            in_edges[in_count] = .{ .from = u, .weight = edge_weights[eidx] };
            in_count += 1;
        }
    }
    // Also check shortcut adjacency
    for (adj_in.*[v].items) |se| {
        if (contracted[se.target]) continue;
        if (in_count < 128) {
            in_edges[in_count] = .{ .from = se.target, .weight = se.weight };
            in_count += 1;
        }
    }

    // Collect outgoing edges from v
    const OutEdge = struct { to: NodeId, weight: f64 };
    var out_edges: [128]OutEdge = undefined;
    var out_count: usize = 0;

    const out_targets = base_out.neighbors(v);
    const out_eidxs = base_out.edgeIndices(v);
    for (out_targets, out_eidxs) |w, eidx| {
        if (!edge_alive.isSet(eidx)) continue;
        if (contracted[w]) continue;
        if (out_count < 128) {
            out_edges[out_count] = .{ .to = w, .weight = edge_weights[eidx] };
            out_count += 1;
        }
    }
    for (adj_out.*[v].items) |se| {
        if (contracted[se.target]) continue;
        if (out_count < 128) {
            out_edges[out_count] = .{ .to = se.target, .weight = se.weight };
            out_count += 1;
        }
    }

    // For each (u, w) pair: check if shortcut needed via witness search
    for (in_edges[0..in_count]) |in_e| {
        for (out_edges[0..out_count]) |out_e| {
            if (in_e.from == out_e.to) continue; // skip self-loops
            const shortcut_weight = in_e.weight + out_e.weight;

            // Is there a witness path u→w not through v that's <= shortcut_weight?
            const has_witness = witnessExists(
                base_out, edge_weights, edge_alive, contracted,
                adj_out.*, in_e.from, v, out_e.to, shortcut_weight,
                allocator,
            );

            if (!has_witness) {
                // Need shortcut u→w
                try sc_from.append(in_e.from);
                try sc_to.append(out_e.to);
                try sc_weight.append(shortcut_weight);
                try sc_middle.append(v);

                // Add to dynamic adjacency for future contractions
                try adj_out.*[in_e.from].append(allocator, .{
                    .target = out_e.to,
                    .weight = shortcut_weight,
                    .middle = v,
                });
                try adj_in.*[out_e.to].append(allocator, .{
                    .target = in_e.from,
                    .weight = shortcut_weight,
                    .middle = v,
                });
            }
        }
    }
}

const ShortcutEdge = struct { target: NodeId, weight: f64, middle: NodeId };

/// Local Dijkstra from `start`, skipping `skip_node` and contracted nodes.
/// Uses sparse HashMap instead of full distance array for memory efficiency.
/// Returns true if `target` is reachable within `max_dist`.
fn witnessExists(
    base_out: *const CSR,
    edge_weights: []const f64,
    edge_alive: std.DynamicBitSet,
    contracted: []const bool,
    adj_out: []const std.ArrayListUnmanaged(ShortcutEdge),
    start: NodeId,
    skip_node: NodeId,
    target: NodeId,
    max_dist: f64,
    allocator: Allocator,
) bool {
    const WItem = struct {
        id: NodeId,
        d: f64,
        fn order(ctx: void, a: @This(), b: @This()) std.math.Order {
            _ = ctx;
            return std.math.order(a.d, b.d);
        }
    };

    var dist = std.AutoHashMap(NodeId, f64).init(allocator);
    defer dist.deinit();
    var pq = std.PriorityQueue(WItem, void, WItem.order).initContext({});
    defer pq.deinit(allocator);

    dist.put(start, 0) catch return false;
    pq.push(allocator, .{ .id = start, .d = 0 }) catch return false;

    var settled: u32 = 0;
    while (pq.pop()) |cur| {
        const cd = dist.get(cur.id) orelse continue;
        if (cur.d > cd) continue;
        if (cur.d > max_dist) break;
        if (cur.id == target) return true;
        if (settled >= WITNESS_LIMIT) break;
        settled += 1;

        // Base graph edges
        const targets = base_out.neighbors(cur.id);
        const eidxs = base_out.edgeIndices(cur.id);
        for (targets, eidxs) |nid, eidx| {
            if (nid == skip_node) continue;
            if (contracted[nid]) continue;
            if (!edge_alive.isSet(eidx)) continue;
            const nd = cur.d + edge_weights[eidx];
            const existing = dist.get(nid);
            if (existing == null or nd < existing.?) {
                dist.put(nid, nd) catch continue;
                pq.push(allocator, .{ .id = nid, .d = nd }) catch continue;
            }
        }

        // Shortcut edges
        if (cur.id < adj_out.len) {
            for (adj_out[cur.id].items) |se| {
                if (se.target == skip_node) continue;
                if (contracted[se.target]) continue;
                const nd = cur.d + se.weight;
                const existing = dist.get(se.target);
                if (existing == null or nd < existing.?) {
                    dist.put(se.target, nd) catch continue;
                    pq.push(allocator, .{ .id = se.target, .d = nd }) catch continue;
                }
            }
        }
    }

    return false;
}

/// CH query: bidirectional upward Dijkstra on the overlay graph.
pub fn query(
    ch: *const CHIndex,
    allocator: Allocator,
    from_id: NodeId,
    to_id: NodeId,
) !struct { path: []NodeId, weight: f64 } {
    const nc = ch.node_count;

    // Flat arrays for O(1) distance lookups
    var fwd_dist = try allocator.alloc(f64, nc);
    defer allocator.free(fwd_dist);
    @memset(fwd_dist, INF);

    var bwd_dist = try allocator.alloc(f64, nc);
    defer allocator.free(bwd_dist);
    @memset(bwd_dist, INF);

    var fwd_parent = try allocator.alloc(NodeId, nc);
    defer allocator.free(fwd_parent);
    @memset(fwd_parent, INVALID);

    var bwd_parent = try allocator.alloc(NodeId, nc);
    defer allocator.free(bwd_parent);
    @memset(bwd_parent, INVALID);

    const QItem = struct {
        id: NodeId,
        d: f64,
        fn order(ctx: void, a: @This(), b: @This()) std.math.Order {
            _ = ctx;
            return std.math.order(a.d, b.d);
        }
    };

    var fwd_pq = std.PriorityQueue(QItem, void, QItem.order).initContext({});
    defer fwd_pq.deinit(allocator);
    var bwd_pq = std.PriorityQueue(QItem, void, QItem.order).initContext({});
    defer bwd_pq.deinit(allocator);

    fwd_dist[from_id] = 0;
    try fwd_pq.push(allocator, .{ .id = from_id, .d = 0 });

    bwd_dist[to_id] = 0;
    try bwd_pq.push(allocator, .{ .id = to_id, .d = 0 });

    var best_total: f64 = INF;
    var meeting: NodeId = INVALID;

    while (fwd_pq.items.len > 0 or bwd_pq.items.len > 0) {
        const fwd_min = if (fwd_pq.peek()) |item| item.d else INF;
        const bwd_min = if (bwd_pq.peek()) |item| item.d else INF;
        if (fwd_min + bwd_min >= best_total) break;

        if (fwd_min <= bwd_min) {
            if (fwd_pq.pop()) |cur| {
                if (cur.d > fwd_dist[cur.id]) continue;
                // Check meeting
                if (bwd_dist[cur.id] < INF) {
                    const total = cur.d + bwd_dist[cur.id];
                    if (total < best_total) {
                        best_total = total;
                        meeting = cur.id;
                    }
                }
                // Expand upward outgoing
                const targets = ch.upOutNeighbors(cur.id);
                const weights = ch.upOutWeights(cur.id);
                for (targets, weights) |nid, w| {
                    const nd = cur.d + w;
                    if (nd < fwd_dist[nid]) {
                        fwd_dist[nid] = nd;
                        fwd_parent[nid] = cur.id;
                        try fwd_pq.push(allocator, .{ .id = nid, .d = nd });
                    }
                }
            }
        } else {
            if (bwd_pq.pop()) |cur| {
                if (cur.d > bwd_dist[cur.id]) continue;
                // Check meeting
                if (fwd_dist[cur.id] < INF) {
                    const total = fwd_dist[cur.id] + cur.d;
                    if (total < best_total) {
                        best_total = total;
                        meeting = cur.id;
                    }
                }
                // Expand upward incoming (backward from target)
                const targets = ch.upInNeighbors(cur.id);
                const eidxs = ch.upInEdgeIndices(cur.id);
                for (targets, eidxs) |nid, ei| {
                    const nd = cur.d + ch.up_weights[ei];
                    if (nd < bwd_dist[nid]) {
                        bwd_dist[nid] = nd;
                        bwd_parent[nid] = cur.id;
                        try bwd_pq.push(allocator, .{ .id = nid, .d = nd });
                    }
                }
            }
        }
    }

    if (meeting == INVALID) return error.PathNotFound;

    // Reconstruct path: forward (from→meeting) + backward (meeting→to)
    var path = std.array_list.Managed(NodeId).init(allocator);
    errdefer path.deinit();

    // Forward: trace meeting back to from
    {
        var cur = meeting;
        while (cur != from_id) {
            try path.append(cur);
            const p = fwd_parent[cur];
            if (p == INVALID) return error.PathNotFound;
            cur = p;
        }
        try path.append(from_id);
        std.mem.reverse(NodeId, path.items);
    }

    // Backward: trace meeting forward to to
    {
        var cur = bwd_parent[meeting];
        if (cur != INVALID and meeting != to_id) {
            while (cur != to_id) {
                try path.append(cur);
                const p = bwd_parent[cur];
                if (p == INVALID) break;
                cur = p;
            }
            try path.append(to_id);
        } else if (meeting != to_id) {
            try path.append(to_id);
        }
    }

    // Unpack shortcuts in the path
    const unpacked = try unpackPath(ch, path.items, allocator);
    path.deinit();

    return .{ .path = unpacked, .weight = best_total };
}

/// Recursively unpack shortcuts in a CH path to produce the actual node sequence.
fn unpackPath(ch: *const CHIndex, path: []const NodeId, allocator: Allocator) ![]NodeId {
    if (path.len <= 1) {
        const result = try allocator.alloc(NodeId, path.len);
        @memcpy(result, path);
        return result;
    }

    var result = std.array_list.Managed(NodeId).init(allocator);
    errdefer result.deinit();
    try result.append(path[0]);

    for (0..path.len - 1) |i| {
        const u = path[i];
        const v = path[i + 1];
        try unpackEdge(ch, u, v, &result);
    }

    return result.toOwnedSlice();
}

fn unpackEdge(ch: *const CHIndex, u: NodeId, v: NodeId, result: *std.array_list.Managed(NodeId)) !void {
    // Find the edge u→v in the upward graph and check if it's a shortcut
    const targets = ch.upOutNeighbors(u);
    const middles = ch.upOutMiddle(u);

    for (targets, middles) |t, mid| {
        if (t == v) {
            if (mid == INVALID) {
                // Original edge
                try result.append(v);
                return;
            }
            // Shortcut: u→mid→v, recursively unpack both halves
            try unpackEdge(ch, u, mid, result);
            try unpackEdge(ch, mid, v, result);
            return;
        }
    }

    // Edge not found in upward graph — might be in the downward direction
    // (backward path uses reverse). Just add the node.
    try result.append(v);
}
