"""Constants and small helpers shared by the adapter's modules."""
import os
import re
import signal

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET_DIR = os.path.join(PACKAGE_DIR, "_target")
TERMINAL_HOLDER = os.path.join(PACKAGE_DIR, "terminal.py")
CONSOLES = ("internalConsole", "integratedTerminal", "externalTerminal")
# Signals the terminal holder may pass on to the program (Ctrl-C, Ctrl-\, hang-up).
TERMINAL_SIGNALS = (signal.SIGINT, signal.SIGQUIT, signal.SIGHUP)
# Seam's own plumbing between `seam dap` and the adapter; not the program's business.
PRIVATE_ENV = ("SEAM_DAP_FD", "SEAM_NOTE_FD", "SEAM_PYTHON")
PY_SUFFIXES = (".py", ".pyw", ".pyi")
EVAL_FRAME = "_PyEval_EvalFrameDefault"

R_BREAKPOINT = 1
R_STEP = 2
R_RETURN_NATIVE = 3
R_ATTACHED = 5
R_EXCEPTION = 6
R_UNCAUGHT = 7
LOG_FLAG = 0x100  # set in a trap's reason when logpoint messages are waiting
EVAL_PLEASE_STOP_BIT = 1 << 5   # _PY_EVAL_PLEASE_STOP_BIT in CPython 3.14's pycore_ceval.h

EXCEPTION_FILTERS = [
    {"filter": "uncaught", "label": "Uncaught Python exceptions", "default": True,
     "description": "Stop when an exception nobody handled is about to end the program "
                    "or a thread. The frames it passed through can still be inspected."},
    {"filter": "raised", "label": "Raised Python exceptions", "default": False,
     "description": "Stop when an exception is raised in your code, or first reaches it "
                    "from a library, even if it is handled afterwards."},
    {"filter": "cpp_throw", "label": "C++ throw", "default": False,
     "description": "Stop when native code throws a C++ exception."},
    {"filter": "rust_panic", "label": "Rust panic", "default": False,
     "description": "Stop when Rust code panics."},
]
PYTHON_EXCEPTION_FILTERS = ("uncaught", "raised")

HELPER_SYMBOLS = (
    "seam_trap", "seam_dispatch", "seam_pending", "seam_req_buf", "seam_req_len",
    "seam_resp_ptr", "seam_resp_len", "seam_req_cap", "seam_pend_buf", "seam_pend_len",
    "seam_step_gen",
)

# Source paths that mark a native frame as binding-layer glue rather than user code.
FRAMEWORK_PATHS = (
    "/include/pybind11/", "/include/nanobind/", "/nanobind/src/", "/.cargo/registry/",
    "/rustc/", "/usr/include/", "/usr/lib/", "/usr/local/include/",
    "/include/python3",  # CPython's own header inlines (Py_INCREF, vectorcall helpers)
)
# Function names that are glue even when their line info points into user files
# (macro-generated trampolines, Cython argument-parsing wrappers, module init).
FRAMEWORK_FUNCTIONS = re.compile(
    r"^(__pyx_pw_|__pyx_pymod_|__Pyx_|__pyx_tp_|PyInit_|_GLOBAL__sub_I_|pybind11::|"
    r"nanobind::|pyo3::|core::|alloc::|std::)"
    r"|__pyfunction_|__pymethod_|__pyo3_|_PYO3_DEF|::trampoline")
CYTHON_GLUE = re.compile(r"^(__pyx_pw_|__pyx_pymod_|__Pyx_|__pyx_tp_)")
SYSTEM_LIB_PREFIXES = ("/usr/lib/", "/lib/", "/usr/lib64/", "/lib64/", "[")
# Frame classes a step never ends in: Seam keeps going until user code or Python.
GLUE = ("framework", "nodebug", "system")
MAX_STEP_IN_LOCATIONS = 20000

# Signals that mean the program has gone wrong. They stop the debugger by default, and at
# such a stop Seam runs nothing in the process, not even its own bookkeeping.
FAULT_SIGNALS = ("SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE", "SIGABRT")
# LLDB uses these itself (breakpoints, interrupting); their handling is left alone.
LLDB_SIGNALS = ("SIGTRAP", "SIGSTOP")
# The final stop reply of the debug-server protocol: W<code> exited, X<signal> killed.
EXIT_PACKET = re.compile(r"read packet: \$([WX])([0-9a-fA-F]{2})")
# A stop reply that reports a child process: the thread that made it, then "fork" or
# "vfork" with the child's pid, or "vforkdone" once a vfork child has left the parent's
# memory (it has called exec, or exited).
FORK_PACKET = re.compile(
    r"read packet: \$T[0-9a-fA-F]{2}thread:(?:p[0-9a-f]+\.)?([0-9a-f]+);.*?"
    r"reason:(fork|vforkdone|vfork);(?:.*?fork:p([0-9a-f]+)\.)?")

UNSAFE_MESSAGE = (
    "Seam cannot run Python here: the process is stopped in native code, where the "
    "interpreter may be in an inconsistent state. Python can be evaluated at Python "
    "breakpoints and steps."
)


class DapError(Exception):
    pass


HIT_CONDITION = re.compile(r"^\s*(==|=|>=|>|<=|<|%)?\s*(\d+)\s*$")


def parse_hit_condition(text):
    """A breakpoint's hit-count condition as (operator, number), or None if there is none.

    `5` or `==5`: the fifth hit only. `>=5`, `>5`, `<5`, `<=5`: as written. `%5`: every
    fifth hit. A hit is counted when the breakpoint's ordinary condition, if any, holds.
    """
    if text is None or not str(text).strip():
        return None
    match = HIT_CONDITION.match(str(text))
    if not match or (match.group(1) == "%" and int(match.group(2)) == 0):
        raise DapError("hit count %r is not understood; use a number, optionally after "
                       "one of == >= > <= < %%" % text)
    operator = match.group(1) or "=="
    return ("==" if operator == "=" else operator, int(match.group(2)))


def hit_condition_met(condition, hits):
    operator, number = condition
    return {"==": hits == number, ">=": hits >= number, ">": hits > number,
            "<=": hits <= number, "<": hits < number,
            "%": hits % number == 0}[operator]


def fill_log_message(template, value_of):
    """A logpoint's message with each {expression} replaced by value_of(expression)."""
    return re.sub(r"\{([^{}]*)\}", lambda match: value_of(match.group(1)), template)


class _Arguments(dict):
    """Request arguments: a missing required one is the client's error, not a Seam bug."""

    def __missing__(self, key):
        raise DapError("missing argument '%s'" % key)
