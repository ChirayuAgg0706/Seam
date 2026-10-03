"""The `seam` launcher.

`seam dap` speaks the Debug Adapter Protocol on stdin/stdout. The adapter itself runs
inside LLDB's embedded Python (see docs/decisions.md); this process only starts LLDB and
relays bytes over a socket pair.
"""
import argparse
import os
import shutil
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
    env = dict(os.environ, SEAM_DAP_FD=str(theirs.fileno()))
    log_path = os.environ.get("SEAM_LOG")
    sink = open(log_path + ".lldb", "ab") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(
        [lldb, "--batch", "--no-lldbinit", "-o", 'command script import "%s"' % entry],
        stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, env=env,
        pass_fds=[theirs.fileno()])
    theirs.close()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

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
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="seam", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version="seam " + __version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dap", help="run the debug adapter on stdin/stdout")
    args = parser.parse_args(argv)
    if args.command == "dap":
        sys.exit(run_dap())
