"""Debugging sessions against real open-source extension projects. Opt-in: SEAM_TEST_PROJECTS=1.

The projects are built from source with debug info by tools/build_projects.sh, into a
virtual environment under $SEAM_SCALE_DIR (default ~/.cache/seam/scale); their sources stay
where the debug info says they are. One project per binding layer:

    regex           plain C API, one very large C file
    msgpack         Cython (generated C, no line directives)
    contourpy       pybind11, C++ templates in headers
    pydantic_core   PyO3, optimised Rust: 123,000 functions and inlined instances

    tools/build_projects.sh regex msgpack contourpy pydantic_core
    SEAM_TEST_PROJECTS=1 scripts/test.sh -q -s tests/test_projects.py

A project that has not been built is skipped. Each scenario prints how long its steps took.
"""
import os
import subprocess
import time

import pytest

from conftest import marker_line, target

pytestmark = pytest.mark.skipif(os.environ.get("SEAM_TEST_PROJECTS") != "1",
                                reason="real-project scenarios are opt-in: SEAM_TEST_PROJECTS=1")

ROOT = os.environ.get("SEAM_SCALE_DIR", os.path.expanduser("~/.cache/seam/scale"))
PYTHON = os.path.join(ROOT, "venv", "bin", "python")


def source(project, *parts):
    return os.path.join(ROOT, "src", project, *parts)


def line_of(path, text, after=None):
    """Number of the first line containing `text` (below the first one containing `after`)."""
    seen = after is None
    with open(path, errors="replace") as fh:
        for number, line in enumerate(fh, 1):
            if not seen:
                seen = after in line
            elif text in line:
                return number
    raise AssertionError("%r not found in %s" % (text, path))


def built(module):
    if not os.path.exists(PYTHON):
        pytest.skip("no environment at %s: run tools/build_projects.sh" % PYTHON)
    done = subprocess.run([PYTHON, "-c", "import " + module], capture_output=True)
    if done.returncode != 0:
        pytest.skip("%s is not built: run tools/build_projects.sh" % module)


class Session:
    """A launched session that times its steps."""

    def __init__(self, dap, program, breakpoints=(), args=(), **launch):
        self.dap = dap
        self.program = program
        self.times = []
        started = time.monotonic()
        dap.launch(program, PYTHON, args=args, breakpoints={
            program: [marker_line(program, name) for name in breakpoints]}, **launch)
        self.launched = started

    def line(self, name):
        return marker_line(self.program, name)

    def wait(self, label, started=None):
        started = started or time.monotonic()
        event, body = self.dap.wait_any(("stopped", "exited"), timeout=120)
        self.times.append((label, time.monotonic() - started))
        assert event == "stopped", "%s: the program ran to its end" % label
        self.tid = body["threadId"]
        self.stack = self.dap.stack(self.tid)
        return body

    def first_stop(self):
        return self.wait("launch to first stop", self.launched)

    def step(self, command, label=None):
        started = time.monotonic()
        self.dap.send(command, {"threadId": self.tid})
        return self.wait(label or command, started)

    def cont(self, label="continue"):
        started = time.monotonic()
        self.dap.cont(self.tid)
        return self.wait(label, started)

    def top(self):
        frame = self.stack[0]
        return frame["name"], frame.get("source", {}).get("path", ""), frame["line"]

    def names(self):
        return [frame["name"] for frame in self.stack]

    def finish(self, code=0):
        self.dap.set_breakpoints(self.program, [])
        self.dap.request("setFunctionBreakpoints", {"breakpoints": []})
        self.dap.cont(self.tid)
        assert self.dap.wait_exit(timeout=120) == code
        print("\n" + "\n".join("    %-46s %6.3f s" % row for row in self.times))


def test_regex_step_in_out_and_native_variables(dap):
    built("regex")
    s = Session(dap, target("project_regex.py"), ["search"])
    s.first_stop()
    s.step("stepIn", "step in, Python to native (first)")
    name, path, _ = s.top()
    assert name == "pattern_search" and path == source("regex", "regex_3", "_regex.c"), s.top()
    assert s.names()[1:] == ["main", "<module>"]
    assert {"self", "args", "kwargs"} <= set(dap.scope(s.stack[0]["id"]))
    s.step("next", "step over a native line")
    # Clang can place the entry stop on the wrapper's return statement. Next
    # completes that statement and returns directly to its Python caller.
    if s.top()[0] == "pattern_search":
        s.step("stepOut", "step out, native to Python")
    assert s.top() == ("main", s.program, s.line("search"))
    s.finish()


