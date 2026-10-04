"""The debugged program starts child processes. Children are not debugged (v1); they must
run as if no debugger were there, and the parent's session must stay healthy.

LLDB's handling of fork differs between versions, so every scenario makes the program
print what became of each child, and a failing assertion shows that output.
"""
import os
import signal
import subprocess
import time

import pytest

from conftest import CAPI_SRC, marker_line, pid_alive, target
from dapclient import Terminal
from test_python import py_frames

pytestmark = pytest.mark.smoke

CHILDREN = target("children.py")


def run_to_exit(dap, limit=60):
    """Continue through every stop. Returns the stops as (reason, function, line) and the
    program's exit code."""
    stops = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"], timeout=90)
        if name == "exited":
            return stops, body["exitCode"]
        frame = dap.stack(body["threadId"])[0]
        stops.append((body["reason"], frame["name"], frame["line"]))
        assert len(stops) <= limit, "too many stops: %s\n%s" % (stops, dap.output)
        dap.cont()


def all_breakpoints(dap, extra_functions=()):
    """A Python line, a native line and a native function breakpoint on the path `work`
    takes. Returns the stops one call of `work` makes in the debugged process."""
    python_line = marker_line(CHILDREN, "work-add")
    dap.set_breakpoints(CHILDREN, [python_line])
    dap.set_breakpoints(CAPI_SRC, [marker_line(CAPI_SRC, "add-impl-return")])
    dap.request("setFunctionBreakpoints", {"breakpoints": [
        {"name": name} for name in ("st_add",) + tuple(extra_functions)]})
    return [("breakpoint", "work", python_line), ("breakpoint", "st_add"),
            ("breakpoint", "add_impl")]


def same_stops(stops, expected):
    """Compare stops with what is expected (native ones by function: lines move at -O2)."""
    return len(stops) == len(expected) and all(
        got[:len(want)] == want for got, want in zip(stops, expected))


def launch(dap, capi, *args, **extra):
    dap.launch(CHILDREN, dap.python, args=list(args), env=capi.env, stopOnEntry=True, **extra)
    return dap.wait_stopped()["threadId"]


def test_fork(dap, capi):
    launch(dap, capi, "fork")
    per_call = all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    # The parent stops at its own breakpoints before and after the fork; the child runs
    # the same lines and the same native function without stopping, and without dying.
    assert "fork child: exit 7" in dap.output, dap.output
    assert same_stops(stops, per_call * 2), (stops, dap.output)
    assert code == 0
    # In the child the helper has gone dormant; in the parent it is as before.
    assert "fork child: work 12, helper None" in dap.output, dap.output
    assert "parent: work 4 6, helper seam" in dap.output


def test_fork_made_by_native_code(dap, capi):
    launch(dap, capi, "native-fork")
    per_call = all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    assert "native fork child: exit 7" in dap.output, dap.output
    assert same_stops(stops, per_call * 2), (stops, dap.output)
    assert code == 0 and "parent: work 4 6" in dap.output


def test_subprocess_system_and_posix_spawn(dap, capi):
    launch(dap, capi, "spawn")
    # `execve` is what the new child itself calls, in memory it shares with the parent
    # (vfork): a breakpoint there must neither stop the parent nor kill the child.
    per_call = all_breakpoints(dap, extra_functions=("execve",))
    dap.cont()
    stops, code = run_to_exit(dap)
    out = dap.output
    for line in ("subprocess python: exit 5", "subprocess true: exit 0",
                 "subprocess ls: exit 0 True", "system: exit 3", "posix_spawn: exit 0",
                 "popen: exit 0 PIPED", "parent: work 4 6"):
        assert line in out, out
    # A child that is itself Python starts as if Seam were not there.
    assert "python child: helper None, modules [], env [], path []" in out, out
    assert same_stops(stops, per_call * 2), (stops, out)
    assert code == 0


@pytest.mark.parametrize("method", ["fork", "forkserver", "spawn"])
def test_multiprocessing(dap, capi, method):
    launch(dap, capi, "pool", method)
    per_call = all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    out = dap.output
    assert "pool %s: [2, 4, 6, 8, 10, 12]" % method in out, out
    assert "process %s: exit 0" % method in out, out
    assert same_stops(stops, per_call * 2), (stops, out)
    assert code == 0 and "parent: work 4 6" in out


def test_process_pool_executor(dap, capi):
    launch(dap, capi, "executor")
    per_call = all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    assert "executor: [2, 4, 6, 8, 10, 12]" in dap.output, dap.output
    assert same_stops(stops, per_call * 2), (stops, dap.output)
    assert code == 0


def test_fork_while_other_threads_run(dap, capi):
    launch(dap, capi, "threads")
    # Breakpoints the children run over, and the other threads keep hitting LLDB's
    # machinery with a native logpoint while the main thread forks.
    dap.set_breakpoints(CHILDREN, [marker_line(CHILDREN, "work-add"),
                                   marker_line(CHILDREN, "threads-stop")])
    dap.set_breakpoints(CAPI_SRC, [{"line": marker_line(CAPI_SRC, "add-impl-return"),
                                    "condition": "a == 5"}])
    dap.cont()
    stop = dap.wait_stopped(60)
    out = dap.output
    for round_number in range(3):
        assert "threads %d child: work 12" % round_number in out, out
        assert "threads %d child: exit 7" % round_number in out, out
        assert "threads %d subprocess: exit 0" % round_number in out, out
    frame = dap.stack(stop["threadId"])[0]
    assert (frame["name"], frame["line"]) == ("fork_with_threads",
                                              marker_line(CHILDREN, "threads-stop"))
    assert len(dap.request("threads")["threads"]) == 4
    dap.set_breakpoints(CHILDREN, [])
    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert "parent: threads counted True" in dap.output


def test_step_over_lines_that_start_children(dap, capi):
    tid = launch(dap, capi, "step")
    dap.set_breakpoints(CHILDREN, [marker_line(CHILDREN, "step-fork")])
    dap.cont()
    dap.wait_stopped()
    dap.set_breakpoints(CHILDREN, [])
    for marker in ("step-reap", "step-subprocess", "step-system", "step-print"):
        stop = dap.step("next", tid)
        assert stop["reason"] == "step", (stop, dap.output)
        assert py_frames(dap.stack(tid))[0] == ("stepping", marker_line(CHILDREN, marker)), \
            dap.output
    assert "step child: work 12" in dap.output, dap.output
    assert "step child: exit 7" in dap.output, dap.output
    dap.cont()
    assert dap.wait_exit() == 0
    assert "parent: stepped exit 0 exit 3" in dap.output
