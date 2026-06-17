//! Transparent RESP TCP proxy for vex-embed.
//!
//! Per client connection we open one upstream connection to vex and pump
//! bytes in both directions. The proxy is byte-for-byte transparent EXCEPT
//! for the `EMBED <text>` command, which it intercepts: it never reaches
//! vex (vex deliberately does not compute embeddings). Everything else is
//! forwarded untouched, so a client pointed at the proxy behaves exactly
//! like one talking to vex.
//!
//! Threading model: one accept loop, then per connection a small thread
//! pair — the client→vex direction is driven on the connection's own thread
//! (so it can peek for EMBED), and a second thread copies vex→client. This
//! is a proxy, not the DB hot path; a thread per direction is fine and keeps
//! the blocking embedding HTTP call (embedder.embed) off vex's event loop,
//! which is the whole reason this process exists.

const std = @import("std");
const Allocator = std.mem.Allocator;
const c = std.c;
const vex_log = @import("vex_log");
const config_mod = @import("config.zig");
const embedder = @import("embedder.zig");
const resp_detect = @import("resp_detect.zig");

pub const Proxy = struct {
    allocator: Allocator,
    cfg: config_mod.Config,
    listen_fd: c.fd_t = -1,
    stop: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),

    pub fn init(allocator: Allocator, cfg: config_mod.Config) Proxy {
        return .{ .allocator = allocator, .cfg = cfg };
    }

    pub fn deinit(self: *Proxy) void {
        self.stop.store(true, .release);
        if (self.listen_fd >= 0) {
            _ = c.close(self.listen_fd);
            self.listen_fd = -1;
        }
    }

    /// Bind + listen. Does not spawn a thread; call `run` to block-serve.
    pub fn listen(self: *Proxy) !void {
        const fd = c.socket(c.AF.INET, c.SOCK.STREAM, 0);
        if (fd < 0) return error.SocketFailed;

        var yes: c_int = 1;
        _ = c.setsockopt(fd, c.SOL.SOCKET, c.SO.REUSEADDR, &yes, @sizeOf(c_int));

        var addr: c.sockaddr.in = .{
            .family = c.AF.INET,
            .port = std.mem.nativeToBig(u16, self.cfg.listen_port),
            .addr = 0, // INADDR_ANY
        };
        if (c.bind(fd, @ptrCast(&addr), @sizeOf(c.sockaddr.in)) < 0) {
            _ = c.close(fd);
            return error.BindFailed;
        }
        if (c.listen(fd, 128) < 0) {
            _ = c.close(fd);
            return error.ListenFailed;
        }
        self.listen_fd = fd;
        vex_log.info("vex-embed: listening on :{d} -> vex {s}:{d}", .{
            self.cfg.listen_port, self.cfg.vex_host, self.cfg.vex_port,
        });
    }

    /// Accept loop. Returns when `stop` is set and accept is interrupted.
    pub fn run(self: *Proxy) void {
        while (!self.stop.load(.acquire)) {
            var caddr: c.sockaddr.in = undefined;
            var clen: c.socklen_t = @sizeOf(c.sockaddr.in);
            const conn = c.accept(self.listen_fd, @ptrCast(&caddr), &clen);
            if (conn < 0) {
                if (self.stop.load(.acquire)) return;
                continue;
            }
            // Detach a thread per client. On spawn failure, drop the client.
            const ctx = self.allocator.create(ConnCtx) catch {
                _ = c.close(conn);
                continue;
            };
            ctx.* = .{ .proxy = self, .client_fd = conn };
            const t = std.Thread.spawn(.{}, ConnCtx.serve, .{ctx}) catch {
                self.allocator.destroy(ctx);
                _ = c.close(conn);
                continue;
            };
            t.detach();
        }
    }
};

