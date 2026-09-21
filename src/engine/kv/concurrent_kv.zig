const std = @import("std");
const Allocator = std.mem.Allocator;
const KVStore = @import("kv.zig").KVStore;
const EvictionPolicy = @import("kv.zig").EvictionPolicy;
const obs_stats = @import("../../observability/stats.zig");

const STRIPE_COUNT = 256;
const STRIPE_MASK = STRIPE_COUNT - 1;

/// Number of entries to sample per stripe when looking for an eviction victim.
/// Matches the single-threaded KVStore.evictIfNeeded sample size.
const EVICTION_SAMPLE_SIZE: usize = 5;

/// Returned by setInternal when maxmemory is configured with noeviction policy
/// and the write would exceed the budget.
pub const SetError = error{MaxMemoryReached} || Allocator.Error;

/// Thread-safe KV store using bucket-striped locking.
/// 256 stripes, each with its own mutex + HashMap.
/// Any thread can access any key with minimal contention.
pub const ConcurrentKV = struct {
    stripes: [STRIPE_COUNT]Stripe,
    allocator: Allocator,
    io: std.Io,
    cached_now_ms: i64 = 0,

    /// Maxmemory budget in bytes. 0 = unlimited (hot path becomes byte-identical
    /// to pre-maxmemory behavior). Wire from server construction.
    maxmemory: usize = 0,
    /// Eviction policy applied when `total_bytes > maxmemory`.
    eviction_policy: EvictionPolicy = .noeviction,
    /// Sum of `key.len + value.len` across live entries. Atomic so the hot path
    /// can update it under the stripe lock without coordinating across stripes.
    /// Uses `.monotonic` — this is a budget heuristic, not a synchronization point.
    total_bytes: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),

    pub const INLINE_BUF_SIZE = 32;

    /// Reactor entries keep tiny values inline and larger values in reusable,
    /// owned buffers. The plain KVStore layout is independent of this hot path.
    pub const Entry = struct {
        value: []const u8,
        value_capacity: usize = 0,
        expires_at: i64 = 0,
        last_access: i64 = 0,
        int_value: i64 = 0,
        inline_buf: [INLINE_BUF_SIZE]u8 = undefined,
        inline_len: u16 = 0,
        flags: KVStore.EntryFlags = .{},

        pub fn bytes(self: *const Entry) []const u8 {
            return if (self.flags.is_inline) self.inline_buf[0..self.inline_len] else self.value;
        }

        /// Free the original allocation length, including retained spare capacity.
        fn allocation(self: *const Entry) []const u8 {
            return self.value.ptr[0..@max(self.value_capacity, self.value.len)];
        }

        fn freeValue(self: *const Entry, allocator: Allocator) void {
            if (!self.flags.is_inline) allocator.free(self.allocation());
        }

        /// Caller holds the stripe write lock. Allocation failure leaves the old
        /// value untouched. Shrinking below half capacity releases excess memory.
        fn replaceValue(self: *Entry, allocator: Allocator, value: []const u8) !void {
            if (value.len <= INLINE_BUF_SIZE) {
                var copy: [INLINE_BUF_SIZE]u8 = undefined;
                @memcpy(copy[0..value.len], value);
                self.freeValue(allocator);
                @memcpy(self.inline_buf[0..value.len], copy[0..value.len]);
                self.inline_len = @intCast(value.len);
                self.value = self.inline_buf[0..value.len];
                self.value_capacity = 0;
                self.flags.is_inline = true;
            } else if (!self.flags.is_inline and self.allocation().len >= value.len and
                self.allocation().len / 2 <= value.len)
            {
                // Equal-size overwrites and modest size changes allocate nothing.
                const capacity = self.allocation().len;
                const dest = @constCast(self.value.ptr)[0..value.len];
                std.mem.copyForwards(u8, dest, value);
                self.value = dest;
                self.value_capacity = capacity;
            } else {
                const replacement = try allocator.dupe(u8, value);
                self.freeValue(allocator);
                self.value = replacement;
                self.value_capacity = replacement.len;
                self.flags.is_inline = false;
            }
        }
    };

    /// Cache-line aligned to prevent false sharing between workers.
    /// Uses pthread_rwlock: GETs take read-lock (parallel), SETs take write-lock (exclusive).
    const Stripe = struct {
        rwlock: std.c.pthread_rwlock_t align(64) = std.mem.zeroes(std.c.pthread_rwlock_t),
        map: std.StringHashMap(Entry),
        ttl_count: u32 = 0,
        tombstone_count: u32 = 0,
    };

    /// Owned value returned by get(). Caller must call deinit() to free.
    pub const OwnedValue = struct {
        data: []const u8,
        allocator: Allocator,

        pub fn deinit(self: OwnedValue) void {
            self.allocator.free(self.data);
        }
    };

    pub fn init(allocator: Allocator, io: std.Io) ConcurrentKV {
        var self: ConcurrentKV = .{
            .stripes = undefined,
            .allocator = allocator,
            .io = io,
        };
        for (&self.stripes) |*s| {
            s.map = std.StringHashMap(Entry).init(allocator);
            // Zero-init rwlock — works on Linux. macOS needs initStripes() after placement.
        }
        return self;
    }

    /// Initialize rwlocks after placement for macOS compatibility. Maps grow
    /// under their stripe's exclusive lock instead of reserving millions of slots.
    pub fn initStripes(self: *ConcurrentKV) void {
        const init_fn = @extern(*const fn (*std.c.pthread_rwlock_t, ?*const anyopaque) callconv(.c) c_int, .{ .name = "pthread_rwlock_init" });
        for (&self.stripes) |*s| {
            _ = init_fn(&s.rwlock, null);
        }
    }

    pub fn deinit(self: *ConcurrentKV) void {
        for (&self.stripes) |*s| {
            var iter = s.map.iterator();
            while (iter.next()) |entry| {
                self.allocator.free(entry.key_ptr.*);
                entry.value_ptr.freeValue(self.allocator);
            }
            s.map.deinit();
        }
    }

    /// Import all entries from an existing KVStore (single-threaded, at startup).
    pub fn importFrom(self: *ConcurrentKV, source: *KVStore) !void {
        var iter = source.map.iterator();
        while (iter.next()) |entry| {
            const idx = stripeIndex(entry.key_ptr.*);
            const s = &self.stripes[idx];
            const owned_key = try self.allocator.dupe(u8, entry.key_ptr.*);
            errdefer self.allocator.free(owned_key);
            const owned_val = try self.allocator.dupe(u8, entry.value_ptr.value);
            errdefer self.allocator.free(owned_val);
            if (entry.value_ptr.flags.deleted) continue; // skip tombstones
            var flags = entry.value_ptr.flags;
            flags.is_inline = false; // imported values are heap-allocated, not inline
            try s.map.put(owned_key, .{
                .value = owned_val,
                .expires_at = entry.value_ptr.expires_at,
                .flags = flags,
            });
            _ = self.total_bytes.fetchAdd(owned_key.len + owned_val.len, .monotonic);
        }
    }

    // ── Single-key operations (lock one stripe) ──

    pub fn get(self: *ConcurrentKV, key: []const u8) ?OwnedValue {
        const s = self.getStripe(key);
        readLockStripe(s);
        defer readUnlockStripe(s);

        const entry = s.map.getPtr(key) orelse return null;
        if (self.isExpired(entry)) return null;
        if (entry.flags.is_integer) {
            // Format native int to string
            var buf: [24]u8 = undefined;
            const str = std.fmt.bufPrint(&buf, "{d}", .{entry.int_value}) catch return null;
            const copy = self.allocator.dupe(u8, str) catch return null;
            return .{ .data = copy, .allocator = self.allocator };
        }
        const copy = self.allocator.dupe(u8, entry.bytes()) catch return null;
        return .{ .data = copy, .allocator = self.allocator };
    }

    pub fn set(self: *ConcurrentKV, key: []const u8, value: []const u8) !void {
        return self.setInternal(key, value, 0);
    }

    /// SET with pre-allocated key+value. Caller provides owned memory.
    /// ConcurrentKV takes ownership. Old value freed OUTSIDE the lock.
    /// On insert, owned_key is used. On update, owned_key is freed by caller
    /// (returned as stale_key).
    pub fn setPrealloc(
        self: *ConcurrentKV,
        key: []const u8,
        owned_key: []u8,
        owned_value: []u8,
        expires_at: i64,
    ) struct { stale_val: ?[]const u8, stale_key: ?[]const u8 } {
        const s = self.getStripe(key);
        writeLockStripe(s);

        defer writeUnlockStripe(s);
        // Adopt the supplied allocation even for short values. This path already
        // owns a buffer (COPY), so allocating or copying it again gains nothing.
        const gop = s.map.getOrPut(owned_key) catch {
            return .{ .stale_val = owned_value, .stale_key = owned_key };
        };
        const old_len = if (gop.found_existing) gop.value_ptr.value.len else 0;
        const old_val = if (gop.found_existing and !gop.value_ptr.flags.is_inline)
            gop.value_ptr.allocation()
        else
            null;
        gop.key_ptr.* = if (gop.found_existing) gop.key_ptr.* else owned_key;
        gop.value_ptr.* = .{
            .value = owned_value,
            .value_capacity = owned_value.len,
            .expires_at = expires_at,
            .flags = .{ .has_ttl = expires_at != 0 },
        };
        const added = owned_value.len + (if (gop.found_existing) @as(usize, 0) else owned_key.len);
        if (added > old_len) _ = self.total_bytes.fetchAdd(added - old_len, .monotonic);
        if (added < old_len) _ = self.total_bytes.fetchSub(old_len - added, .monotonic);
        return .{ .stale_val = old_val, .stale_key = if (gop.found_existing) owned_key else null };
    }

    pub fn setEx(self: *ConcurrentKV, key: []const u8, value: []const u8, ttl_seconds: i64) !void {
        const expires = self.nowMillis() + ttl_seconds * 1000;
        return self.setInternal(key, value, expires);
    }

    pub fn setPx(self: *ConcurrentKV, key: []const u8, value: []const u8, ttl_millis: i64) !void {
        return self.setInternal(key, value, self.nowMillis() + ttl_millis);
    }

    /// Delete a key. Returns stale key+value for caller to free OUTSIDE any lock.
    pub fn deleteStale(self: *ConcurrentKV, key: []const u8) struct { found: bool, stale_key: ?[]const u8, stale_val: ?[]const u8 } {
        const s = self.getStripe(key);
        writeLockStripe(s);
        const result = s.map.fetchRemove(key);
        writeUnlockStripe(s);
        if (result) |kv| {
            // Inline values point into the entry's inline_buf (not heap-allocated) — don't free
            const stale_val = if (!kv.value.flags.is_inline) kv.value.allocation() else null;
            const removed_bytes = kv.key.len + kv.value.value.len;
            _ = self.total_bytes.fetchSub(removed_bytes, .monotonic);
            return .{ .found = true, .stale_key = kv.key, .stale_val = stale_val };
        }
        return .{ .found = false, .stale_key = null, .stale_val = null };
    }

    pub fn delete(self: *ConcurrentKV, key: []const u8) bool {
        const stale = self.deleteStale(key);
        if (stale.stale_key) |k| self.allocator.free(k);
        if (stale.stale_val) |v| self.allocator.free(v);
        return stale.found;
    }

    pub fn exists(self: *ConcurrentKV, key: []const u8) bool {
        const s = self.getStripe(key);
        readLockStripe(s);
        defer readUnlockStripe(s);

        const entry = s.map.getPtr(key) orelse return false;
        if (self.isExpired(entry)) return false;
        return true;
    }

    pub fn ttl(self: *ConcurrentKV, key: []const u8) ?i64 {
        const s = self.getStripe(key);
        readLockStripe(s);
        defer readUnlockStripe(s);

        const entry = s.map.getPtr(key) orelse return null;
        if (self.isExpired(entry)) return null;
        if (!entry.flags.has_ttl) return -1;
        return @divTrunc(entry.expires_at - self.nowMillis(), 1000);
    }

    pub fn restoreEntry(self: *ConcurrentKV, key: []const u8, value: []const u8, expires_at: ?i64) !void {
        return self.setInternal(key, value, expires_at orelse 0);
    }

    // ── Bulk operations ──

    /// Lazy FLUSHALL: swap stripe maps to fresh empty ones, push old entries
    /// to garbage queue for async free. Returns instantly (~500ns for 256 stripes).
    pub fn flushdb(self: *ConcurrentKV) void {
        const alloc = self.allocator;
        for (&self.stripes) |*s| {
            writeLockStripe(s);
            var old_map = s.map;
            s.map = std.StringHashMap(Entry).init(alloc);
            writeUnlockStripe(s);

            var iter = old_map.iterator();
            while (iter.next()) |entry| {
                alloc.free(entry.key_ptr.*);
                entry.value_ptr.freeValue(alloc);
            }
            old_map.deinit();
        }
        self.total_bytes.store(0, .monotonic);
    }

    pub fn dbsize(self: *ConcurrentKV) usize {
        self.readLockAll();
        defer self.readUnlockAll();

        var total: usize = 0;
        for (&self.stripes) |*s| {
            total += s.map.count();
        }
        return total;
    }

    pub fn keys(self: *ConcurrentKV, allocator: Allocator, pattern: []const u8) ![][]const u8 {
        self.readLockAll();
        defer self.readUnlockAll();

        var result = std.array_list.Managed([]const u8).init(allocator);
        errdefer result.deinit();

        const match_all = std.mem.eql(u8, pattern, "*");
        for (&self.stripes) |*s| {
            var iter = s.map.iterator();
            while (iter.next()) |entry| {
                if (match_all or globMatch(pattern, entry.key_ptr.*)) {
                    try result.append(entry.key_ptr.*);
                }
            }
        }
        return result.toOwnedSlice();
    }

    /// Atomic INCR/DECR using native i64 storage. No string parse/format under lock.
    /// First call parses the string; subsequent calls use cached int_value directly.
    pub fn incrBy(self: *ConcurrentKV, key: []const u8, delta: i64) error{ NotAnInteger, OutOfMemory }!i64 {
        const s = self.getStripe(key);
        writeLockStripe(s);

        const result = s.map.getPtr(key);
        if (result) |existing| {
            if (self.isExpired(existing)) {
                // Treat expired as 0
                existing.int_value = delta;
                existing.flags = .{ .is_integer = true, .is_inline = existing.flags.is_inline };
                existing.last_access = self.cached_now_ms;
                writeUnlockStripe(s);
                return delta;
            }

            // If already marked as integer, use cached int_value directly (~1ns)
            if (existing.flags.is_integer) {
                existing.int_value += delta;
                existing.last_access = self.cached_now_ms;
                const new_val = existing.int_value;
                writeUnlockStripe(s);
                return new_val;
            }

            // First INCR on a string value — parse once, then use native from here on
            const current = std.fmt.parseInt(i64, existing.bytes(), 10) catch {
                writeUnlockStripe(s);
                return error.NotAnInteger;
            };
            existing.int_value = current + delta;
            existing.flags = .{ .is_integer = true, .is_inline = existing.flags.is_inline };
            existing.last_access = self.cached_now_ms;
            const new_val = existing.int_value;
            writeUnlockStripe(s);
            return new_val;
        }

        const alloc = self.allocator;
        const owned_key = alloc.dupe(u8, key) catch {
            writeUnlockStripe(s);
            return error.OutOfMemory;
        };
        s.map.put(owned_key, .{
            .value = &[_]u8{},
            .int_value = delta,
            .last_access = self.cached_now_ms,
            .flags = .{ .is_integer = true },
        }) catch {
            alloc.free(owned_key);
            writeUnlockStripe(s);
            return error.OutOfMemory;
        };

        writeUnlockStripe(s);
        return delta;
    }

    // ── Public stripe access (for inlined GET hot path in worker) ──

    pub fn getStripePublic(self: *ConcurrentKV, key: []const u8) *Stripe {
        return self.getStripe(key);
    }

    pub fn readLockStripePublic(_: *ConcurrentKV, s: *Stripe) void {
        readLockStripe(s);
    }

    pub fn readUnlockStripePublic(_: *ConcurrentKV, s: *Stripe) void {
        readUnlockStripe(s);
    }

    /// Set the fast allocator (pool arena). Call after init, before use.
    // ── Internal helpers ──

    pub fn setInternal(self: *ConcurrentKV, key: []const u8, value: []const u8, expires_at: i64) SetError!void {
        const s = self.getStripe(key);
        const alloc = self.allocator;
        writeLockStripe(s);
        defer writeUnlockStripe(s);

        // Maxmemory enforcement. Gated on `maxmemory != 0` so the hot path stays
        // byte-identical when the budget is unset.
        if (self.maxmemory != 0) {
            // Existing entry's value-bytes are already counted in total_bytes;
            // they will be freed and replaced atomically here. To get a correct
            // pre-check, subtract them from the projected delta.
            var old_value_bytes: usize = 0;
            var key_already_present: bool = false;
            if (s.map.getPtr(key)) |existing| {
                old_value_bytes = existing.value.len;
                key_already_present = true;
            }

            const new_entry_bytes: usize = (if (key_already_present) 0 else key.len) + value.len;
            const current_total: u64 = self.total_bytes.load(.monotonic);
            const projected: u64 = current_total -| old_value_bytes +| new_entry_bytes;

            if (projected > self.maxmemory) {
                switch (self.eviction_policy) {
                    .noeviction => return error.MaxMemoryReached,
                    .allkeys_lru => self.evictFromStripeLocked(s, projected),
                }
            }
        }

        const now = self.cached_now_ms;
        if (s.map.getPtr(key)) |existing| {
            const old_value_len = existing.value.len;
            try existing.replaceValue(alloc, value);
            existing.flags = .{ .has_ttl = expires_at != 0, .is_inline = existing.flags.is_inline };
            existing.expires_at = expires_at;
            existing.last_access = now;
            if (value.len > old_value_len) _ = self.total_bytes.fetchAdd(value.len - old_value_len, .monotonic);
            if (value.len < old_value_len) _ = self.total_bytes.fetchSub(old_value_len - value.len, .monotonic);
        } else {
            const owned_key = try alloc.dupe(u8, key);
            errdefer alloc.free(owned_key);
            var entry: Entry = .{ .value = &.{}, .expires_at = expires_at, .last_access = now };
            try entry.replaceValue(alloc, value);
            errdefer entry.freeValue(alloc);
            entry.flags.has_ttl = expires_at != 0;
            // bytes() resolves inline storage at its current address after a move.
            try s.map.put(owned_key, entry);
            _ = self.total_bytes.fetchAdd(owned_key.len + value.len, .monotonic);
        }
    }

    /// Sample-LRU eviction from a single stripe. Caller holds the stripe's
    /// write lock. Picks the oldest of up to EVICTION_SAMPLE_SIZE entries by
    /// `last_access`, frees it, and repeats until `total_bytes <= maxmemory`
    /// or the stripe is empty.
    ///
    /// Stripe-local sampling sacrifices a bit of eviction quality (we might
    /// pick an older key in some other stripe) but avoids any cross-stripe
    /// locking, keeping the SET path lock-local.
    fn evictFromStripeLocked(self: *ConcurrentKV, s: *Stripe, initial_projected: u64) void {
        const alloc = self.allocator;
        var projected = initial_projected;

        while (projected > self.maxmemory) {
            if (s.map.count() == 0) return;

            var oldest_key: ?[]const u8 = null;
            var oldest_access: i64 = std.math.maxInt(i64);
            var samples: usize = 0;
            var it = s.map.iterator();
            while (it.next()) |entry| {
                if (entry.value_ptr.flags.deleted) continue;
                if (entry.value_ptr.last_access < oldest_access) {
                    oldest_access = entry.value_ptr.last_access;
                    oldest_key = entry.key_ptr.*;
                }
                samples += 1;
                if (samples >= EVICTION_SAMPLE_SIZE) break;
            }

            const victim_key = oldest_key orelse return;

            // Remove victim — this stripe is already write-locked, safe to mutate map.
            const removed = s.map.fetchRemove(victim_key) orelse return;
            const freed_bytes: usize = removed.key.len + removed.value.value.len;
            removed.value.freeValue(alloc);
            alloc.free(removed.key);

            _ = self.total_bytes.fetchSub(freed_bytes, .monotonic);
            _ = obs_stats.evicted_keys.fetchAdd(1, .monotonic);

            // Recompute projected: shrink by freed bytes (saturating).
            projected = projected -| freed_bytes;
        }
    }

    pub fn stripeIndex(key: []const u8) usize {
        // StringHashMap uses Wyhash seed 0 and its low bits for the bucket.
        // An independent seed avoids forcing every key in a stripe into the
        // same subset of buckets, especially when its table is still small.
        return @as(usize, std.hash.Wyhash.hash(1, key)) & STRIPE_MASK;
    }

    fn getStripe(self: *ConcurrentKV, key: []const u8) *Stripe {
        return &self.stripes[stripeIndex(key)];
    }

    /// Read-lock: multiple readers in parallel (for GET, EXISTS, TTL)
    fn readLockStripe(s: *Stripe) void {
        _ = std.c.pthread_rwlock_rdlock(&s.rwlock);
    }

    fn readUnlockStripe(s: *Stripe) void {
        _ = std.c.pthread_rwlock_unlock(&s.rwlock);
    }

    fn writeLockStripe(s: *Stripe) void {
        _ = std.c.pthread_rwlock_wrlock(&s.rwlock);
    }

    fn writeUnlockStripe(s: *Stripe) void {
        _ = std.c.pthread_rwlock_unlock(&s.rwlock);
    }

    fn readLockAll(self: *ConcurrentKV) void {
        for (&self.stripes) |*s| readLockStripe(s);
    }

    fn readUnlockAll(self: *ConcurrentKV) void {
        for (&self.stripes) |*s| readUnlockStripe(s);
    }

    /// Update cached clock. Call once per event loop tick.
    pub fn updateClock(self: *ConcurrentKV) void {
        self.cached_now_ms = std.Io.Timestamp.now(self.io, .real).toMilliseconds();
    }

    pub fn nowMillis(self: *const ConcurrentKV) i64 {
        return self.cached_now_ms;
    }

    fn isExpired(self: *const ConcurrentKV, entry: *const Entry) bool {
        if (!entry.flags.has_ttl) return false;
        return self.cached_now_ms > entry.expires_at;
    }
};

/// Minimal glob matcher supporting '*' (match any) and '?' (match one).
fn globMatch(pattern: []const u8, string: []const u8) bool {
    var pi: usize = 0;
    var si: usize = 0;
    var star_p: ?usize = null;
    var star_s: usize = 0;

    while (si < string.len) {
        if (pi < pattern.len and (pattern[pi] == '?' or pattern[pi] == string[si])) {
            pi += 1;
            si += 1;
        } else if (pi < pattern.len and pattern[pi] == '*') {
            star_p = pi;
            star_s = si;
            pi += 1;
        } else if (star_p) |sp| {
            pi = sp + 1;
            star_s += 1;
            si = star_s;
        } else {
            return false;
        }
    }

    while (pi < pattern.len and pattern[pi] == '*') pi += 1;
    return pi == pattern.len;
}
