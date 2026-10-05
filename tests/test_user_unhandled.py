"""The "user-unhandled" exception filter: stop when an exception leaves the user's code
for library code that called it (a callback, a thread's target, a test under pytest)."""
import pytest

from conftest import marker_line, target
from test_python import py_frames

pytestmark = pytest.mark.smoke

UNHANDLED = target("unhandled.py")
FILTER = ["user_unhandled"]


def test_exception_leaving_a_callback_for_the_library_that_called_it(dap):
    dap.launch(UNHANDLED, dap.python, args=["callback"], exceptions=FILTER)
    advertised = {f["filter"]: f for f in dap.capabilities["exceptionBreakpointFilters"]}
    assert advertised["user_unhandled"]["default"] is False

    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    assert stop["description"] == "ValueError: cannot replace bad"
    tid = stop["threadId"]
    stack = dap.stack(tid)
    # The callback is on top, at the line that raised; below it the library that called
    # it, and below that the user's own callers.
    assert py_frames(stack)[0] == ("check", marker_line(UNHANDLED, "callback-raise"))
    assert stack[1]["name"] == "sub" and stack[1]["source"]["path"].endswith("re/__init__.py")
    assert py_frames(stack)[-3:-1] == [("replace", marker_line(UNHANDLED, "library-call")),
                                       ("main", marker_line(UNHANDLED, "module-call"))]
    assert dap.scope(stack[0]["id"])["word"]["value"] == "'bad'"
    assert dap.evaluate("match.group(0) + '!'", stack[0]["id"])["result"] == "'bad!'"
    info = dap.request("exceptionInfo", {"threadId": tid})
    assert info["exceptionId"] == "ValueError" and info["breakMode"] == "userUnhandled"
    assert info["description"] == "cannot replace bad"
    assert "raise ValueError" in info["details"]["stackTrace"]

    # The user's own handler further out catches it; that is no second stop. The next
    # exception was raised one call deeper, in a frame that has unwound by the time the
    # exception leaves the callback: it is shown on top all the same, with its variables.
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["description"] == "ValueError: cannot replace worse"
    stack = dap.stack(tid)
    assert py_frames(stack)[:2] == [("explode", marker_line(UNHANDLED, "explode-raise")),
                                    ("check", marker_line(UNHANDLED, "callback-call"))]
    assert stack[2]["name"] == "sub"
    assert dap.scope(stack[0]["id"])["detail"]["value"] == "'cannot replace worse'"
    assert dap.scope(stack[1]["id"])["word"]["value"] == "'worse'"
    assert dap.evaluate("word.upper()", stack[1]["id"])["result"] == "'WORSE'"

    # The frame is on its way out, so there is nothing to step through: a step carries on.
    dap.request("next", {"threadId": tid})
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 0, body
    for text in ("caught: cannot replace bad", "caught: cannot replace worse", "end"):
        assert text in dap.output


def test_exceptions_that_stay_in_user_code_or_in_libraries_do_not_stop(dap):
    dap.launch(UNHANDLED, dap.python, args=["quiet"], exceptions=FILTER)
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 0, (body, dap.output)
    assert "quiet: False [1, 0, 2, 1, 0]" in dap.output and "end" in dap.output


def test_stepping_follows_an_exception_as_before_with_the_filter_on(dap):
    # The filter listens to the same event a step uses to follow an exception upwards.
    call = marker_line(UNHANDLED, "quiet-call")
    dap.launch(UNHANDLED, dap.python, args=["quiet"], exceptions=FILTER,
               breakpoints={UNHANDLED: [call]})
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(UNHANDLED, [])
    assert dap.step("stepIn", tid)["reason"] == "step"
    assert py_frames(dap.stack(tid))[0] == ("lookup", marker_line(UNHANDLED, "lookup-raise"))
    # Stepping over the line that raises lands in the caller's handler.
    assert dap.step("next", tid)["reason"] == "step"
    assert py_frames(dap.stack(tid))[0] in [
        ("quiet", marker_line(UNHANDLED, m)) for m in ("quiet-except", "quiet-pass")]
    dap.cont()
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 0, body


def test_exception_leaving_a_threads_target(dap):
    dap.launch(UNHANDLED, dap.python, args=["thread"], exceptions=FILTER)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["description"] == "KeyError: 'missing'"
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack)[0] == ("lookup", marker_line(UNHANDLED, "lookup-raise"))
    assert stack[1]["name"] == "Thread.run"
    dap.cont()
    assert dap.wait_exit() == 0
    assert "KeyError: 'missing'" in dap.plain_output and "end" in dap.output


@pytest.mark.parametrize("mode, code", [("uncaught", 1), ("exit", 3)])
def test_an_exception_nobody_catches_is_the_uncaught_filters_business(dap, mode, code):
    dap.launch(UNHANDLED, dap.python, args=[mode], exceptions=["user_unhandled", "uncaught"])
    if mode == "uncaught":
        # One stop, not two: the exception never passes into library code.
        stop = dap.wait_stopped()
        info = dap.request("exceptionInfo", {"threadId": stop["threadId"]})
        assert (info["exceptionId"], info["breakMode"]) == ("KeyError", "unhandled")
        dap.cont()
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == code, body


def test_switching_the_filter_off_leaves_nothing_armed(dap):
    dap.launch(UNHANDLED, dap.python, args=["callback"], exceptions=FILTER)
    dap.wait_stopped()
    assert dap.status()["agent"]["exception_filters"] == ["user_unhandled"]
    dap.request("setExceptionBreakpoints", {"filters": []})
    status = dap.status()["agent"]
    assert status["exception_filters"] == [] and status["global_events"] == 0
    dap.cont()
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 0, body
