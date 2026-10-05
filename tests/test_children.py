"""The debugged program starts child processes. Children are not debugged (v1); they must
run as if no debugger were there, and the parent's session must stay healthy.

LLDB's handling of fork differs between versions, so every scenario makes the program
print what became of each child, and a failing assertion shows that output.
"""
import os
import re
import signal
import subprocess
import time

import pytest

from conftest import CAPI_SRC, marker_line, pid_alive, target
from dapclient import Terminal
from test_python import py_frames

pytestmark = pytest.mark.smoke

CHILDREN = target("children.py")
NOTICE = "Seam: the program started a child process (pid %d)"


def run_to_exit(dap, limit=60):
    """Continue through every stop. Returns the stops as (reason, function, line) and the
    program's exit code."""
    stops = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"], timeout=90)
        if name == "exited":
            return stops, body["exitCode"]
        frame = dap.stack(body["threadId"])[0]
        stops.append((body["reason"], frame["name"], frame["line"]))
        assert len(stops) <= limit, "too many stops: %s\n%s" % (stops, dap.output)
        dap.cont()


def all_breakpoints(dap, extra_functions=()):
    """A Python line, a native line and a native function breakpoint on the path `work`
    takes. Returns the stops one call of `work` makes in the debugged process."""
    python_line = marker_line(CHILDREN, "work-add")
    dap.set_breakpoints(CHILDREN, [python_line])
    dap.set_breakpoints(CAPI_SRC, [marker_line(CAPI_SRC, "add-impl-return")])
    dap.request("setFunctionBreakpoints", {"breakpoints": [
        {"name": name} for name in ("st_add",) + tuple(extra_functions)]})
    return [("breakpoint", "work", python_line), ("breakpoint", "st_add"),
            ("breakpoint", "add_impl")]


def same_stops(stops, expected):
    """Compare stops with what is expected (native ones by function: lines move at -O2)."""
    return len(stops) == len(expected) and all(
        got[:len(want)] == want for got, want in zip(stops, expected))


def launch(dap, capi, *args, **extra):
    dap.launch(CHILDREN, dap.python, args=list(args), env=capi.env, stopOnEntry=True, **extra)
    return dap.wait_stopped()["threadId"]


def wait_output(dap, pattern, timeout=30):
    """Read what the adapter sends until the output matches `pattern`; returns the match."""
    deadline = time.monotonic() + timeout
    while True:
        match = re.search(pattern, dap.output)
        if match:
            return match
        assert time.monotonic() < deadline, "no %r in the output:\n%s" % (pattern, dap.output)
        dap.drain(0.05, "none")


