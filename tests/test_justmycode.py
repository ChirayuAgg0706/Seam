"""Stepping keeps to the user's code (`justMyCode`, on by default).

A step never ends in a standard-library or installed-package Python file. It does end in
user code such a library calls. When the stepped function returns into a library, the step
ends where user code runs next: the next line of it the thread runs, or the user's frame
the library returns to. With `justMyCode: false` library code is stepped like any other.
"""
import os
import subprocess
import sys

import pytest

from conftest import CAPI_SRC, at_line, marker_line, target
from test_async import assert_nothing_armed, finish, step, top, user_frames

pytestmark = pytest.mark.smoke

MYCODE = target("mycode.py")
MYNATIVE = target("mynative.py")
UNIT_CASES = target("unit_cases.py")
PYTEST_CASES = target("cases_for_pytest.py")
WHEELS = target("wheels.py")


def m(marker):
    return marker_line(MYCODE, marker)


def launch(dap, marker, **extra):
    dap.launch(MYCODE, dap.python, breakpoints={MYCODE: [m(marker)]}, **extra)
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    return stop["threadId"]


def library_file(python, module):
    """Where the interpreter under test keeps a standard-library module's source."""
    out = subprocess.run([python, "-c", "import %s; print(%s.__file__)" % (module, module)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return os.path.realpath(out)


def names(stack):
    return [f["name"] for f in stack]


# ------------------------------------------------------- the standard library

def test_step_in_does_not_enter_library_code(dap, iteration):
    tid = launch(dap, "main-library")
    # string.capwords is Python code, but not the user's: step in acts like step over.
    assert step(dap, tid, "stepIn") == ("main", m("main-callback"))
    assert dap.scope(dap.stack(tid)[0]["id"])["title"]["value"] == "'Just My Code'"
    # Step over a library call that calls the user's function back does not stop in it.
    assert step(dap, tid) == ("main", m("main-callback") + 1)
    finish(dap, MYCODE)


def test_with_just_my_code_off_step_in_enters_library_code(dap, iteration):
    tid = launch(dap, "main-library", justMyCode=False)
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    frame = dap.stack(tid)[0]
    assert frame["name"] == "capwords"
    assert frame["source"]["path"] == library_file(dap.python, "string")
    assert step(dap, tid, "stepOut") == ("main", m("main-library"))
    finish(dap, MYCODE)


def test_step_in_lands_in_a_callback_called_by_a_library(dap, iteration):
    tid = launch(dap, "main-callback")
    dap.set_breakpoints(MYCODE, [])
    assert step(dap, tid, "stepIn") == ("weight", m("weight-body"))
    stack = dap.stack(tid)
    # The library's frame is on the stack and is shown; it is just not stepped through.
    assert names(stack)[:3] == ["weight", "nlargest", "main"]
    assert user_frames(stack, MYCODE) == [
        ("weight", m("weight-body")), ("main", m("main-callback")),
        ("<module>", m("module-call"))]
    assert dap.scope(stack[0]["id"])["item"]["value"] == "'pear'"

    # Out of the callback means into heapq, which is not a place to stop. The step ends
    # where user code runs next: here heapq calls the callback again.
    assert step(dap, tid, "stepOut") == ("weight", m("weight-body"))
    assert dap.scope(dap.stack(tid)[0]["id"])["item"]["value"] == "'fig'"
    # The same over the callback's last line.
    assert step(dap, tid) == ("weight", m("weight-body"))
    assert dap.scope(dap.stack(tid)[0]["id"])["item"]["value"] == "'banana'"
    # After the last call heapq returns to the user's frame: the step ends there, on the
    # calling line.
    assert step(dap, tid, "stepOut") == ("main", m("main-callback"))
    assert step(dap, tid) == ("main", m("main-callback") + 1)
    assert dap.scope(dap.stack(tid)[0]["id"])["longest"]["value"] == "['banana', 'pear']"
    finish(dap, MYCODE)


def test_step_out_of_a_callback_called_by_a_builtin_ends_in_its_caller(dap, iteration):
    tid = launch(dap, "main-sorted")
    dap.set_breakpoints(MYCODE, [])
    assert step(dap, tid, "stepIn") == ("weight", m("weight-body"))
    # sorted() is native: the callback's Python caller is the user's own frame, so the
    # step ends there once sorted() is done, as it always has.
    assert step(dap, tid, "stepOut") == ("main", m("main-sorted"))
    assert step(dap, tid) == ("main", m("main-return"))
    finish(dap, MYCODE)


def test_step_through_a_context_manager_made_by_contextlib(dap, iteration):
    tid = launch(dap, "main-with")
    dap.set_breakpoints(MYCODE, [])
    # contextlib's wrapper and its __enter__ are passed through.
    assert step(dap, tid, "stepIn") == ("managed", m("managed-enter"))
    assert names(dap.stack(tid))[:3] == [
        "managed", "_GeneratorContextManager.__enter__", "main"]
    assert step(dap, tid) == ("managed", m("managed-yield"))
    # Over the yield: the `with` body runs unseen, and the step ends in the generator
    # when contextlib's __exit__ resumes it.
    assert step(dap, tid) == ("managed", m("managed-exit"))
    assert dap.scope(dap.stack(tid)[0]["id"])["log"]["value"] == "['enter', 'body']"
    # Off its end, through __exit__, back on the user's `with` line.
    assert step(dap, tid) == ("main", m("main-with"))
    assert step(dap, tid) == ("main", m("main-dataclass"))
    finish(dap, MYCODE)


def test_step_in_at_the_yield_of_a_context_manager_goes_to_the_with_body(dap, iteration):
    tid = launch(dap, "managed-yield")
    dap.set_breakpoints(MYCODE, [])
    assert step(dap, tid, "stepIn") == ("main", m("main-body"))
    assert step(dap, tid, "stepIn") == ("main", m("main-with"))
    assert step(dap, tid, "stepIn") == ("managed", m("managed-exit"))
    finish(dap, MYCODE)


def test_step_in_skips_code_generated_from_a_string(dap, iteration):
    tid = launch(dap, "main-dataclass")
    # A dataclass's __init__ is compiled from a string by the dataclasses module: there
    # is no source to show, and it is not something the user wrote.
    assert step(dap, tid, "stepIn") == ("main", m("main-sorted"))
    finish(dap, MYCODE)


def test_with_just_my_code_off_step_in_enters_generated_code(dap, iteration):
    tid = launch(dap, "main-dataclass", justMyCode=False)
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    frame = dap.stack(tid)[0]
    assert "__init__" in frame["name"] and "source" not in frame
    assert step(dap, tid, "stepOut") == ("main", m("main-dataclass"))
    finish(dap, MYCODE)


def test_a_breakpoint_set_in_a_library_file_still_stops(dap, iteration):
    path = library_file(dap.python, "string")
    with open(path) as fh:
        found = [number for number, text in enumerate(fh, 1)
                 if "return (sep or ' ').join(" in text]
    assert len(found) == 1, "string.capwords looks different in this version: %s" % path
    dap.launch(MYCODE, dap.python, breakpoints={path: found})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    tid = stop["threadId"]
    frame = dap.stack(tid)[0]
    assert (frame["name"], frame["line"], frame["source"]["path"]) == (
        "capwords", found[0], path)
    # A step from there does not stay in the library: it ends where the library returns
    # to user code.
    assert step(dap, tid) == ("main", m("main-library"))
    assert step(dap, tid) == ("main", m("main-callback"))
    finish(dap, path)


def test_a_program_given_on_the_command_line_can_be_stepped(dap):
    code = "a = 1\nb = a + 1\nprint('sum', a + b)\n"
    dap.launch(MYCODE, dap.python, pythonArgs=["-c", code], stopOnEntry=True)
    tid = dap.wait_stopped()["threadId"]
    # Compiled from a string, but by nobody else's code: this is the program.
    assert top(dap, tid) == ("<module>", 1)
    for line in (2, 3):
        assert dap.step("next", tid)["reason"] == "step"
        assert top(dap, tid) == ("<module>", line)
    assert_nothing_armed(dap)
    dap.cont()
    assert dap.wait_exit() == 0
    assert "sum 3" in dap.output


# ------------------------------------------------------------- test runners

def test_stepping_off_the_end_of_a_test_goes_to_the_next_user_code(dap, iteration):
    line = marker_line(UNIT_CASES, "first-assert")
    dap.launch(UNIT_CASES, dap.python, module="unittest", args=["unit_cases"],
               breakpoints={UNIT_CASES: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(UNIT_CASES, [])
    stack = dap.stack(tid)
    # Every frame below the test is the runner's: there is no user frame to return to.
    assert user_frames(stack, UNIT_CASES) == [("Cases.test_first", line)]
    assert len(stack) > 5
    assert step(dap, tid) == ("Cases.setUp", marker_line(UNIT_CASES, "cases-setup"))
    assert step(dap, tid, "stepOut") == (
        "Cases.test_second", marker_line(UNIT_CASES, "second-body"))
    finish(dap, UNIT_CASES)
    assert "Ran 2 tests" in dap.output and "OK" in dap.output


def test_stop_on_entry_is_the_first_line_of_user_code(dap):
    dap.launch(UNIT_CASES, dap.python, module="unittest", args=["unit_cases"],
               stopOnEntry=True)
    stop = dap.wait_stopped()
    frame = dap.stack(stop["threadId"])[0]
    assert (frame["name"], frame["source"]["path"]) == ("<module>", UNIT_CASES)
    dap.cont()
    assert dap.wait_exit() == 0


def test_with_just_my_code_off_stop_on_entry_is_the_first_python_line(dap):
    dap.launch(UNIT_CASES, dap.python, module="unittest", args=["unit_cases"],
               stopOnEntry=True, justMyCode=False)
    stop = dap.wait_stopped()
    frame = dap.stack(stop["threadId"])[0]
    # Whatever runs first, long before the user's module is imported.
    assert frame.get("source", {}).get("path") != UNIT_CASES
    dap.cont()
    assert dap.wait_exit() == 0


def test_stepping_through_tests_run_by_pytest(dap, iteration):
    """pytest is an installed package. The interpreter debugged here is the one running
    this suite: it is the one that is sure to have pytest."""
    line = marker_line(PYTEST_CASES, "first-assert")
    dap.launch(PYTEST_CASES, sys.executable, module="pytest",
               args=["-q", "--noconftest", "-p", "no:cacheprovider", "cases_for_pytest.py"],
               breakpoints={PYTEST_CASES: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(PYTEST_CASES, [])
    stack = dap.stack(tid)
    assert user_frames(stack, PYTEST_CASES) == [("test_first", line)]
    assert any("site-packages" in f.get("source", {}).get("path", "") for f in stack)

    def at(marker):
        return marker_line(PYTEST_CASES, marker)

    # Off the end of the test: pytest finishes the fixture, then sets it up again for
    # the next test. Never a stop in pytest's or pluggy's files.
    assert step(dap, tid) == ("base", at("fixture-teardown"))
    assert step(dap, tid) == ("base", at("fixture-body"))
    assert step(dap, tid) == ("base", at("fixture-yield"))
    # The value goes to pytest; the next user code is the test that asked for it.
    assert step(dap, tid, "stepIn") == ("test_second", at("second-body"))
    finish(dap, PYTEST_CASES)
    assert "2 passed" in dap.output


# ------------------------------------------------- installed packages, native code

def test_step_in_skips_an_installed_package(dap, wheels_python, iteration):
    line = marker_line(WHEELS, "numpy-call")
    body = marker_line(WHEELS, "scale-body")
    dap.launch(WHEELS, wheels_python, args=["callbacks"], breakpoints={WHEELS: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(WHEELS, [])
    # np.vectorize is Python code in site-packages. The first user code the line reaches
    # is the function given to it.
    assert step(dap, tid, "stepIn") == ("scale", body)
    stack = dap.stack(tid)
    assert any("site-packages" in f.get("source", {}).get("path", "") for f in stack)
    assert user_frames(stack, WHEELS)[1][0] == "use_numpy"
    # Out of it, into numpy's Python code: the step ends at numpy's next call of it.
    assert step(dap, tid, "stepOut") == ("scale", body)
    finish(dap, WHEELS)


def test_with_just_my_code_off_step_in_enters_an_installed_package(dap, wheels_python):
    line = marker_line(WHEELS, "numpy-call")
    dap.launch(WHEELS, wheels_python, args=["callbacks"], breakpoints={WHEELS: [line]},
               justMyCode=False)
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(WHEELS, [])
    assert dap.step("stepIn", tid)["reason"] == "step"
    assert "site-packages" in dap.stack(tid)[0]["source"]["path"]
    finish(dap, WHEELS)


def test_step_in_reaches_native_user_code_through_a_library(dap, capi, iteration):
    line = marker_line(MYNATIVE, "main-native-key")
    dap.launch(MYNATIVE, dap.python, env=capi.env, breakpoints={MYNATIVE: [line]})
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(MYNATIVE, [])
    # heapq's Python code calls the user's native function. Its lines are not a place to
    # stop, so the first user code this line reaches is native.
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert stack[0]["name"] == "st_add" and stack[0]["source"]["path"] == CAPI_SRC
    assert at_line(capi, stack[0]["line"], marker_line(CAPI_SRC, "add-first"))
    assert "nlargest" in names(stack)
    assert user_frames(stack, MYNATIVE)[0] == ("main", line)
    assert_nothing_armed(dap)
    # Out of the native function: the Python frame it returns to is heapq's, so the step
    # carries on to the user's frame.
    stop = dap.step("stepOut", tid)
    assert stop["reason"] == "step"
    assert top(dap, tid) == ("main", line)
    assert step(dap, tid) == ("main", marker_line(MYNATIVE, "main-return"))
    assert dap.scope(dap.stack(tid)[0]["id"])["top"]["value"] == "[3, 2]"
    finish(dap, MYNATIVE)