def test_regex_native_breakpoints_and_python_callback(dap):
    built("regex")
    c_file = source("regex", "regex_3", "_regex.c")
    call = line_of(c_file, "item = PyObject_CallObject(replacement, args);")
    s = Session(dap, target("project_regex.py"))
    dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "pattern_sub"}]})
    assert not dap.set_breakpoints(c_file, [call])[0]["verified"]  # module not loaded yet
    s.wait("run to the function breakpoint", s.launched)
    assert s.names() == ["pattern_sub", "main", "<module>"]
    s.cont("continue to the line breakpoint")
    assert s.top()[0] == "pattern_subx" and s.top()[2] == call
    assert s.names()[1:] == ["pattern_sub", "main", "<module>"]
    s.step("stepIn", "step in, native to the Python callback")
    assert s.top() == ("shout", s.program, s.line("shout-body"))
    assert s.names()[1:3] == ["pattern_subx", "pattern_sub"]
    dap.set_breakpoints(c_file, [])
    s.step("stepOut", "step out, Python callback to native")
    assert s.top()[0] == "pattern_subx"
    s.finish()


def test_regex_exception_raised_by_the_library(dap):
    built("regex")
    program = target("project_regex.py")
    dap.launch(program, PYTHON, args=["error"], exceptions=["raised", "uncaught"])
    for mode in ("always", "unhandled"):
        stop = dap.wait_stopped()
        assert stop["reason"] == "exception" and stop["text"] == "TypeError", stop
        info = dap.request("exceptionInfo", {"threadId": stop["threadId"]})
        assert info["breakMode"] == mode
        frame = dap.stack(stop["threadId"])[0]
        assert (frame["name"], frame["line"]) == ("main", marker_line(program, "error"))
        dap.cont()
    assert dap.wait_exit() == 1


def test_pydantic_core_step_in_lands_in_the_validator(dap):
    built("pydantic_core")
    s = Session(dap, target("project_pydantic.py"), ["validate", "callback"])
    s.first_stop()
    # The first step-in of the session resolves and classifies every function of the
    # module once (123,000 with their inlined instances): seconds.
    s.step("stepIn", "step in, Python to native (first)")
    name, path, _ = s.top()
    assert "SchemaValidator" in name and name.endswith("validate_python"), name
    assert path == source("pydantic_core", "src", "validators", "mod.rs")
    assert s.names()[1:] == ["main", "<module>"], s.names()  # PyO3's trampolines are hidden
    assert {"self", "input", "strict"} <= set(dap.scope(s.stack[0]["id"]))
    s.step("next", "step over a native line")
    assert s.top()[0] == name
    s.step("stepOut", "step out, native to Python")
    assert s.top() == ("main", s.program, s.line("validate"))
    s.cont()
    s.step("stepIn", "step in, Python to native (again)")
    assert s.top()[0] == name
    assert s.times[-1][1] < 2.0, "a later step-in took %.2f s" % s.times[-1][1]
    s.finish()


def test_pydantic_core_breakpoints_callback_and_stepping_out(dap):
    built("pydantic_core")
    rust = source("pydantic_core", "src", "validators", "function.rs")
    call = line_of(rust, "self.func.call1(py, (input.to_object(py),))",
                   after="impl Validator for FunctionPlainValidator")
    s = Session(dap, target("project_pydantic.py"))
    dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "validate_python"}]})
    dap.set_breakpoints(rust, [call])
    s.wait("run to the function breakpoint", s.launched)
    assert s.top()[0].endswith("validate_python") and s.names()[1:] == ["main", "<module>"]
    dap.request("setFunctionBreakpoints", {"breakpoints": []})
    s.cont("continue to the line breakpoint")
    name, path, where = s.top()
    assert "FunctionPlainValidator" in name and (path, where) == (rust, call), s.top()
    callers = s.names()[1:]
    # Optimized Apple Rust/LLDB can omit the intermediate _validate frame.
    has_validate_frame = "SchemaValidator>::_validate" in callers[0]
    if has_validate_frame:
        callers = callers[1:]
    assert callers[0].endswith("validate_python") and callers[1:] == ["main", "<module>"]
    dap.set_breakpoints(rust, [])
    # The line is seven pieces of inlined glue and then the call: one press.
    s.step("stepIn", "step in, native to the Python callback")
    assert s.top() == ("double", s.program, s.line("double-body")), s.top()
    s.step("stepOut", "step out, Python callback to native")
    assert "FunctionPlainValidator" in s.top()[0]
    # Out of each native frame in turn (the first has glue inlined at this very place).
    s.step("stepOut", "step out, native to native")
    if has_validate_frame:
        assert "_validate" in s.top()[0], s.top()
        s.step("stepOut")
    assert s.top()[0].endswith("validate_python"), s.top()
    s.step("stepOut", "step out, native to Python")
    assert s.top() == ("main", s.program, s.line("callback"))
    s.finish()


