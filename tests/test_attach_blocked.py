"""Attaching to a process whose main thread is blocked in a system call."""
import os
import subprocess

import pytest

from conftest import target

pytestmark = pytest.mark.smoke

BLOCKED = target("attach_blocked.py")


@pytest.fixture
def blocked(python, capi):
    proc = subprocess.Popen([python, BLOCKED], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=dict(os.environ, **capi.env))
    assert proc.stdout.readline().strip() == "ready"
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def tell(proc, text):
    proc.stdin.write(text + "\n")
    proc.stdin.flush()


def test_a_refused_attach_leaves_nothing_behind_in_the_process(dap, blocked):
    dap.request("initialize", {"adapterID": "seam"})
    reply = dap.request("attach", {"pid": blocked.pid, "timeout": 2}, check=False)
    assert not reply["success"]
    assert "did not load the Seam helper within 2 s" in reply["message"]
    assert "blocked in a system call" in reply["message"]
    dap.close()
    assert blocked.poll() is None, "the process must survive a refused attach"

    # The main thread now runs Python again. Whatever the attach attempt queued in the
    # process must not run after the debugger has gone, let alone break the program.
    tell(blocked, "go")
    assert blocked.stdout.readline().split() == ["woke", str(sum(range(200000)))]
    tell(blocked, "quit")
    assert blocked.stdout.readline().strip() == "stopped clean"
    assert blocked.wait(timeout=10) == 0
    assert blocked.stderr.read() == ""