const ConnCtx = struct {
    proxy: *Proxy,
    client_fd: c.fd_t,

    fn serve(self: *ConnCtx) void {
        const proxy = self.proxy;
        const allocator = proxy.allocator;
        defer {
            _ = c.close(self.client_fd);
            allocator.destroy(self);
        }

        // Open upstream vex connection.
        const vex_fd = connectVex(allocator, proxy.cfg) catch {
            // Reply with a RESP error so the client isn't left hanging.
            writeAll(self.client_fd, "-ERR vex-embed: upstream connect failed\r\n") catch {};
            return;
        };
        defer _ = c.close(vex_fd);

        // vex → client pump on its own thread.
        const back = allocator.create(PumpCtx) catch return;
        back.* = .{ .src = vex_fd, .dst = self.client_fd, .stop = std.atomic.Value(bool).init(false) };
        const back_thread = std.Thread.spawn(.{}, PumpCtx.pump, .{back}) catch {
            allocator.destroy(back);
            return;
        };
        defer {
            back.stop.store(true, .release);
            _ = c.shutdown(vex_fd, c.SHUT.RDWR);
            back_thread.join();
            allocator.destroy(back);
        }

        // client → vex direction, with EMBED interception.
        self.clientToVex(vex_fd);
    }

    /// Drive client→vex. Buffers just enough to detect a complete EMBED
    /// command at the head of the stream; bytes that aren't EMBED are
    /// forwarded unchanged.
    fn clientToVex(self: *ConnCtx, vex_fd: c.fd_t) void {
        const allocator = self.proxy.allocator;
        var pending = std.array_list.Managed(u8).init(allocator);
        defer pending.deinit();

        var rbuf: [16 * 1024]u8 = undefined;
        while (true) {
            const n = c.read(self.client_fd, &rbuf, rbuf.len);
            if (n <= 0) return;
            pending.appendSlice(rbuf[0..@intCast(n)]) catch return;

            // Drain complete commands from the head of `pending`.
            while (pending.items.len > 0) {
                const det = resp_detect.detectEmbed(pending.items);
                switch (det) {
                    .embed => |e| {
                        self.handleEmbed(pending.items[e.text_start..e.text_end]);
                        replaceHead(&pending, e.consumed) catch return;
                    },
                    .not_embed => |consumed| {
                        // A full, non-EMBED command sits at the head — forward
                        // exactly those bytes to vex and drop them.
                        writeAll(vex_fd, pending.items[0..consumed]) catch return;
                        replaceHead(&pending, consumed) catch return;
                    },
                    .incomplete => {
                        // Not a full command yet. If the head can't possibly be
                        // EMBED (doesn't start like `*…EMBED`), forwarding the
                        // partial bytes is safe and keeps latency low. We only
                        // hold bytes back while they might still become EMBED.
                        if (resp_detect.mightBeEmbed(pending.items)) break; // wait for more
                        writeAll(vex_fd, pending.items) catch return;
                        pending.clearRetainingCapacity();
                        break;
                    },
                }
            }
        }
    }

    /// Compute the embedding and reply to the client with a RESP bulk string
    /// of the raw little-endian f32 bytes. The client then feeds those bytes
    /// to GRAPH.SETVEC / VECSEARCH.
    fn handleEmbed(self: *ConnCtx, text: []const u8) void {
        const allocator = self.proxy.allocator;
        const vec = embedder.embed(allocator, self.proxy.cfg, text) catch |err| {
            var ebuf: [128]u8 = undefined;
            const msg = std.fmt.bufPrint(&ebuf, "-ERR vex-embed: {s}\r\n", .{@errorName(err)}) catch
                "-ERR vex-embed: embedding failed\r\n";
            writeAll(self.client_fd, msg) catch {};
            return;
        };
        defer allocator.free(vec);

        const bytes = embedder.floatsToBytes(allocator, vec) catch {
            writeAll(self.client_fd, "-ERR vex-embed: out of memory\r\n") catch {};
            return;
        };
        defer allocator.free(bytes);

        // RESP bulk string: $<len>\r\n<bytes>\r\n
        var hdr: [32]u8 = undefined;
        const h = std.fmt.bufPrint(&hdr, "${d}\r\n", .{bytes.len}) catch return;
        writeAll(self.client_fd, h) catch return;
        writeAll(self.client_fd, bytes) catch return;
        writeAll(self.client_fd, "\r\n") catch return;

        // TODO(auto-rewrite): per-command interception for CACHE.* / MEMORY.*
        // hooks in here. Instead of returning the vector to the client, the
        // proxy would embed the text argument inline and rewrite the command
        // into the vector form before forwarding to vex (e.g. CACHE.SET key
        // <text> → GRAPH.SETVEC … <vec>). Out of scope for the scaffold.
    }
};

