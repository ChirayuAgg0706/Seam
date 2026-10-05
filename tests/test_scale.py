"""Does Seam hold up on native code of realistic size? Opt-in: SEAM_TEST_SCALE=1.

Every extension of the ordinary suite has a handful of functions. These scenarios run the
same debugging session against `seamtest` and against a generated extension with 15,000
functions (tests/ext/gen_large.py), time every request from the client side, and fail if
anything that takes milliseconds on the small module takes seconds on the large one. The
limits are generous: they are meant to catch a cost that grows with the size of a module
(an order of magnitude), not a slow machine.

    SEAM_TEST_SCALE=1 scripts/test.sh -q -s tests/test_scale.py
    SEAM_SCALE_FUNCTIONS=60000   another size (default 15000, 500 functions per file)
    SEAM_SCALE_DIR=...           where the module is generated and built
                                 (default ~/.cache/seam/scale)

The table of measurements is printed and written to $SEAM_SCALE_DIR/report-<opt>.md. The
adapter's log is written with timestamps (SEAM_LOG_TIMES=1); for anything slower than
half a second the report names the two log lines the longest wait lies between.
"""
import os
import re
import subprocess
import sys
import time

import pytest

from conftest import CAPI_SRC, EXT, marker_line, target

sys.path.insert(0, EXT)
import gen_large  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("SEAM_TEST_SCALE") != "1",
                                reason="scale scenarios are opt-in: set SEAM_TEST_SCALE=1")

