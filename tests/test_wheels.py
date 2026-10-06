"""Real third-party wheels (numpy, orjson): no debug info, optimised, in a virtualenv."""
import time

import pytest

from conftest import marker_line, target
from test_python import ground_truth, py_frames

pytestmark = pytest.mark.smoke

WHEELS = target("wheels.py")


def python_part(stack):
    """The frames of a merged stack that are Python source, as (name, line).

    Seam shows qualified names (`vectorize.__call__`); the traceback module, which the
    ground truth comes from, only has the bare name. Compare bare names.
    """
    return [(f["name"].split(".")[-1], f["line"]) for f in stack
            if f.get("source", {}).get("path", "").endswith(".py")]


def test_python_callback_called_from_numpy(dap, wheels_python):
    line = marker_line(WHEELS, "scale-body")
    dap.launch(WHEELS, wheels_python, args=["callbacks"], breakpoints={WHEELS: [line]})
    stop = dap.wait_stopped()
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == ("scale", line)
    # numpy's C frames sit between `scale` and its Python callers. Whatever LLDB makes of
    # them, the Python frames must be exactly what Python itself reports.
    assert python_part(stack) == ground_truth(dap, stack[0]["id"])
    names = [f["name"] for f in stack]
    assert "use_numpy" in names and names[-1] == "<module>"
    assert dap.scope(stack[0]["id"])["x"]["value"] in ("0", "np.int64(0)")

    stop = dap.step("next", tid)
    assert py_frames(dap.stack(tid))[0] == ("scale", line + 1)
    dap.set_breakpoints(WHEELS, [])
    stop = dap.step("stepOut", tid)  # back into numpy's own Python code, or ours
    assert stop["reason"] == "step"
    assert python_part(dap.stack(tid))[-1] == ("<module>", marker_line(WHEELS, "module-numpy"))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "numpy 12" in dap.output and "done" in dap.output


def test_python_callback_called_from_a_rust_wheel(dap, wheels_python):
    line = marker_line(WHEELS, "encode-body")
    dap.launch(WHEELS, wheels_python, args=["callbacks"], breakpoints={WHEELS: [line]})
    stop = dap.wait_stopped()
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert python_part(stack) == [
        ("encode", line),
        ("use_orjson", marker_line(WHEELS, "orjson-call")),
        ("<module>", marker_line(WHEELS, "module-orjson")),
    ]
    assert python_part(stack) == ground_truth(dap, stack[0]["id"])
    assert dap.evaluate("type(obj).__name__", stack[0]["id"])["result"] == "'Odd'"
    # Leaving the callback returns through orjson (no debug info) to the calling line.
    stop = dap.step("stepOut", tid)
    assert py_frames(dap.stack(tid))[0] == ("use_orjson", marker_line(WHEELS, "orjson-call"))
    dap.set_breakpoints(WHEELS, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert 'orjson {"value":{"odd":"Odd"}}' in dap.output


def test_step_in_on_a_call_into_a_wheel_without_debug_info(dap, wheels_python):
    line = marker_line(WHEELS, "orjson-call")
    dap.launch(WHEELS, wheels_python, args=["callbacks"], breakpoints={WHEELS: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(WHEELS, [])
    # There is no native source to stop in; the step lands in the Python callback that
    # orjson calls, which is the first user code the statement reaches.
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    assert py_frames(dap.stack(tid))[0] == ("encode", marker_line(WHEELS, "encode-body"))
    dap.cont()
    assert dap.wait_exit() == 0


def test_pause_inside_numpy(dap, wheels_python):
    dap.launch(WHEELS, wheels_python, args=["busy", "4"])
    dap.wait_output("ready to pause")
    time.sleep(1.5)
    dap.request("pause", {"threadId": 0})
    stop = dap.wait_stopped()
    assert stop["reason"] == "pause"
    stacks = [dap.stack(t["id"]) for t in dap.request("threads")["threads"]]
    main = [s for s in stacks if python_part(s)]
    assert len(main) == 1
    # np.sort is itself a Python function, so it may be on top; below it, our frames.
    assert python_part(main[0])[-2:] == [
        ("busy", marker_line(WHEELS, "busy-sort")),
        ("<module>", marker_line(WHEELS, "module-busy")),
    ]
    dap.cont()
    assert dap.wait_exit() == 0
    assert "rounds True" in dap.output


def test_exception_raised_by_a_rust_wheel(dap, wheels_python):
    dap.launch(WHEELS, wheels_python, args=["error"], exceptions=["raised"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "JSONDecodeError"
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack)[0] == ("bad_json", marker_line(WHEELS, "error-call"))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "caught JSONDecodeError" in dap.output
