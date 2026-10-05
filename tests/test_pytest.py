"""Debugging a pytest run: the launch configuration `"module": "pytest"`.

The project under test is tests/targets/pytest_project. pytest rewrites the assertions of
test modules, captures output at the file-descriptor level, enables faulthandler and,
with pytest-xdist, runs the tests in child processes; the scenarios check that Seam's
breakpoints, stacks, stepping, crash stops, logpoints and exit codes hold up under that.
"""
import ast
import os
import signal

import pytest

from conftest import CAPI_SRC, _run, at_line, marker_line, target
from dapclient import Terminal
from test_wheels import python_part

pytestmark = pytest.mark.smoke

PROJECT = target("pytest_project")
TESTS = os.path.join(PROJECT, "test_calc.py")
FIXTURES = os.path.join(PROJECT, "conftest.py")
SOUND = "not fails and not crashes"  # the tests of the project that are meant to pass


@pytest.fixture(scope="session")
def pytest_python(wheels_python):
    """The interpreter under test, in an environment that has pytest and pytest-xdist.

    It is the environment of the wheel scenarios with the two packages added. They have
    a ready-marker of their own, so an environment made before this fixture existed
    gets them too.
    """
    ready = os.path.join(os.path.dirname(os.path.dirname(wheels_python)), ".seam-pytest-ready")
    if not os.path.exists(ready):
        _run(["uv", "pip", "install", "--quiet", "--python", wheels_python,
              "pytest>=8", "pytest-xdist"])
        with open(ready, "w"):
            pass
    return wheels_python


@pytest.fixture
def run_pytest(dap, capi, pytest_python):
    """Launch `python -m pytest <args>` in the project under Seam."""
    def launch(*args, **options):
        dap.launch(TESTS, pytest_python, module="pytest", args=list(args), cwd=PROJECT,
                   env=capi.env, **options)
    return launch


TRUTH = ("[(f.name, f.lineno) for f in __import__('traceback').extract_stack() "
         "if f.filename.endswith('.py') and 'seam' not in f.filename.rsplit('/', 1)[-1]][%d:%d]")


def python_truth(dap, frame_id):
    """The Python stack as Python itself reports it, newest first, Seam's frames removed.

    Only frames from .py files, like `python_part` (runpy's two at the bottom are frozen).
    Under pytest the stack is some forty frames deep, more than fits in one value as
    Seam shows it, so it is fetched a page at a time.
    """
    frames = []
    while True:
        page = ast.literal_eval(
            dap.evaluate(TRUTH % (len(frames), len(frames) + 15), frame_id)["result"])
        frames += page
        if len(page) < 15:
            return frames[::-1]


def names(stack):
    return [f["name"] for f in stack]


def run_to_exit(dap):
    """Continue through every stop; returns the stops' top frames and the exit code."""
    stops = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"], timeout=90)
        if name == "exited":
            return stops, body["exitCode"]
        frame = dap.stack(body["threadId"])[0]
        stops.append((body["reason"], frame["name"], frame["line"]))
        assert len(stops) < 40, (stops, dap.output)
        dap.cont()


