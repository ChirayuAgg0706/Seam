"""The same scenarios against every binding layer: C API, pybind11, nanobind, Cython, PyO3.

"Lands in user code" means: the top frame's source file is the extension's own source
(not a framework header or generated glue) and it is the user's function.
"""
import pytest

from conftest import at_line, marker_line, target

pytestmark = pytest.mark.smoke

BINDING = target("binding.py")
ADD_MARKER = {"capi": "add-first"}


def describe(stack):
    return [(f["name"], f.get("source", {}).get("path"), f["line"]) for f in stack]


def python_frames(stack):
    return [(f["name"], f["line"]) for f in stack
            if f.get("source", {}).get("path") == BINDING]


def launch(dap, binding, breakpoints):
    dap.launch(BINDING, dap.python, args=[binding.module], env=binding.env,
               breakpoints=breakpoints)
    return dap.wait_stopped()["threadId"]


def test_step_in_lands_in_user_code_and_step_out_returns(dap, binding, iteration):
    call = marker_line(BINDING, "bind-add")
    tid = launch(dap, binding, {BINDING: [call]})
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    top = stack[0]
    assert top.get("source", {}).get("path") == binding.source, describe(stack)
    assert "add" in top["name"], describe(stack)
    body = marker_line(binding.source, ADD_MARKER.get(binding.layer, "add-body"))
    assert binding.opt != "O0" or body - 2 <= top["line"] <= body, describe(stack)
    assert python_frames(stack) == [("main", call),
                                    ("<module>", marker_line(BINDING, "module-main"))]
    if binding.opt == "O0":
        # Binding glue (dispatchers, trampolines, argument-parsing wrappers) is hidden:
        # the user's function sits directly on top of the Python frame that called it.
        assert [f["name"] for f in stack[1:]] == ["main", "<module>"], describe(stack)

    dap.step("stepOut", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == ("main", call), describe(stack)
    dap.step("next", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == ("main", marker_line(BINDING, "bind-after"))
    assert dap.scope(stack[0]["id"])["total"]["value"] == "42"

    dap.set_breakpoints(BINDING, [])
    status = dap.status()
    assert status["stepInBreakpointsEnabled"] is False and status["pythonStepArmed"] is False
    assert status["agent"]["stepping"] is None and status["agent"]["global_events"] == 0
    dap.cont()
    assert dap.wait_exit() == 0
    assert "sum 42" in dap.output and "back 6" in dap.output


def test_native_breakpoint_then_into_and_out_of_a_python_callback(dap, binding, iteration):
    line = marker_line(binding.source, "callback-call")
    dap.launch(BINDING, dap.python, args=[binding.module], env=binding.env,
               breakpoints={binding.source: [line]})
    event, body = dap.wait_any(("stopped", "exited"))
    resolved = [e["body"]["breakpoint"] for e in dap.events
                if e["event"] == "breakpoint" and e["body"]["breakpoint"]["verified"]]
    if event == "exited":
        # Only legitimate with optimisation: the compiler left the statement with no code
        # of its own (it is a single inlined library call), so no breakpoint can be placed
        # on it. Seam must say so rather than pretend, and the program must run normally.
        assert binding.opt != "O0", "breakpoint on the call line was never hit"
        assert not resolved and body["exitCode"] == 0
        pytest.skip("the %s -%s build has no code on the call line; Seam reported the "
                    "breakpoint as unverified" % (binding.layer, binding.opt))
    tid = body["threadId"]
    stack = dap.stack(tid)
    top = stack[0]
    assert top.get("source", {}).get("path") == binding.source, describe(stack)
    assert at_line(binding, top["line"], line) and "call_back" in top["name"], describe(stack)
    if top["line"] > line:
        # Optimised build: the call line has no code of its own, so the breakpoint was
        # moved to the next line that has, which is after the callback already ran.
        assert resolved and resolved[-1]["line"] == top["line"], (resolved, top)
        pytest.skip("the %s -%s build moved the breakpoint past the call (line %d -> %d); "
                    "Seam reported the new line" % (binding.layer, binding.opt, line, top["line"]))
    assert python_frames(stack) == [("main", marker_line(BINDING, "bind-callback")),
                                    ("<module>", marker_line(BINDING, "module-main"))]

    # One "step in" enters the callback at -O0. Two known exceptions (see STATUS.md):
    # Cython's generated C spreads one .pyx line over many small line-table ranges that
    # alternate with the `def` line, and optimised code of any layer has the same shape,
    # so there several presses are needed before the call itself is reached.
    presses = 0
    several = binding.layer == "cython" or binding.opt != "O0"
    for presses in range(1, 13 if several else 2):
        stop = dap.step("stepIn", tid)
        # When several presses are needed, one of them can land on another address
        # range of the breakpoint's own line, which is reported as a breakpoint stop.
        allowed = ("step", "breakpoint") if several else ("step",)
        assert stop["reason"] in allowed, (stop, describe(dap.stack(tid)))
        stack = dap.stack(tid)
        if stack[0]["name"] == "cb":
            break
        assert "call_back" in stack[0]["name"], describe(stack)
    assert presses == 1 or several, "took %d presses" % presses
    assert (stack[0]["name"], stack[0]["line"]) == ("cb", marker_line(BINDING, "cb-body")), \
        describe(stack)
    assert dap.evaluate("v", stack[0]["id"])["result"] == "5"
    if "could not unwind the native stack" in dap.output:
        # LLDB itself lost the native frames below the callback (LLDB 20 cannot unwind
        # nanobind's optimised library code; its own `bt` stops there too). Seam says so
        # and still shows every Python frame; the native caller cannot be stepped back to.
        assert binding.opt != "O0", dap.output
        assert python_frames(stack)[-2:] == [
            ("main", marker_line(BINDING, "bind-callback")),
            ("<module>", marker_line(BINDING, "module-main"))], describe(stack)
        pytest.skip("LLDB could not unwind below the callback in the %s -%s build; Seam "
                    "reported it and kept the Python frames" % (binding.layer, binding.opt))
    user_native = [f for f in stack if f.get("source", {}).get("path") == binding.source]
    assert user_native and "call_back" in user_native[0]["name"], describe(stack)

    dap.step("stepOut", tid)
    stack = dap.stack(tid)
    top = stack[0]
    assert top.get("source", {}).get("path") == binding.source, describe(stack)
    assert "call_back" in top["name"], describe(stack)

    dap.step("stepOut", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == \
        ("main", marker_line(BINDING, "bind-callback")), describe(stack)

    dap.set_breakpoints(binding.source, [])
    status = dap.status()
    assert status["stepInBreakpointsEnabled"] is False and status["pythonStepArmed"] is False
    assert status["nativeStepInProgress"] is False
    dap.cont()
    assert dap.wait_exit() == 0
    assert "back 6" in dap.output
