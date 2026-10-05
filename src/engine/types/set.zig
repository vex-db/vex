const std = @import("std");
const Allocator = std.mem.Allocator;
const SortedSetStore = @import("sorted_set.zig").SortedSetStore;

/// Set storage: maps key -> unordered set of unique string members.
pub const SetStore = struct {
    // Maps use the same hash/count as the command partitions. The caller owns
    // the corresponding command lock through response serialization; global
    // and multi-key operations own all command partitions.
    const Partition = struct { map: std.StringHashMap(MemberSet) align(64) };
    partitions: []Partition,
    allocator: Allocator,

    pub fn partitionIndex(self: *const SetStore, key: []const u8) usize {
        return std.hash.Wyhash.hash(0, key) & (self.partitions.len - 1);
    }

    fn mapFor(self: *SetStore, key: []const u8) *std.StringHashMap(MemberSet) {
        return &self.partitions[self.partitionIndex(key)].map;
    }


    const MemberSet = struct {
        members: std.StringHashMap(void),
        allocator: Allocator,

        fn init(allocator: Allocator) MemberSet {
            return .{ .members = std.StringHashMap(void).init(allocator), .allocator = allocator };
        }

        fn deinit(self: *MemberSet) void {
            var it = self.members.iterator();
            while (it.next()) |entry| self.allocator.free(entry.key_ptr.*);
            self.members.deinit();
        }
    };

    pub fn init(allocator: Allocator) !SetStore {
        return initWithPartitionCount(allocator, SortedSetStore.default_partition_count);
    }

    /// Startup-only; must match the command lock coordinator's partition count.
    pub fn initWithPartitionCount(allocator: Allocator, count: usize) !SetStore {
        try SortedSetStore.validatePartitionCount(count);
        const partitions = try allocator.alloc(Partition, count);
        for (partitions) |*part| part.* = .{ .map = std.StringHashMap(MemberSet).init(allocator) };
        return .{ .partitions = partitions, .allocator = allocator };
    }

    /// Caller owns all command partitions, or exclusive access to the store.
    pub fn flush(self: *SetStore) void {
        for (self.partitions) |*part| {
            var it = part.map.iterator();
            while (it.next()) |entry| {
                entry.value_ptr.deinit();
                self.allocator.free(entry.key_ptr.*);
            }
            part.map.clearRetainingCapacity();
        }
    }

    pub fn deinit(self: *SetStore) void {
        self.flush();
        for (self.partitions) |*part| part.map.deinit();
        self.allocator.free(self.partitions);
    }

    /// SADD key member [member ...] — add members, returns count of NEW members added.
    pub fn sadd(self: *SetStore, key: []const u8, members: []const []const u8) !usize {
        const s = try self.getOrCreate(key);
        var added: usize = 0;
        for (members) |member| {
            const gop = try s.members.getOrPut(member);
            if (!gop.found_existing) {
                gop.key_ptr.* = try self.allocator.dupe(u8, member);
                added += 1;
            }
        }
        return added;
    }

    /// SADD with pre-allocated owned members. Caller allocated, set takes ownership.
    /// Frees members that already exist (duplicates).
    pub fn saddOwned(self: *SetStore, key: []const u8, owned: []const []u8) !usize {
        const s = try self.getOrCreate(key);
        var added: usize = 0;
        for (owned) |member| {
            const gop = try s.members.getOrPut(member);
            if (!gop.found_existing) {
                gop.key_ptr.* = member; // take ownership
                added += 1;
            } else {
                self.allocator.free(member); // duplicate, free the pre-alloc
            }
        }
        return added;
    }

    /// SREM key member [member ...] — remove members, returns count removed.
    pub fn srem(self: *SetStore, key: []const u8, members: []const []const u8) usize {
        const s = self.mapFor(key).getPtr(key) orelse return 0;
        var removed: usize = 0;
        for (members) |member| {
            const entry = s.members.fetchRemove(member) orelse continue;
            self.allocator.free(entry.key);
            removed += 1;
        }
        if (s.members.count() == 0) self.removeKey(key);
        return removed;
    }

    /// SISMEMBER key member — check membership.
    pub fn sismember(self: *SetStore, key: []const u8, member: []const u8) bool {
        const s = self.mapFor(key).getPtr(key) orelse return false;
        return s.members.contains(member);
    }

    /// SCARD key — cardinality (number of members).
    pub fn scard(self: *SetStore, key: []const u8) usize {
        const s = self.mapFor(key).getPtr(key) orelse return 0;
        return s.members.count();
    }

    /// SMEMBERS key — return all members.
    pub fn smembers(self: *SetStore, key: []const u8, allocator: Allocator) ![]const []const u8 {
        const s = self.mapFor(key).getPtr(key) orelse return &[_][]const u8{};
        const count = s.members.count();
        if (count == 0) return &[_][]const u8{};
        const result = try allocator.alloc([]const u8, count);
        var i: usize = 0;
        var it = s.members.iterator();
        while (it.next()) |entry| {
            result[i] = entry.key_ptr.*;
            i += 1;
        }
        return result;
    }

    /// SUNION key [key ...] — return union of all sets.
    pub fn sunion(self: *SetStore, keys: []const []const u8, allocator: Allocator) ![]const []const u8 {
        var union_set = std.StringHashMap(void).init(allocator);
        defer union_set.deinit();
        for (keys) |key| {
            const s = self.mapFor(key).getPtr(key) orelse continue;
            var it = s.members.iterator();
            while (it.next()) |entry| {
                try union_set.put(entry.key_ptr.*, {});
            }
        }
        if (union_set.count() == 0) return &[_][]const u8{};
        const result = try allocator.alloc([]const u8, union_set.count());
        var i: usize = 0;
        var it = union_set.iterator();
        while (it.next()) |entry| {
            result[i] = entry.key_ptr.*;
            i += 1;
        }
        return result;
    }

    /// SINTER key [key ...] — return intersection of all sets.
    pub fn sinter(self: *SetStore, keys: []const []const u8, allocator: Allocator) ![]const []const u8 {
        if (keys.len == 0) return &[_][]const u8{};
        // Start with the first set
        const first = self.mapFor(keys[0]).getPtr(keys[0]) orelse return &[_][]const u8{};
        var result_list = std.array_list.Managed([]const u8).init(allocator);
        defer result_list.deinit();
        var it = first.members.iterator();
        while (it.next()) |entry| {
            var in_all = true;
            for (keys[1..]) |other_key| {
                const other = self.mapFor(other_key).getPtr(other_key) orelse {
                    in_all = false;
                    break;
                };
                if (!other.members.contains(entry.key_ptr.*)) {
                    in_all = false;
                    break;
                }
            }
            if (in_all) try result_list.append(entry.key_ptr.*);
        }
        if (result_list.items.len == 0) return &[_][]const u8{};
        return try result_list.toOwnedSlice();
    }

    /// SDIFF key [key ...] — return members in first set but not in others.
    pub fn sdiff(self: *SetStore, keys: []const []const u8, allocator: Allocator) ![]const []const u8 {
        if (keys.len == 0) return &[_][]const u8{};
        const first = self.mapFor(keys[0]).getPtr(keys[0]) orelse return &[_][]const u8{};
        var result_list = std.array_list.Managed([]const u8).init(allocator);
        defer result_list.deinit();
        var it = first.members.iterator();
        while (it.next()) |entry| {
            var in_other = false;
            for (keys[1..]) |other_key| {
                const other = self.mapFor(other_key).getPtr(other_key) orelse continue;
                if (other.members.contains(entry.key_ptr.*)) {
                    in_other = true;
                    break;
                }
            }
            if (!in_other) try result_list.append(entry.key_ptr.*);
        }
        if (result_list.items.len == 0) return &[_][]const u8{};
        return try result_list.toOwnedSlice();
    }

    /// Check if a key exists as a set.
    pub fn exists(self: *SetStore, key: []const u8) bool {
        return self.mapFor(key).contains(key);
    }

    /// Delete a set key entirely.
    pub fn delete(self: *SetStore, key: []const u8) bool {
        var entry = self.mapFor(key).fetchRemove(key) orelse return false;
        entry.value.deinit();
        self.allocator.free(entry.key);
        return true;
    }

    fn getOrCreate(self: *SetStore, key: []const u8) !*MemberSet {
        const map = self.mapFor(key);
        if (map.getPtr(key)) |existing| return existing;
        const owned = try self.allocator.dupe(u8, key);
        errdefer self.allocator.free(owned);
        const gop = try map.getOrPut(owned);
        gop.key_ptr.* = owned;
        gop.value_ptr.* = MemberSet.init(self.allocator);
        return gop.value_ptr;
    }

    fn removeKey(self: *SetStore, key: []const u8) void {
        var entry = self.mapFor(key).fetchRemove(key) orelse return;
        entry.value.deinit();
        self.allocator.free(entry.key);
    }
};

// ─── Tests ────────────────────────────────────────────────────────────

