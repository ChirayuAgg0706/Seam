"""The adapter's own failure paths: the client is told why, and nothing is left behind."""
import os
import http.server
import signal
import subprocess
import threading
import time

import pytest

from conftest import marker_line, pid_alive, target

BASIC = target("basic.py")
EXITS = target("exits.py")


@pytest.mark.smoke
def test_launch_does_not_wait_for_network_symbol_servers(make_client, monkeypatch):
    """An inherited distro debuginfod URL must not turn local startup into network I/O."""
    requests = []

    class Symbols(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_error(404)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Symbols)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = "http://127.0.0.1:%d" % server.server_port
    monkeypatch.setenv("DEBUGINFOD_URLS", url)
    client = make_client()
    try:
        client.launch(BASIC, client.python, stopOnEntry=True, debugInfoLookup=True)
        stop = client.wait_stopped()
        frame = client.stack(stop["threadId"])[0]
        # Keep the program's environment intact: only the debugger's lookup is disabled.
        assert client.evaluate("__import__('os').environ['DEBUGINFOD_URLS']",
                               frame["id"])["result"] == repr(url)
        client.cont()
        assert client.wait_exit() == 0
        assert not requests, requests
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        worker.join()


def descendants(pid):
    """Every live process below `pid` (LLDB, and the debug server LLDB starts)."""
    found = []
    todo = [pid]
    while todo:
        out = subprocess.run(["pgrep", "-P", str(todo.pop())],
                             capture_output=True, text=True).stdout
        for child in map(int, out.split()):
            found.append(child)
            todo.append(child)
    return found


def running_session(client):
    """Start a program that runs until killed. Returns its pid and Seam's own processes."""
    line = marker_line(EXITS, "wait-loop")
    client.launch(EXITS, client.python, args=["wait"], breakpoints={EXITS: [line]})
    client.wait_stopped()
    pid = client.status()["pid"]
    client.set_breakpoints(EXITS, [])
    client.cont()
    time.sleep(0.3)
    return pid, descendants(client.proc.pid)


def survivors(pids, timeout=15):
    """Command lines of the processes in `pids` still alive after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.05)
    alive = []
    for pid in pids:
        if pid_alive(pid):
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                alive.append("%d: %s" % (pid, fh.read().replace(b"\0", b" ").decode()))
            os.kill(pid, signal.SIGKILL)  # do not let one failure disturb later tests
    return alive


def test_client_closes_the_connection_without_disconnecting(make_client):
    client = make_client()
    pid, ours = running_session(client)
    assert pid in ours and len(ours) >= 3  # lldb, its debug server, the program
    client.proc.stdin.close()
    assert client.proc.wait(timeout=20) == 0
    assert survivors(ours) == []


@pytest.mark.parametrize("how", [signal.SIGTERM, signal.SIGKILL])
def test_adapter_process_is_killed(make_client, how):
    client = make_client()
    pid, ours = running_session(client)
    os.kill(client.proc.pid, how)
    client.proc.wait(timeout=20)
    assert survivors(ours) == []


def test_lldb_dies_under_the_adapter(make_client):
    client = make_client(stderr=subprocess.PIPE)
    pid, ours = running_session(client)
    lldb_pid = subprocess.run(["pgrep", "-P", str(client.proc.pid)],
                              capture_output=True, text=True).stdout.split()
    assert len(lldb_pid) == 1
    os.kill(int(lldb_pid[0]), signal.SIGKILL)
    # The client learns the session is over, and `seam dap` says why and fails.
    assert client.wait_event("terminated", 20) is not None
    assert client.proc.wait(timeout=20) != 0
    assert "LLDB" in client.proc.stderr.read().decode()
    assert survivors(ours) == []


def test_bad_requests_get_an_error_and_the_session_carries_on(dap):
    line = marker_line(BASIC, "inner-first")
    early = dap.request("stackTrace", {"threadId": 1}, check=False)
    assert not early["success"] and "not stopped" in early["message"]
    assert dap.request("threads") == {"threads": []}

    dap.launch(BASIC, dap.python, breakpoints={BASIC: [line]})
    stop = dap.wait_stopped()
    tid = stop["threadId"]

    for command, arguments, expect in [
        ("noSuchRequest", {}, "unsupported request"),
        ("stackTrace", {"threadId": 987654321}, "unknown thread"),
        ("stackTrace", {}, "threadId"),
        ("scopes", {"frameId": 987654321}, "frame"),
        ("variables", {"variablesReference": 987654321}, "variablesReference"),
        ("setBreakpoints", {"breakpoints": [{"line": 1}]}, "source.path"),
        ("next", {"threadId": 987654321}, "unknown thread"),
        ("launch", {"program": BASIC}, "already"),
        ("attach", {"pid": 1}, "already"),
    ]:
        reply = dap.request(command, arguments, check=False)
        assert not reply["success"], command
        assert expect in reply["message"], (command, reply["message"])
        assert "internal error" not in reply["message"], (command, reply["message"])

    # The session is unharmed.
    stack = dap.stack(tid)
    assert stack[0]["line"] == line
    assert dap.evaluate("a + b", stack[0]["id"])["result"] == "15"
    dap.cont()
    running = dap.evaluate("1 + 1", check=False)
    assert not running["success"]
    assert dap.wait_exit() == 0


def test_launch_problems_are_reported_clearly(make_client, tmp_path):
    cases = [
        ({"program": BASIC, "python": "/no/such/python"}, "/no/such/python"),
        ({"program": BASIC, "python": "/usr/bin/true"}, "CPython"),
        ({"python": "python3"}, "'program' or 'module'"),
        ({"program": BASIC, "python": "python3", "stopOnSignals": ["SIGNOPE"]}, "SIGNOPE"),
        ({"program": BASIC, "python": "python3", "cwd": str(tmp_path / "missing")}, "missing"),
    ]
    for arguments, expect in cases:
        client = make_client()
        client.request("initialize", {"adapterID": "seam"})
        reply = client.request("launch", arguments, check=False)
        assert not reply["success"], arguments
        assert expect in reply["message"], (arguments, reply["message"])
        ours = descendants(client.proc.pid)
        client.close()
        assert survivors(ours) == [], arguments


def test_a_program_that_does_not_exist(dap, tmp_path):
    dap.launch(str(tmp_path / "nothing.py"), dap.python, cwd=str(tmp_path))
    assert dap.wait_exit() == 2
    assert "nothing.py" in dap.output
