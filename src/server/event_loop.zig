const std = @import("std");
const builtin = @import("builtin");
const linux = std.os.linux;
const vex_log = @import("../log.zig");
const probes_mod = @import("../observability/probes.zig");

/// A submit_and_wait that returns faster than this did not deschedule the
/// thread (the CQE was already posted); slower means a genuine kernel sleep.
const WAIT_BLOCKED_NS: u64 = 2_000;

const is_linux = builtin.os.tag == .linux;
const is_darwin = builtin.os.tag == .macos or builtin.os.tag == .ios or
    builtin.os.tag == .tvos or builtin.os.tag == .watchos or
    builtin.os.tag == .visionos;

const MAX_EVENTS = 256;
const FD_TABLE_SIZE = 4096;

// io_uring operation tags encoded in upper bits of user_data
const OP_POLL: u64 = 0;
const OP_RECV: u64 = 1;
const OP_SEND: u64 = 2;
const OP_AOF_WRITE: u64 = 3;
const OP_AOF_FSYNC: u64 = 4;

fn encodeUserData(op: u64, fd: i32) u64 {
    return (op << 48) | @as(u64, @intCast(@as(u32, @bitCast(fd))));
}

fn decodeOp(user_data: u64) u64 {
    return (user_data >> 48) & 0xFFFF;
}

fn decodeFd(user_data: u64) i32 {
    return @bitCast(@as(u32, @truncate(user_data)));
}

/// Monotonic clock in ns (VDSO clock_gettime; same source as probes.zig).
/// Used by the spin-before-park budget — cheap enough to read between batches
/// of userspace CQ peeks.
inline fn monoNs() u64 {
    var ts: std.c.timespec = undefined;
    _ = std.c.clock_gettime(std.c.CLOCK.MONOTONIC, &ts);
    return @as(u64, @intCast(ts.sec)) * 1_000_000_000 + @as(u64, @intCast(ts.nsec));
}

/// Spin-before-park budget, in ns, read once from VEX_POLL_SPIN_US (microseconds).
/// 0 (the default / unset / unparseable) preserves the original park-immediately
/// behavior exactly — no extra syscalls, no spin, no clock reads.
fn readSpinNs() u64 {
    const raw = std.c.getenv("VEX_POLL_SPIN_US") orelse return 0;
    const s = std.mem.span(raw);
    const us = std.fmt.parseInt(u64, std.mem.trim(u8, s, " \t\r\n"), 10) catch return 0;
    return us *| 1_000;
}

/// VEX_POLL_SPIN_ADAPTIVE: default true (polite). Set to "0"/"false"/"no" to
/// pin the warm gate on — always spin the budget (Dragonfly-style busy poll).
fn readSpinAdaptive() bool {
    const raw = std.c.getenv("VEX_POLL_SPIN_ADAPTIVE") orelse return true;
    const s = std.mem.trim(u8, std.mem.span(raw), " \t\r\n");
    return !(std.mem.eql(u8, s, "0") or std.mem.eql(u8, s, "false") or std.mem.eql(u8, s, "no"));
}

// ── io_uring NAPI busy-poll ──────────────────────────────────────────────
// When VEX_NAPI_BUSY_POLL_US > 0, register the ring's NAPI context so a waiting
// io_uring_enter busy-polls the NIC's receive queue INLINE for up to that many
// microseconds before parking — the worker processes the network softirq work
// in its own context instead of waiting for a separate softirq to run and then
// a scheduler wakeup. This attacks the context-switch/parking churn AND the
// kernel-network cost directly (unlike the app-level spin above, which polled an
// already-empty completion queue). Requires Linux >= 6.9; no-ops gracefully if
// the kernel rejects the registration.
const IORING_REGISTER_NAPI: usize = 27;

const io_uring_napi = extern struct {
    busy_poll_to: u32,
    prefer_busy_poll: u8,
    pad: [3]u8 = .{ 0, 0, 0 },
    resv: u64 = 0,
};

fn readNapiBusyPollUs() u32 {
    const raw = std.c.getenv("VEX_NAPI_BUSY_POLL_US") orelse return 0;
    const s = std.mem.trim(u8, std.mem.span(raw), " \t\r\n");
    return std.fmt.parseInt(u32, s, 10) catch 0;
}

/// Register NAPI busy-poll on `ring_fd`. Returns true on success.
fn registerNapi(ring_fd: i32, busy_poll_us: u32) bool {
    var cfg = io_uring_napi{ .busy_poll_to = busy_poll_us, .prefer_busy_poll = 1 };
    // nr_args MUST be 1 for IORING_REGISTER_NAPI (matches liburing); the kernel
    // rejects 0 with -EINVAL.
    const rc = linux.syscall4(
        .io_uring_register,
        @as(usize, @intCast(ring_fd)),
        IORING_REGISTER_NAPI,
        @intFromPtr(&cfg),
        1,
    );
    return @as(isize, @bitCast(rc)) >= 0;
}

