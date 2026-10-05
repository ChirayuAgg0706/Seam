"""A long-running program that calls into the extension built elsewhere (attach scenario).
Prints "ready <pid>" once it is looping."""
import ctypes
import os
import sys
import time

import seam_mapped

# Let a non-ancestor debugger attach even where Yama restricts ptrace (e.g. CI runners).
PR_SET_PTRACER = 0x59616D61
ctypes.CDLL(None).prctl(PR_SET_PTRACER, ctypes.c_ulong(-1), 0, 0, 0)

STOP = False


def tick(n):
    value = seam_mapped.scale(n, 2)  # tick-body
    return value


def main():
    print("ready", os.getpid(), flush=True)
    deadline = time.time() + 60
    while not STOP and time.time() < deadline:
        tick(3)
        time.sleep(0.05)
    print("stopped" if STOP else "timeout", flush=True)
    return 0 if STOP else 1


sys.exit(main())