/// Background copy: src → dst until EOF/error or `stop`.
const PumpCtx = struct {
    src: c.fd_t,
    dst: c.fd_t,
    stop: std.atomic.Value(bool),

    fn pump(self: *PumpCtx) void {
        var buf: [16 * 1024]u8 = undefined;
        while (!self.stop.load(.acquire)) {
            const n = c.read(self.src, &buf, buf.len);
            if (n <= 0) return;
            writeAll(self.dst, buf[0..@intCast(n)]) catch return;
        }
    }
};

// ── helpers ─────────────────────────────────────────────────────────

fn connectVex(allocator: Allocator, cfg: config_mod.Config) !c.fd_t {
    const addr = resolveHost(allocator, cfg.vex_host) orelse return error.ResolveFailed;
    const sock = c.socket(c.AF.INET, c.SOCK.STREAM, 0);
    if (sock < 0) return error.SocketFailed;
    errdefer _ = c.close(sock);

    var sa: c.sockaddr.in = .{
        .family = c.AF.INET,
        .port = std.mem.nativeToBig(u16, cfg.vex_port),
        .addr = addr,
    };
    if (c.connect(sock, @ptrCast(&sa), @sizeOf(c.sockaddr.in)) < 0) return error.ConnectFailed;
    return sock;
}

/// Drop the first `n` bytes of `list`, shifting the tail down.
fn replaceHead(list: *std.array_list.Managed(u8), n: usize) !void {
    if (n >= list.items.len) {
        list.clearRetainingCapacity();
        return;
    }
    std.mem.copyForwards(u8, list.items[0 .. list.items.len - n], list.items[n..]);
    list.shrinkRetainingCapacity(list.items.len - n);
}

fn writeAll(fd: c.fd_t, data: []const u8) !void {
    var off: usize = 0;
    while (off < data.len) {
        const n = c.write(fd, data.ptr + off, data.len - off);
        if (n <= 0) return error.WriteFailed;
        off += @intCast(n);
    }
}

// Host resolution mirrors embedder.zig / src/cluster/replication.zig.
fn resolveHost(allocator: Allocator, host: []const u8) ?u32 {
    if (parseIpv4(host)) |ip| return ip;
    const host_z = allocator.dupeSentinel(u8, host, 0) catch return null;
    defer allocator.free(host_z);
    var hints: c.addrinfo = std.mem.zeroes(c.addrinfo);
    hints.family = c.AF.INET;
    var result: ?*c.addrinfo = null;
    const gai = c.getaddrinfo(host_z, null, &hints, &result);
    if (@intFromEnum(gai) != 0) return null;
    defer if (result) |r| c.freeaddrinfo(r);
    if (result) |res| {
        const a: *c.sockaddr.in = @ptrCast(@alignCast(res.addr));
        return a.addr;
    }
    return null;
}

fn parseIpv4(s: []const u8) ?u32 {
    var octets: [4]u8 = undefined;
    var idx: usize = 0;
    var start: usize = 0;
    for (s, 0..) |ch, i| {
        if (ch == '.') {
            if (idx >= 3) return null;
            octets[idx] = std.fmt.parseInt(u8, s[start..i], 10) catch return null;
            idx += 1;
            start = i + 1;
        }
    }
    if (idx != 3) return null;
    octets[3] = std.fmt.parseInt(u8, s[start..], 10) catch return null;
    return @as(u32, octets[0]) | (@as(u32, octets[1]) << 8) |
        (@as(u32, octets[2]) << 16) | (@as(u32, octets[3]) << 24);
}
