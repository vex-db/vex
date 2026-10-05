const std = @import("std");

/// One dominant DB-0 key, sampled by workers. Locks remain authoritative;
/// an old routing snapshot is safe across activation/release transitions.
pub const AdaptiveOwner = struct {
    pub const max_key = 128;
    const window_ns = 200 * std.time.ns_per_ms;
    const hold_ns = std.time.ns_per_s;
    const cooldown_ns = 2 * std.time.ns_per_s;
    mutex: std.c.pthread_mutex_t = std.c.PTHREAD_MUTEX_INITIALIZER,
    generation: std.atomic.Value(u64) = .init(0),
    key: [max_key]u8 = undefined,
    key_len: usize = 0,
    candidate: [max_key]u8 = undefined,
    candidate_len: usize = 0,
    votes: usize = 0,
    matches: usize = 0,
    contended: usize = 0,
    wait_ns: u64 = 0,
    samples: usize = 0,
    active_hits: usize = 0,
    queue_ns: u64 = 0,
    previous: [max_key]u8 = undefined,
    previous_len: usize = 0,
    hot_windows: usize = 0,
    cold_windows: usize = 0,
    window_start: u64 = 0,
    since: u64 = 0,
    cooldown_until: u64 = 0,

    pub const Change = enum { none, activate, release };

    pub fn now() u64 {
        var ts: std.c.timespec = undefined;
        _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
        return @as(u64, @intCast(ts.sec)) * std.time.ns_per_s + @as(u64, @intCast(ts.nsec));
    }

    pub fn snapshot(self: *AdaptiveOwner, key: *[max_key]u8, len: *usize) u64 {
        _ = std.c.pthread_mutex_lock(&self.mutex);
        defer _ = std.c.pthread_mutex_unlock(&self.mutex);
        len.* = self.key_len;
        @memcpy(key[0..len.*], self.key[0..len.*]);
        return self.generation.load(.monotonic);
    }

    pub fn observe(self: *AdaptiveOwner, key: []const u8, waited: u64, queued: u64, at: u64) Change {
        if (key.len == 0 or key.len > max_key) return .none;
        _ = std.c.pthread_mutex_lock(&self.mutex);
        defer _ = std.c.pthread_mutex_unlock(&self.mutex);
        var change: Change = .none;
        if (self.window_start == 0) self.window_start = at;
        if (at >= self.window_start and at - self.window_start >= window_ns) {
            if (self.key_len > 0) {
                const cold = self.samples < 128 or self.active_hits * 5 < self.samples;
                const overloaded = self.active_hits > 0 and self.queue_ns / self.active_hits > 5 * std.time.ns_per_ms;
                self.cold_windows = if (cold or overloaded) self.cold_windows + 1 else 0;
                if (at - self.since >= hold_ns and self.cold_windows >= 3) {
                    self.key_len = 0;
                    self.cooldown_until = at + cooldown_ns;
                    self.hot_windows = 0;
                    _ = self.generation.fetchAdd(1, .release);
                    change = .release;
                }
            } else {
                const hot = self.samples >= 128 and self.matches * 5 >= self.samples * 3 and
                    self.contended * 10 >= self.matches and self.wait_ns / @max(1, self.matches) >= 500;
                const same = std.mem.eql(u8, self.candidate[0..self.candidate_len], self.previous[0..self.previous_len]);
                self.hot_windows = if (hot and same) self.hot_windows + 1 else if (hot) 1 else 0;
                self.previous_len = self.candidate_len;
                @memcpy(self.previous[0..self.previous_len], self.candidate[0..self.candidate_len]);
                if (self.hot_windows >= 3 and at >= self.cooldown_until) {
                    self.key_len = self.candidate_len;
                    @memcpy(self.key[0..self.key_len], self.candidate[0..self.key_len]);
                    self.since = at;
                    self.cold_windows = 0;
                    _ = self.generation.fetchAdd(1, .release);
                    change = .activate;
                }
            }
            self.window_start = at;
            self.samples = 0;
            self.active_hits = 0;
            self.queue_ns = 0;
            self.votes = 0;
            self.matches = 0;
            self.contended = 0;
            self.wait_ns = 0;
        }
        self.samples += 1;
        if (self.key_len > 0 and std.mem.eql(u8, key, self.key[0..self.key_len])) {
            self.active_hits += 1;
            self.queue_ns += queued;
        }
        // Boyer-Moore candidate selection; matches is a conservative lower
        // bound after replacement. Enter only for a >60% dominant key.
        if (self.votes == 0 and !std.mem.eql(u8, key, self.candidate[0..self.candidate_len])) {
            self.candidate_len = key.len;
            @memcpy(self.candidate[0..key.len], key);
            self.matches = 0;
            self.contended = 0;
            self.wait_ns = 0;
        }
        if (std.mem.eql(u8, key, self.candidate[0..self.candidate_len])) {
            self.votes += 1;
            self.matches += 1;
            self.contended += @intFromBool(waited > 0);
            self.wait_ns += waited;
        } else self.votes -= 1;
        return change;
    }
};

test "adaptive owner needs contention, enters, releases and cools down" {
    var policy: AdaptiveOwner = .{};
    var tick: u64 = 1;
    // A frequent uncontended key must never activate.
    for (0..5) |_| {
        for (0..200) |_| _ = policy.observe("hot", 0, 0, tick);
        tick += 201 * std.time.ns_per_ms;
    }
    try std.testing.expectEqual(@as(usize, 0), policy.key_len);
    for (0..5) |_| {
        for (0..200) |_| _ = policy.observe("hot", 2000, 0, tick);
        tick += 201 * std.time.ns_per_ms;
    }
    try std.testing.expectEqualStrings("hot", policy.key[0..policy.key_len]);
    for (0..10) |_| {
        for (0..200) |i| _ = policy.observe(if (i % 2 == 0) "cold-a" else "cold-b", 2000, 0, tick);
        tick += 201 * std.time.ns_per_ms;
    }
    try std.testing.expectEqual(@as(usize, 0), policy.key_len);
    const deadline = policy.cooldown_until;
    while (tick < deadline) : (tick += 201 * std.time.ns_per_ms) {
        for (0..200) |_| _ = policy.observe("hot", 2000, 0, tick);
        try std.testing.expectEqual(@as(usize, 0), policy.key_len);
    }
}


test "distributed contention does not activate and queue overload releases" {
    var policy: AdaptiveOwner = .{};
    var tick: u64 = 1;
    for (0..6) |_| {
        for (0..256) |i| _ = policy.observe(if (i % 2 == 0) "a" else "b", 5000, 0, tick);
        tick += 201 * std.time.ns_per_ms;
    }
    try std.testing.expectEqual(@as(usize, 0), policy.key_len);
    for (0..5) |_| {
        for (0..256) |_| _ = policy.observe("hot", 5000, 0, tick);
        tick += 201 * std.time.ns_per_ms;
    }
    try std.testing.expectEqualStrings("hot", policy.key[0..policy.key_len]);
    for (0..6) |_| {
        for (0..256) |_| _ = policy.observe("hot", 0, 6 * std.time.ns_per_ms, tick);
        tick += 201 * std.time.ns_per_ms;
    }
    try std.testing.expectEqual(@as(usize, 0), policy.key_len);
    // Samples timestamped before another worker advanced the window are safe.
    _ = policy.observe("hot", 1000, 0, policy.window_start - 1);
}
