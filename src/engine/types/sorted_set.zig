const std = @import("std");
const Allocator = std.mem.Allocator;

/// Hash lookup by member plus an AVL tree ordered by (score, member).
/// Existing-member score updates average O(1). Ordered reads synchronize the
/// changed members, then rank/count cost O(log n), ranges O(log n + k).
/// Callers serialize access, including key creation/deletion and flush.
pub const SortedSetStore = struct {
    pub const default_partition_count = 256;
    pub const max_partition_count = 4096;

    pub fn validatePartitionCount(count: usize) !void {
        if (count == 0 or count > max_partition_count or !std.math.isPowerOfTwo(count))
            return error.InvalidPartitionCount;
    }
    const Partition = struct {
        mutex: std.c.pthread_mutex_t align(64) = std.c.PTHREAD_MUTEX_INITIALIZER,
        map: std.StringHashMap(ZSet),
        // Collection TTLs share the command partition lock, including non-zsets.
        expirations: std.StringHashMap(i64),
    };
    partitions: []Partition,
    allocator: Allocator,

    pub fn partitionIndex(self: *const SortedSetStore, key: []const u8) usize {
        return std.hash.Wyhash.hash(0, key) & (self.partitions.len - 1);
    }

    fn mapFor(self: *SortedSetStore, key: []const u8) *std.StringHashMap(ZSet) {
        return &self.partitions[self.partitionIndex(key)].map;
    }

    /// Hold through response serialization: ZRANGE borrows member bytes.
    pub fn lockKey(self: *SortedSetStore, key: []const u8) usize {
        const index = self.partitionIndex(key);
        _ = std.c.pthread_mutex_lock(&self.partitions[index].mutex);
        return index;
    }

    pub fn tryLockKey(self: *SortedSetStore, key: []const u8) ?usize {
        const index = self.partitionIndex(key);
        return if (std.c.pthread_mutex_trylock(&self.partitions[index].mutex) == .SUCCESS) index else null;
    }

    pub fn unlockKey(self: *SortedSetStore, index: usize) void {
        _ = std.c.pthread_mutex_unlock(&self.partitions[index].mutex);
    }

    /// Global operations acquire partitions in ascending order. Never call
    /// while holding a key lock; EXEC owns all partitions for its duration.
    pub fn lockAll(self: *SortedSetStore) void {
        for (self.partitions) |*part| _ = std.c.pthread_mutex_lock(&part.mutex);
    }

    pub fn unlockAll(self: *SortedSetStore) void {
        var i: usize = self.partitions.len;
        while (i > 0) {
            i -= 1;
            self.unlockKey(i);
        }
    }

    pub fn isCommand(cmd: []const u8) bool {
        inline for (.{ "ZADD", "ZREM", "ZSCORE", "ZCARD", "ZRANK", "ZREVRANK", "ZRANGE", "ZINCRBY", "ZCOUNT" }) |name| {
            if (std.ascii.eqlIgnoreCase(cmd, name)) return true;
        }
        return false;
    }

    pub fn isWrite(cmd: []const u8) bool {
        return std.ascii.eqlIgnoreCase(cmd, "ZADD") or std.ascii.eqlIgnoreCase(cmd, "ZREM") or std.ascii.eqlIgnoreCase(cmd, "ZINCRBY");
    }

    pub const Entry = struct {
        member: []const u8,
        score: f64,
    };

    const Node = struct {
        entry: Entry, // Score currently represented in the ordered index.
        score: f64, // Latest score, also visible before the next ordered read.
        pending_next: ?*Node = null,
        pending: bool = false,
        left: ?*Node = null,
        right: ?*Node = null,
        height: i32 = 1,
        size: usize = 1,

        fn count(node: ?*Node) usize {
            return if (node) |n| n.size else 0;
        }

        fn depth(node: ?*Node) i32 {
            return if (node) |n| n.height else 0;
        }

        fn refresh(self: *Node) void {
            self.size = 1 + count(self.left) + count(self.right);
            self.height = 1 + @max(depth(self.left), depth(self.right));
        }

        fn less(a: Entry, b: Entry) bool {
            if (a.score != b.score) return a.score < b.score;
            return std.mem.order(u8, a.member, b.member) == .lt;
        }

        fn rotateLeft(self: *Node) *Node {
            const top = self.right.?;
            self.right = top.left;
            top.left = self;
            self.refresh();
            top.refresh();
            return top;
        }

        fn rotateRight(self: *Node) *Node {
            const top = self.left.?;
            self.left = top.right;
            top.right = self;
            self.refresh();
            top.refresh();
            return top;
        }

        fn balance(self: *Node) *Node {
            self.refresh();
            const skew = depth(self.left) - depth(self.right);
            if (skew > 1) {
                const left = self.left.?;
                if (depth(left.left) < depth(left.right)) self.left = left.rotateLeft();
                return self.rotateRight();
            }
            if (skew < -1) {
                const right = self.right.?;
                if (depth(right.right) < depth(right.left)) self.right = right.rotateRight();
                return self.rotateLeft();
            }
            return self;
        }

        fn insert(root: ?*Node, node: *Node) *Node {
            const top = root orelse return node;
            if (less(node.entry, top.entry)) {
                top.left = insert(top.left, node);
            } else {
                top.right = insert(top.right, node);
            }
            return top.balance();
        }

        /// Detach by identity, preserving nodes referenced by the member map.
        fn remove(top: *Node, node: *Node) ?*Node {
            if (top == node) {
                if (top.left == null) return top.right;
                var successor = top.right orelse return top.left;
                while (successor.left) |left| successor = left;
                successor.right = remove(top.right.?, successor);
                successor.left = top.left;
                return successor.balance();
            }
            if (less(node.entry, top.entry)) {
                top.left = remove(top.left.?, node);
            } else {
                top.right = remove(top.right.?, node);
            }
            return top.balance();
        }

        fn copyRange(top: ?*Node, base: usize, start: usize, end: usize, result: []Entry) void {
            const node = top orelse return;
            const rank = base + count(node.left);
            if (start < rank) copyRange(node.left, base, start, end, result);
            if (start <= rank and rank < end) result[rank - start] = node.entry;
            if (end > rank + 1) copyRange(node.right, rank + 1, start, end, result);
        }
    };

    const ZSet = struct {
        members: std.StringHashMap(*Node),
        root: ?*Node = null,
        changed: ?*Node = null,
        changed_count: usize = 0,
        allocator: Allocator,

        fn init(allocator: Allocator) ZSet {
            return .{ .members = std.StringHashMap(*Node).init(allocator), .allocator = allocator };
        }

        fn deinit(self: *ZSet, allocator: Allocator) void {
            var it = self.members.valueIterator();
            while (it.next()) |node| {
                allocator.free(node.*.entry.member);
                allocator.destroy(node.*);
            }
            self.members.deinit();
        }

        fn update(self: *ZSet, node: *Node, score: f64) void {
            if (node.score == score) return;
            node.score = score;
            // Coalesce any number of writes to a member into one index update.
            if (!node.pending) {
                node.pending = true;
                node.pending_next = self.changed;
                self.changed = node;
                self.changed_count += 1;
            }
        }

        const IndexedEntry = struct { entry: Entry, node: *Node };

        fn buildBalanced(entries: []const IndexedEntry) ?*Node {
            if (entries.len == 0) return null;
            const middle = entries.len / 2;
            const item = entries[middle];
            item.node.* = .{
                .entry = item.entry,
                .score = item.entry.score,
                .left = buildBalanced(entries[0..middle]),
                .right = buildBalanced(entries[middle + 1 ..]),
            };
            item.node.refresh();
            return item.node;
        }

        fn rebuild(self: *ZSet) bool {
            const entries = self.allocator.alloc(IndexedEntry, self.members.count()) catch return false;
            defer self.allocator.free(entries);
            var it = self.members.valueIterator();
            var i: usize = 0;
            while (it.next()) |node| : (i += 1) {
                entries[i] = .{ .entry = .{ .member = node.*.entry.member, .score = node.*.score }, .node = node.* };
            }
            std.mem.sort(IndexedEntry, entries, {}, struct {
                fn less(_: void, a: IndexedEntry, b: IndexedEntry) bool {
                    return Node.less(a.entry, b.entry);
                }
            }.less);
            self.root = buildBalanced(entries);
            self.changed = null;
            self.changed_count = 0;
            return true;
        }

        fn synchronize(self: *ZSet) void {
            // Rebuilding is cheaper than many individual tree removals/inserts
            // after a full-set write burst. Allocation failure uses the
            // allocation-free incremental path without changing query results.
            if (self.changed_count >= 4096 and self.changed_count > self.members.count() / 2 and self.rebuild()) return;
            while (self.changed) |node| {
                self.changed = node.pending_next;
                node.pending = false;
                node.pending_next = null;
                if (node.entry.score == node.score) continue;
                self.root = Node.remove(self.root.?, node);
                node.* = .{ .entry = .{ .member = node.entry.member, .score = node.score }, .score = node.score };
                self.root = Node.insert(self.root, node);
            }
            self.changed_count = 0;
        }

        fn put(self: *ZSet, allocator: Allocator, member: []const u8, score: f64) !bool {
            if (std.math.isNan(score)) return error.InvalidScore;
            if (self.members.get(member)) |node| {
                self.update(node, score);
                return false;
            }
            // Reserve everything before publishing either index. A failed
            // allocation must not leave borrowed keys or uninitialized values.
            try self.members.ensureUnusedCapacity(1);
            const owned = try allocator.dupe(u8, member);
            errdefer allocator.free(owned);
            const node = try allocator.create(Node);
            node.* = .{ .entry = .{ .member = owned, .score = score }, .score = score };
            self.members.putAssumeCapacity(owned, node);
            self.root = Node.insert(self.root, node);
            return true;
        }

        fn below(self: *const ZSet, score: f64, inclusive: bool) usize {
            var total: usize = 0;
            var cursor = self.root;
            while (cursor) |node| {
                if (node.entry.score < score or (inclusive and node.entry.score == score)) {
                    total += Node.count(node.left) + 1;
                    cursor = node.right;
                } else cursor = node.left;
            }
            return total;
        }
    };

    pub fn init(allocator: Allocator) !SortedSetStore {
        return initWithPartitionCount(allocator, default_partition_count);
    }

    /// Startup-only: changing the count after insertion would remap keys.
    pub fn initWithPartitionCount(allocator: Allocator, count: usize) !SortedSetStore {
        try validatePartitionCount(count);
        const partitions = try allocator.alloc(Partition, count);
        errdefer allocator.free(partitions);
        const init_fn = @extern(*const fn (*std.c.pthread_mutex_t, ?*const anyopaque) callconv(.c) c_int, .{ .name = "pthread_mutex_init" });
        const destroy_fn = @extern(*const fn (*std.c.pthread_mutex_t) callconv(.c) c_int, .{ .name = "pthread_mutex_destroy" });
        var initialized: usize = 0;
        errdefer for (partitions[0..initialized]) |*part| {
            _ = destroy_fn(&part.mutex);
        };
        for (partitions) |*part| {
            part.* = .{ .map = std.StringHashMap(ZSet).init(allocator), .expirations = std.StringHashMap(i64).init(allocator) };
            // Heap allocation keeps mutexes at their final addresses.
            if (init_fn(&part.mutex, null) != 0) return error.MutexInitFailed;
            initialized += 1;
        }
        return .{ .partitions = partitions, .allocator = allocator };
    }

    /// Caller holds all partitions, or has exclusive ownership of the store.
    pub fn flush(self: *SortedSetStore) void {
        for (self.partitions) |*part| {
            var it = part.map.iterator();
            while (it.next()) |entry| {
                entry.value_ptr.deinit(self.allocator);
                self.allocator.free(entry.key_ptr.*);
            }
            part.map.clearRetainingCapacity();
            var expiry_it = part.expirations.keyIterator();
            while (expiry_it.next()) |key| self.allocator.free(key.*);
            part.expirations.clearRetainingCapacity();
        }
    }

    pub fn flushDb(self: *SortedSetStore, db: u8) void {
        var prefix_buf: [16]u8 = undefined;
        const prefix = std.fmt.bufPrint(&prefix_buf, "db:{d}:", .{db}) catch unreachable;
        for (self.partitions) |*part| {
            // Removal does not rehash the map; iterator visits each slot once.
            var it = part.map.iterator();
            while (it.next()) |entry| {
                if (std.mem.startsWith(u8, entry.key_ptr.*, prefix)) {
                    const key = entry.key_ptr.*;
                    entry.value_ptr.deinit(self.allocator);
                    _ = part.map.remove(key);
                    self.allocator.free(key);
                }
            }
        }
    }

    pub fn deinit(self: *SortedSetStore) void {
        self.flush();
        const destroy_fn = @extern(*const fn (*std.c.pthread_mutex_t) callconv(.c) c_int, .{ .name = "pthread_mutex_destroy" });
        for (self.partitions) |*part| {
            part.map.deinit();
            part.expirations.deinit();
            _ = destroy_fn(&part.mutex);
        }
        self.allocator.free(self.partitions);
    }

    /// Caller holds this key's partition (or all partitions).
    pub fn collectionExpiry(self: *SortedSetStore, key: []const u8) ?i64 {
        return self.partitions[self.partitionIndex(key)].expirations.get(key);
    }

    pub fn setCollectionExpiry(self: *SortedSetStore, key: []const u8, deadline: i64) !void {
        const map = &self.partitions[self.partitionIndex(key)].expirations;
        if (map.getPtr(key)) |value| { value.* = deadline; return; }
        const owned = try self.allocator.dupe(u8, key);
        errdefer self.allocator.free(owned);
        try map.put(owned, deadline);
    }

    pub fn clearCollectionExpiry(self: *SortedSetStore, key: []const u8) bool {
        const map = &self.partitions[self.partitionIndex(key)].expirations;
        const removed = map.fetchRemove(key) orelse return false;
        self.allocator.free(removed.key);
        return true;
    }

    /// ZADD key score member [score member ...] — count newly added members.
    pub fn zadd(self: *SortedSetStore, key: []const u8, score_members: []const []const u8) !usize {
        // Validate the entire batch before changing any scores.
        var i: usize = 0;
        while (i + 1 < score_members.len) : (i += 2) {
            const score = std.fmt.parseFloat(f64, score_members[i]) catch return error.InvalidScore;
            if (std.math.isNan(score)) return error.InvalidScore;
        }
        const zs = try self.getOrCreate(key);
        var added: usize = 0;
        i = 0;
        while (i + 1 < score_members.len) : (i += 2) {
            const score = try std.fmt.parseFloat(f64, score_members[i]);
            if (try zs.put(self.allocator, score_members[i + 1], score)) added += 1;
        }
        return added;
    }

    pub fn zrem(self: *SortedSetStore, key: []const u8, members: []const []const u8) usize {
        const zs = self.mapFor(key).getPtr(key) orelse return 0;
        // Drain pending links before any member nodes can be freed.
        zs.synchronize();
        var removed: usize = 0;
        for (members) |member| {
            const entry = zs.members.fetchRemove(member) orelse continue;
            zs.root = Node.remove(zs.root.?, entry.value);
            self.allocator.free(entry.key);
            self.allocator.destroy(entry.value);
            removed += 1;
        }
        if (zs.members.count() == 0) _ = self.delete(key);
        return removed;
    }

    pub fn zscore(self: *SortedSetStore, key: []const u8, member: []const u8) ?f64 {
        const zs = self.mapFor(key).getPtr(key) orelse return null;
        return (zs.members.get(member) orelse return null).score;
    }

    pub fn zcard(self: *SortedSetStore, key: []const u8) usize {
        const zs = self.mapFor(key).getPtr(key) orelse return 0;
        return zs.members.count();
    }

    pub fn zrank(self: *SortedSetStore, key: []const u8, member: []const u8) ?usize {
        const zs = self.mapFor(key).getPtr(key) orelse return null;
        const target = zs.members.get(member) orelse return null;
        zs.synchronize();
        var rank: usize = 0;
        var cursor = zs.root;
        while (cursor) |node| {
            if (node == target) return rank + Node.count(node.left);
            if (Node.less(target.entry, node.entry)) {
                cursor = node.left;
            } else {
                rank += Node.count(node.left) + 1;
                cursor = node.right;
            }
        }
        unreachable; // Every member is also in the ordered index.
    }

    pub fn zrange(self: *SortedSetStore, key: []const u8, start_in: i64, stop_in: i64, allocator: Allocator) ![]const Entry {
        const zs = self.mapFor(key).getPtr(key) orelse return &.{};
        const len: i64 = @intCast(zs.members.count());
        var start = start_in;
        var stop = stop_in;
        if (start < 0) start += len;
        if (stop < 0) stop += len;
        start = @max(start, 0);
        stop = @min(stop, len - 1);
        if (start > stop) return &.{};
        const s: usize = @intCast(start);
        const end: usize = @intCast(stop + 1);
        const result = try allocator.alloc(Entry, end - s);
        zs.synchronize();
        Node.copyRange(zs.root, 0, s, end, result);
        return result;
    }

    pub fn zincrby(self: *SortedSetStore, key: []const u8, delta: f64, member: []const u8) !f64 {
        if (std.math.isNan(delta)) return error.InvalidScore;
        const zs = try self.getOrCreate(key);
        if (zs.members.get(member)) |node| {
            const score = node.score + delta;
            if (std.math.isNan(score)) return error.InvalidScore;
            zs.update(node, score);
            return score;
        }
        _ = try zs.put(self.allocator, member, delta);
        return delta;
    }

    pub fn zcount(self: *SortedSetStore, key: []const u8, min: f64, max: f64) usize {
        const zs = self.mapFor(key).getPtr(key) orelse return 0;
        if (min > max or std.math.isNan(min) or std.math.isNan(max)) return 0;
        zs.synchronize();
        return zs.below(max, true) - zs.below(min, false);
    }

    pub fn exists(self: *SortedSetStore, key: []const u8) bool {
        return self.mapFor(key).contains(key);
    }

    pub fn delete(self: *SortedSetStore, key: []const u8) bool {
        var entry = self.mapFor(key).fetchRemove(key) orelse return false;
        entry.value.deinit(self.allocator);
        self.allocator.free(entry.key);
        return true;
    }

    fn getOrCreate(self: *SortedSetStore, key: []const u8) !*ZSet {
        if (self.mapFor(key).getPtr(key)) |existing| return existing;
        const owned = try self.allocator.dupe(u8, key);
        errdefer self.allocator.free(owned);
        const gop = try self.mapFor(key).getOrPut(owned);
        gop.value_ptr.* = ZSet.init(self.allocator);
        return gop.value_ptr;
    }
};
