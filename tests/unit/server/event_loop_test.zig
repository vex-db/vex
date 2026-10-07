// Migrated unit tests for src/server/event_loop.zig.

const std = @import("std");
const EventLoop = @import("../../../src/server/event_loop.zig").EventLoop;

test "pipe read triggers readable event" {
    var el = try EventLoop.init();
    defer el.deinit();

    var pipe_fds: [2]std.c.fd_t = undefined;
    const pipe_rc = std.c.pipe(&pipe_fds);
    if (pipe_rc != 0) return error.PipeFailed;
    defer {
        _ = std.c.close(pipe_fds[0]);
        _ = std.c.close(pipe_fds[1]);
    }

    const read_fd = pipe_fds[0];
    const write_fd = pipe_fds[1];

    try el.addFd(read_fd, @intCast(read_fd));

    const byte = [1]u8{'x'};
    const wrc = std.c.write(write_fd, &byte, 1);
    try std.testing.expect(wrc == 1);

    var events: [16]EventLoop.Event = undefined;
    const ready = try el.poll(&events, 100);

    try std.testing.expect(ready.len >= 1);

    var found = false;
    for (ready) |ev| {
        if (ev.fd == read_fd) {
            try std.testing.expect(ev.readable);
            found = true;
            break;
        }
    }
    try std.testing.expect(found);
}

test "notify wakes poll" {
    var el = try EventLoop.init();
    defer el.deinit();

    el.notify();

    var events: [16]EventLoop.Event = undefined;
    const ready = try el.poll(&events, 100);

    try std.testing.expect(ready.len >= 1);

    var found = false;
    for (ready) |ev| {
        if (el.isNotifyFd(ev.fd)) {
            try std.testing.expect(ev.readable);
            found = true;
            break;
        }
    }
    try std.testing.expect(found);

    el.drainNotify();
}

test "high descriptor delivers events and survives removal and reuse" {
    if (@import("builtin").os.tag != .linux) return;
    var el = try EventLoop.init();
    defer el.deinit();
    var pipes: [2]std.c.fd_t = undefined;
    if (std.c.pipe(&pipes) != 0) return error.PipeFailed;
    defer _ = std.c.close(pipes[0]);
    defer _ = std.c.close(pipes[1]);
    const high = std.c.fcntl(pipes[0], std.c.F.DUPFD, @as(c_int, 8192));
    if (high < 0) return error.HighDescriptorUnavailable;
    defer _ = std.c.close(high);
    try el.addFd(high, 42);
    try el.enableWrite(high, 42);
    try el.disableWrite(high, 42);
    el.removeFd(high);
    try el.addFd(high, 99);
    const byte = [1]u8{'x'};
    try std.testing.expect(std.c.write(pipes[1], &byte, 1) == 1);
    var events: [16]EventLoop.Event = undefined;
    var found = false;
    for (try el.poll(&events, 100)) |event| {
        if (event.fd == high and event.readable) {
            try std.testing.expectEqual(@as(usize, 99), event.data);
            found = true;
        }
    }
    try std.testing.expect(found);
    el.removeFd(high);
}
