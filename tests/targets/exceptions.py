"""Raises exceptions in the way named on the command line."""
import json
import sys
import threading

import seamtest

# Exercise CPython's exception reporting. Ubuntu's apport hook imports many native
# libraries and can consume the scenario's exit deadline under LLDB; a replaced hook
# is covered explicitly by the `hooked` mode below.
sys.excepthook = sys.__excepthook__


def deepest(kind):
    detail = "deep-local"
    if kind == "value":
        raise ValueError("bad value " + detail)  # raise-here
    return detail


def middle(kind):
    items = [1, 2, 3]
    return deepest(kind), items  # middle-call


def handled():
    try:
        middle("value")  # handled-call
    except ValueError as exc:
        return str(exc)


def from_library():
    try:
        json.loads("{bad")  # library-call
    except ValueError:
        return "library handled"


def from_native():
    try:
        seamtest.fail()  # native-call
    except ValueError:
        return "native handled"


mode = sys.argv[1]
if mode == "handled":
    print(handled(), flush=True)  # handled-print
    print(from_library(), flush=True)
    print(from_native(), flush=True)
elif mode in ("uncaught", "hooked"):
    if mode == "hooked":
        sys.excepthook = lambda kind, value, tb: print("custom hook:", value, flush=True)
    middle("value")  # module-call
elif mode == "thread":
    worker = threading.Thread(target=middle, args=("value",), name="raiser")
    worker.start()
    worker.join()
    print("thread done", flush=True)
elif mode == "exit":
    sys.exit(4)
print("end", flush=True)
