const std = @import("std");
const builtin = @import("builtin");
const Allocator = std.mem.Allocator;
const KVStore = @import("kv.zig").KVStore;
const EvictionPolicy = @import("kv.zig").EvictionPolicy;
const obs_stats = @import("../../observability/stats.zig");
const fractional_map = @import("fractional_map.zig");

extern fn malloc_trim(pad: usize) c_int;

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
    cached_now_ms: std.atomic.Value(i64) = std.atomic.Value(i64).init(0),
    /// Sticky after the first TTL is accepted. Keeping it sticky avoids mutation
    /// bookkeeping on the hot path; idle maintenance then has bounded scan cost.
    has_expiry: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),

    /// Maxmemory budget in bytes. 0 = unlimited (hot path becomes byte-identical
    /// to pre-maxmemory behavior). Wire from server construction.
    maxmemory: usize = 0,
    /// Eviction policy applied when `total_bytes > maxmemory`.
    eviction_policy: EvictionPolicy = .noeviction,
    /// Sum of `key.len + value.len` across live entries. Atomic so the hot path
    /// can update it under the stripe lock without coordinating across stripes.
    /// Uses `.monotonic` — this is a budget heuristic, not a synchronization point.
    total_bytes: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),

    pub const INLINE_BUF_SIZE = 24;

    /// Reactor entries keep tiny values inline and larger values in reusable,
    /// owned buffers. The plain KVStore layout is independent of this hot path.
    pub const Entry = struct {
        const TAG_MASK = 3;
        const HEAP_TAG = 0;
        const INLINE_TAG = 1;
        const META_TAG = 2;
        const COMBINED_TAG = 3;
        const COMBINED_AUX_BIT: u64 = 1 << 4;

        /// Kept out of the common table slot. `aux` holds EntryFlags in its
        /// low byte and the inline length above it when the value is inline.
        const Metadata = struct {
            expires_at: i64 = 0,
            last_access: i64 = 0,
            int_value: i64 = 0,
            aux: u64 = 0,
        };

        // Inline bytes and heap fields are mutually exclusive. Plain strings
        // use this direct storage and a tag; only TTL/int/LRU entries chase
        // the aligned Metadata pointer carried by state.
        storage: extern union {
            inline_buf: [INLINE_BUF_SIZE]u8,
            heap: extern struct { ptr: [*]const u8, len: usize, capacity: usize },
        } = .{ .heap = .{ .ptr = "", .len = 0, .capacity = 0 } },
        state: usize = HEAP_TAG,

        comptime {
            std.debug.assert(@sizeOf(Metadata) == 32);
            std.debug.assert(@alignOf(Metadata) >= 4);
            std.debug.assert(@sizeOf(Entry) == 32);
            std.debug.assert(@alignOf(Entry) == 8);
        }

        fn metadata(self: *const Entry) ?*Metadata {
            if (self.state & TAG_MASK != META_TAG) return null;
            return @ptrFromInt(self.state & ~@as(usize, TAG_MASK));
        }

        fn inlineLen(self: *const Entry) usize {
            if (self.metadata()) |meta| return @intCast(meta.aux >> 8);
            return self.state >> 2;
        }

        fn flagsFromAux(aux: u64) KVStore.EntryFlags {
            return @bitCast(@as(u8, @truncate(aux)));
        }

        fn auxWith(entry_flags: KVStore.EntryFlags, inline_len: usize, combined: bool) u64 {
            const raw_flags = @as(u64, @as(u8, @bitCast(entry_flags))) & ~COMBINED_AUX_BIT;
            return (@as(u64, @intCast(inline_len)) << 8) |
                raw_flags |
                if (combined) COMBINED_AUX_BIT else 0;
        }

        pub fn flags(self: *const Entry) KVStore.EntryFlags {
            if (self.metadata()) |meta| return flagsFromAux(meta.aux);
            return .{ .is_inline = (self.state & TAG_MASK) == INLINE_TAG };
        }

        pub fn isInline(self: *const Entry) bool {
            return self.flags().is_inline;
        }

        pub fn isCombined(self: *const Entry) bool {
            if (self.metadata()) |meta| return meta.aux & COMBINED_AUX_BIT != 0;
            return self.state & TAG_MASK == COMBINED_TAG;
        }

        pub fn hasMetadata(self: *const Entry) bool {
            return self.metadata() != null;
        }

        pub fn hasTtl(self: *const Entry) bool {
            return self.flags().has_ttl;
        }

        pub fn isInteger(self: *const Entry) bool {
            return self.flags().is_integer;
        }

        pub fn expiresAt(self: *const Entry) i64 {
            return if (self.metadata()) |meta| meta.expires_at else 0;
        }

        pub fn lastAccess(self: *const Entry) i64 {
            return if (self.metadata()) |meta| meta.last_access else 0;
        }

        pub fn integerPtr(self: *Entry) ?*i64 {
            return if (self.metadata()) |meta| &meta.int_value else null;
        }

        pub fn integerValue(self: *const Entry) i64 {
            return if (self.metadata()) |meta| meta.int_value else 0;
        }

        fn setMetadata(self: *Entry, meta: *Metadata) void {
            self.state = @intFromPtr(meta) | META_TAG;
        }

        fn setInlineState(self: *Entry, len: usize) void {
            if (self.metadata()) |meta| {
                var entry_flags = flagsFromAux(meta.aux);
                entry_flags.is_inline = true;
                meta.aux = auxWith(entry_flags, len, false);
            } else {
                self.state = (len << 2) | INLINE_TAG;
            }
        }

        fn setHeapState(self: *Entry) void {
            if (self.metadata()) |meta| {
                var entry_flags = flagsFromAux(meta.aux);
                entry_flags.is_inline = false;
                meta.aux = auxWith(entry_flags, 0, self.isCombined());
            } else {
                self.state = HEAP_TAG;
            }
        }

        fn ensureMetadata(self: *Entry, allocator: Allocator) !*Metadata {
            if (self.metadata()) |meta| return meta;
            const meta = try allocator.create(Metadata);
            meta.* = .{ .aux = auxWith(self.flags(), self.inlineLen(), self.isCombined()) };
            self.setMetadata(meta);
            return meta;
        }

        fn freeMetadata(self: *Entry, allocator: Allocator) void {
            const meta = self.metadata() orelse return;
            const entry_flags = flagsFromAux(meta.aux);
            const len: usize = @intCast(meta.aux >> 8);
            const combined = meta.aux & COMBINED_AUX_BIT != 0;
            allocator.destroy(meta);
            self.state = if (entry_flags.is_inline)
                (len << 2) | INLINE_TAG
            else if (combined)
                COMBINED_TAG
            else
                HEAP_TAG;
        }

        pub fn bytes(self: *const Entry) []const u8 {
            return if (self.isInline())
                self.storage.inline_buf[0..self.inlineLen()]
            else
                self.storage.heap.ptr[0..self.storage.heap.len];
        }

        /// Free the original allocation length, including retained spare capacity.
        fn allocation(self: *const Entry, key_len: usize) []const u8 {
            if (self.isCombined()) {
                const start: [*]const u8 = self.storage.heap.ptr - key_len;
                return start[0 .. key_len + self.storage.heap.capacity];
            }
            return self.storage.heap.ptr[0..self.storage.heap.capacity];
        }

        fn freeValue(self: *const Entry, allocator: Allocator, key_len: usize) void {
            if (!self.isInline()) allocator.free(self.allocation(key_len));
        }

        /// Caller holds the stripe write lock. Allocation failure leaves the old
        /// value untouched. Shrinking below half capacity releases excess memory.
        fn replaceValue(self: *Entry, allocator: Allocator, value: []const u8, key_len: usize) !void {
            if (value.len <= INLINE_BUF_SIZE) {
                var copy: [INLINE_BUF_SIZE]u8 = undefined;
                @memcpy(copy[0..value.len], value);
                self.freeValue(allocator, key_len);
                self.storage = .{ .inline_buf = copy };
                self.setInlineState(value.len);
            } else if (!self.isInline() and self.storage.heap.capacity >= value.len and
                self.storage.heap.capacity / 2 <= value.len)
            {
                // Equal-size overwrites and modest size changes allocate nothing.
                const dest = @constCast(self.storage.heap.ptr)[0..value.len];
                std.mem.copyForwards(u8, dest, value);
                self.storage.heap.len = value.len;
            } else {
                const replacement = try allocator.dupe(u8, value);
                self.freeValue(allocator, key_len);
                self.storage = .{ .heap = .{
                    .ptr = replacement.ptr,
                    .len = replacement.len,
                    .capacity = replacement.len,
                } };
                self.setHeapState();
            }
        }
    };

    /// Cache-line aligned to prevent false sharing between workers.
    /// Uses pthread_rwlock: GETs take read-lock (parallel), SETs take write-lock (exclusive).
    const Stripe = struct {
        rwlock: std.c.pthread_rwlock_t align(64) = std.mem.zeroes(std.c.pthread_rwlock_t),
        map: fractional_map.StringHashMap(Entry),
        ttl_count: u32 = 0,
        tombstone_count: u32 = 0,
    };

    pub const Expired = struct { key: []const u8, value: ?[]const u8, metadata: ?*Entry.Metadata, free_key: bool };
    pub const SweepCursor = struct { next_stripe: usize = 0, buckets: [STRIPE_COUNT]usize = @splat(0) };

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
            s.map = fractional_map.StringHashMap(Entry).init(allocator);
            // Zero-init rwlock — works on Linux. macOS needs initStripes() after placement.
        }
        self.cached_now_ms.store(std.Io.Timestamp.now(io, .real).toMilliseconds(), .release);
        return self;
    }

    const Combined = struct { key: []u8, entry: Entry };

    fn allocateCombined(allocator: Allocator, key: []const u8, value: []const u8) !Combined {
        const block = try allocator.alloc(u8, key.len + value.len);
        @memcpy(block[0..key.len], key);
        @memcpy(block[key.len..], value);
        return .{
            .key = block[0..key.len],
            .entry = .{
                .storage = .{ .heap = .{
                    .ptr = block[key.len..].ptr,
                    .len = value.len,
                    .capacity = value.len,
                } },
                .state = Entry.COMBINED_TAG,
            },
        };
    }

    fn freeEntry(self: *ConcurrentKV, key: []const u8, entry: *Entry) void {
        const combined = entry.isCombined();
        entry.freeValue(self.allocator, key.len);
        if (!combined) self.allocator.free(key);
        entry.freeMetadata(self.allocator);
    }

    fn setStringMetadata(entry: *Entry, meta: *Entry.Metadata, expires_at: i64, now: i64) void {
        const is_inline = entry.isInline();
        meta.* = .{
            .expires_at = expires_at,
            .last_access = now,
            .aux = Entry.auxWith(
                .{ .has_ttl = expires_at != 0, .is_inline = is_inline },
                if (is_inline) entry.inlineLen() else 0,
                entry.isCombined(),
            ),
        };
        entry.setMetadata(meta);
    }

    /// The maintenance thread calls this after invalidating WATCH versions.
    pub fn freeExpired(self: *ConcurrentKV, stale: Expired) void {
        if (stale.free_key) self.allocator.free(stale.key);
        if (stale.value) |value| self.allocator.free(value);
        if (stale.metadata) |meta| self.allocator.destroy(meta);
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
                self.freeEntry(entry.key_ptr.*, entry.value_ptr);
            }
            s.map.deinit();
        }
    }

    /// Import all entries from an existing KVStore (single-threaded, at startup).
    pub fn importFrom(self: *ConcurrentKV, source: *KVStore) !void {
        var iter = source.map.iterator();
        while (iter.next()) |entry| {
            if (entry.value_ptr.flags.deleted) continue; // do not allocate tombstone copies
            const idx = stripeIndex(entry.key_ptr.*);
            const s = &self.stripes[idx];
            const owned_key = try self.allocator.dupe(u8, entry.key_ptr.*);
            errdefer self.allocator.free(owned_key);
            var flags = entry.value_ptr.flags;
            var copied: Entry = .{};
            try copied.replaceValue(self.allocator, entry.value_ptr.value, owned_key.len);
            errdefer copied.freeValue(self.allocator, owned_key.len);
            if (flags.has_ttl or flags.is_integer or self.eviction_policy == .allkeys_lru) {
                const meta = try copied.ensureMetadata(self.allocator);
                flags.is_inline = copied.isInline();
                meta.* = .{
                    .expires_at = entry.value_ptr.expires_at,
                    .last_access = entry.value_ptr.last_access,
                    .int_value = entry.value_ptr.int_value,
                    .aux = Entry.auxWith(flags, if (flags.is_inline) copied.inlineLen() else 0, false),
                };
            }
            errdefer copied.freeMetadata(self.allocator);
            try s.map.put(owned_key, copied);
            _ = self.total_bytes.fetchAdd(owned_key.len + entry.value_ptr.value.len, .monotonic);
            if (flags.has_ttl) self.has_expiry.store(true, .release);
        }
    }

    // ── Single-key operations (lock one stripe) ──

    pub fn get(self: *ConcurrentKV, key: []const u8) ?OwnedValue {
        const s = self.getStripe(key);
        readLockStripe(s);
        defer readUnlockStripe(s);

        const entry = s.map.getPtr(key) orelse return null;
        if (self.isExpired(entry)) return null;
        if (entry.isInteger()) {
            // Format native int to string
            var buf: [24]u8 = undefined;
            const str = std.fmt.bufPrint(&buf, "{d}", .{entry.integerValue()}) catch return null;
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
        const needs_metadata = expires_at != 0 or self.eviction_policy == .allkeys_lru;
        const new_meta = if (needs_metadata) self.allocator.create(Entry.Metadata) catch {
            return .{ .stale_val = owned_value, .stale_key = owned_key };
        } else null;
        // Adopt the supplied allocation even for short values. This path already
        // owns a buffer (COPY), so allocating or copying it again gains nothing.
        const gop = s.map.getOrPut(owned_key) catch {
            if (new_meta) |meta| self.allocator.destroy(meta);
            return .{ .stale_val = owned_value, .stale_key = owned_key };
        };
        const old_len = if (gop.found_existing) gop.value_ptr.bytes().len else 0;
        const old_combined = gop.found_existing and gop.value_ptr.isCombined();
        const old_val = if (gop.found_existing and !gop.value_ptr.isInline())
            gop.value_ptr.allocation(gop.key_ptr.*.len)
        else
            null;
        const old_meta = if (gop.found_existing) gop.value_ptr.metadata() else null;
        gop.key_ptr.* = if (gop.found_existing and !old_combined) gop.key_ptr.* else owned_key;
        gop.value_ptr.* = .{
            .storage = .{ .heap = .{ .ptr = owned_value.ptr, .len = owned_value.len, .capacity = owned_value.len } },
        };
        if (new_meta) |meta| {
            meta.* = .{
                .expires_at = expires_at,
                .last_access = self.nowMillis(),
                .aux = Entry.auxWith(.{ .has_ttl = expires_at != 0 }, 0, false),
            };
            gop.value_ptr.setMetadata(meta);
        }
        if (old_meta) |meta| self.allocator.destroy(meta);
        if (expires_at != 0) self.has_expiry.store(true, .release);
        const added = owned_value.len + (if (gop.found_existing) @as(usize, 0) else owned_key.len);
        if (added > old_len) _ = self.total_bytes.fetchAdd(added - old_len, .monotonic);
        if (added < old_len) _ = self.total_bytes.fetchSub(old_len - added, .monotonic);
        return .{ .stale_val = old_val, .stale_key = if (gop.found_existing and !old_combined) owned_key else null };
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
            const combined = kv.value.isCombined();
            const stale_val = if (!kv.value.isInline()) kv.value.allocation(kv.key.len) else null;
            const removed_bytes = kv.key.len + kv.value.bytes().len;
            if (kv.value.metadata()) |meta| self.allocator.destroy(meta);
            _ = self.total_bytes.fetchSub(removed_bytes, .monotonic);
            return .{ .found = true, .stale_key = if (combined) null else kv.key, .stale_val = stale_val };
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
        if (!entry.hasTtl()) return -1;
        return @divTrunc(entry.expiresAt() - self.nowMillis(), 1000);
    }

    pub fn restoreEntry(self: *ConcurrentKV, key: []const u8, value: []const u8, expires_at: ?i64) !void {
        return self.setInternal(key, value, expires_at orelse 0);
    }

    // ── Bulk operations ──

    /// FLUSHDB swaps each stripe to an empty map, then frees its old entries
    /// outside that stripe lock.
    pub fn flushdb(self: *ConcurrentKV) void {
        const alloc = self.allocator;
        for (&self.stripes) |*s| {
            writeLockStripe(s);
            var old_map = s.map;
            s.map = fractional_map.StringHashMap(Entry).init(alloc);
            writeUnlockStripe(s);

            var iter = old_map.iterator();
            while (iter.next()) |entry| {
                self.freeEntry(entry.key_ptr.*, entry.value_ptr);
            }
            old_map.deinit();
        }
        self.total_bytes.store(0, .monotonic);
    }

    /// Call once after FLUSHDB has also released the non-KV stores.
    pub fn trimAfterFlush(self: *ConcurrentKV) void {
        // FLUSHDB is the explicit bulk-reclamation boundary. On Linux glibc
        // with the process c allocator, return fully freed heap pages now;
        // never trim in a request or maintenance hot path.
        if (comptime builtin.os.tag == .linux and builtin.abi == .gnu and builtin.link_libc) {
            if (self.allocator.vtable == std.heap.c_allocator.vtable) _ = malloc_trim(0);
        }
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

    /// Requested metadata bytes only; table/key/value accounting remains
    /// separate so diagnostics cannot infer this from a residual.
    pub fn metadataRequestedBytes(self: *ConcurrentKV) usize {
        self.readLockAll();
        defer self.readUnlockAll();
        var total: usize = 0;
        for (&self.stripes) |*s| {
            var iter = s.map.iterator();
            while (iter.next()) |entry| {
                if (entry.value_ptr.hasMetadata()) total += @sizeOf(Entry.Metadata);
            }
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
        defer writeUnlockStripe(s);

        const result = s.map.getPtr(key);
        if (result) |existing| {
            if (self.isExpired(existing)) {
                // Treat expired as 0
                const meta = existing.ensureMetadata(self.allocator) catch return error.OutOfMemory;
                const is_inline = existing.isInline();
                meta.* = .{
                    .last_access = self.nowMillis(),
                    .int_value = delta,
                    .aux = Entry.auxWith(.{ .is_integer = true, .is_inline = is_inline }, if (is_inline) existing.inlineLen() else 0, existing.isCombined()),
                };
                return delta;
            }

            // If already marked as integer, use cached int_value directly (~1ns)
            if (existing.isInteger()) {
                const meta = existing.metadata().?;
                meta.int_value += delta;
                meta.last_access = self.nowMillis();
                const new_val = meta.int_value;
                return new_val;
            }

            // First INCR on a string value — parse once, then use native from here on
            const current = std.fmt.parseInt(i64, existing.bytes(), 10) catch {
                return error.NotAnInteger;
            };
            const meta = existing.ensureMetadata(self.allocator) catch return error.OutOfMemory;
            const is_inline = existing.isInline();
            meta.* = .{
                .last_access = self.nowMillis(),
                .int_value = current + delta,
                .aux = Entry.auxWith(.{ .is_integer = true, .is_inline = is_inline }, if (is_inline) existing.inlineLen() else 0, existing.isCombined()),
            };
            const new_val = meta.int_value;
            return new_val;
        }

        const alloc = self.allocator;
        const owned_key = alloc.dupe(u8, key) catch {
            return error.OutOfMemory;
        };
        errdefer alloc.free(owned_key);
        var entry: Entry = .{};
        entry.setInlineState(0);
        const meta = entry.ensureMetadata(alloc) catch return error.OutOfMemory;
        meta.* = .{ .last_access = self.nowMillis(), .int_value = delta, .aux = Entry.auxWith(.{ .is_integer = true }, 0, false) };
        s.map.put(owned_key, entry) catch {
            entry.freeMetadata(alloc);
            return error.OutOfMemory;
        };
        _ = self.total_bytes.fetchAdd(owned_key.len, .monotonic);
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
                old_value_bytes = existing.bytes().len;
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

        const now = self.nowMillis();
        const needs_metadata = expires_at != 0 or self.eviction_policy == .allkeys_lru;
        if (s.map.getEntry(key)) |gop| {
            const old = gop.value_ptr.*;
            const old_key = gop.key_ptr.*;
            const old_value_len = old.bytes().len;
            const old_combined = old.isCombined();
            const old_allocation = if (old.isInline()) null else old.allocation(old_key.len);
            const old_meta = old.metadata();

            // Reuse a direct heap buffer when its capacity remains suitable.
            if (value.len > INLINE_BUF_SIZE and !old.isInline() and
                old.storage.heap.capacity >= value.len and old.storage.heap.capacity / 2 <= value.len)
            {
                const fresh_meta = if (needs_metadata and old_meta == null)
                    try alloc.create(Entry.Metadata)
                else
                    null;
                std.mem.copyForwards(u8, @constCast(old.storage.heap.ptr)[0..value.len], value);
                gop.value_ptr.storage.heap.len = value.len;
                if (needs_metadata) {
                    setStringMetadata(gop.value_ptr, fresh_meta orelse old_meta.?, expires_at, now);
                } else {
                    gop.value_ptr.freeMetadata(alloc);
                }
            } else {
                // Stage all allocations before changing the map key or entry.
                var prepared_meta: ?*Entry.Metadata = null;
                if (needs_metadata and old_meta == null) prepared_meta = try alloc.create(Entry.Metadata);
                errdefer if (prepared_meta) |meta| alloc.destroy(meta);

                var next: Entry = .{};
                var next_key: []u8 = undefined;
                if (value.len > INLINE_BUF_SIZE) {
                    const combined = try allocateCombined(alloc, key, value);
                    next = combined.entry;
                    next_key = combined.key;
                } else {
                    if (old_combined) {
                        next_key = try alloc.dupe(u8, key);
                    } else {
                        next_key = @constCast(old_key);
                    }
                    @memcpy(next.storage.inline_buf[0..value.len], value);
                    next.setInlineState(value.len);
                }
                errdefer self.freeEntry(next_key, &next);

                if (needs_metadata) {
                    const meta = prepared_meta orelse old_meta.?;
                    setStringMetadata(&next, meta, expires_at, now);
                    prepared_meta = null;
                }

                const replaces_key = next_key.ptr != old_key.ptr;
                if (replaces_key) gop.key_ptr.* = next_key;
                gop.value_ptr.* = next;
                if (old_allocation) |allocation| alloc.free(allocation);
                if (!old_combined and replaces_key) alloc.free(old_key);
                if (!needs_metadata) {
                    if (old_meta) |meta| alloc.destroy(meta);
                }
            }
            if (expires_at != 0) self.has_expiry.store(true, .release);
            if (value.len > old_value_len) _ = self.total_bytes.fetchAdd(value.len - old_value_len, .monotonic);
            if (value.len < old_value_len) _ = self.total_bytes.fetchSub(old_value_len - value.len, .monotonic);
            return;
        }

        var entry: Entry = .{};
        var owned_key: []u8 = undefined;
        if (value.len > INLINE_BUF_SIZE) {
            const combined = try allocateCombined(alloc, key, value);
            owned_key = combined.key;
            entry = combined.entry;
        } else {
            owned_key = try alloc.dupe(u8, key);
            @memcpy(entry.storage.inline_buf[0..value.len], value);
            entry.setInlineState(value.len);
        }
        errdefer self.freeEntry(owned_key, &entry);
        if (needs_metadata) {
            const meta = try entry.ensureMetadata(alloc);
            setStringMetadata(&entry, meta, expires_at, now);
        }
        if (expires_at != 0) self.has_expiry.store(true, .release);
        const inserted = try s.map.getOrPut(owned_key);
        std.debug.assert(!inserted.found_existing);
        inserted.key_ptr.* = owned_key;
        inserted.value_ptr.* = entry;
        _ = self.total_bytes.fetchAdd(owned_key.len + value.len, .monotonic);
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
        var projected = initial_projected;

        while (projected > self.maxmemory) {
            if (s.map.count() == 0) return;

            var oldest_key: ?[]const u8 = null;
            var oldest_access: i64 = std.math.maxInt(i64);
            var samples: usize = 0;
            var it = s.map.iterator();
            while (it.next()) |entry| {
                if (entry.value_ptr.lastAccess() < oldest_access) {
                    oldest_access = entry.value_ptr.lastAccess();
                    oldest_key = entry.key_ptr.*;
                }
                samples += 1;
                if (samples >= EVICTION_SAMPLE_SIZE) break;
            }

            const victim_key = oldest_key orelse return;

            // Remove victim — this stripe is already write-locked, safe to mutate map.
            var removed = s.map.fetchRemove(victim_key) orelse return;
            const freed_bytes: usize = removed.key.len + removed.value.bytes().len;
            self.freeEntry(removed.key, &removed.value);

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

    /// Remove a bounded number of expired entries. The maintenance owner holds
    /// the global KV mutex and frees returned allocations after WATCH versions
    /// are bumped. Cursors address physical HashMap buckets, not entries, so a
    /// sparse table cannot make an idle pass scan an unbounded prefix.
    pub fn sweepExpired(self: *ConcurrentKV, now_ms: i64, cursor: *SweepCursor, removed: *[512]Expired) usize {
        if (!self.has_expiry.load(.acquire)) return 0;
        self.cached_now_ms.store(now_ms, .release);
        var count: usize = 0;
        var visited: usize = 0;
        while (visited < STRIPE_COUNT and count < removed.len) : (visited += 1) {
            const stripe_index = cursor.next_stripe;
            cursor.next_stripe = (cursor.next_stripe + 1) & STRIPE_MASK;
            const s = &self.stripes[stripe_index];
            if (std.c.pthread_rwlock_trywrlock(&s.rwlock) != .SUCCESS) continue;
            var detached: ?fractional_map.StringHashMap(Entry) = null;
            defer {
                writeUnlockStripe(s);
                if (detached) |*map| map.deinit();
            }
            const capacity: usize = @intCast(s.map.capacity());
            if (capacity == 0) continue;
            cursor.buckets[stripe_index] %= capacity;
            var scanned: usize = 0;
            var victims: usize = 0;
            while (scanned < @min(@as(usize, 32), capacity) and victims < 64 and count < removed.len) : (scanned += 1) {
                const bucket = cursor.buckets[stripe_index];
                cursor.buckets[stripe_index] = (bucket + 1) % capacity;
                const metadata = s.map.unmanaged.metadata orelse continue;
                if (!metadata[bucket].isUsed()) continue;
                var it = s.map.iterator();
                it.index = @intCast(bucket);
                const entry = it.next() orelse continue;
                if (!entry.value_ptr.hasTtl() or now_ms <= entry.value_ptr.expiresAt()) continue;
                const stale = s.map.fetchRemove(entry.key_ptr.*) orelse continue;
                const combined = stale.value.isCombined();
                removed[count] = .{
                    .key = stale.key,
                    .value = if (stale.value.isInline()) null else stale.value.allocation(stale.key.len),
                    .metadata = stale.value.metadata(),
                    .free_key = !combined,
                };
                _ = self.total_bytes.fetchSub(stale.key.len + stale.value.bytes().len, .monotonic);
                _ = obs_stats.expired_keys.fetchAdd(1, .monotonic);
                count += 1;
                victims += 1;
            }
            if (s.map.count() == 0 and s.map.capacity() != 0) {
                detached = s.map;
                s.map = fractional_map.StringHashMap(Entry).init(self.allocator);
                cursor.buckets[stripe_index] = 0;
            }
        }
        return count;
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
        self.cached_now_ms.store(std.Io.Timestamp.now(self.io, .real).toMilliseconds(), .release);
    }

    pub fn nowMillis(self: *const ConcurrentKV) i64 {
        return self.cached_now_ms.load(.acquire);
    }

    fn isExpired(self: *const ConcurrentKV, entry: *const Entry) bool {
        if (!entry.hasTtl()) return false;
        return self.nowMillis() > entry.expiresAt();
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
