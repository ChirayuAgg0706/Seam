"""A program whose main thread is blocked in a system call until it is told to go on."""
import ctypes
import sys
import threading
import time

import seamtest

# Let a non-ancestor debugger attach even where Yama restricts ptrace (e.g. CI runners).
PR_SET_PTRACER = 0x59616D61
if sys.platform.startswith("linux"):
    ctypes.CDLL(None).prctl(PR_SET_PTRACER, ctypes.c_ulong(-1), 0, 0, 0)

ticks = 0


def background():
    """Another thread that keeps running Python while the main thread is blocked."""
    global ticks
    while True:
        ticks += 1  # background-tick
        time.sleep(0.02)


def busy(rounds):
    total = 0
    for i in range(rounds):
        total += i  # busy-body
    return total


def main():
    threading.Thread(target=background, daemon=True).start()
    print("ready", flush=True)
    # Retry EINTR entirely in C. Python's readline can service a debugger request
    # during its signal checks on macOS, so it is not an inevitably blocked target.
    seamtest.blocked_read()
    print("woke", busy(200000), flush=True)
    sys.stdin.readline()
    print("stopped", "helper loaded" if "seam_agent" in sys.modules else "clean", flush=True)


main()
