"""Exception breakpoints: uncaught and raised Python exceptions, C++ throw, Rust panic."""
import pytest

from conftest import marker_line, target
from test_python import ground_truth, py_frames

pytestmark = pytest.mark.smoke

EXC = target("exceptions.py")
THROWS = target("throws.py")


def names(stack):
    return [f["name"] for f in stack]


def test_filters_are_advertised(dap, capi):
    dap.launch(EXC, dap.python, args=["none"], env=capi.env)
    filters = {f["filter"]: f for f in dap.capabilities["exceptionBreakpointFilters"]}
    assert set(filters) == {"uncaught", "raised", "user_unhandled", "cpp_throw", "rust_panic"}
    assert filters["uncaught"]["default"] is True and filters["raised"]["default"] is False
    assert dap.wait_exit() == 0


def test_uncaught_exception_shows_the_frames_it_passed_through(dap, capi):
    dap.launch(EXC, dap.python, args=["uncaught"], env=capi.env, exceptions=["uncaught"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    assert stop["description"] == "ValueError: bad value deep-local"
    assert stop["text"] == "ValueError"
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert py_frames(stack) == [
        ("deepest", marker_line(EXC, "raise-here")),
        ("middle", marker_line(EXC, "middle-call")),
        ("<module>", marker_line(EXC, "module-call")),
    ]
    assert all(f["source"]["path"] == EXC for f in stack)

    # The frames have unwound, but their variables are intact and code runs in them.
    deepest = dap.scope(stack[0]["id"])
    assert deepest["detail"]["value"] == "'deep-local'" and deepest["kind"]["value"] == "'value'"
    middle = dap.scope(stack[1]["id"])
    children = dap.variables(middle["items"]["variablesReference"])
    assert [children["[%d]" % i]["value"] for i in range(3)] == ["1", "2", "3"]
    assert dap.evaluate("detail.upper()", stack[0]["id"])["result"] == "'DEEP-LOCAL'"
    assert dap.evaluate("sum(items)", stack[1]["id"])["result"] == "6"
    assert "mode" in dap.scope(stack[2]["id"], "Globals")

    info = dap.request("exceptionInfo", {"threadId": tid})
    assert info["exceptionId"] == "ValueError" and info["breakMode"] == "unhandled"
    assert info["description"] == "bad value deep-local"
    assert info["details"]["typeName"] == "ValueError"
    trace = info["details"]["stackTrace"]
    assert trace.startswith("Traceback") and "in deepest" in trace and "raise ValueError" in trace

    # Nothing is left to step through; a step lets the program finish.
    dap.request("next", {"threadId": tid})
    assert dap.wait_exit() == 1
    assert "ValueError: bad value deep-local" in dap.plain_output
    assert "end" not in dap.output


def test_uncaught_exception_is_seen_when_the_program_replaces_the_hook(dap, capi):
    dap.launch(EXC, dap.python, args=["hooked"], env=capi.env, exceptions=["uncaught"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "ValueError"
    assert py_frames(dap.stack(stop["threadId"]))[0] == ("deepest", marker_line(EXC, "raise-here"))
    dap.cont()
    assert dap.wait_exit() == 1
    assert "custom hook: bad value deep-local" in dap.output


def test_uncaught_exception_in_a_thread(dap, capi):
    dap.launch(EXC, dap.python, args=["thread"], env=capi.env, exceptions=["uncaught"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "ValueError"
    threads = dap.request("threads")["threads"]
    assert len(threads) == 2
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack)[:2] == [("deepest", marker_line(EXC, "raise-here")),
                                    ("middle", marker_line(EXC, "middle-call"))]
    assert "Thread.run" in names(stack)
    assert dap.scope(stack[0]["id"])["detail"]["value"] == "'deep-local'"
    # The main thread is waiting in join(), with its real stack.
    other = [t["id"] for t in threads if t["id"] != stop["threadId"]][0]
    assert names(dap.stack(other))[-1] == "<module>"
    dap.cont()
    assert dap.wait_exit() == 0
    assert "thread done" in dap.output and "ValueError: bad value" in dap.plain_output


def test_raised_exceptions_stop_once_where_they_reach_user_code(dap, capi):
    dap.launch(EXC, dap.python, args=["handled"], env=capi.env, exceptions=["raised"])

    # Raised in user code: stop on the raise, with the live stack.
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "ValueError"
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert py_frames(stack) == [
        ("deepest", marker_line(EXC, "raise-here")),
        ("middle", marker_line(EXC, "middle-call")),
        ("handled", marker_line(EXC, "handled-call")),
        ("<module>", marker_line(EXC, "handled-print")),
    ]
    assert py_frames(stack) == ground_truth(dap, stack[0]["id"])
    assert dap.scope(stack[0]["id"])["detail"]["value"] == "'deep-local'"
    info = dap.request("exceptionInfo", {"threadId": tid})
    assert info["exceptionId"] == "ValueError" and info["breakMode"] == "always"

    # Raised inside a library: stop where it arrives in user code, not inside json.
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "JSONDecodeError"
    stack = dap.stack(tid)
    assert py_frames(stack)[0] == ("from_library", marker_line(EXC, "library-call"))
    info = dap.request("exceptionInfo", {"threadId": tid})
    assert info["exceptionId"] == "json.decoder.JSONDecodeError"

    # Raised by native code: stop on the Python line that called it.
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["description"] == "ValueError: native failure"
    stack = dap.stack(tid)
    assert py_frames(stack)[0] == ("from_native", marker_line(EXC, "native-call"))

    # Switching the filter off leaves nothing armed in the program.
    dap.request("setExceptionBreakpoints", {"filters": []})
    status = dap.status()["agent"]
    assert status["exception_filters"] == [] and status["global_events"] == 0
    dap.cont()
    assert dap.wait_exit() == 0
    for text in ("bad value deep-local", "library handled", "native handled", "end"):
        assert text in dap.output


def test_no_exception_stops_unless_asked_for(dap, capi):
    dap.launch(EXC, dap.python, args=["handled"], env=capi.env, exceptions=["uncaught"])
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 0, body


def test_sys_exit_is_not_an_uncaught_exception(dap, capi):
    dap.launch(EXC, dap.python, args=["exit"], env=capi.env, exceptions=["uncaught"])
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 4, body


def test_stop_on_cpp_throw(dap, build_layer):
    ext = build_layer("pybind11")
    dap.launch(THROWS, dap.python, args=[ext.module], env=ext.env, exceptions=["cpp_throw"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    assert stop["description"] == "C++ exception thrown: std::runtime_error", dap.tail_log()
    tid = stop["threadId"]
    stack = dap.stack(tid)
    position = names(stack).index("call_native")
    assert stack[position]["line"] == marker_line(THROWS, "throw-call")
    assert names(stack)[position + 1] == "<module>"
    if ext.opt == "O0":
        thrower = [f for f in stack[:position] if f.get("source", {}).get("path") == ext.source]
        assert thrower and thrower[0]["line"] == marker_line(ext.source, "throw-here")
        assert "fail" in thrower[0]["name"]
    assert dap.scope(stack[position]["id"])["reason"]["value"] == "'boom'"
    info = dap.request("exceptionInfo", {"threadId": tid})
    assert info["exceptionId"] == "std::runtime_error"
    dap.request("next", {"threadId": tid})  # control leaves by unwinding: this continues
    assert dap.wait_exit() == 0
    assert "caught RuntimeError" in dap.output


def test_stop_on_rust_panic(dap, build_layer):
    ext = build_layer("pyo3")
    dap.launch(THROWS, dap.python, args=[ext.module], env=ext.env, exceptions=["rust_panic"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["description"] == "Rust panic"
    stack = dap.stack(stop["threadId"])
    position = names(stack).index("call_native")
    assert stack[position]["line"] == marker_line(THROWS, "throw-call")
    if ext.opt == "O0":
        thrower = [f for f in stack[:position] if f.get("source", {}).get("path") == ext.source]
        assert thrower and thrower[0]["line"] == marker_line(ext.source, "panic-here")
    dap.cont()
    assert dap.wait_exit() == 0
    assert "caught PanicException" in dap.output