// ── io_uring one-thread-per-ring setup flags ─────────────────────────────
// SINGLE_ISSUER + DEFER_TASKRUN + COOP_TASKRUN: completion task-work runs on
// this worker's io_uring_enter (submit_and_wait) instead of being forced via an
// IPI/softirq — fewer cross-task wakeups and lower per-completion overhead for a
// reactor where one thread owns one ring. DEFER_TASKRUN requires SINGLE_ISSUER,
// which requires the ring to be ENABLED by its sole submitter — so the ring is
// created R_DISABLED and enabled from the worker thread (see enableRing).
// Requires Linux >= 6.1. VEX_URING_FLAGS=0 forces the plain (flags=0) ring.
const IORING_REGISTER_ENABLE_RINGS: usize = 12;
const URING_OPT_FLAGS: u32 = linux.IORING_SETUP_SINGLE_ISSUER |
    linux.IORING_SETUP_DEFER_TASKRUN |
    linux.IORING_SETUP_COOP_TASKRUN;

fn readUringFlags() u32 {
    const raw = std.c.getenv("VEX_URING_FLAGS") orelse return URING_OPT_FLAGS;
    const s = std.mem.trim(u8, std.mem.span(raw), " \t\r\n");
    if (std.mem.eql(u8, s, "0") or std.mem.eql(u8, s, "false")) return 0;
    return URING_OPT_FLAGS;
}