SCALE = target("scale.py")
BENCH = target("scale_bench.py")
FUNCTIONS = int(os.environ.get("SEAM_SCALE_FUNCTIONS", "15000"))
FILES = max(1, FUNCTIONS // 500)
SCALE_DIR = os.environ.get("SEAM_SCALE_DIR", os.path.expanduser("~/.cache/seam/scale"))
PROBE = FUNCTIONS // 2  # the function the scenarios step into: one in the middle

# Seconds. With 15,000 functions a step or a request takes 0.05 s or less on an idle
# machine, as it does with seamtest alone; before entry traps a step-in took 1.3 s.
LIMIT = 1.0
# The first step-in of a session resolves and classifies every function of the module
# (0.6 s for 15,000), and starting the program pays for LLDB reading its symbols.
ONCE_LIMITS = {"first time": 5.0, "launch": 15.0, "run to": 15.0}


class Subject:
    """What one run of the scenario debugs: a module, one of its functions, its sources."""

    def __init__(self, label, module, function, c_function, source, helper_return,
                 callback_source, callback_function, others=()):
        self.label = label
        self.module = module
        self.function = function            # as Python calls it
        self.c_function = c_function        # as the debug info calls it
        self.source = source                # file of c_function
        self.helper_return = helper_return  # (file, line) inside the helper c_function calls
        self.callback_source = callback_source
        self.callback_function = callback_function
        self.others = list(others)


@pytest.fixture(scope="module")
def large(request, pyinfo):
    """The generated extension, built at the requested optimisation level."""
    opt = request.config.getoption("--opt")
    directory = os.path.join(SCALE_DIR, "large-%d" % FUNCTIONS)
    out_dir = gen_large.build(directory, opt, FUNCTIONS, FILES, include=pyinfo["include"])
    return directory, out_dir


@pytest.fixture(scope="module")
def subjects(large, capi):
    directory, out_dir = large
    tag = gen_large.function_name(PROBE)
    source = gen_large.source_of(directory, PROBE, FUNCTIONS, FILES)
    main = gen_large.main_source(directory)
    mix = "mix%02d-return" % (PROBE * FILES // FUNCTIONS)
    small = dict(module="seamtest", function="add", c_function="st_add", source=CAPI_SRC,
                 helper_return=(CAPI_SRC, marker_line(CAPI_SRC, "add-impl-return")),
                 callback_source=CAPI_SRC, callback_function="st_call_back")
    env = {"PYTHONPATH": capi.dir + os.pathsep + out_dir}
    return env, [
        Subject("seamtest alone", **small),
        Subject("seamtest, large module loaded", others=["seam_large"], **small),
        Subject("large module", "seam_large", tag, "lg_" + tag, source,
                (source, marker_line(source, mix)), main, "lg_call_back",
                others=["seamtest"]),
    ]


class Clock:
    """Times requests from the client side and finds the long waits in the adapter's log."""

    def __init__(self, dap):
        self.dap = dap
        self.rows = []       # (label, seconds, where the time went)
        self.started = None
        self.offset = 0

    def start(self):
        self.started = time.monotonic()
        try:
            self.offset = os.path.getsize(self.dap.log_path)
        except OSError:
            self.offset = 0

    def stop(self, label):
        seconds = time.monotonic() - self.started
        self.rows.append((label, seconds, self.longest_wait() if seconds > 0.5 else ""))
        return seconds

    def longest_wait(self):
        """The two consecutive log lines of the last measurement that are furthest apart."""
        try:
            with open(self.dap.log_path, errors="replace") as fh:
                fh.seek(self.offset)
                lines = fh.readlines()
        except OSError:
            return ""
        stamped = []
        for line in lines:
            match = re.match(r"\s*(\d+\.\d+) (.*)", line)
            if match:
                stamped.append((float(match.group(1)), match.group(2).strip()))
        best = (0.0, "", "")
        for (t0, before), (t1, after) in zip(stamped, stamped[1:]):
            if t1 - t0 > best[0]:
                best = (t1 - t0, before, after)
        if not best[0]:
            return ""
        return "%.2f s between `%s` and `%s`" % (best[0], best[1][:70], best[2][:70])

    def request(self, label, command, arguments=None):
        self.start()
        body = self.dap.request(command, arguments, timeout=120)
        self.stop(label)
        return body

    def step(self, label, command, tid):
        self.start()
        self.dap.request(command, {"threadId": tid})
        stop = self.dap.wait_stopped(timeout=120)
        self.stop(label)
        return stop

    def cont(self, label, tid):
        self.start()
        self.dap.cont(tid)
        stop = self.dap.wait_stopped(timeout=120)
        self.stop(label)
        return stop


def top(dap, tid):
    frame = dap.stack(tid)[0]
    return frame["name"], frame["line"], frame.get("source", {}).get("path")


def run_scenario(dap, python, env, subject, opt):
    """One debugging session through every kind of step. Returns the clock's rows."""
    clock = Clock(dap)
    exact = opt == "O0"
    line = {name: marker_line(SCALE, name) for name in (
        "after-import", "first-call", "second-call", "third-call", "callback-call",
        "python-call", "last-call", "print-total", "cb-body", "helper-body")}

    def at_python(tid, marker):
        name, where, path = top(dap, tid)
        assert (path, where) == (SCALE, line[marker]), (subject.label, marker, name, where, path)

    def in_native(tid, function):
        name, where, path = top(dap, tid)
        assert function in name, (subject.label, function, name, where, path)

    clock.start()
    dap.launch(SCALE, python, args=[subject.module, subject.function] + subject.others,
               env=env, breakpoints={SCALE: [line["after-import"]]})
    tid = dap.wait_stopped(timeout=120)["threadId"]
    clock.stop("launch to first stop (Python breakpoint, modules loaded)")
    at_python(tid, "after-import")

    # Breakpoints set while the module is loaded.
    helper_file, helper_line = subject.helper_return
    clock.start()
    answer = dap.set_breakpoints(helper_file, [helper_line])
    clock.stop("set native breakpoint by file and line")
    assert answer[0]["verified"], answer
    clock.request("set function breakpoint by name", "setFunctionBreakpoints",
                  {"breakpoints": [{"name": subject.c_function}]})

    clock.cont("continue to the function breakpoint", tid)
    in_native(tid, subject.c_function)
    clock.start()
    stack = dap.stack(tid)
    clock.stop("stack trace at a native stop")
    assert [f["name"] for f in stack[1:]] == ["main", "<module>"], stack
    clock.start()
    variables = dap.scope(stack[0]["id"])
    clock.stop("variables at a native stop")
    assert "args" in variables, variables
    dap.request("setFunctionBreakpoints", {"breakpoints": []})

    clock.cont("continue to the native line breakpoint", tid)
    name, where, path = top(dap, tid)
    assert path == helper_file and (not exact or where == helper_line), (name, where, path)
    clock.step("step out, native to native", "stepOut", tid)
    in_native(tid, subject.c_function)
    clock.step("step out, native to Python", "stepOut", tid)
    at_python(tid, "first-call")
    clock.start()
    dap.set_breakpoints(helper_file, [])
    clock.stop("remove the native breakpoint")

    clock.step("step over a Python line", "next", tid)
    at_python(tid, "second-call")
    clock.step("step in, Python to native (first time)", "stepIn", tid)
    in_native(tid, subject.c_function)
    assert top(dap, tid)[2] == subject.source
    clock.step("step over a native line", "next", tid)
    in_native(tid, subject.c_function)
    clock.step("step out, native to Python", "stepOut", tid)
    at_python(tid, "second-call")

    dap.step("next", tid)
    at_python(tid, "third-call")
    clock.step("step in, Python to native (again)", "stepIn", tid)
    in_native(tid, subject.c_function)
    dap.step("stepOut", tid)
    dap.step("next", tid)
    at_python(tid, "callback-call")

    clock.step("step in, Python to native (third time)", "stepIn", tid)
    in_native(tid, subject.callback_function)
    call = marker_line(subject.callback_source, "callback-call")
    for _ in range(4):
        if top(dap, tid)[1] >= call:
            break
        dap.step("next", tid)
    # With optimisation several presses can be needed (see STATUS.md); the time is for all.
    clock.start()
    for _ in range(12):
        dap.step("stepIn", tid)
        if top(dap, tid)[0] == "cb":
            break
    clock.stop("step in, native to a Python callback")
    at_python(tid, "cb-body")
    clock.step("step out, Python callback to native", "stepOut", tid)
    in_native(tid, subject.callback_function)
    dap.step("stepOut", tid)
    at_python(tid, "callback-call")

    dap.step("next", tid)
    at_python(tid, "python-call")
    clock.step("step in, Python to Python", "stepIn", tid)
    at_python(tid, "helper-body")
    clock.step("step out, Python to Python", "stepOut", tid)
    at_python(tid, "python-call")
    dap.step("next", tid)
    at_python(tid, "last-call")
    clock.step("step over a Python line that calls native", "next", tid)
    at_python(tid, "print-total")

    status = dap.status()
    assert status["stepInBreakpointsEnabled"] is False and status["pythonStepArmed"] is False
    clock.start()
    dap.cont()
    assert dap.wait_exit(timeout=120) == 0
    clock.stop("continue to exit")
    return clock.rows


def run_pending(dap, python, env, subject):
    """Native breakpoints given before the module is loaded. Returns the clock's rows."""
    clock = Clock(dap)
    helper_file, helper_line = subject.helper_return
    clock.start()
    dap.request("initialize", {"adapterID": "seam", "clientID": "tests"})
    dap.request("launch", {"program": SCALE, "python": python, "cwd": os.path.dirname(SCALE),
                           "args": [subject.module, subject.function] + subject.others,
                           "env": env})
    dap.wait_event("initialized")
    clock.stop("launch request")
    clock.start()
    answer = dap.set_breakpoints(helper_file, [helper_line])
    clock.stop("set native breakpoint by file and line, module not loaded")
    assert not answer[0]["verified"], answer
    clock.request("set function breakpoint by name, module not loaded",
                  "setFunctionBreakpoints", {"breakpoints": [{"name": subject.c_function}]})
    clock.start()
    dap.request("configurationDone")
    tid = dap.wait_stopped(timeout=120)["threadId"]
    clock.stop("run to the pending function breakpoint (module loads on the way)")
    assert subject.c_function in top(dap, tid)[0]
    clock.cont("continue to the pending line breakpoint", tid)
    assert top(dap, tid)[2] == helper_file
    dap.set_breakpoints(helper_file, [])
    dap.request("setFunctionBreakpoints", {"breakpoints": []})
    dap.cont()
    assert dap.wait_exit(timeout=120) == 0
    return clock.rows


def table(columns, results):
    """A Markdown table: one row per measurement, one column per subject."""
    labels = []
    for rows in results:
        for label, _, _ in rows:
            if label not in labels:
                labels.append(label)
    out = ["| | " + " | ".join(columns) + " |", "|---|" + "---:|" * len(columns)]
    notes = []
    for label in labels:
        cells = []
        for column, rows in zip(columns, results):
            found = [(seconds, where) for name, seconds, where in rows if name == label]
            cells.append(" / ".join("%.3f" % seconds for seconds, _ in found) or "")
            notes += ["- %s, %s: %s" % (column, label, where) for _, where in found if where]
        out.append("| %s | %s |" % (label, " | ".join(cells)))
    return "\n".join(out + [""] + notes) + "\n"


def report(request, title, text):
    opt = request.config.getoption("--opt")
    text = "## %s (%d functions, -%s)\n\n%s\n" % (title, FUNCTIONS, opt, text)
    print("\n" + text)
    os.makedirs(SCALE_DIR, exist_ok=True)
    with open(os.path.join(SCALE_DIR, "report-%s.md" % opt), "a") as fh:
        fh.write(text)


@pytest.fixture
def timed_client(make_client, monkeypatch):
    monkeypatch.setenv("SEAM_LOG_TIMES", "1")
    clients = []

    def make():
        clients.append(make_client())
        return clients[-1]
    yield make
    for client in clients:
        client.close()


def too_slow(rows):
    def limit(label):
        return next((seconds for word, seconds in ONCE_LIMITS.items() if word in label), LIMIT)

    return ["%s took %.2f s (%s)" % (label, seconds, where or "no log detail")
            for label, seconds, where in rows if seconds > limit(label)]


def test_stepping_and_breakpoints_do_not_slow_down_with_module_size(
        timed_client, python, subjects, request):
    env, cases = subjects
    opt = request.config.getoption("--opt")
    results = [run_scenario(timed_client(), python, env, subject, opt) for subject in cases]
    report(request, "One session, every kind of step: seconds",
           table([subject.label for subject in cases], results))
    slow = too_slow(results[-1]) + too_slow(results[-2])
    assert not slow, "\n".join(slow)


def test_pending_native_breakpoints_resolve_when_a_large_module_loads(
        timed_client, python, subjects, request):
    env, cases = subjects
    results = [run_pending(timed_client(), python, env, subject) for subject in cases]
    report(request, "Breakpoints set before the module is loaded: seconds",
           table([subject.label for subject in cases], results))
    slow = too_slow(results[-1]) + too_slow(results[-2])
    assert not slow, "\n".join(slow)


def bench(python, env, args):
    out = subprocess.run([python, BENCH] + args, capture_output=True, text=True, check=True,
                         env=dict(os.environ, **env)).stdout
    return float(re.search(r"elapsed ([\d.]+)", out).group(1))


def bench_under_seam(make, python, env, args):
    dap = make()
    dap.launch(BENCH, python, args=args, env=env)
    assert dap.wait_exit(timeout=300) == 0
    return float(re.search(r"elapsed ([\d.]+)", dap.output).group(1))


@pytest.mark.parametrize("mode", ["cpu", "native"])
def test_overhead_without_breakpoints_with_a_large_module_loaded(
        timed_client, python, subjects, request, mode):
    """As test_overhead.py, with the large module loaded and (native) being called."""
    env, _ = subjects
    args = [mode, "seam_large", gen_large.function_name(PROBE)]
    base, seam = [], []
    for _ in range(3):
        for _ in range(3):
            base.append(bench(python, env, args))
            seam.append(bench_under_seam(timed_client, python, env, args))
        if min(seam) / min(base) < 1.10:
            break
    ratio = min(seam) / min(base)
    report(request, "Overhead with no breakpoints, large module loaded [%s]" % mode,
           "plain %.3f s, under Seam %.3f s, ratio %.3f\n" % (min(base), min(seam), ratio))
    assert ratio < 1.10, "Seam slowed the %s workload by %.1f%%" % (mode, (ratio - 1) * 100)
