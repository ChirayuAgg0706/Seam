"""Exceptions that come out of a Cython module.

Found with msgpack: Cython puts a frame of its own into the traceback, with the .pyx path
as it was at build time ("msgpack/_unpacker.pyx"), relative and nowhere to be found at run
time. Seam took that frame for the user's code, so "Raised Python exceptions" never
stopped for an exception raised by a Cython library, and at an uncaught exception the
frame was shown with a source path made up from the working directory.
"""
import os
import subprocess
import sys

import pytest

from conftest import BUILD, EXT, SHARED_FLAGS, Extension, _unavailable, marker_line, target

pytestmark = pytest.mark.smoke

CYRAISE = target("cyraise.py")
SOURCE = os.path.join(EXT, "cython", "seam_cyraise.pyx")


@pytest.fixture(scope="module")
def cyraise(request, pyinfo):
    try:
        import Cython  # noqa: F401
    except ImportError:
        _unavailable("Cython is not installed in the test environment")
    opt = request.config.getoption("--opt")
    out_dir = os.path.join(BUILD, "cyraise-%s-%s" % (opt, pyinfo["tag"]))
    out = os.path.join(out_dir, "seam_cyraise.so")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(SOURCE):
        os.makedirs(out_dir, exist_ok=True)
        c_file = os.path.join(out_dir, "seam_cyraise.c")
        # As a library is built: from the source's own directory, without line directives.
        subprocess.run([sys.executable, "-m", "cython", "-3", os.path.basename(SOURCE),
                        "-o", c_file], cwd=os.path.dirname(SOURCE), check=True)
        subprocess.run(["gcc", *SHARED_FLAGS, "-g", "-" + opt, "-I", pyinfo["include"],
                        c_file, "-o", out], check=True)
    return Extension(out_dir, opt, SOURCE, "seam_cyraise")


def python_frames(stack):
    return [(f["name"], f["line"]) for f in stack if f.get("source", {}).get("path") == CYRAISE]


def test_raised_in_a_cython_module_stops_in_the_caller(dap, cyraise):
    dap.launch(CYRAISE, dap.python, env=cyraise.env, exceptions=["raised", "uncaught"])
    # Raised by the library, first reaches the user's code in main: one stop per exception.
    for marker in ("fail-call", "fail-again"):
        stop = dap.wait_stopped()
        assert stop["reason"] == "exception" and stop["text"] == "ValueError", stop
        info = dap.request("exceptionInfo", {"threadId": stop["threadId"]})
        assert info["breakMode"] == "always", info
        stack = dap.stack(stop["threadId"])
        assert python_frames(stack)[0] == ("main", marker_line(CYRAISE, marker)), stack
        dap.cont()

    # Nobody handles the second one. The frame Cython added to the traceback is shown,
    # without pretending to know a file for it.
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    info = dap.request("exceptionInfo", {"threadId": stop["threadId"]})
    assert info["breakMode"] == "unhandled", info
    stack = dap.stack(stop["threadId"])
    assert "fail" in stack[0]["name"], stack
    for frame in stack:
        path = frame.get("source", {}).get("path")
        assert path is None or os.path.exists(path), frame
    assert python_frames(stack) == [("main", marker_line(CYRAISE, "fail-again")),
                                    ("<module>", marker_line(CYRAISE, "module-main"))]
    dap.cont()
    assert dap.wait_exit() == 1
    assert "caught bad value 3" in dap.output
