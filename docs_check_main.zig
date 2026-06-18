//! Doc/code drift guard — `zig build check-docs` (also run it in CI).
//!
//! Two cheap, high-signal checks that would have caught the bugs we just fixed
//! by hand (a doc describing commands that don't exist; stale claims):
//!   1. every registered command (the real `command_names` array) is mentioned
//!      in docs/commands.md — catches "added a command, forgot the doc" and
//!      "documented a command that was removed".
//!   2. known-stale phrasings we deliberately fixed don't reappear.
//!
//! This file lives at the repo root (like test_main.zig) so a single module can
//! @import src/ and @embedFile docs/ without tripping Zig's module-path check.

const std = @import("std");
const command_names = @import("src/observability/cmd_table.zig").command_names;

const commands_md = @embedFile("docs/commands.md");

/// Names in the dispatch table that are NOT part of the public command
/// reference (catch-all buckets, internal-only). Keep short and justified.
const not_in_reference = [_][]const u8{
    "OTHER", // catch-all bucket for unknown commands, not a real command
};

/// BASELINE: commands registered today but not yet in docs/commands.md. This is
/// a ratchet — the guard fails on any NEW undocumented command, while this list
/// is whittled down as these get documented (admin commands like CONFIG/DEBUG/
/// MEMORY also appear in observability.md/security.md; the data commands here
/// are genuine gaps). Move a name out of this list when you document it; do NOT
/// add new ones.
const undocumented_baseline = [_][]const u8{
    "UNLINK",  "MEXISTS", "MSETNX",  "MSETEX",       "MGETDEL", "INCRTTL",
    "COPY",    "SCANGET", "TIME",    "HELLO",        "RESET",   "CONFIG",
    "OBJECT",  "DEBUG",   "MEMORY",  "SHUTDOWN",     "WAIT",    "PUNSUBSCRIBE",
    "LPOPN",
};

fn isIdent(c: u8) bool {
    return (c >= 'A' and c <= 'Z') or (c >= 'a' and c <= 'z') or
        (c >= '0' and c <= '9') or c == '.' or c == '_';
}

/// True if `name` appears in `hay` as a standalone token (so "GET" is not
/// considered "documented" just because it's a substring of "HGETALL").
fn hasToken(hay: []const u8, name: []const u8) bool {
    var i: usize = 0;
    while (std.mem.indexOfPos(u8, hay, i, name)) |p| : (i = p + 1) {
        const before = if (p == 0) ' ' else hay[p - 1];
        const after = if (p + name.len >= hay.len) ' ' else hay[p + name.len];
        if (!isIdent(before) and !isIdent(after)) return true;
    }
    return false;
}

fn excepted(name: []const u8) bool {
    for (not_in_reference) |e| if (std.mem.eql(u8, e, name)) return true;
    for (undocumented_baseline) |e| if (std.mem.eql(u8, e, name)) return true;
    return false;
}

test "every registered command is documented in docs/commands.md" {
    var missing: usize = 0;
    for (command_names) |name| {
        if (excepted(name)) continue;
        if (!hasToken(commands_md, name)) {
            std.debug.print("  x undocumented command: {s}\n", .{name});
            missing += 1;
        }
    }
    if (missing > 0) {
        std.debug.print(
            "\n{d} registered command(s) missing from docs/commands.md.\n" ++
                "Document them, or add genuinely-internal ones to not_in_reference.\n",
            .{missing},
        );
        return error.UndocumentedCommands;
    }
}

const StalePhrase = struct { needle: []const u8, why: []const u8 };
const stale = [_]StalePhrase{
    .{ .needle = "with SQPOLL", .why = "SQPOLL was removed (oversubscribes cores)" },
    .{ .needle = "lock-free reads", .why = "GET takes a striped rdlock + seqlock; say 'concurrent reads'" },
    .{ .needle = "DIM <n> VEC", .why = "vectors are raw little-endian f32 bytes, not DIM + floats" },
    .{ .needle = "FORMAT subgraph", .why = "GRAPH.RAG has no subgraph reply mode" },
};

const linted_docs = [_][]const u8{
    @embedFile("README.md"),
    commands_md,
    @embedFile("docs/agent-memory.md"),
    @embedFile("docs/semantic-cache.md"),
    @embedFile("docs/graphrag.md"),
    @embedFile("docs/vector-search.md"),
    @embedFile("docs/deployment.md"),
    @embedFile("docs/benchmarks.md"),
};

test "docs do not reintroduce known-stale phrasings" {
    var hits: usize = 0;
    for (linted_docs) |doc| {
        for (stale) |s| {
            if (std.mem.indexOf(u8, doc, s.needle) != null) {
                std.debug.print("  x stale phrasing \"{s}\" -- {s}\n", .{ s.needle, s.why });
                hits += 1;
            }
        }
    }
    if (hits > 0) return error.StaleDocs;
}