def test_pydantic_core_uncaught_validation_error(dap):
    built("pydantic_core")
    program = target("project_pydantic.py")
    dap.launch(program, PYTHON, args=["error"], exceptions=["uncaught"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "ValidationError", stop
    frame = dap.stack(stop["threadId"])[0]
    assert (frame["name"], frame["line"]) == ("main", marker_line(program, "error"))
    dap.cont()
    assert dap.wait_exit() == 1


def test_msgpack_step_in_and_callback_stack(dap):
    built("msgpack")
    c_file = source("msgpack", "msgpack", "_cmsgpack.c")
    s = Session(dap, target("project_msgpack.py"), ["pack", "encode-body"])
    s.first_stop()
    s.step("stepIn", "step in, Python to native (first)")
    name, path, _ = s.top()
    # Cython's implementation function, not its argument-parsing wrapper (__pyx_pw_).
    assert name.startswith("__pyx_pf_") and name.endswith("Packer_6pack"), name
    assert path == c_file and s.names()[1:] == ["main", "<module>"]
    s.step("stepOut", "step out, native to Python")
    assert s.top() == ("main", s.program, s.line("pack"))
    s.cont("continue to the Python callback")
    assert s.top() == ("encode", s.program, s.line("encode-body"))
    native = [f for f in s.stack if f.get("source", {}).get("path") == c_file]
    assert native and s.names()[-2:] == ["main", "<module>"], s.names()
    s.finish()


def test_msgpack_exception_raised_by_the_library(dap):
    built("msgpack")
    program = target("project_msgpack.py")
    dap.launch(program, PYTHON, args=["error"], exceptions=["raised", "uncaught"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "FormatError", stop
    assert dap.request("exceptionInfo", {"threadId": stop["threadId"]})["breakMode"] == "always"
    frame = dap.stack(stop["threadId"])[0]
    assert (frame["name"], frame["line"]) == ("main", marker_line(program, "error"))
    dap.cont()
    stop = dap.wait_stopped()
    for frame in dap.stack(stop["threadId"]):
        path = frame.get("source", {}).get("path")
        assert path is None or os.path.exists(path), frame
    dap.cont()
    assert dap.wait_exit() == 1


def test_contourpy_step_in_and_cpp_throw(dap):
    built("contourpy")
    s = Session(dap, target("project_contourpy.py"), ["lines"], args=["error"],
                exceptions=["cpp_throw"])
    s.first_stop()
    s.step("stepIn", "step in, Python to native (first)")
    name, path, _ = s.top()
    assert name.startswith("contourpy::BaseContourGenerator"), name
    assert path == source("contourpy", "src", "base_impl.h")
    # Clang inlines pre_lines into lines and puts the entry stop inside it.
    if "::pre_lines(" in name:
        assert "::lines(" in s.names()[1] and s.names()[2:] == ["main", "<module>"]
        s.step("stepOut", "step out of the inline function")
        assert "::lines(" in s.top()[0], s.top()
    else:
        assert "::lines(" in name and s.names()[1:] == ["main", "<module>"], s.names()
    assert dap.scope(s.stack[0]["id"])["level"]["value"] == "0.5"
    s.step("next", "step over a native line")
    s.step("stepOut", "step out, native to Python")
    assert s.top() == ("main", s.program, s.line("lines"))
    dap.set_breakpoints(s.program, [])
    stop = s.cont("continue to the C++ throw")
    assert stop["reason"] == "exception" and "std::invalid_argument" in stop["description"]
    assert any("check_levels" in name for name in s.names()), s.names()
    assert s.names()[-2:] == ["main", "<module>"]
    s.finish(code=1)
