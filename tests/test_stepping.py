"""Stepping across the Python/native boundary, in every direction."""
import pytest

from conftest import CAPI_SRC, at_line, marker_line, target
from test_mixed import names

pytestmark = pytest.mark.smoke

MIXED = target("mixed.py")
RUNNING = target("running.py")


def top(dap, tid):
    frame = dap.stack(tid)[0]
    return frame["name"], frame["line"]


def assert_nothing_armed(dap):
    status = dap.status()
    assert status["stepInBreakpointsEnabled"] is False
    assert status["nativeStepInProgress"] is False
    assert status["pythonStepArmed"] is False
    if status["safe"]:
        assert status["agent"]["stepping"] is None
    return status


def test_step_in_from_python_to_native_and_back_out(dap, capi, iteration):
    call = marker_line(MIXED, "leaf-add")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={MIXED: [call]})
    tid = dap.wait_stopped()["threadId"]
    assert top(dap, tid) == ("leaf", call)

    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert names(stack)[:2] == ["st_add", "leaf"]
    assert stack[0]["source"]["path"] == CAPI_SRC
    assert at_line(capi, stack[0]["line"], marker_line(CAPI_SRC, "add-first"))
    assert_nothing_armed(dap)

    # Step out of native code: back on the Python line that made the call.
    stop = dap.step("stepOut", tid)
    assert stop["reason"] == "step"
    assert top(dap, tid) == ("leaf", call)
    assert_nothing_armed(dap)
    dap.step("next", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == ("leaf", marker_line(MIXED, "leaf-return"))
    assert dap.scope(stack[0]["id"])["doubled"]["value"] == "40"

    dap.set_breakpoints(MIXED, [])
    status = assert_nothing_armed(dap)
    assert status["agent"]["global_events"] == 0 and status["agent"]["local_events"] == {}
    dap.cont()
    assert dap.wait_exit() == 0


def test_stepping_over_the_end_of_a_native_function_returns_to_python(dap, capi, iteration):
    line = marker_line(CAPI_SRC, "add-return")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={CAPI_SRC: [line]})
    tid = dap.wait_stopped()["threadId"]
    assert top(dap, tid) == ("st_add", line)
    for _ in range(3):
        dap.step("next", tid)
        if top(dap, tid)[0] != "st_add":
            break
    assert top(dap, tid) == ("leaf", marker_line(MIXED, "leaf-add"))
    assert_nothing_armed(dap)
    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_step_in_from_native_to_python_callback_and_back_out(dap, capi, iteration):
    line = marker_line(CAPI_SRC, "callback-call")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={CAPI_SRC: [line]})
    tid = dap.wait_stopped()["threadId"]
    assert top(dap, tid) == ("st_call_back", line)

    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert names(stack)[:3] == ["leaf", "st_call_back", "middle"]
    assert stack[0]["line"] == marker_line(MIXED, "leaf-add")
    assert dap.evaluate("v", stack[0]["id"])["result"] == "20"
    assert_nothing_armed(dap)

    # Step out of the Python callback: back in its native caller.
    stop = dap.step("stepOut", tid)
    assert stop["reason"] == "step"
    name, at = top(dap, tid)
    assert name == "st_call_back"
    assert at_line(capi, at, line, marker_line(CAPI_SRC, "callback-after"))
    assert_nothing_armed(dap)

    # Keep stepping over in C until it returns: lands on the Python line that called it.
    for _ in range(8):
        dap.step("next", tid)
        if top(dap, tid)[0] != "st_call_back":
            break
    assert top(dap, tid) == ("middle", marker_line(MIXED, "middle-callback"))
    assert_nothing_armed(dap)
    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_stepping_over_the_end_of_a_python_callback_returns_to_native(dap, capi, iteration):
    line = marker_line(MIXED, "leaf-return")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={MIXED: [line]})
    tid = dap.wait_stopped()["threadId"]
    assert top(dap, tid) == ("leaf", line)
    stop = dap.step("next", tid)
    assert stop["reason"] == "step"
    name, at = top(dap, tid)
    assert name == "st_call_back"
    assert at_line(capi, at, marker_line(CAPI_SRC, "callback-call"),
                   marker_line(CAPI_SRC, "callback-after"))
    assert_nothing_armed(dap)
    dap.set_breakpoints(MIXED, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_native_exception_does_not_break_stepping(dap, capi, iteration):
    call = marker_line(MIXED, "main-fail")
    caught = marker_line(MIXED, "main-caught")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={MIXED: [call]})
    tid = dap.wait_stopped()["threadId"]

    dap.step("stepIn", tid)
    name, at = top(dap, tid)
    assert name == "st_fail" and at_line(capi, at, marker_line(CAPI_SRC, "fail-first"))
    # The C function returns NULL with an exception set. Stepping out must follow the
    # exception to where Python handles it, not run away.
    stop = dap.step("stepOut", tid)
    assert stop["reason"] == "step"
    name, at = top(dap, tid)
    assert name == "main" and call <= at <= caught
    for _ in range(3):
        if top(dap, tid)[1] == caught:
            break
        dap.step("next", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == ("main", caught)
    assert "native failure" in dap.scope(stack[0]["id"])["exc"]["value"]
    assert_nothing_armed(dap)
    dap.set_breakpoints(MIXED, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert "caught native failure" in dap.output and "done" in dap.output


def test_step_over_a_python_line_whose_native_call_raises(dap, capi, iteration):
    call = marker_line(MIXED, "main-fail")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={MIXED: [call]})
    tid = dap.wait_stopped()["threadId"]
    dap.step("next", tid)
    name, at = top(dap, tid)
    assert name == "main" and call < at <= marker_line(MIXED, "main-caught")
    assert_nothing_armed(dap)
    dap.set_breakpoints(MIXED, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_step_in_on_a_python_line_without_native_user_code_stays_in_python(dap, capi, iteration):
    line = marker_line(MIXED, "main-print")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={MIXED: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.step("stepIn", tid)  # print() is native but not user code: behaves like step over
    name, at = top(dap, tid)
    fail = marker_line(MIXED, "main-fail")
    assert name == "main" and at in (fail - 1, fail)  # the `try:` line or its first statement
    assert_nothing_armed(dap)
    dap.set_breakpoints(MIXED, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_pause_then_step_python(dap, capi, iteration):
    import time

    dap.launch(RUNNING, dap.python, env=capi.env)
    time.sleep(0.5)
    dap.request("pause", {"threadId": 0})
    stop = dap.wait_stopped()
    assert stop["reason"] == "pause"
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert "main" in names(stack) and names(stack)[-1] == "<module>"
    assert dap.status()["safe"] is False

    # Stopped at an arbitrary point inside the interpreter: the step is armed at the
    # next safe point and still ends on a Python line.
    stop = dap.step("next", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert stack[0]["source"]["path"] == RUNNING
    dap.evaluate("globals().update(STOP=True)", stack[0]["id"])
    assert_nothing_armed(dap)
    dap.cont()
    assert dap.wait_exit() == 0
