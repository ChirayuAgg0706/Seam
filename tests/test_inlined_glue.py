"""Stepping from a place where glue is inlined into the user's function.

Found in pydantic-core (optimised Rust): the thread was in a user function at a point
where `Result::map_err` is inlined, so the newest frame was glue with the user's function
right below it, at the same PC and stack pointer. Seam took the user's frame for a caller
and ran "until return to it"; the program ran to its end. The same shape exists at -O0
wherever a CPython header inline is used, which is what tests/ext/capi/seam_inline.c does.
"""
import os
import subprocess

import pytest

from conftest import BUILD, EXT, Extension, marker_line, target

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
        subprocess.run(["gcc", "-shared", "-fPIC", "-g", "-" + opt, "-Wall",
                        "-I", pyinfo["include"], SOURCE, "-o", out], check=True)
    return Extension(out_dir, opt, SOURCE, "seam_inline")


def top(dap, tid):
    frame = dap.stack(tid)[0]
    return frame["name"], frame["line"]


def stop_inside_the_inlined_glue(dap, ext):
    """Stop in si_keep with Py_INCREF's inlined code as the newest native frame."""
    line = marker_line(SOURCE, "keep-incref")
    dap.launch(INLINE, dap.python, env=ext.env, breakpoints={SOURCE: [line]})
    event, body = dap.wait_any(("stopped", "exited"))
    if event == "exited":
        pytest.skip("this build has no code of its own on the Py_INCREF line")
    tid = body["threadId"]
    assert top(dap, tid)[0] == "si_keep"
    dap.set_breakpoints(SOURCE, [])
    with open(dap.log_path, errors="replace") as fh:
        before = fh.read().count("Py_INCREF")
    for _ in range(4):
        dap.step("stepIn", tid)
        # The stack shown is the user's function; the glue frame on top is in the log.
        assert top(dap, tid)[0] == "si_keep"
        with open(dap.log_path, errors="replace") as fh:
            if fh.read().count("Py_INCREF") > before:
                return tid
    pytest.skip("stepping in did not enter Py_INCREF's inlined code in this build")


def test_step_out_with_glue_inlined_on_top_returns_to_the_caller(dap, inline_ext, iteration):
    tid = stop_inside_the_inlined_glue(dap, inline_ext)
    dap.send("stepOut", {"threadId": tid})
    event, body = dap.wait_any(("stopped", "exited"))
    assert event == "stopped", "step out ran the program to its end"
    assert top(dap, tid) == ("main", marker_line(INLINE, "keep-call"))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "same True" in dap.output


def test_step_out_of_python_called_by_inlined_glue_returns_to_the_native_caller(
        dap, inline_ext, iteration):
    """`__del__` runs inside Py_DECREF, which is inlined into si_drop: the address the
    call returns to belongs to the glue frame, not to si_drop's own."""
    line = marker_line(INLINE, "del-body")
    dap.launch(INLINE, dap.python, env=inline_ext.env, breakpoints={INLINE: [line]})
    tid = dap.wait_stopped()["threadId"]
    names = [f["name"] for f in dap.stack(tid)]
    assert names == ["Noisy.__del__", "si_drop", "main", "<module>"], names
    dap.set_breakpoints(INLINE, [])
    dap.send("stepOut", {"threadId": tid})
    event, body = dap.wait_any(("stopped", "exited"))
    assert event == "stopped", "step out ran the program to its end"
    name, where = top(dap, tid)
    assert name == "si_drop", (name, where)
    if inline_ext.opt == "O0":
        assert where in (marker_line(SOURCE, "drop-decref"), marker_line(SOURCE, "drop-return"))
    dap.send("stepOut", {"threadId": tid})
    event, body = dap.wait_any(("stopped", "exited"))
    assert event == "stopped", "step out ran the program to its end"
    assert top(dap, tid) == ("main", marker_line(INLINE, "drop-call"))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "dropped" in dap.output


def test_step_over_with_glue_inlined_on_top_stays_in_the_function(dap, inline_ext, iteration):
    tid = stop_inside_the_inlined_glue(dap, inline_ext)
    for _ in range(8):
        dap.send("next", {"threadId": tid})
        event, body = dap.wait_any(("stopped", "exited"))
        assert event == "stopped", "step over ran the program to its end"
        name, line = top(dap, tid)
        if name != "si_keep":
            break
        assert line >= marker_line(SOURCE, "keep-incref")
    # Stepping over the end of the function comes out on the Python line that called it.
    assert (name, line) == ("main", marker_line(INLINE, "keep-call"))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "same True" in dap.output