pub const EventLoop = struct {
    pub const Event = struct {
        fd: i32,
        data: usize,
        readable: bool,
        writable: bool,
        err: bool,
        hup: bool,
        op: u2 = 0, // 0=poll, 1=recv, 2=send
        bytes: i32 = 0, // bytes transferred (recv/send CQE result)
    };

    // --- Backend handle ---
    // macOS: kqueue fd
    // Linux: io_uring instance (stored separately) or epoll fd
    kq_or_epfd: i32,

    // --- io_uring (Linux only) ---
    ring: if (is_linux) linux.IoUring else void,

    // --- Notify mechanism ---
    notify_read_fd: i32,
    notify_write_fd: i32,

    // --- macOS event buffer ---
    events_buf: if (is_darwin) [MAX_EVENTS]std.c.Kevent else void,

    // --- Linux: fd -> user data + tracking which fds have active polls ---
    fd_data: if (is_linux) [FD_TABLE_SIZE]usize else void,
    fd_active: if (is_linux) [FD_TABLE_SIZE]bool else void,
    fd_want_write: if (is_linux) [FD_TABLE_SIZE]bool else void,
    /// Whether OP_POLL completions for this fd should re-arm the poll.
    /// True for poll-driven fds (TLS, legacy); false for recv-mode fds,
    /// which are driven by recv/send SQEs and only arm a poll transiently
    /// while write interest is registered (send(2) EAGAIN fallback).
    fd_poll_rearm: if (is_linux) [FD_TABLE_SIZE]bool else void,

    /// use_uring is set at init time; if io_uring fails we fall back to epoll.
    use_uring: if (is_linux) bool else void,
    /// Set when the one-shot notify poll_add could not be re-armed (SQ full);
    /// retried at the top of every pollIoUring tick.
    notify_rearm_pending: if (is_linux) bool else void,
    epoll_events_buf: if (is_linux) [MAX_EVENTS]linux.epoll_event else void,

    /// Spin-before-park budget in ns (0 = park immediately, original behavior).
    /// Set from VEX_POLL_SPIN_US at init. Keeps the worker thread warm under
    /// load: peek the already-mmap'd completion queue for up to this long before
    /// sleeping in the kernel, so a request that lands a few µs from now is
    /// served on an already-running core instead of paying a ~2–5µs scheduler
    /// wakeup. io_uring path only.
    spin_ns: u64,
    /// Adaptive gate: only spin when the previous tick woke quickly (load is
    /// arriving). A worker that just slept a long time parks immediately, so an
    /// idle server stays near 0% CPU and we don't oversubscribe shared hosts.
    spin_hot: bool,
    /// When false (VEX_POLL_SPIN_ADAPTIVE=0), the gate above is ignored and the
    /// worker spins the full budget every tick — "always warm", Dragonfly-style:
    /// zero wakeups whenever load flows, at the cost of burning CPU under light
    /// load. Pair with a large VEX_POLL_SPIN_US for a near-never-park busy poll.
    /// Default true (polite: spin only when recently busy).
    spin_adaptive: bool,
    /// Ring was created R_DISABLED (SINGLE_ISSUER/DEFER_TASKRUN path) and must
    /// be enabled from the worker thread before first submit. enableRing()
    /// (called in Worker.run on the worker's own thread) flips this to false.
    pending_enable: bool,

    pub fn init() !EventLoop {
        if (is_linux) {
            return initLinux();
        } else if (is_darwin) {
            return initDarwin();
        } else {
            @compileError("Unsupported OS");
        }
    }

    fn initLinux() !EventLoop {
        // SQPOLL deliberately NOT used (kthread-per-ring oversubscribes at
        // --workers > 1). Try the one-thread-per-ring optimization flags first
        // (created R_DISABLED, enabled on the worker thread); fall back to a
        // plain ring, then epoll. VEX_URING_FLAGS=0 forces the plain ring.
        const opt = readUringFlags();
        if (opt != 0) {
            if (linux.IoUring.init(1024, opt | linux.IORING_SETUP_R_DISABLED)) |ring_val| {
                return initLinuxUring(ring_val, true);
            } else |_| {} // kernel < 6.1 or flags unsupported → plain ring
        }
        if (linux.IoUring.init(1024, 0)) |ring_val| {
            return initLinuxUring(ring_val, false);
        } else |_| {
            return initLinuxEpoll();
        }
    }

    fn initLinuxUring(ring_val: linux.IoUring, defer_enable: bool) !EventLoop {
        var ring = ring_val;
        const efd_raw = linux.eventfd(0, linux.EFD.NONBLOCK);
        if (efd_raw > std.math.maxInt(usize) / 2) {
            ring.deinit();
            return initLinuxEpoll();
        }
        const efd: i32 = @intCast(efd_raw);

        var self = EventLoop{
            .kq_or_epfd = 0,
            .ring = ring,
            .use_uring = true,
            .notify_rearm_pending = false,
            .epoll_events_buf = undefined,
            .notify_read_fd = efd,
            .notify_write_fd = efd,
            .events_buf = {},
            .fd_data = @splat(0),
            .fd_active = @splat(false),
            .fd_want_write = @splat(false),
            .fd_poll_rearm = @splat(false),
            .spin_ns = readSpinNs(),
            .spin_hot = false,
            .spin_adaptive = readSpinAdaptive(),
            .pending_enable = defer_enable,
        };

        // A R_DISABLED ring cannot be submitted to until enabled from the sole
        // issuer (the worker thread) — defer the notify poll-add + NAPI to
        // enableRing(). The plain ring is armed here as before.
        if (!defer_enable) {
            self.submitPollAdd(efd, @as(u32, linux.POLL.IN)) catch {
                ring.deinit();
                _ = std.c.close(efd);
                return initLinuxEpoll();
            };
            self.armNapi();
        }

        probes_mod.ring_mode.store(2, .monotonic);
        vex_log.info("event_loop: io_uring backend active (opt_flags={}, deferred_enable={})", .{ defer_enable, defer_enable });

        return self;
    }

    /// Enable a R_DISABLED ring FROM THE CALLING (worker) thread, making it the
    /// SINGLE_ISSUER submitter, then arm the notify poll + NAPI. No-op unless
    /// the ring is pending enable. MUST be called on the worker's own thread.
    pub fn enableRing(self: *EventLoop) void {
        if (!is_linux) return;
        if (!self.use_uring or !self.pending_enable) return;
        const rc = linux.syscall4(.io_uring_register, @as(usize, @intCast(self.ring.fd)), IORING_REGISTER_ENABLE_RINGS, 0, 0);
        if (@as(isize, @bitCast(rc)) < 0)
            vex_log.warn("event_loop: ENABLE_RINGS failed (rc={d})", .{@as(isize, @bitCast(rc))});
        self.submitPollAdd(self.notify_read_fd, @as(u32, linux.POLL.IN)) catch {
            self.notify_rearm_pending = true;
        };
        self.armNapi();
        self.pending_enable = false;
    }

    /// Opt-in NAPI busy-poll (VEX_NAPI_BUSY_POLL_US). Off by default.
    fn armNapi(self: *EventLoop) void {
        if (!is_linux) return;
        const napi_us = readNapiBusyPollUs();
        if (napi_us > 0) {
            if (registerNapi(self.ring.fd, napi_us))
                vex_log.info("event_loop: io_uring NAPI busy-poll on ({d}us)", .{napi_us})
            else
                vex_log.warn("event_loop: NAPI busy-poll unavailable (kernel < 6.9?), continuing without", .{});
        }
    }

    fn initLinuxEpoll() !EventLoop {
        const epfd_raw = linux.epoll_create1(0);
        if (epfd_raw > std.math.maxInt(usize) / 2) return error.EpollCreateFailed;
        const epfd: i32 = @intCast(epfd_raw);

        const efd_raw = linux.eventfd(0, linux.EFD.NONBLOCK);
        if (efd_raw > std.math.maxInt(usize) / 2) {
            _ = std.c.close(epfd);
            return error.EventFdFailed;
        }
        const efd: i32 = @intCast(efd_raw);

        var ev = linux.epoll_event{ .events = linux.EPOLL.IN | linux.EPOLL.ET, .data = .{ .fd = efd } };
        const ctl_rc = linux.epoll_ctl(epfd, linux.EPOLL.CTL_ADD, efd, &ev);
        if (ctl_rc > std.math.maxInt(usize) / 2) {
            _ = std.c.close(efd);
            _ = std.c.close(epfd);
            return error.EpollCtlFailed;
        }

        probes_mod.ring_mode.store(1, .monotonic);
        vex_log.warn("event_loop: epoll backend (io_uring unavailable)", .{});

        return .{
            .kq_or_epfd = epfd,
            .ring = undefined,
            .use_uring = false,
            .notify_rearm_pending = false,
            .epoll_events_buf = undefined,
            .notify_read_fd = efd,
            .notify_write_fd = efd,
            .events_buf = {},
            .fd_data = @splat(0),
            .fd_active = @splat(false),
            .fd_want_write = @splat(false),
            .fd_poll_rearm = @splat(false),
            .spin_ns = 0, // epoll path parks via epoll_wait timeout, no CQ to peek
            .spin_hot = false,
            .spin_adaptive = true,
            .pending_enable = false,
        };
    }

    fn initDarwin() !EventLoop {
        const kq = std.c.kqueue();
        if (kq < 0) return error.KqueueFailed;
        probes_mod.ring_mode.store(1, .monotonic);

        var pipe_fds: [2]std.c.fd_t = undefined;
        if (std.c.pipe(&pipe_fds) != 0) {
            _ = std.c.close(kq);
            return error.PipeFailed;
        }

        setNonBlocking(pipe_fds[0]);
        setNonBlocking(pipe_fds[1]);

        var changelist = [1]std.c.Kevent{.{
            .ident = @intCast(pipe_fds[0]),
            .filter = std.c.EVFILT.READ,
            .flags = std.c.EV.ADD | std.c.EV.CLEAR,
            .fflags = 0,
            .data = 0,
            .udata = 0,
        }};
        const rc = std.c.kevent(kq, &changelist, 1, @ptrCast(&changelist), 0, null);
        if (rc < 0) {
            _ = std.c.close(pipe_fds[0]);
            _ = std.c.close(pipe_fds[1]);
            _ = std.c.close(kq);
            return error.KeventFailed;
        }

        return .{
            .kq_or_epfd = kq,
            .ring = {},
            .notify_read_fd = pipe_fds[0],
            .notify_write_fd = pipe_fds[1],
            .events_buf = undefined,
            .fd_data = {},
            .fd_active = {},
            .fd_want_write = {},
            .fd_poll_rearm = {},
            .notify_rearm_pending = {},
            .use_uring = {},
            .epoll_events_buf = {},
            .spin_ns = 0, // kqueue path parks via kevent timeout, no CQ to peek
            .spin_hot = false,
            .spin_adaptive = true,
            .pending_enable = false,
        };
    }

    pub fn deinit(self: *EventLoop) void {
        if (is_linux) {
            if (self.use_uring) {
                self.ring.deinit();
            } else {
                _ = std.c.close(self.kq_or_epfd);
            }
            _ = std.c.close(self.notify_read_fd);
        } else {
            _ = std.c.close(self.notify_read_fd);
            _ = std.c.close(self.notify_write_fd);
            _ = std.c.close(self.kq_or_epfd);
        }
    }

    pub fn addFd(self: *EventLoop, fd: i32, data: usize) !void {
        return self.addFdInternal(fd, data, true);
    }

    /// Register an fd whose I/O is driven by recv/send SQEs rather than poll
    /// readiness. No poll_add is armed — a poll CQE would only duplicate the
    /// recv completion (plus a wasted read() -> EAGAIN per request). Write
    /// interest still arms a poll transiently via enableWrite.
    /// Falls back to normal poll registration on non-uring backends.
    pub fn addFdRecvMode(self: *EventLoop, fd: i32, data: usize) !void {
        return self.addFdInternal(fd, data, false);
    }

    fn addFdInternal(self: *EventLoop, fd: i32, data: usize, poll_mode: bool) !void {
        setNonBlocking(fd);

        if (is_linux) {
            const idx = fdIdx(fd) orelse return error.FdOutOfRange;
            self.fd_data[idx] = data;
            self.fd_active[idx] = true;
            self.fd_want_write[idx] = false;
            self.fd_poll_rearm[idx] = poll_mode;
            if (self.use_uring) {
                if (poll_mode) try self.submitPollAdd(fd, @as(u32, linux.POLL.IN));
            } else {
                var ev = linux.epoll_event{ .events = linux.EPOLL.IN | linux.EPOLL.ET, .data = .{ .fd = fd } };
                const rc = linux.epoll_ctl(self.kq_or_epfd, linux.EPOLL.CTL_ADD, fd, &ev);
                if (rc > std.math.maxInt(usize) / 2) return error.EpollCtlFailed;
            }
        } else {
            var changelist = [1]std.c.Kevent{.{
                .ident = @intCast(fd),
                .filter = std.c.EVFILT.READ,
                .flags = std.c.EV.ADD | std.c.EV.CLEAR,
                .fflags = 0,
                .data = 0,
                .udata = data,
            }};
            const rc = std.c.kevent(self.kq_or_epfd, &changelist, 1, @ptrCast(&changelist), 0, null);
            if (rc < 0) return error.KeventFailed;
        }
    }

    pub fn removeFd(self: *EventLoop, fd: i32) void {
        if (is_linux) {
            if (fdIdx(fd)) |idx| {
                self.fd_active[idx] = false;
                self.fd_data[idx] = 0;
                self.fd_want_write[idx] = false;
                self.fd_poll_rearm[idx] = false;
            }
            if (!self.use_uring) {
                _ = linux.epoll_ctl(self.kq_or_epfd, linux.EPOLL.CTL_DEL, fd, null);
            }
        } else {
            var changelist = [2]std.c.Kevent{
                .{ .ident = @intCast(fd), .filter = std.c.EVFILT.READ, .flags = std.c.EV.DELETE, .fflags = 0, .data = 0, .udata = 0 },
                .{ .ident = @intCast(fd), .filter = std.c.EVFILT.WRITE, .flags = std.c.EV.DELETE, .fflags = 0, .data = 0, .udata = 0 },
            };
            _ = std.c.kevent(self.kq_or_epfd, &changelist, 2, @ptrCast(&changelist), 0, null);
        }
    }

    pub fn enableWrite(self: *EventLoop, fd: i32, data: usize) !void {
        if (is_linux) {
            if (fdIdx(fd)) |idx| {
                self.fd_data[idx] = data;
                self.fd_want_write[idx] = true;
                // recv-mode fds have no poll_add in flight, so arm one now;
                // pollIoUring keeps re-arming it while fd_want_write holds.
                // OUT only: reads stay owned by the pending recv SQE.
                // Poll-mode fds pick up OUT interest on their next re-arm.
                if (self.use_uring and !self.fd_poll_rearm[idx]) {
                    try self.submitPollAdd(fd, @as(u32, linux.POLL.OUT));
                }
            }
            if (!self.use_uring) {
                var ev = linux.epoll_event{ .events = linux.EPOLL.IN | linux.EPOLL.OUT | linux.EPOLL.ET, .data = .{ .fd = fd } };
                _ = linux.epoll_ctl(self.kq_or_epfd, linux.EPOLL.CTL_MOD, fd, &ev);
            }
        } else {
            var changelist = [1]std.c.Kevent{.{
                .ident = @intCast(fd),
                .filter = std.c.EVFILT.WRITE,
                .flags = std.c.EV.ADD | std.c.EV.CLEAR,
                .fflags = 0,
                .data = 0,
                .udata = data,
            }};
            const rc = std.c.kevent(self.kq_or_epfd, &changelist, 1, @ptrCast(&changelist), 0, null);
            if (rc < 0) return error.KeventFailed;
        }
    }

    pub fn disableWrite(self: *EventLoop, fd: i32, data: usize) !void {
        if (is_linux) {
            if (fdIdx(fd)) |idx| {
                self.fd_data[idx] = data;
                self.fd_want_write[idx] = false;
            }
            if (!self.use_uring) {
                var ev = linux.epoll_event{ .events = linux.EPOLL.IN | linux.EPOLL.ET, .data = .{ .fd = fd } };
                _ = linux.epoll_ctl(self.kq_or_epfd, linux.EPOLL.CTL_MOD, fd, &ev);
            }
        } else {
            var changelist = [1]std.c.Kevent{.{
                .ident = @intCast(fd),
                .filter = std.c.EVFILT.WRITE,
                .flags = std.c.EV.DELETE,
                .fflags = 0,
                .data = 0,
                .udata = data,
            }};
            const rc = std.c.kevent(self.kq_or_epfd, &changelist, 1, @ptrCast(&changelist), 0, null);
            if (rc < 0) return error.KeventFailed;
        }
    }

    pub fn poll(self: *EventLoop, out: []Event, timeout_ms: i32) ![]Event {
        if (is_linux) {
            if (self.use_uring) {
                return self.pollIoUring(out, timeout_ms);
            } else {
                return self.pollEpoll(out, timeout_ms);
            }
        } else {
            return self.pollKqueue(out, timeout_ms);
        }
    }

    fn pollIoUring(self: *EventLoop, out: []Event, timeout_ms: i32) ![]Event {
        _ = timeout_ms; // io_uring submit_and_wait with wait_nr=1 blocks until at least 1 completion

        // A failed notify re-arm (SQ full) would otherwise permanently deafen
        // this worker to new-connection/pub-sub wakeups. Retry it here, where
        // the SQE rides the imminent submit_and_wait; poll_add is level-
        // checked at arm time, so a notify written in the meantime still fires.
        if (self.notify_rearm_pending) {
            self.notify_rearm_pending = false;
            self.submitPollAdd(self.notify_read_fd, @as(u32, linux.POLL.IN)) catch {
                self.notify_rearm_pending = true;
            };
        }

        const probe_on = probes_mod.isEnabled();
        const wait_t0: u64 = if (probe_on) probes_mod.start() else 0;

        // Harvest buffer. The copy MUST be capped at out.len: anything copied
        // out of the ring beyond what fits in `out` would be silently dropped
        // (a lost send CQE wedges send_pending; a lost recv CQE kills the
        // connection's recv loop). Capping leaves the excess in the CQ for the
        // next tick instead.
        var cqes: [MAX_EVENTS]linux.io_uring_cqe = undefined;
        const max_cqes: u32 = @intCast(@min(out.len, MAX_EVENTS));
        var n: u32 = 0;

        // Keep-warm path: when the previous tick woke quickly (load is arriving)
        // and a spin budget is configured, submit the pending SQEs WITHOUT
        // blocking, then spin-peek the (userspace, mmap'd) completion queue for
        // up to spin_ns. A request landing a few µs from now is then served on
        // this still-running core, with no kernel sleep/wake. When spin_ns == 0
        // this whole branch is skipped and the code below is byte-for-byte the
        // original park-immediately behavior.
        if (self.spin_ns != 0 and (self.spin_hot or !self.spin_adaptive)) {
            _ = self.ring.submit() catch return error.IoUringSubmitFailed;
            const deadline = monoNs() +% self.spin_ns;
            var spins: u32 = 0;
            while (true) {
                n = self.ring.copy_cqes(cqes[0..max_cqes], 0) catch return error.IoUringCqeFailed;
                if (n != 0) break;
                spins +%= 1;
                std.atomic.spinLoopHint();
                // Amortize the clock read across a batch of cheap CQ peeks.
                if ((spins & 0x3F) == 0 and monoNs() >= deadline) break;
            }
        }

        // Park path: nothing was ready (idle, cold, or the spin window expired).
        // submit_and_wait(1) submits any still-pending SQEs and sleeps until at
        // least one completion. Re-derive spin_hot from how long we actually
        // slept: a quick wake means traffic is flowing, so stay warm next tick;
        // a long sleep means we are idle, so park immediately next time.
        if (n == 0) {
            const sleep_t0: u64 = if (self.spin_ns != 0) monoNs() else 0;
            _ = self.ring.submit_and_wait(1) catch return error.IoUringSubmitFailed;
            n = self.ring.copy_cqes(cqes[0..max_cqes], 0) catch return error.IoUringCqeFailed;
            if (self.spin_ns != 0) self.spin_hot = (monoNs() -% sleep_t0) < self.spin_ns;
        } else {
            self.spin_hot = true;
        }

        if (probe_on) {
            if (probes_mod.current) |p| {
                const waited = probes_mod.sinceNs(wait_t0);
                p.wait_enter.record(waited);
                if (waited > WAIT_BLOCKED_NS) p.wait_blocked.record(waited);
                p.cqes_per_wake.record(n);
            }
        }

        var n_recv: u64 = 0;
        var n_send: u64 = 0;
        var n_poll: u64 = 0;

        var out_idx: usize = 0;
        for (cqes[0..n]) |cqe| {
            if (out_idx >= max_cqes) break;

            const op = decodeOp(cqe.user_data);
            const fd = decodeFd(cqe.user_data);
            const res = cqe.res;

            if (op == OP_RECV) {
                n_recv += 1;
                // recv completion — deliver bytes to worker
                const idx = fdIdx(fd) orelse continue;
                if (!self.fd_active[idx]) continue;
                out[out_idx] = .{
                    .fd = fd,
                    .data = self.fd_data[idx],
                    .readable = false,
                    .writable = false,
                    .err = res < 0,
                    .hup = res == 0,
                    .op = 1,
                    .bytes = res,
                };
                out_idx += 1;
            } else if (op == OP_AOF_WRITE) {
                // AOF write completion — ignore, wait for linked fsync
                continue;
            } else if (op == OP_AOF_FSYNC) {
                // AOF fsync completion — signal worker that flush is durable
                out[out_idx] = .{
                    .fd = fd,
                    .data = 0,
                    .readable = false,
                    .writable = false,
                    .err = res < 0,
                    .hup = false,
                    .op = 3, // AOF flush complete
                    .bytes = res,
                };
                out_idx += 1;
            } else if (op == OP_SEND) {
                n_send += 1;
                // send completion — deliver result to worker
                const idx = fdIdx(fd) orelse continue;
                if (!self.fd_active[idx]) continue;
                out[out_idx] = .{
                    .fd = fd,
                    .data = self.fd_data[idx],
                    .readable = false,
                    .writable = false,
                    .err = res < 0,
                    .hup = false,
                    .op = 2,
                    .bytes = res,
                };
                out_idx += 1;
            } else {
                // OP_POLL: existing poll_add completion logic
                n_poll += 1;

                if (fd == self.notify_read_fd) {
                    out[out_idx] = .{ .fd = fd, .data = 0, .readable = true, .writable = false, .err = false, .hup = false };
                    out_idx += 1;
                    self.submitPollAdd(fd, linux.POLL.IN) catch |err| {
                        vex_log.warn("event_loop: re-arm poll on notify fd={d} failed: {s} (will retry)", .{ fd, @errorName(err) });
                        self.notify_rearm_pending = true;
                    };
                    continue;
                }

                const idx = fdIdx(fd) orelse continue;
                if (!self.fd_active[idx]) continue;

                if (res < 0) {
                    out[out_idx] = .{ .fd = fd, .data = self.fd_data[idx], .readable = false, .writable = false, .err = true, .hup = false };
                    out_idx += 1;
                    continue;
                }

                const revents: u32 = @intCast(res);
                out[out_idx] = .{
                    .fd = fd,
                    .data = self.fd_data[idx],
                    .readable = (revents & linux.POLL.IN) != 0,
                    .writable = (revents & linux.POLL.OUT) != 0,
                    .err = (revents & linux.POLL.ERR) != 0,
                    .hup = (revents & linux.POLL.HUP) != 0,
                };
                out_idx += 1;

                // Re-arm the poll for this fd (io_uring poll_add is one-shot).
                // recv-mode fds (fd_poll_rearm=false) only keep a poll alive
                // while write interest is registered (send EAGAIN fallback).
                if (self.fd_active[idx] and (self.fd_poll_rearm[idx] or self.fd_want_write[idx])) {
                    const poll_in: u32 = linux.POLL.IN;
                    const poll_out: u32 = linux.POLL.OUT;
                    const poll_mask: u32 = if (!self.fd_poll_rearm[idx])
                        poll_out // recv-mode: write interest only; recv SQEs own reads
                    else if (self.fd_want_write[idx])
                        poll_in | poll_out
                    else
                        poll_in;
                    self.submitPollAdd(fd, poll_mask) catch |err| {
                        vex_log.warn("event_loop: re-arm poll on fd={d} failed: {s}", .{ fd, @errorName(err) });
                    };
                }
            }
        }

        // SQEs queued during processing (sends, recv re-arms, poll re-arms)
        // are NOT flushed here — the submit_and_wait(1) at the top of the
        // next tick submits them in the same enter it waits with, keeping
        // the hot path at one syscall per wakeup.

        if (probe_on) {
            if (probes_mod.current) |p| {
                p.cqe_recv.record(n_recv);
                p.cqe_send.record(n_send);
                p.cqe_poll.record(n_poll);
            }
        }

        return out[0..out_idx];
    }

    fn pollEpoll(self: *EventLoop, out: []Event, timeout_ms: i32) ![]Event {
        const max: u32 = @intCast(@min(out.len, MAX_EVENTS));
        const rc = linux.epoll_wait(self.kq_or_epfd, &self.epoll_events_buf, max, timeout_ms);
        if (rc > std.math.maxInt(usize) / 2) return error.EpollWaitFailed;
        const n: usize = rc;
        for (0..n) |i| {
            const ev = self.epoll_events_buf[i];
            const fd = ev.data.fd;
            const idx = fdIdx(fd);
            const udata: usize = if (idx) |j| self.fd_data[j] else 0;
            out[i] = .{
                .fd = fd,
                .data = udata,
                .readable = (ev.events & linux.EPOLL.IN) != 0,
                .writable = (ev.events & linux.EPOLL.OUT) != 0,
                .err = (ev.events & linux.EPOLL.ERR) != 0,
                .hup = (ev.events & linux.EPOLL.HUP) != 0,
            };
        }
        return out[0..n];
    }

    fn pollKqueue(self: *EventLoop, out: []Event, timeout_ms: i32) ![]Event {
        const max: u32 = @intCast(@min(out.len, MAX_EVENTS));
        var ts: std.c.timespec = undefined;
        var ts_ptr: ?*const std.c.timespec = null;
        if (timeout_ms >= 0) {
            ts = .{
                .sec = @intCast(@divTrunc(timeout_ms, 1000)),
                .nsec = @intCast(@rem(timeout_ms, 1000) * 1_000_000),
            };
            ts_ptr = &ts;
        }
        const changelist_ptr: [*]const std.c.Kevent = @ptrCast(&self.events_buf);
        const eventlist_ptr: [*]std.c.Kevent = @ptrCast(&self.events_buf);
        const rc = std.c.kevent(self.kq_or_epfd, changelist_ptr, 0, eventlist_ptr, @intCast(max), ts_ptr);
        if (rc < 0) return error.KeventFailed;
        const n: usize = @intCast(rc);
        for (0..n) |i| {
            const ev = self.events_buf[i];
            out[i] = .{
                .fd = @intCast(ev.ident),
                .data = ev.udata,
                .readable = ev.filter == std.c.EVFILT.READ,
                .writable = ev.filter == std.c.EVFILT.WRITE,
                .err = (ev.flags & std.c.EV.ERROR) != 0,
                .hup = (ev.flags & std.c.EV.EOF) != 0,
            };
        }
        return out[0..n];
    }

    pub fn notify(self: *EventLoop) void {
        if (is_linux) {
            const val: u64 = 1;
            const buf: *const [8]u8 = @ptrCast(&val);
            _ = linux.write(@intCast(self.notify_write_fd), buf, 8);
        } else {
            const byte = [1]u8{1};
            _ = std.c.write(self.notify_write_fd, &byte, 1);
        }
    }

    pub fn isNotifyFd(self: *EventLoop, fd: i32) bool {
        return fd == self.notify_read_fd;
    }

    pub fn drainNotify(self: *EventLoop) void {
        if (is_linux) {
            var buf: [8]u8 = undefined;
            _ = linux.read(@intCast(self.notify_read_fd), &buf, 8);
        } else {
            var buf: [64]u8 = undefined;
            while (true) {
                const rc = std.c.read(self.notify_read_fd, &buf, buf.len);
                if (rc <= 0) break;
            }
        }
    }

    // --- io_uring helpers ---

    fn submitPollAdd(self: *EventLoop, fd: i32, poll_mask: u32) !void {
        _ = try self.ring.poll_add(encodeUserData(OP_POLL, fd), fd, poll_mask);
    }

    /// Submit a recv SQE. Buffer must remain valid until CQE.
    pub fn submitRecv(self: *EventLoop, fd: i32, buf: []u8) !void {
        _ = try self.ring.recv(encodeUserData(OP_RECV, fd), fd, .{ .buffer = buf }, 0);
    }

    /// Submit a send SQE. Buffer must remain valid until CQE.
    pub fn submitSend(self: *EventLoop, fd: i32, buf: []const u8) !void {
        _ = try self.ring.send(encodeUserData(OP_SEND, fd), fd, buf, 0);
    }

    /// Submit a write SQE linked to fsync for AOF durability.
    /// The write and fsync are chained: fsync executes only after write completes.
    pub fn submitAofWriteFsync(self: *EventLoop, fd: i32, buf: []const u8, offset: u64) !void {
        // Write SQE — linked to next SQE (fsync)
        const write_sqe = try self.ring.write(encodeUserData(OP_AOF_WRITE, fd), fd, buf, offset);
        write_sqe.flags |= linux.IOSQE_IO_LINK;
        // Fsync SQE — executes after write completes
        _ = try self.ring.fsync(encodeUserData(OP_AOF_FSYNC, fd), fd, 0);
    }

    /// Flush any queued SQEs to the kernel (non-blocking).
    pub fn flushSqes(self: *EventLoop) void {
        if (is_linux and self.use_uring) {
            const probe_on = probes_mod.isEnabled();
            const t0: u64 = if (probe_on) probes_mod.start() else 0;
            _ = self.ring.submit() catch |err| {
                vex_log.warn("event_loop: flushSqes ring.submit failed: {s}", .{@errorName(err)});
            };
            if (probe_on) {
                if (probes_mod.current) |p| probes_mod.finish(&p.flush_enter, t0);
            }
        }
    }

    fn fdIdx(fd: i32) ?usize {
        if (fd >= 0 and @as(usize, @intCast(fd)) < FD_TABLE_SIZE) {
            return @intCast(fd);
        }
        return null;
    }
};

fn setNonBlocking(fd: i32) void {
    const flags = std.c.fcntl(fd, std.c.F.GETFL);
    if (flags < 0) return;
    const o_nonblock: c_int = if (is_linux) 0o4000 else 0x0004;
    _ = std.c.fcntl(fd, std.c.F.SETFL, @as(c_int, flags) | o_nonblock);
}

// c_int is a builtin type, no need to redefine

