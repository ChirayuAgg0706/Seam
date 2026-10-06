"""The program crashes, is signalled, or ends: Seam reports it and leaves nothing behind."""
import os
import signal
import time

import pytest

from conftest import CAPI_SRC, at_line, marker_line, target

CRASH = target("crash.py")
SIGNALS = target("signals.py")
EXITS = target("exits.py")


def names(stack):
    return [f["name"] for f in stack]


@pytest.mark.smoke
def test_segfault_in_native_code_stops_with_the_merged_stack(dap, capi):
    dap.launch(CRASH, dap.python, args=["segv"], env=capi.env)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    assert "SIGSEGV" in stop["description"]
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert names(stack) == ["st_crash", "inner", "outer", "<module>"]
    assert at_line(capi, stack[0]["line"], marker_line(CAPI_SRC, "crash-here"))
    assert [f["line"] for f in stack[1:]] == [
        marker_line(CRASH, m) for m in ("inner-segv", "outer-call", "module-call")]

    # The Python side of a crashed process is read from memory only.
    assert dap.scope(stack[1]["id"])["label"]["value"] == "'inner-local'"
    assert dap.scope(stack[2]["id"])["values"]["value"] == "[1, 2, 3]"
    if capi.opt == "O0":
        assert dap.scope(stack[0]["id"])["before"]["value"] == "7"
    refused = dap.evaluate("label", stack[1]["id"], check=False)
    assert not refused["success"] and "native code" in refused["message"]

    info = dap.request("exceptionInfo", {"threadId": tid})
    assert info["exceptionId"] == "SIGSEGV" and info["breakMode"] == "always"

    # A native expression that calls a function still works and does not deliver the
    # pending signal; changing Python breakpoints here is accepted and runs nothing.
    pid = dap.status()["pid"]
    assert dap.evaluate("(int)getpid()", stack[0]["id"])["result"] == str(pid)
    dap.set_breakpoints(CRASH, [marker_line(CRASH, "outer-call")])
    assert names(dap.stack(tid)) == names(stack)

    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGSEGV
    dap.wait_event("terminated")
    assert "terminated by signal SIGSEGV" in dap.output
    assert "before" in dap.output and "not reached" not in dap.output


@pytest.mark.smoke
@pytest.mark.parametrize("command", ["next", "stepIn", "stepOut"])
def test_stepping_at_a_crash_ends_the_program_without_running_python(dap, capi, command):
    dap.launch(CRASH, dap.python, args=["segv"], env=capi.env)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    dap.request(command, {"threadId": stop["threadId"]})
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited", body
    assert body["exitCode"] == 128 + signal.SIGSEGV


@pytest.mark.smoke
def test_abort_in_native_code(dap, capi):
    dap.launch(CRASH, dap.python, args=["abort"], env=capi.env)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGABRT" in stop["description"]
    stack = dap.stack(stop["threadId"])
    position = names(stack).index("st_do_abort")
    # Keep the actual fault selected; lower libc frames stay deemphasized.
    assert stack[0].get("presentationHint", "normal") == "normal", stack[0]
    assert all(f.get("presentationHint") == "subtle" for f in stack[1:position]), stack[:position]
    assert names(stack)[position:] == ["st_do_abort", "inner", "outer", "<module>"]
    assert stack[position + 1]["line"] == marker_line(CRASH, "inner-abort")
    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGABRT
    assert "terminated by signal SIGABRT" in dap.output


@pytest.mark.smoke
def test_crash_on_another_thread(dap, capi):
    dap.launch(CRASH, dap.python, args=["segv", "thread"], env=capi.env)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGSEGV" in stop["description"]
    threads = dap.request("threads")["threads"]
    assert len(threads) == 2
    stack = dap.stack(stop["threadId"])
    assert names(stack)[:3] == ["st_crash", "inner", "outer"]
    assert names(stack)[-1].endswith("_bootstrap")
    other = [t["id"] for t in threads if t["id"] != stop["threadId"]][0]
    assert names(dap.stack(other))[-1] == "<module>"
    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGSEGV


@pytest.mark.smoke
def test_signals_the_program_handles_do_not_stop_the_debugger(dap):
    dap.launch(SIGNALS, dap.python)
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited", body
    assert body["exitCode"] == 0
    assert "signals usr1,term,alrm,int" in dap.output


def test_stop_on_a_chosen_signal(dap):
    dap.launch(SIGNALS, dap.python, stopOnSignals=["SIGUSR1"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGUSR1" in stop["description"]
    stack = dap.stack(stop["threadId"])
    assert names(stack)[-1] == "<module>"
    # Continuing delivers the signal: the program's handler runs.
    dap.cont()
    assert dap.wait_exit() == 0
    assert "signals usr1,term,alrm,int" in dap.output


@pytest.mark.parametrize("how,code", [("sys-exit", 3), ("os-exit", 7), ("raise", 1)])
def test_exit_codes(dap, how, code):
    dap.launch(EXITS, dap.python, args=[how])
    assert dap.wait_exit() == code
    dap.wait_event("terminated")
    assert "exiting by " + how in dap.output
    if how == "raise":
        assert "RuntimeError: uncaught on purpose" in dap.plain_output


def test_program_killed_from_outside(dap):
    line = marker_line(EXITS, "wait-loop")
    dap.launch(EXITS, dap.python, args=["wait"], breakpoints={EXITS: [line]})
    dap.wait_stopped()
    pid = dap.status()["pid"]
    dap.set_breakpoints(EXITS, [])
    dap.cont()
    time.sleep(0.3)
    os.kill(pid, signal.SIGKILL)
    assert dap.wait_exit(10) == 128 + signal.SIGKILL
    dap.wait_event("terminated")
    assert "terminated by signal SIGKILL" in dap.output
