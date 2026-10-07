#!/usr/bin/env python3
"""Compile the patched helper alone; no server, Docker, or clock changes.

Usage: python3 check_memtier_monotonic_clock.py /path/to/patched/memtier/source
"""
import json
import pathlib
import subprocess
import sys
import tempfile

source = pathlib.Path(sys.argv[1]).resolve()
mock_header = '''#include <time.h>
int fake_clock_gettime(clockid_t, struct timespec *);
#define clock_gettime fake_clock_gettime
#include "monotonic_clock.h"
#undef clock_gettime
'''
main = mock_header + '''
#include <assert.h>
#include <errno.h>
#include <string.h>
static int calls = 0, real_calls = 0, mono_calls = 0, fail_at = 0;
static int64_t realtime = 1700000000000000LL;
int fake_clock_gettime(clockid_t clock, struct timespec *ts) {
    if (++calls == fail_at) { errno = EIO; return -1; }
    int64_t us;
    if (clock == CLOCK_REALTIME) { ++real_calls; us = realtime; }
    else {
        assert(clock == CLOCK_MONOTONIC);
        const int64_t values[] = {10999900, 11000000, 11000219};
        assert(mono_calls < 3);
        us = values[mono_calls++];
    }
    ts->tv_sec = us / 1000000;
    ts->tv_nsec = (us % 1000000) * 1000;
    return 0;
}
void other_translation_unit(struct timeval *);
int main(int argc, char **argv) {
    if (argc > 1) fail_at = atoi(argv[1]);
    const int64_t offset = memtier_clock_epoch_offset_us();
    assert(offset == 1700000000000000LL - 10999900);
    struct timeval a, b;
    memtier_gettimeofday(&a);
    realtime -= 219; // The wall clock reverses; elapsed time must not.
    other_translation_unit(&b);
    assert(real_calls == 1 && mono_calls == 3);
    assert(a.tv_sec == 1700000000 && a.tv_usec == 100);
    assert(b.tv_sec == 1700000000 && b.tv_usec == 319);
    assert((b.tv_sec - a.tv_sec) * 1000000 + b.tv_usec - a.tv_usec == 219);
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix="memtier-clock-check-") as name:
    tmp = pathlib.Path(name)
    (tmp / "main.cpp").write_text(main)
    (tmp / "other.cpp").write_text(mock_header + "void other_translation_unit(struct timeval *tv) { memtier_gettimeofday(tv); }\n")
    binary = tmp / "check"
    subprocess.run(["c++", "-std=c++11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(source), str(tmp / "main.cpp"), str(tmp / "other.cpp"), "-o", str(binary)], check=True)
    result = subprocess.run([str(binary)], capture_output=True, text=True, check=True)
    lines = result.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("MEMTIER_CLOCK "), lines
    metadata = json.loads(lines[0].removeprefix("MEMTIER_CLOCK "))
    assert metadata == {"clock_source": "CLOCK_MONOTONIC", "epoch_offset_us": 1699999989000100, "source_tag": "2.5.1", "source_commit": "5f634d171b83efca9640c5a87606c47b34d3d330"}, metadata
    for failed_call in (1, 2, 3):
        failure = subprocess.run([str(binary), str(failed_call)], capture_output=True, text=True, cwd=tmp)
        assert failure.returncode != 0 and "memtier clock_gettime" in failure.stderr, failure
print("PASS: backward realtime, monotonic delta, timeval normalization, shared two-TU offset, one metadata line, fail-closed clock errors")
