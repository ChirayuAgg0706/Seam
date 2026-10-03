"""Attach to a running process, debug it, detach, and check it carries on unharmed."""
import subprocess

import pytest

from conftest import marker_line, pid_alive, target

pytestmark = pytest.mark.smoke

ATTACH = target("attach_target.py")


def start_target(python):
    proc = subprocess.Popen([python, ATTACH], stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline().split()
    assert line and line[0] == "ready", line
    return proc


def test_attach_break_inspect_detach(dap, python, pyinfo):
    proc = start_target(python)
    try:
        line = marker_line(ATTACH, "tick-body")
        dap.request("initialize", {"adapterID": "seam"})
        dap.request("attach", {"pid": proc.pid})
        dap.wait_event("initialized")
        # 3.14 uses PEP 768 (memory writes only); older versions need a pending call.
        expected = "PEP 768" if pyinfo["tag"] >= "cp314" else "a pending call"
        assert expected in dap.output, dap.output
        dap.set_breakpoints(ATTACH, [line])
        dap.request("configurationDone")

        stop = dap.wait_stopped()
        assert stop["reason"] == "breakpoint"
        tid = stop["threadId"]
        stack = dap.stack(tid)
        assert [(f["name"], f["line"]) for f in stack[:1]] == [("tick", line)]
        assert [f["name"] for f in stack] == ["tick", "main", "<module>"]
        n = int(dap.scope(stack[0]["id"])["n"]["value"])
        assert n > 0

        dap.step("next", tid)
        stack = dap.stack(tid)
        assert (stack[0]["name"], stack[0]["line"]) == ("tick", line + 1)
        assert dap.scope(stack[0]["id"])["value"]["value"] == str(n + 1)

        dap.evaluate("globals().update(STOP=True)", stack[0]["id"])
        dap.set_breakpoints(ATTACH, [])
        status = dap.status()
        assert status["agent"]["global_events"] == 0 and status["agent"]["local_events"] == {}
        dap.close()  # detaches: the program must keep running on its own

        assert proc.wait(timeout=20) == 0
        assert proc.stdout.read().strip() == "stopped"
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not pid_alive(proc.pid)


def test_attach_to_something_that_is_not_python_fails_cleanly(dap):
    proc = subprocess.Popen(["sleep", "30"])
    try:
        dap.request("initialize", {"adapterID": "seam"})
        reply = dap.request("attach", {"pid": proc.pid}, check=False)
        assert not reply["success"]
        assert "CPython" in reply["message"]
        dap.close()
        assert proc.poll() is None, "the process must survive a refused attach"
    finally:
        proc.kill()
        proc.wait()