def wait_gone(pids, timeout=15):
    """The processes in `pids` that are still alive after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.05)
    return [p for p in pids if pid_alive(p)]


def kill_all(pids):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def notices(dap):
    return re.findall(r"Seam: the program started a child process \(pid (\d+)\)", dap.output)


def test_fork(dap, capi):
    launch(dap, capi, "fork")
    per_call = all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    # The parent stops at its own breakpoints before and after the fork; the child runs
    # the same lines and the same native function without stopping, and without dying.
    assert "fork child: exit 7" in dap.output, dap.output
    assert same_stops(stops, per_call * 2), (stops, dap.output)
    assert code == 0
    # In the child the helper has switched itself off; in the parent it is as before.
    assert "fork child: work 12, helper None" in dap.output, dap.output
    assert "parent: work 4 6, helper seam" in dap.output
    # The user is told, once, that the child is not being debugged.
    child = re.search(r"forked (\d+)", dap.output).group(1)
    assert notices(dap) == [child], dap.output


def test_fork_made_by_native_code(dap, capi):
    launch(dap, capi, "native-fork")
    per_call = all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    # Python's at-fork hooks did not run in this child; the helper noticed all the same.
    assert "native fork child: work 12, helper None" in dap.output, dap.output
    assert "native fork child: exit 7" in dap.output, dap.output
    assert same_stops(stops, per_call * 2), (stops, dap.output)
    assert code == 0 and "parent: work 4 6" in dap.output


def test_subprocess_system_and_posix_spawn(dap, capi):
    tid = launch(dap, capi, "spawn")
    # `execve` is what a new child itself calls, in memory it shares with the parent
    # (vfork): a breakpoint there must neither stop the parent nor kill the child.
    per_call = all_breakpoints(dap, extra_functions=("execve",))
    python_child = marker_line(CHILDREN, "spawn-python")
    dap.set_breakpoints(CHILDREN, [marker_line(CHILDREN, "work-add"), python_child])
    dap.cont()
    stops = []
    while True:
        stop = dap.wait_stopped(60)
        frame = dap.stack(tid)[0]
        if frame["line"] == python_child:
            break
        stops.append((stop["reason"], frame["name"], frame["line"]))
        dap.cont()
    out = dap.output
    for line in ("subprocess true: exit 0", "subprocess ls: exit 0 True", "system: exit 3",
                 "posix_spawn: exit 0"):
        assert line in out, out
    # Children that are not Python are not worth a message.
    assert notices(dap) == [], out

    dap.cont()
    more, code = run_to_exit(dap)
    out = dap.output
    assert "subprocess python: exit 5" in out, out
    # A child that is itself Python starts as if Seam were not there.
    assert "python child: helper None, modules [], env [], path []" in out, out
    popen = re.search(r"popen: pid (\d+), exit 0 PIPED", out)
    assert popen, out
    assert notices(dap) == [popen.group(1)], out
    assert same_stops(stops + more, per_call * 2), (stops, more, out)
    assert code == 0 and "parent: work 4 6" in out


def test_expressions_that_start_children(dap, capi):
    tid = launch(dap, capi, "fork")
    dap.set_breakpoints(CHILDREN, [marker_line(CHILDREN, "work-add")])
    dap.set_breakpoints(CAPI_SRC, [marker_line(CAPI_SRC, "add-impl-return")])
    dap.cont()
    dap.wait_stopped()
    frame = dap.stack(tid)[0]
    # Typed into the debug console at a Python stop. (A fork would end LLDB's evaluation
    # of the call and leave the child stopped for ever, if Seam let LLDB unwind.)
    for expression, result in (("subprocess.run(['/bin/true']).returncode", "0"),
                               ("os.system('exit 4') >> 8", "4"),
                               ("subprocess.check_output(['echo', 'hi'])", "b'hi\\n'")):
        assert dap.evaluate(expression, frame["id"])["result"] == result, dap.tail_log()
    # A forked child that comes back from the expression has nowhere to return to: it
    # ends there, quietly.
    assert dap.evaluate("os.waitpid(os.fork(), 0)[1]", frame["id"])["result"] == "0"
    assert py_frames(dap.stack(tid))[0] == ("work", marker_line(CHILDREN, "work-add"))
    assert dap.scope(dap.stack(tid)[0]["id"])["n"]["value"] == "1"

    # And a native expression at a native stop.
    dap.cont()
    dap.wait_stopped()
    frame = dap.stack(tid)[0]
    assert frame["name"] == "add_impl"
    assert dap.evaluate('(int)system("exit 4")', frame["id"])["result"] == str(4 << 8)
    dap.set_breakpoints(CHILDREN, [])
    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert "fork child: exit 7" in dap.output and "parent: work 4 6" in dap.output, dap.output


@pytest.mark.parametrize("method", ["fork", "forkserver", "spawn"])
def test_multiprocessing(dap, capi, method):
    launch(dap, capi, "pool", method)
    per_call = all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    out = dap.output
    assert "pool %s: [2, 4, 6, 8, 10, 12]" % method in out, out
    assert "process %s: exit 0" % method in out, out
    assert same_stops(stops, per_call * 2), (stops, out)
    assert code == 0 and "parent: work 4 6" in out
    assert len(notices(dap)) == 1, out


def test_process_pool_executor(dap, capi):
    launch(dap, capi, "executor")
    per_call = all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    assert "executor: [2, 4, 6, 8, 10, 12]" in dap.output, dap.output
    assert same_stops(stops, per_call * 2), (stops, dap.output)
    assert code == 0


def lldb_major(dap):
    """LLDB's major version (the process must be stopped)."""
    return int(re.search(r"version (\d+)", dap.request("seam/status")["lldb"]).group(1))


