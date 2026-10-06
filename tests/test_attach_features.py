"""Exception stops, function breakpoints, logpoints and crashes on an attached process.

These were built and tested on launched programs; a process Seam attached to has the
helper loaded late and by another route, and must behave the same.
"""
import os
import signal
import subprocess

import pytest

from conftest import CAPI_SRC, marker_line, target

pytestmark = pytest.mark.smoke

SERVER = target("attach_features.py")


@pytest.fixture
def server(python, capi):
    proc = subprocess.Popen([python, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=dict(os.environ, **capi.env))
    assert proc.stdout.readline().strip() == "ready"
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def tell(proc, command):
    proc.stdin.write(command + "\n")
    proc.stdin.flush()


def answer(proc):
    return proc.stdout.readline().strip()


def attach(dap, proc, exceptions=()):
    dap.request("initialize", {"adapterID": "seam"})
    dap.request("attach", {"pid": proc.pid})
    dap.wait_event("initialized")
    dap.request("setExceptionBreakpoints", {"filters": list(exceptions)})
    dap.request("configurationDone")


def names(stack):
    return [f["name"] for f in stack]


def test_exception_breakpoints_on_an_attached_process(dap, server, iteration):
    attach(dap, server, exceptions=["raised"])
    tell(server, "caught 3")
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    assert stop["description"] == "ValueError: bad value 3"
    stack = dap.stack(stop["threadId"])
    assert names(stack) == ["fail", "caught", "main", "<module>"]
    assert stack[0]["line"] == marker_line(SERVER, "fail-raise")
    info = dap.request("exceptionInfo", {"threadId": stop["threadId"]})
    assert info["exceptionId"] == "ValueError" and info["description"] == "bad value 3"
    assert dap.evaluate("n + 1", stack[0]["id"])["result"] == "4"
    dap.cont()
    assert answer(server) == "caught 3"

    # The filters are changed while the program runs. Uncaught, on a thread: the thread
    # ends, the program carries on.
    dap.request("setExceptionBreakpoints", {"filters": ["uncaught"]})
    tell(server, "caught 4")
    assert answer(server) == "caught 4"       # caught exceptions no longer stop
    tell(server, "thread 5")
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["text"] == "ValueError"
    stack = dap.stack(stop["threadId"])
    assert stack[0]["name"] == "fail" and "Thread.run" in names(stack)
    dap.cont()
    assert answer(server) == "thread done"

    # Uncaught, on the main thread: the program ends as it would have.
    tell(server, "raise 6")
    stop = dap.wait_stopped()
    assert stop["description"] == "ValueError: bad value 6"
    assert names(dap.stack(stop["threadId"])) == ["fail", "main", "<module>"]
    dap.cont()
    assert dap.wait_exit() == 1
    assert server.wait(timeout=10) == 1


def test_detaching_takes_the_exception_hooks_out_again(dap, server):
    attach(dap, server, exceptions=["uncaught", "raised"])
    tell(server, "caught 1")
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    dap.cont()
    assert answer(server) == "caught 1"
    dap.close()  # detaches

    # On its own again: exceptions are handled the way the program handles them.
    tell(server, "caught 2")
    assert answer(server) == "caught 2"
    tell(server, "thread 3")
    assert answer(server) == "thread done"
    tell(server, "quit")
    assert answer(server) == "stopped"
    assert server.wait(timeout=10) == 0


def test_function_breakpoint_logpoint_and_set_variable_on_an_attached_process(dap, server, capi):
    attach(dap, server)
    reply = dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "work"}]})
    assert [b["verified"] for b in reply["breakpoints"]] == [True]
    tell(server, "work 5")
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    frame = dap.stack(stop["threadId"])[0]
    assert frame["name"] == "work"

    # Change the argument, swap the function breakpoint for a logpoint and a native
    # breakpoint that stops on its second hit.
    scopes = dap.request("scopes", {"frameId": frame["id"]})["scopes"]
    local_ref = [s for s in scopes if s["name"] == "Locals"][0]["variablesReference"]
    dap.request("setVariable", {"variablesReference": local_ref, "name": "n", "value": "40"})
    dap.request("setFunctionBreakpoints", {"breakpoints": []})
    dap.set_breakpoints(SERVER, [{"line": marker_line(SERVER, "work-body"),
                                  "logMessage": "n is {n}"}])
    dap.set_breakpoints(CAPI_SRC, [{"line": marker_line(CAPI_SRC, "add-impl-return"),
                                    "hitCondition": "2"}])
    dap.cont()
    assert answer(server) == "work 41"

    tell(server, "work 1")
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert names(stack) == ["add_impl", "st_add", "work", "main", "<module>"]
    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    assert answer(server) == "work 2"
    logged = [line for line in dap.output.splitlines() if line.startswith("n is")]
    assert logged == ["n is 40", "n is 1"]

    dap.set_breakpoints(SERVER, [])
    dap.close()
    tell(server, "work 7")
    assert answer(server) == "work 8"
    tell(server, "quit")
    assert server.wait(timeout=10) == 0


def test_crash_on_an_attached_process(dap, server):
    attach(dap, server)
    tell(server, "crash")
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGSEGV" in stop["description"]
    assert names(dap.stack(stop["threadId"])) == ["st_crash", "main", "<module>"]
    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGSEGV
    assert server.wait(timeout=10) == -signal.SIGSEGV
