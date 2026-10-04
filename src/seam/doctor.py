"""`seam doctor`: check that this machine can run Seam.

Looks at each prerequisite in turn, says how to fix what is missing, and finishes with
the only check that really settles it: a short debug session against a three-line
program, through the same adapter an editor would use.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from seam import __version__, cli

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_target",
                      "_seam_trap.abi3.so")
PROGRAM = "value = 20\nvalue += 22\nprint('seam doctor', value)\n"


class _Report:
    def __init__(self):
        self.problems = 0

    def ok(self, text):
        print("ok       " + text)

    def note(self, text):
        print("note     " + text)

    def problem(self, text, fix):
        self.problems += 1
        print("PROBLEM  " + text)
        print("         " + fix)


def _run(command, timeout=60):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout + done.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def _check_platform(report):
    machine = platform.machine()
    if sys.platform.startswith("linux") and machine in ("x86_64", "AMD64"):
        report.ok("platform: Linux x86-64")
        return True
    report.problem("platform: %s %s" % (sys.platform, machine),
                   "Seam supports Linux on x86-64 only.")
    return False


def _check_lldb(report):
    lldb = cli.find_lldb()
    if lldb is None:
        report.problem("LLDB: not found", cli.NO_LLDB)
        return False
    status, text = _run([lldb, "--version"])
    version = re.search(r"version (\d+)\.(\d+)(\.\d+)?", text or "")
    if status != 0 or not version:
        report.problem("LLDB: `%s --version` failed: %s" % (lldb, (text or "").strip()[:200]),
                       "Install LLDB 18 or newer, or point SEAM_LLDB at a working one.")
        return False
    if int(version.group(1)) < 18:
        report.problem("LLDB: %s is version %s" % (lldb, version.group(0)[8:]),
                       "Seam needs LLDB 18 or newer (Debian/Ubuntu: apt install lldb-18).")
        return False
    report.ok("LLDB: %s, version %s" % (lldb, version.group(0)[8:]))
    status, text = _run([lldb, "--batch", "--no-lldbinit", "-o",
                         "script import sys; print('seam-python', *sys.version_info[:2])"])
    scripting = re.search(r"seam-python (\d+) (\d+)", text or "")
    if not scripting:
        report.problem("LLDB: its Python scripting does not work",
                       "Install the Python bindings that match this LLDB (Debian/Ubuntu: "
                       "python3-lldb-%s)." % version.group(1))
        return False
    report.ok("LLDB: Python scripting works (Python %s.%s inside LLDB)" % scripting.groups())
    return True


def _check_helper(report):
    if os.path.exists(HELPER):
        report.ok("helper: built (%s)" % os.path.basename(HELPER))
        return True
    report.problem("helper: %s is missing" % HELPER,
                   "Reinstall Seam with a C compiler and the Python headers available "
                   "(Debian/Ubuntu: apt install gcc python3-dev), then pip install again.")
    return False


def _check_ptrace(report):
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope") as fh:
            scope = int(fh.read().strip())
    except (OSError, ValueError):
        report.note("ptrace: no Yama restriction found")
        return
    if scope == 0:
        report.ok("ptrace: unrestricted; launch and attach are both possible")
    elif scope == 1:
        report.note("ptrace: restricted to child processes (ptrace_scope=1). Launching "
                    "under Seam works; attaching to a running process needs "
                    "`sudo sysctl kernel.yama.ptrace_scope=0`.")
    else:
        report.problem("ptrace: ptrace_scope=%d forbids debugging for ordinary users" % scope,
                       "Lower it with `sudo sysctl kernel.yama.ptrace_scope=1` (launch) "
                       "or =0 (launch and attach).")


def _check_python(report, python):
    path = python if os.sep in python else shutil.which(python)
    if not path or not os.path.exists(path):
        report.problem("Python to debug: %s not found" % python,
                       "Pass the interpreter you run your program with: "
                       "seam doctor --python /path/to/python")
        return None
    status, text = _run([path, "-c",
                         "import sys, sysconfig; print('seam-target', sys.implementation.name, "
                         "sys.version_info[0], sys.version_info[1], "
                         "int(bool(sysconfig.get_config_var('Py_GIL_DISABLED'))))"])
    found = re.search(r"seam-target (\S+) (\d+) (\d+) (\d)", text or "")
    if status != 0 or not found:
        report.problem("Python to debug: %s did not run: %s" % (path, (text or "").strip()[:200]),
                       "Pass a working CPython 3.12+ interpreter with --python.")
        return None
    name, major, minor, free_threaded = found.group(1), int(found.group(2)), \
        int(found.group(3)), found.group(4) == "1"
    label = "Python to debug: %s (%s %d.%d%s)" % (path, name, major, minor,
                                                 ", free-threaded" if free_threaded else "")
    if name != "cpython":
        report.problem(label, "Seam debugs CPython only.")
    elif (major, minor) < (3, 12):
        report.problem(label, "Seam needs CPython 3.12 or newer (it relies on "
                              "sys.monitoring, added in 3.12).")
    elif free_threaded:
        report.problem(label, "Free-threaded (no-GIL) builds are not supported yet; use "
                              "the standard build of the same version.")
    else:
        report.ok(label)
        return path
    return None


def _adapter_command():
    """The command that starts the adapter of the installation being checked."""
    launcher = sys.argv[0]
    if os.path.isdir(launcher):
        # Run as a directory: the copy inside the VS Code extension, which is installed
        # nowhere that `-m seam` could find. It is started again the way this was.
        return [sys.executable, "-I", launcher, "dap"]
    return [sys.executable, "-m", "seam", "dap"]


class _Session:
    """The few lines of DAP client the live check needs."""

    def __init__(self, log_path):
        env = dict(os.environ, SEAM_LOG=log_path)
        self.proc = subprocess.Popen(_adapter_command(), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.seq = 0
        self.deadline = time.monotonic() + 90
        # Reads block; if the adapter stops talking, ending it ends the read.
        self.watchdog = threading.Timer(90, self.proc.kill)
        self.watchdog.daemon = True
        self.watchdog.start()

    def send(self, command, arguments=None):
        self.seq += 1
        data = json.dumps({"seq": self.seq, "type": "request", "command": command,
                           "arguments": arguments or {}}).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.proc.stdin.flush()
        return self.seq

    def read(self):
        out = self.proc.stdout
        length = None
        while True:
            line = out.readline()
            if not line:
                raise RuntimeError("the adapter closed the connection: "
                                   + self.proc.stderr.read().decode(errors="replace").strip())
            line = line.strip()
            if not line:
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        return json.loads(out.read(length))

    def until(self, what, match):
        while time.monotonic() < self.deadline:
            message = self.read()
            if message.get("type") == "response" and not message.get("success"):
                raise RuntimeError("%s failed: %s" % (message.get("command"),
                                                      message.get("message")))
            if match(message):
                return message
        raise RuntimeError("timed out waiting for " + what)

    def request(self, command, arguments=None):
        seq = self.send(command, arguments)
        return self.until("the answer to " + command, lambda m: m.get("request_seq") == seq)

    def event(self, name):
        return self.until("the %s event" % name,
                          lambda m: m.get("type") == "event" and m.get("event") == name)

    def close(self):
        self.watchdog.cancel()
        try:
            self.send("disconnect")
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


def _live_check(report, python):
    directory = tempfile.mkdtemp(prefix="seam-doctor-")
    program = os.path.join(directory, "program.py")
    log_path = os.path.join(directory, "seam.log")
    with open(program, "w") as fh:
        fh.write(PROGRAM)
    session = _Session(log_path)
    try:
        session.request("initialize", {"adapterID": "seam", "clientID": "seam doctor"})
        session.send("launch", {"program": program, "python": python, "cwd": directory})
        session.event("initialized")
        session.request("setBreakpoints", {"source": {"path": program},
                                           "breakpoints": [{"line": 3}]})
        session.request("configurationDone")
        stop = session.event("stopped")["body"]
        frames = session.request("stackTrace", {"threadId": stop["threadId"]})["body"]
        top = frames["stackFrames"][0]
        value = session.request("evaluate", {"expression": "value", "frameId": top["id"],
                                             "context": "repl"})["body"]["result"]
        if (top["line"], value) != (3, "42"):
            raise RuntimeError("stopped at line %s with value = %s; expected line 3 and 42"
                               % (top["line"], value))
        session.request("continue", {"threadId": stop["threadId"]})
        code = session.event("exited")["body"]["exitCode"]
        if code != 0:
            raise RuntimeError("the program exited with status %s" % code)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        report.problem("debug session: %s" % exc,
                       "The adapter's log is in %s (seam.log and seam.log.lldb); include "
                       "it when reporting this." % directory)
        return
    finally:
        session.close()
    shutil.rmtree(directory, ignore_errors=True)
    report.ok("debug session: launched a program, stopped at a breakpoint, evaluated an "
              "expression, ran to the end")


def run(python):
    print("seam %s" % __version__)
    report = _Report()
    usable = _check_platform(report)
    usable = _check_lldb(report) and usable
    usable = _check_helper(report) and usable
    _check_ptrace(report)
    target = _check_python(report, python)
    if usable and target:
        _live_check(report, target)
    else:
        report.note("debug session: not tried, because of the problems above")
    if report.problems:
        print("%d problem%s found." % (report.problems, "" if report.problems == 1 else "s"))
        return 1
    print("Seam is ready to use.")
    return 0
