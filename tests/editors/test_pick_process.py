"""Attach with the pid as an editor's process picker hands it over: as text.

In VS Code `"pid": "${command:seam.pickProcess}"` is replaced by whatever the command
returned, which is a string; other clients may pass a picker's answer on the same way.
"""
import pytest

from conftest import marker_line, pid_alive
from test_attach import ATTACH, start_target


def test_attach_takes_the_pid_as_text(dap, python):
    proc = start_target(python)
    try:
        line = marker_line(ATTACH, "tick-body")
        dap.request("initialize", {"adapterID": "seam"})
        dap.request("attach", {"pid": str(proc.pid)})
        dap.wait_event("initialized")
        assert "attached to pid %d" % proc.pid in dap.output
        dap.set_breakpoints(ATTACH, [line])
        dap.request("configurationDone")
        stop = dap.wait_stopped()
        frame = dap.stack(stop["threadId"])[0]
        assert (frame["name"], frame["line"]) == ("tick", line)
        dap.evaluate("globals().update(STOP=True)", frame["id"])
        dap.set_breakpoints(ATTACH, [])
        dap.close()  # detaches: the program carries on and ends by itself
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not pid_alive(proc.pid)


@pytest.mark.parametrize("pid", ["${command:seam.pickProcess}", "12 34", "python", [7]])
def test_attach_refuses_a_pid_that_is_not_a_number(dap, pid):
    dap.request("initialize", {"adapterID": "seam"})
    reply = dap.request("attach", {"pid": pid}, check=False)
    assert not reply["success"]
    assert reply["message"] == "attach needs a process id as 'pid'; %r is not one" % (pid,)


@pytest.mark.parametrize("pid", [None, "", 0, "0", -4])
def test_attach_without_a_pid_says_so(dap, pid):
    dap.request("initialize", {"adapterID": "seam"})
    reply = dap.request("attach", {"pid": pid}, check=False)
    assert not reply["success"]
    assert reply["message"] == "attach needs a 'pid'"
