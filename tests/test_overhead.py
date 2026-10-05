"""With no breakpoints set, steady work must run within 10% of its normal speed.

Interpreter startup and initial extension loading are excluded from the work timer.
"""
import os
import re
import subprocess

import pytest

from conftest import target

BENCH = target("bench.py")
RUNS = int(os.environ.get("SEAM_BENCH_RUNS", "5"))


def plain(python, mode, env):
    out = subprocess.run([python, BENCH, mode], capture_output=True, text=True, check=True,
                         env=dict(os.environ, **env)).stdout
    return float(re.search(r"elapsed ([\d.]+)", out).group(1))


def under_seam(make_client, python, mode, env):
    dap = make_client()
    try:
        dap.launch(BENCH, python, args=[mode], env=env)
        assert dap.wait_exit(timeout=120) == 0
        return float(re.search(r"elapsed ([\d.]+)", dap.output).group(1))
    finally:
        dap.close()


def measure(make_client, python, mode, env, limit=None):
    # Interleave the two configurations and take the best of each: the minimum is the
    # least noisy estimate of the cost of the work itself. Noise only ever adds time, so
    # when a busy machine pushes the ratio over `limit`, more runs can only move both
    # minimums towards the truth: measure up to two more rounds before believing it.
    base, seam = [], []
    for _ in range(3):
        for _ in range(RUNS):
            base.append(plain(python, mode, env))
            seam.append(under_seam(make_client, python, mode, env))
        if limit is None or min(seam) / min(base) < limit:
            break
    return min(base), min(seam)


@pytest.mark.parametrize("mode", ["cpu", "native"])
def test_overhead_without_breakpoints_is_under_ten_percent(make_client, python, capi, mode,
                                                           record_property):
    base, seam = measure(make_client, python, mode, capi.env, limit=1.10)
    ratio = seam / base
    record_property("ratio", ratio)
    print("\noverhead[%s]: plain %.3fs, under Seam %.3fs, ratio %.3f" % (mode, base, seam, ratio))
    assert ratio < 1.10, "Seam slowed the %s workload by %.1f%%" % (mode, (ratio - 1) * 100)


def test_thread_heavy_overhead_is_reported(make_client, python, capi):
    """Thread creation is where a ptrace-based debugger costs something: measured and
    printed, with only a loose bound, because LLDB must handle every thread start/exit."""
    base, seam = measure(make_client, python, "threads", capi.env)
    ratio = seam / base
    print("\noverhead[threads]: plain %.3fs, under Seam %.3fs, ratio %.2f" % (base, seam, ratio))
    assert ratio < 50