def test_fork_while_other_threads_run(dap, capi):
    launch(dap, capi, "threads")
    # The children run over these; in the parent the other threads keep reaching the
    # native one (its condition never holds there) while the main thread forks.
    dap.set_breakpoints(CHILDREN, [marker_line(CHILDREN, "work-add"),
                                   marker_line(CHILDREN, "threads-stop")])
    if lldb_major(dap) >= 19:
        dap.set_breakpoints(CAPI_SRC, [{"line": marker_line(CAPI_SRC, "add-impl-return"),
                                        "condition": "a == 5"}])
    # else: LLDB 18 breaks when a thread reaches a breakpoint at the moment another one
    # starts a child with vfork. It cannot evaluate the condition then ("Couldn't
    # allocate space for the stack frame"), stops, and from there on evaluates nothing
    # or loses the program (seen in six CI runs; nothing Seam tried from outside cured
    # it). LLDB 19 fixed its handling of vfork with several threads. Under LLDB 18 the
    # scenario therefore runs without the breakpoint the other threads keep reaching.
    dap.cont()
    stop = dap.wait_stopped(60)
    out = dap.output
    for round_number in range(3):
        assert "threads %d child: work 12" % round_number in out, out
        assert "threads %d child: exit 7" % round_number in out, out
        assert "threads %d subprocess: exit 0" % round_number in out, out
    frame = dap.stack(stop["threadId"])[0]
    assert (frame["name"], frame["line"]) == ("fork_with_threads",
                                              marker_line(CHILDREN, "threads-stop"))
    assert len(dap.request("threads")["threads"]) == 4
    assert len(notices(dap)) == 1, out
    dap.set_breakpoints(CHILDREN, [])
    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert "parent: threads counted True" in dap.output


def test_children_started_by_several_threads_at_once(dap, capi):
    launch(dap, capi, "concurrent")
    all_breakpoints(dap)
    dap.cont()
    stops, code = run_to_exit(dap)
    if code == -1 and "lldb version 18." in dap.output:
        # llvm-project #81564: LLDB 18 cannot follow child processes started by several
        # threads at the same moment and loses the program. Seam can only say so.
        assert "LLDB lost contact with the program" in dap.output, dap.output
        pytest.skip("LLDB 18 lost the program (llvm-project #81564); Seam reported it")
    assert "concurrent: all 16 children exited as expected" in dap.output, dap.output
    assert (stops, code) == ([], 0)


def test_step_over_lines_that_start_children(dap, capi):
    tid = launch(dap, capi, "step")
    dap.set_breakpoints(CHILDREN, [marker_line(CHILDREN, "step-fork")])
    dap.cont()
    dap.wait_stopped()
    dap.set_breakpoints(CHILDREN, [])
    for marker in ("step-if", "step-reap", "step-subprocess", "step-system", "step-print"):
        stop = dap.step("next", tid)
        assert stop["reason"] == "step", (stop, dap.output)
        assert py_frames(dap.stack(tid))[0] == ("stepping", marker_line(CHILDREN, marker)), \
            dap.output
    # The child inherited the step in progress and was not disturbed by it.
    assert "step child: work 12, helper None" in dap.output, dap.output
    assert "step child: exit 7" in dap.output, dap.output
    dap.cont()
    assert dap.wait_exit() == 0
    assert "parent: stepped exit 0 exit 3" in dap.output


def test_data_breakpoint_is_not_inherited_by_a_child(dap, capi):
    dap.launch(CHILDREN, dap.python, args=["watch"], env=capi.env,
               breakpoints={CAPI_SRC: [marker_line(CAPI_SRC, "bump-here")]})
    stop = dap.wait_stopped()
    frame = dap.stack(stop["threadId"])[0]
    scopes = {s["name"]: s["variablesReference"]
              for s in dap.request("scopes", {"frameId": frame["id"]})["scopes"]}
    info = dap.request("dataBreakpointInfo", {"variablesReference": scopes["Globals"],
                                              "name": "bump_count"})
    answers = dap.request("setDataBreakpoints", {"breakpoints": [
        {"dataId": info["dataId"], "accessType": "write"}]})["breakpoints"]
    assert answers == [{"verified": True}], answers
    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    seen = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"])
        if name == "exited":
            break
        assert body["reason"] == "data breakpoint", (body, dap.output)
        seen.append(body["description"])
        dap.cont()
    # The child changed its own copy of the variable: no stop, and it lived to exit.
    assert "watch child: exit 42" in dap.output, dap.output
    assert seen == ["bump_count is now 1", "bump_count is now 2"]
    assert body["exitCode"] == 0 and "parent: bumped 1 2" in dap.output


def test_exception_breakpoints_do_not_reach_into_children(dap, capi):
    dap.launch(CHILDREN, dap.python, args=["raise"], env=capi.env,
               exceptions=["uncaught", "raised", "cpp_throw", "rust_panic"])
    stops, code = run_to_exit(dap)
    out = dap.plain_output
    # One child ends with an uncaught exception, as it would without a debugger; in the
    # other a thread does, and threading's own hook (not Seam's) reports it.
    assert "raise child: exit 1" in out and "ValueError: raised by the child" in out, out
    assert "raise thread child: exit 6" in out and "ValueError: raised by a thread" in out, out
    # The parent's own exception is the only stop.
    assert stops == [("exception", "fail", marker_line(CHILDREN, "raise-here"))], (stops, out)
    assert code == 0 and "parent: caught raised by the parent" in out


