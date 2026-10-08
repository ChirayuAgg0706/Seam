"""Stepping from a place where glue is inlined into the user's function.

Found in pydantic-core (optimised Rust): the thread was in a user function at a point
where `Result::map_err` is inlined, so the newest frame was glue with the user's function
right below it, in the same function body. Seam took the user's frame for a caller and ran
"until return to it"; the program ran to its end. And getting from a breakpoint on the
line that calls a Python validator into that validator took seven presses of Step Into,
one per inlined piece of glue on the line.

The same shape exists at -O0 wherever a CPython header inline is used, which is what
tests/ext/capi/seam_inline.c does: Py_INCREF and Py_DECREF are always inlined, and their
code belongs to a header Seam treats as glue.
"""
import os
import subprocess

import pytest

from conftest import BUILD, EXT, SHARED_FLAGS, Extension, marker_line, target

pytestmark = pytest.mark.smoke

INLINE = target("inline.py")
SOURCE = os.path.join(EXT, "capi", "seam_inline.c")


@pytest.fixture(scope="module")
def inline_ext(request, pyinfo):
    opt = request.config.getoption("--opt")
    out_dir = os.path.join(BUILD, "inline-%s-%s" % (opt, pyinfo["tag"]))
    out = os.path.join(out_dir, "seam_inline.so")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(SOURCE):
        os.makedirs(out_dir, exist_ok=True)
        subprocess.run(["gcc", *SHARED_FLAGS, "-g", "-" + opt, "-Wall",
                        "-I", pyinfo["include"], SOURCE, "-o", out], check=True)
    return Extension(out_dir, opt, SOURCE, "seam_inline")


def top(dap, tid):
    frame = dap.stack(tid)[0]
    return frame["name"], frame["line"]


def step(dap, command, tid):
    """A step that must end in a stop: the defect was the program running to its end."""
    dap.send(command, {"threadId": tid})
    event, body = dap.wait_any(("stopped", "exited"))
    assert event == "stopped", "%s ran the program to its end" % command
    return body


def test_one_step_in_gets_through_glue_inlined_into_the_line(dap, inline_ext, iteration):
    line = marker_line(SOURCE, "keep-incref")
    # At -O0 the line is reached by stepping from the one before. A breakpoint on it does
    # not do: all its code is the inlined Py_INCREF, and LLDB before 20 moves a breakpoint
    # on such a line to the next line with code of its own (seen on CI with 18 and 19).
    first = marker_line(SOURCE, "keep-first") if inline_ext.opt == "O0" else line
    dap.launch(INLINE, dap.python, env=inline_ext.env, breakpoints={SOURCE: [first]})
    event, body = dap.wait_any(("stopped", "exited"))
    if event == "exited":
        pytest.skip("this build has no code of its own on the Py_INCREF line")
    tid = body["threadId"]
    assert top(dap, tid)[0] == "si_keep"
    dap.set_breakpoints(SOURCE, [])
    if inline_ext.opt == "O0":
        assert top(dap, tid) == ("si_keep", first)
        step(dap, "next", tid)
        assert top(dap, tid) == ("si_keep", line)
    # Py_INCREF is several lines of a header, inlined here. None of them is a place the
    # user can see: one press ends on the next line of the user's function (or, with
    # optimisation, wherever the function goes next), not on the same line again.
    step(dap, "stepIn", tid)
    name, where = top(dap, tid)
    if inline_ext.opt == "O0":
        assert (name, where) == ("si_keep", marker_line(SOURCE, "keep-after"))
    else:
        assert (name, where) != ("si_keep", line)
    if name == "si_keep":
        step(dap, "stepOut", tid)
    assert top(dap, tid) == ("main", marker_line(INLINE, "keep-call"))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "same True" in dap.output


def stop_in_the_destructor_and_step_out(dap, ext):
    """`__del__` runs inside Py_DECREF, which is inlined into si_drop: the address the call
    returns to belongs to the glue frame, not to a frame of si_drop's own. Stepping out
    of it must end in si_drop, with the glue frame still on top of it."""
    line = marker_line(INLINE, "del-body")
    dap.launch(INLINE, dap.python, env=ext.env, breakpoints={INLINE: [line]})
    tid = dap.wait_stopped()["threadId"]
    names = [f["name"] for f in dap.stack(tid)]
    assert names == ["Noisy.__del__", "si_drop", "main", "<module>"], names
    dap.set_breakpoints(INLINE, [])
    step(dap, "stepOut", tid)
    name, where = top(dap, tid)
    assert name == "si_drop", (name, where)
    if ext.opt == "O0":
        assert where in (marker_line(SOURCE, "drop-decref"), marker_line(SOURCE, "drop-return"))
    return tid


def test_step_out_of_python_called_by_inlined_glue_and_out_again(dap, inline_ext, iteration):
    tid = stop_in_the_destructor_and_step_out(dap, inline_ext)
    step(dap, "stepOut", tid)
    assert top(dap, tid) == ("main", marker_line(INLINE, "drop-call"))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "dropped" in dap.output


def test_step_over_with_glue_inlined_on_top_stays_in_the_function(dap, inline_ext, iteration):
    tid = stop_in_the_destructor_and_step_out(dap, inline_ext)
    for _ in range(6):
        step(dap, "next", tid)
        name, where = top(dap, tid)
        if name != "si_drop":
            break
        assert where >= marker_line(SOURCE, "drop-decref")
    # Stepping over the end of the function comes out on the Python line that called it.
    assert (name, where) == ("main", marker_line(INLINE, "drop-call"))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "dropped" in dap.output
