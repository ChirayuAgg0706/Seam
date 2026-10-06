"""A long-running program that does what it is told on standard input (attach scenarios).

It polls its input, so its main thread keeps reaching points where the helper can load.
"""
import ctypes
import select
import sys
import threading

import seamtest

# Test CPython's exception exit, not Ubuntu's crash-reporting service. Apport imports
# native libraries after the exception stop and can exceed the scenario's exit timeout
# under LLDB. Replacement exception hooks are covered by exceptions.py's hooked mode.
sys.excepthook = sys.__excepthook__

# Let a non-ancestor debugger attach even where Yama restricts ptrace (e.g. CI runners).
PR_SET_PTRACER = 0x59616D61
ctypes.CDLL(None).prctl(PR_SET_PTRACER, ctypes.c_ulong(-1), 0, 0, 0)


def work(n):
    total = seamtest.add(n, 1)  # work-body
    return total


def fail(n):
    raise ValueError("bad value %d" % n)  # fail-raise


def caught(n):
    try:
        fail(n)
    except ValueError:
        return "caught %d" % n


def in_thread(n):
    worker = threading.Thread(target=fail, args=(n,), name="worker")
    worker.start()
    worker.join()
    return "thread done"


def main():
    print("ready", flush=True)
    while True:
        if not select.select([sys.stdin], [], [], 0.05)[0]:
            continue
        command, _, arg = sys.stdin.readline().strip().partition(" ")
        n = int(arg or 0)
        if command == "work":
            print("work", work(n), flush=True)
        elif command == "caught":
            print(caught(n), flush=True)
        elif command == "thread":
            print(in_thread(n), flush=True)
        elif command == "raise":
            fail(n)
        elif command == "crash":
            seamtest.crash()
        else:  # "quit", or the input was closed
            break
    print("stopped", flush=True)


main()
