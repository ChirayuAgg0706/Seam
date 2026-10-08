"""The `seam` launcher.

`seam dap` speaks the Debug Adapter Protocol on stdin/stdout. The adapter itself runs
inside LLDB's embedded Python (see docs/decisions.md); this process only starts LLDB and
relays bytes over a socket pair.

`seam doctor` checks that this machine can run Seam (see doctor.py).
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading

from seam import __version__

# Prefer validated versions without LLDB 18's threaded-child-process bug, even
# when the distro's unversioned `lldb` still points to 18. Explicit overrides win.
LLDB_CANDIDATES = ("lldb-20", "lldb-19", "lldb", "lldb-21", "lldb-18")
NO_LLDB = ("No `lldb` was found on PATH. Install Apple's command-line tools on macOS "
           "(xcode-select --install), or LLDB 19+ on Linux (apt install lldb-19). "
           "Set SEAM_LLDB to override its location.")
ADAPTER_ENTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapter",
                             "lldb_entry.py")


def find_lldb():
    """Path of the LLDB to use, or None."""
    override = os.environ.get("SEAM_LLDB")
    if override:
        return override if shutil.which(override) else None
    for name in LLDB_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def lldb_command(lldb):
    return [lldb, "--batch", "--no-lldbinit", "-o", 'command script import "%s"' % ADAPTER_ENTRY]


def _tail(path, lines=8):
    try:
        with open(path, errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).rstrip()
    except OSError:
        return ""


def run_dap():
    lldb = find_lldb()
    if lldb is None:
        # Editors show a debug adapter's stderr when it dies before answering.
        sys.stderr.write("seam: %s\n" % NO_LLDB)
        return 1
    ours, theirs = socket.socketpair()
    # A second channel on which the adapter reports that it is up, and the programs it
    # launches, so that they can be cleaned up here if LLDB dies without doing it.
    notes_read, notes_write = os.pipe()
    env = dict(os.environ, SEAM_DAP_FD=str(theirs.fileno()), SEAM_NOTE_FD=str(notes_write),
               SEAM_PYTHON=sys.executable)
    log_path = os.environ.get("SEAM_LOG")
    scratch = None
    if log_path:
        lldb_output = log_path + ".lldb"
    else:
        # Kept only to explain a failure; removed on the way out.
        handle, scratch = tempfile.mkstemp(prefix="seam-lldb-", suffix=".txt")
        os.close(handle)
        lldb_output = scratch
    try:
        with open(lldb_output, "ab") as sink:
            try:
                proc = subprocess.Popen(
                    lldb_command(lldb), stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
                    env=env, pass_fds=[theirs.fileno(), notes_write])
            except OSError as exc:
                sys.stderr.write("seam: cannot start %s: %s\n" % (lldb, exc))
                return 1
        theirs.close()
        os.close(notes_write)
        return _relay(proc, ours, notes_read, log_path, lldb_output)
    finally:
        if scratch:
            try:
                os.unlink(scratch)
            except OSError:
                pass


def _relay(proc, ours, notes_read, log_path, lldb_output):
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    launched = []
    ready = threading.Event()

    def read_notes():
        with os.fdopen(notes_read) as notes:
            for line in notes:
                kind, _, value = line.strip().partition(" ")
                if kind == "ready":
                    ready.set()
                elif kind == "launched" and value.isdigit():
                    launched.append(int(value))

    notes_thread = threading.Thread(target=read_notes, daemon=True)
    notes_thread.start()

    def client_to_adapter():
        try:
            while True:
                data = stdin.raw.read(65536)
                if not data:
                    break
                ours.sendall(data)
        except OSError:
            pass
        try:
            ours.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=client_to_adapter, daemon=True).start()
    try:
        while True:
            data = ours.recv(65536)
            if not data:
                break
            stdout.write(data)
            stdout.flush()
    except OSError:
        pass
    try:
        status = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        # The session is over (the adapter closed its end); LLDB is only slow to leave.
        proc.kill()
        proc.wait()
        return 0
    notes_thread.join(2)  # LLDB has gone, so the pipe is at its end
    if not ready.is_set():
        # LLDB ran, but Seam's adapter never started inside it.
        sys.stderr.write(
            "seam: LLDB did not load Seam's adapter. Its Python scripting support is "
            "probably missing or broken (Debian/Ubuntu: the python3-lldb package that "
            "matches your lldb). `seam doctor` checks this (in VS Code: the command "
            "\"Seam: Check This Machine\"). LLDB said:\n%s\n"
            % (_tail(lldb_output) or "(nothing)"))
        return 1
    if status == 0:
        return 0
    # LLDB crashed, was killed, or the adapter inside it failed. A program it launched
    # is left running with nobody attached; and the client would otherwise see the
    # connection close with no explanation.
    for pid in launched:
        try:
            if os.getpgid(pid) == pid:
                # LLDB started it as the leader of its own process group: the children
                # still in that group go with it, as when the session is stopped.
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    reason = "signal %d" % -status if status < 0 else "status %d" % status
    message = "LLDB exited unexpectedly (%s); the debug session is over." % reason
    if log_path:
        message += " See %s and %s.lldb." % (log_path, log_path)
    else:
        message += " Set SEAM_LOG=<file> and run again to capture a log."
    for event, body in (("output", {"category": "stderr", "output": "Seam: %s\n" % message}),
                        ("terminated", {})):
        data = json.dumps({"seq": 0, "type": "event", "event": event, "body": body}).encode()
        try:
            stdout.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
            stdout.flush()
        except OSError:
            break
    sys.stderr.write("seam: %s\n" % message)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="seam", description="Debug Python and native code in one session.",
        epilog=("Requires CPython 3.12+ and LLDB with Python scripting support. "
                "Use the Seam extension in VS Code (a WSL window on Windows), or configure "
                "Neovim's nvim-dap with `seam dap`. "
                "Docs: https://github.com/ChirayuAgg0706/Seam#readme"))
    parser.add_argument("--version", action="version", version="seam " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dap", help="run the debug adapter on stdin/stdout")
    doctor = sub.add_parser(
        "doctor", help="check that this machine can run Seam, with a real debug session")
    doctor.add_argument("--python", default="python3",
                        help="the interpreter you want to debug (default: python3)")
    args = parser.parse_args(argv)
    if args.command == "dap":
        sys.exit(run_dap())
    if args.command == "doctor":
        from seam import doctor as doctor_module

        sys.exit(doctor_module.run(args.python))