def test_breakpoint_in_a_test_function(dap, run_pytest):
    first = marker_line(TESTS, "addition-total")
    after = marker_line(TESTS, "addition-after")
    run_pytest("-q", "test_calc.py::test_addition", breakpoints={TESTS: [first, after]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == ("test_addition", first)
    assert stack[0]["source"]["path"] == TESTS
    # Below the test are pytest's and pluggy's own frames; the whole stack is what
    # Python itself reports, and ends in `python -m pytest`.
    assert python_part(stack) == python_truth(dap, stack[0]["id"])
    assert len(python_part(stack)) > 20
    assert "pytest_pyfunc_call" in names(stack) and names(stack)[-1] == "_run_module_as_main"
    libraries = [f for f in stack if "/site-packages/" in
                 f.get("source", {}).get("path", "")]
    assert libraries
    assert all(f.get("presentationHint") == "subtle" and
               f["source"].get("presentationHint") == "deemphasize" and
               f["source"].get("origin") for f in libraries)
    local = dap.scope(stack[0]["id"])
    assert (local["a"]["value"], local["b"]["value"]) == ("2", "3") and "total" not in local
    assert dap.evaluate("a * b", stack[0]["id"])["result"] == "6"

    # pytest has rewritten the asserts of this module; the lines are still the file's.
    assert dap.step("next", tid)["reason"] == "step"
    assert python_part(dap.stack(tid))[0] == ("test_addition",
                                              marker_line(TESTS, "addition-assert"))
    dap.cont()
    stop = dap.wait_stopped()
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == ("test_addition", after)
    assert dap.scope(stack[0]["id"])["total"]["value"] == "5"
    dap.cont()
    assert dap.wait_exit() == 0
    assert "1 passed" in dap.output


def test_step_from_a_test_into_native_code_and_back(dap, run_pytest, capi):
    call = marker_line(TESTS, "native-call")
    run_pytest("-q", "test_calc.py::test_native", breakpoints={TESTS: [call]})
    tid = dap.wait_stopped()["threadId"]
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert stack[0]["name"] == "st_add" and stack[0]["source"]["path"] == CAPI_SRC
    assert (stack[1]["name"], stack[1]["line"]) == ("test_native", call)
    assert "pytest_pyfunc_call" in names(stack)
    # The test's fixture value, read from memory at this native stop.
    assert dap.scope(stack[1]["id"])["numbers"]["value"] == "[1, 2, 3]"

    stop = dap.step("stepOut", tid)
    assert python_part(dap.stack(tid))[0] == ("test_native", call)
    stop = dap.step("next", tid)
    stack = dap.stack(tid)
    assert python_part(stack)[0] == ("test_native", marker_line(TESTS, "native-after"))
    assert dap.scope(stack[0]["id"])["total"]["value"] == "3"
    dap.set_breakpoints(TESTS, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_native_breakpoint_under_a_test(dap, run_pytest, capi):
    line = marker_line(CAPI_SRC, "add-impl-return")
    run_pytest("-q", "test_calc.py::test_native", breakpoints={CAPI_SRC: [line]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert names(stack)[:3] == ["add_impl", "st_add", "test_native"]
    assert at_line(capi, stack[0]["line"], line)
    assert stack[2]["line"] == marker_line(TESTS, "native-call")
    if capi.opt == "O0":
        assert dap.scope(stack[0]["id"])["sum"]["value"] == "3"
    dap.cont()
    assert dap.wait_exit() == 0


def test_segfault_in_an_extension_during_a_test(dap, run_pytest, capi):
    run_pytest("-q", "test_calc.py::test_crashes")
    # pytest has faulthandler's SIGSEGV handler installed; the debugger still stops first.
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGSEGV" in stop["description"]
    stack = dap.stack(stop["threadId"])
    assert names(stack)[:2] == ["st_crash", "test_crashes"]
    assert at_line(capi, stack[0]["line"], marker_line(CAPI_SRC, "crash-here"))
    assert stack[1]["line"] == marker_line(TESTS, "crash-call")
    assert "pytest_pyfunc_call" in names(stack) and names(stack)[-1] == "_run_module_as_main"
    dap.cont()
    # Carrying on hands the signal to faulthandler. It writes its report (to the real
    # stderr, past pytest's capture) and raises the signal again, which is a second
    # stop, inside the C library this time; the test is still on the stack below.
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGSEGV" in stop["description"]
    assert "Fatal Python error: Segmentation fault" in dap.plain_output
    again = names(dap.stack(stop["threadId"]))
    assert "st_crash" in again and "test_crashes" in again and again[0] != "st_crash", again
    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGSEGV
    assert "terminated by signal SIGSEGV" in dap.output


def test_fixture_parameters_and_a_test_in_a_class(dap, run_pytest):
    fixture = marker_line(FIXTURES, "fixture-yield")
    teardown = marker_line(FIXTURES, "fixture-teardown")
    param = marker_line(TESTS, "param-body")
    method = marker_line(TESTS, "method-body")
    run_pytest("-q", "-k", "doubling or TestGroup", breakpoints={
        FIXTURES: [fixture, teardown], TESTS: [param, method]})
    seen = []
    values = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"], timeout=60)
        if name == "exited":
            break
        frame = dap.stack(body["threadId"])[0]
        seen.append((frame["name"], frame["line"]))
        local = dap.scope(frame["id"])
        if frame["line"] == param:
            values.append((local["value"]["value"], local["doubled"]["value"]))
        elif frame["line"] == method:
            assert local["self"]["type"] == "TestGroup"
            assert local["numbers"]["value"] == "[1, 2, 3]"
        dap.cont()
    assert seen == [("test_doubling", param)] * 3 + [
        ("numbers", fixture), ("TestGroup.test_method", method), ("numbers", teardown)]
    assert values == [("1", "2"), ("4", "8"), ("10", "20")]
    assert body["exitCode"] == 0 and "4 passed" in dap.output


def test_output_capture_logpoints_and_the_exit_code(dap, run_pytest):
    logpoint = {"line": marker_line(TESTS, "print-line"), "logMessage": "logpoint in a test"}
    run_pytest("-q", "-k", "prints or test_fails", breakpoints={TESTS: [logpoint]})
    stops, code = run_to_exit(dap)
    # A failing run's exit code comes through; so does pytest's report of the failure.
    assert (stops, code) == ([], 1)
    assert "assert 3 == 4" in dap.plain_output and "2 failed, 1 passed" in dap.plain_output
    # pytest captured what the test printed; the logpoint is Seam's and is not captured.
    assert "logpoint in a test\n" in dap.output
    assert "printed by a test" not in dap.output


def test_pytest_in_the_terminal(dap, run_pytest):
    dap.terminal = Terminal()
    line = marker_line(TESTS, "print-line")
    run_pytest("-s", "test_calc.py::test_prints", console="integratedTerminal",
               breakpoints={TESTS: [line]})
    stop = dap.wait_stopped()
    assert python_part(dap.stack(stop["threadId"]))[0] == ("test_prints", line)
    dap.cont()
    assert dap.wait_exit() == 0
    dap.terminal.read_until("printed by a test")
    dap.terminal.read_until("1 passed")
    assert "1 passed" not in dap.output


def test_stop_where_a_test_fails(dap, run_pytest):
    # pytest catches the failure, so "uncaught" never fires; "user_unhandled" stops as
    # the exception leaves the test for pytest.
    run_pytest("-q", "-k", "fails or expected_exceptions or addition",
               exceptions=["uncaught", "user_unhandled"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "AssertionError"
    assert "assert 3 == 4" in stop["description"]
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert python_part(stack)[0] == ("test_fails", marker_line(TESTS, "failing-assert"))
    assert stack[1]["name"] == "pytest_pyfunc_call"
    local = dap.scope(stack[0]["id"])
    assert (local["expected"]["value"], local["actual"]["value"]) == ("4", "3")
    assert dap.evaluate("expected - actual", stack[0]["id"])["result"] == "1"
    info = dap.request("exceptionInfo", {"threadId": tid})
    assert info["exceptionId"] == "AssertionError" and info["breakMode"] == "userUnhandled"
    assert "assert actual == expected" in info["details"]["stackTrace"]

    # The second failure is raised in a helper the test calls: the helper's frame has
    # unwound by the time the exception leaves the test, and is shown on top.
    dap.cont()
    stop = dap.wait_stopped()
    assert "helper says no" in stop["description"]
    stack = dap.stack(tid)
    assert python_part(stack)[:2] == [
        ("helper_that_checks", marker_line(TESTS, "helper-assert")),
        ("test_fails_in_a_helper", marker_line(TESTS, "helper-call"))]
    assert stack[2]["name"] == "pytest_pyfunc_call"
    local = dap.scope(stack[0]["id"])
    assert (local["actual"]["value"], local["expected"]["value"]) == ("3", "4")

    # Nothing else stops: not the exception `pytest.raises` expects, not the one caught
    # in the test, not the "skip" that pytest raises through the test.
    dap.cont()
    name, body = dap.wait_any(["stopped", "exited"], timeout=60)
    assert name == "exited" and body["exitCode"] == 1, (body, dap.output)
    assert "2 failed, 1 passed, 1 skipped" in dap.plain_output, dap.output


def test_xdist_workers_are_child_processes(dap, run_pytest):
    lines = [marker_line(TESTS, m) for m in ("addition-total", "param-body", "method-body")]
    run_pytest("-q", "-n", "2", "-k", SOUND, breakpoints={
        TESTS: lines, CAPI_SRC: [marker_line(CAPI_SRC, "add-impl-return")]})
    stops, code = run_to_exit(dap)
    # The tests ran in the workers, which are not debugged: nothing stops, nothing hangs.
    assert (stops, code) == ([], 0), dap.output
    assert "7 passed, 1 skipped" in dap.plain_output, dap.output
    assert dap.output.count("Seam: the program started a child process") == 1, dap.output