def test_logpoints_are_the_parents_only(dap, capi):
    launch(dap, capi, "log")
    dap.set_breakpoints(CHILDREN, [{"line": marker_line(CHILDREN, "work-add"),
                                    "logMessage": "work on {n}"}])
    dap.set_breakpoints(CAPI_SRC, [{"line": marker_line(CAPI_SRC, "add-impl-return"),
                                    "logMessage": "native {a}"}])
    dap.cont()
    stops, code = run_to_exit(dap)
    assert "log child: exit 7" in dap.output, dap.output
    logged = [line for line in dap.output.splitlines()
              if line.startswith(("work on", "native"))]
    assert logged == ["work on 0", "native 0", "work on 1", "native 1",
                      "work on 10", "native 10"], dap.output
    assert (stops, code) == ([], 0)


def test_a_child_that_outlives_the_program_and_the_session(dap, capi, tmp_path):
    launch(dap, capi, "orphan", str(tmp_path))
    all_breakpoints(dap)
    dap.cont()
    orphan = int(wait_output(dap, r"leaving child (\d+) behind").group(1))
    try:
        started = time.monotonic()
        assert dap.wait_exit() == 0
        dap.wait_event("terminated")
        # The child still holds the program's terminal; the exit is reported without
        # waiting for it (it used to take two seconds).
        assert time.monotonic() - started < 1.5
        assert pid_alive(orphan)
        # While the session is open the child's output still arrives.
        (tmp_path / "first").touch()
        wait_output(dap, "orphan: first")
        # The program ended by itself, so ending the session leaves its child alone; with
        # the debug console gone, what the child prints now has nowhere to go.
        dap.close()
        assert pid_alive(orphan)
        (tmp_path / "second").touch()
        deadline = time.monotonic() + 15
        while not (tmp_path / "report").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "report").read_text().splitlines() == [
            "work 12, helper None", "first: printed", "second: EIO"]
        assert wait_gone([orphan]) == []
    finally:
        kill_all([orphan])


@pytest.mark.parametrize("how", ["terminate", "disconnect", "lldb killed"])
def test_stopping_the_session_ends_the_children_in_the_programs_group(dap, capi, how):
    launch(dap, capi, "linger")
    program = dap.status()["pid"]
    dap.cont()
    forked, spawned, detached = map(int, wait_output(
        dap, r"children: (\d+) (\d+) (\d+)").groups())
    try:
        assert all(pid_alive(p) for p in (program, forked, spawned, detached))
        if how == "terminate":
            dap.request("terminate")
            dap.wait_event("terminated")
        elif how == "lldb killed":
            lldb = subprocess.run(["pgrep", "-P", str(dap.proc.pid)],
                                  capture_output=True, text=True).stdout.split()
            os.kill(int(lldb[0]), signal.SIGKILL)
            dap.wait_event("terminated", 20)
        dap.close()
        # The program, the child it forked and the one it started are gone; the child
        # that moved to a session of its own (a daemon) is not Seam's to end.
        assert wait_gone([program, forked, spawned]) == []
        assert pid_alive(detached)
    finally:
        kill_all([program, forked, spawned, detached])


def test_children_in_the_terminal(dap, capi):
    dap.terminal = Terminal()
    dap.launch(CHILDREN, dap.python, args=["terminal"], env=capi.env,
               console="integratedTerminal",
               breakpoints={CHILDREN: [marker_line(CHILDREN, "work-add")]})
    # A child reads the keyboard and writes to the terminal like the program itself.
    dap.terminal.read_until("child? ")
    dap.terminal.type("seam\n")
    dap.terminal.read_until("child heard SEAM")
    dap.terminal.read_until("reader: exit 0")
    # A forked child runs over the breakpoint without stopping.
    dap.terminal.read_until("forked child: tty True, work 12")
    dap.terminal.read_until("terminal child: exit 7")
    # Ctrl-C reaches the child the program is waiting for, as in a real terminal.
    dap.terminal.read_until("asleep")
    dap.terminal.type("\x03")
    dap.terminal.read_until("system: signal SIGINT")
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 0, (body, dap.terminal.text)
