"""Entry traps: the step-in breakpoints Seam places itself in large modules.

By default only modules with thousands of functions get them (tests/test_scale.py, which
is opt-in, measures one). Here they are switched on for every module, so the small test
extensions exercise the same machinery: SEAM_ENTRY_TRAPS=0.
"""
import sys

import pytest

from conftest import CAPI_SRC, at_line, marker_line, target
from test_mixed import names
from test_stepping import assert_nothing_armed, top

pytestmark = pytest.mark.smoke

BINDING = target("binding.py")
SCALE = target("scale.py")
TRAPS = target("traps.py")


@pytest.fixture(autouse=True)
def entry_traps_for_every_module(monkeypatch):
    monkeypatch.setenv("SEAM_ENTRY_TRAPS", "0")


def used_traps(dap, module):
    """True if the session placed entry traps in this module (rather than LLDB breakpoints)."""
    return any(module in path for path in dap.status()["entryTrapModules"])


def log_of(dap):
    with open(dap.log_path, errors="replace") as fh:
        return fh.read()


def test_step_in_through_every_binding_layer(dap, binding, iteration):
    call = marker_line(BINDING, "bind-add")
    dap.launch(BINDING, dap.python, args=[binding.module], env=binding.env,
               breakpoints={BINDING: [call]})
    tid = dap.wait_stopped()["threadId"]
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step" and stop["threadId"] == tid
    stack = dap.stack(tid)
    assert stack[0].get("source", {}).get("path") == binding.source, stack
    assert "add" in stack[0]["name"], stack
    if binding.opt == "O0":
        assert [f["name"] for f in stack[1:]] == ["main", "<module>"], stack
    assert used_traps(dap, binding.module) == sys.platform.startswith("linux")
    assert_nothing_armed(dap)

    dap.step("stepOut", tid)
    assert top(dap, tid) == ("main", call)
    dap.step("next", tid)
    assert top(dap, tid) == ("main", marker_line(BINDING, "bind-after"))
    dap.set_breakpoints(BINDING, [])
    assert_nothing_armed(dap)
    dap.cont()
    assert dap.wait_exit() == 0
    assert "sum 42" in dap.output and "back 6" in dap.output


def test_step_in_that_stays_in_python_leaves_no_trap_behind(dap, capi, iteration):
    line = marker_line(SCALE, "python-call")
    dap.launch(SCALE, dap.python, args=["seamtest", "add"], env=capi.env,
               breakpoints={SCALE: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.step("stepIn", tid)
    assert top(dap, tid) == ("helper", marker_line(SCALE, "helper-body"))
    assert used_traps(dap, "seamtest") == sys.platform.startswith("linux")
    assert_nothing_armed(dap)
    dap.set_breakpoints(SCALE, [])
    # The native functions that had traps on them run normally afterwards.
    dap.cont()
    assert dap.wait_exit() == 0
    assert "total 60" in dap.output


def test_a_breakpoint_of_the_users_on_the_same_function(dap, capi, iteration):
    """LLDB's breakpoint site and the trap want the same address: neither disturbs the other."""
    first = marker_line(SCALE, "first-call")
    dap.launch(SCALE, dap.python, args=["seamtest", "add"], env=capi.env,
               breakpoints={SCALE: [first]})
    tid = dap.wait_stopped()["threadId"]
    dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "st_add"}]})
    stop = dap.step("stepIn", tid)
    assert stop["reason"] in ("step", "breakpoint")
    assert top(dap, tid)[0] == "st_add"
    assert_nothing_armed(dap)
    dap.step("stepOut", tid)
    assert top(dap, tid) == ("main", first)

    # Without the user's breakpoint the same function is reached through the trap, and
    # with it back the function still stops there, once per call.
    dap.request("setFunctionBreakpoints", {"breakpoints": []})
    dap.step("next", tid)
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step" and top(dap, tid)[0] == "st_add"
    assert at_line(capi, top(dap, tid)[1], marker_line(CAPI_SRC, "add-first"))
    dap.step("stepOut", tid)
    assert top(dap, tid) == ("main", marker_line(SCALE, "second-call"))
    dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "st_add"}]})
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint" and top(dap, tid)[0] == "st_add"
    assert names(dap.stack(tid))[1] == "main"
    assert dap.stack(tid)[1]["line"] == marker_line(SCALE, "third-call")
    dap.request("setFunctionBreakpoints", {"breakpoints": []})
    assert_nothing_armed(dap)
    dap.cont()
    assert dap.wait_exit() == 0
    assert "total 60" in dap.output


def test_other_threads_running_into_the_traps(dap, capi, iteration):
    """Threads other than the stepping one call the trapped functions all the time: they
    carry on undisturbed, and the step still ends in the function on its own thread."""
    line = marker_line(TRAPS, "threads-call")
    dap.launch(TRAPS, dap.python, args=["threads"], env=capi.env, breakpoints={TRAPS: [line]})
    tid = dap.wait_stopped()["threadId"]
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step" and stop["threadId"] == tid, stop
    stack = dap.stack(tid)
    assert names(stack)[:2] == ["st_add", "threads"], stack
    if sys.platform.startswith("linux"):
        assert "(not the stepping thread)" in log_of(dap)
    assert_nothing_armed(dap)
    assert dap.status()["leftoverStops"] == 0
    dap.step("stepOut", tid)
    assert top(dap, tid) == ("threads", line)
    dap.set_breakpoints(TRAPS, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert "value 42 workers [True, True]" in dap.output


def test_a_child_forked_during_a_step_in_is_not_left_with_traps(dap, capi, iteration):
    line = marker_line(TRAPS, "fork-call")
    dap.launch(TRAPS, dap.python, args=["fork"], env=capi.env, breakpoints={TRAPS: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.step("stepIn", tid)  # os.fork() is not user code: the step ends on the next line
    assert top(dap, tid) == ("fork", line + 1)
    assert used_traps(dap, "seamtest") == sys.platform.startswith("linux")
    dap.set_breakpoints(TRAPS, [])
    dap.cont()
    assert dap.wait_exit() == 0
    # The child called the native function and exited normally (a trap would kill it).
    assert "child status 0" in dap.output and "parent sum 5" in dap.output


def test_a_breakpoint_set_while_a_step_in_is_in_flight(dap, capi, iteration):
    """Setting a native breakpoint interrupts the program; the traps must not be in the
    way of LLDB while it does, and must be back afterwards."""
    line = marker_line(TRAPS, "sleep-call")
    dap.launch(TRAPS, dap.python, args=["sleep"], env=capi.env, breakpoints={TRAPS: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.send("stepIn", {"threadId": tid})
    native = marker_line(CAPI_SRC, "add-impl-return")
    answer = dap.set_breakpoints(CAPI_SRC, [native])
    assert answer[0]["verified"], answer
    stop = dap.wait_stopped()
    assert stop["reason"] == "step"
    assert top(dap, tid) == ("sleep", marker_line(TRAPS, "sleep-after"))
    assert_nothing_armed(dap)

    # The step is repeated on the next line, which does call native code.
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step" and top(dap, tid)[0] == "st_add"
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint" and top(dap, tid)[0] == "add_impl"
    dap.set_breakpoints(CAPI_SRC, [])
    dap.set_breakpoints(TRAPS, [])
    assert_nothing_armed(dap)
    dap.cont()
    assert dap.wait_exit() == 0
    assert "total 3" in dap.output
