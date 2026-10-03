"""The same scenarios against every binding layer: C API, pybind11, nanobind, Cython, PyO3.

"Lands in user code" means: the top frame's source file is the extension's own source
(not a framework header or generated glue) and it is the user's function.
"""
from conftest import marker_line, target

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
    assert body - 2 <= top["line"] <= body, describe(stack)
    assert python_frames(stack) == [("main", call),
                                    ("<module>", marker_line(BINDING, "module-main"))]

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
    tid = launch(dap, binding, {binding.source: [line]})
    stack = dap.stack(tid)
    top = stack[0]
    assert top.get("source", {}).get("path") == binding.source, describe(stack)
    assert top["line"] == line and "call_back" in top["name"], describe(stack)
    assert python_frames(stack) == [("main", marker_line(BINDING, "bind-callback")),
                                    ("<module>", marker_line(BINDING, "module-main"))]

    # One "step in" enters the callback. Cython is the exception (a known limitation, see
    # STATUS.md): its generated C spreads one .pyx line over many small line-table ranges
    # that alternate with the `def` line, so several presses are needed to reach the call.
    presses = 0
    for presses in range(1, 13 if binding.layer == "cython" else 2):
        stop = dap.step("stepIn", tid)
        assert stop["reason"] == "step"
        stack = dap.stack(tid)
        if stack[0]["name"] == "cb":
            break
        assert "call_back" in stack[0]["name"], describe(stack)
    assert presses == 1 or binding.layer == "cython", "took %d presses" % presses
    assert (stack[0]["name"], stack[0]["line"]) == ("cb", marker_line(BINDING, "cb-body")), \
        describe(stack)
    assert dap.evaluate("v", stack[0]["id"])["result"] == "5"
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
