"""Dies inside native code, in the way named on the command line."""
import sys
import threading

import seamtest


def inner(kind, depth):
    label = "inner-local"
    if kind == "segv":
        seamtest.crash()  # inner-segv
    elif kind == "abort":
        seamtest.do_abort()  # inner-abort
    return label, depth


def outer(kind):
    values = [1, 2, 3]
    return inner(kind, len(values))  # outer-call


print("before", flush=True)
if sys.argv[2:] == ["thread"]:
    worker = threading.Thread(target=outer, args=(sys.argv[1],), name="crasher")
    worker.start()
    worker.join()
else:
    outer(sys.argv[1])  # module-call
print("not reached", flush=True)
