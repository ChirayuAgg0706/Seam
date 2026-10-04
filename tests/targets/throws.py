"""Calls a native function that fails the native way (a C++ throw, a Rust panic)."""
import sys

layer = __import__(sys.argv[1])


def call_native():
    reason = "boom"
    try:
        layer.fail(reason)  # throw-call
    except BaseException as exc:  # PyO3 turns a panic into a BaseException subclass
        return "caught %s" % type(exc).__name__


print(call_native(), flush=True)  # module-call
