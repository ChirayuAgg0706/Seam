"""The `seam` launcher.

`seam dap` speaks the Debug Adapter Protocol on stdin/stdout. The adapter itself runs
inside LLDB's embedded Python (see docs/decisions.md); this process only starts LLDB and
relays bytes over a socket pair.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading

from seam import __version__

LLDB_CANDIDATES = ("lldb", "lldb-21", "lldb-20", "lldb-19", "lldb-18")


def find_lldb():
    override = os.environ.get("SEAM_LLDB")
    if override:
        return override
    for name in LLDB_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    sys.exit("seam: LLDB 18 or newer is required but no `lldb` was found on PATH "
             "(set SEAM_LLDB to its location)")


def run_dap():
    lldb = find_lldb()
    entry = os.path.join(os.path.dirname(os.path.abspath(__file__)), "adapter", "lldb_entry.py")
    ours, theirs = socket.socketpair()
    # A second channel on which the adapter reports the programs it launches, so that
    # they can be cleaned up here if LLDB dies without doing it.
    notes_read, notes_write = os.pipe()
    env = dict(os.environ, SEAM_DAP_FD=str(theirs.fileno()), SEAM_NOTE_FD=str(notes_write),
               SEAM_PYTHON=sys.executable)
    log_path = os.environ.get("SEAM_LOG")
    sink = open(log_path + ".lldb", "ab") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(
        [lldb, "--batch", "--no-lldbinit", "-o", 'command script import "%s"' % entry],
        stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, env=env,
        pass_fds=[theirs.fileno(), notes_write])
    theirs.close()
    os.close(notes_write)
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    launched = []

    def read_notes():
        with os.fdopen(notes_read) as notes:
            for line in notes:
                kind, _, value = line.strip().partition(" ")
                if kind == "launched" and value.isdigit():
                    launched.append(int(value))

    threading.Thread(target=read_notes, daemon=True).start()

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
    if status == 0:
        return 0
    # LLDB crashed, was killed, or the adapter inside it failed. A program it launched
    # is left running with nobody attached; and the client would otherwise see the
    # connection close with no explanation.
    for pid in launched:
        try:
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
    parser = argparse.ArgumentParser(prog="seam", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version="seam " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dap", help="run the debug adapter on stdin/stdout")
    args = parser.parse_args(argv)
    if args.command == "dap":
        sys.exit(run_dap())
