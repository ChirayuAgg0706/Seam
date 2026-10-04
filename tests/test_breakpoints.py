"""Hit-count conditions and logpoints, on Python and on native breakpoints."""
import pytest

from conftest import CAPI_SRC, marker_line, target
from test_python import py_frames

pytestmark = pytest.mark.smoke

LOOP = target("loop.py")
COUNTING = target("counting.py")
VARIABLES = target("variables.py")
WATCH = target("watch.py")
BODY = marker_line(LOOP, "work-body")
NATIVE = marker_line(CAPI_SRC, "add-impl-return")


def stops_until_exit(dap, variable, frame=0):
    """Continue from stop to stop; the value of `variable` at each, then the exit code."""
    seen = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"])
        if name == "exited":
            return seen, body["exitCode"]
        stack = dap.stack(body["threadId"])
        seen.append(dap.scope(stack[frame]["id"])[variable]["value"])
        dap.cont(body["threadId"])


@pytest.mark.parametrize("hit, expected", [
    ("3", ["2"]),                 # the third hit only
    ("==3", ["2"]),
    (">=8", ["7", "8", "9"]),
    (">8", ["8", "9"]),
    ("<3", ["0", "1"]),
    ("<=2", ["0", "1"]),
    ("%4", ["3", "7"]),           # every fourth hit
])
def test_python_hit_count(dap, hit, expected):
    dap.launch(LOOP, dap.python, breakpoints={LOOP: [{"line": BODY, "hitCondition": hit}]})
    assert stops_until_exit(dap, "i") == (expected, 0)


def test_python_hit_count_counts_only_hits_whose_condition_holds(dap):
    spec = {"line": BODY, "condition": "i % 2 == 0", "hitCondition": "3"}
    dap.launch(LOOP, dap.python, breakpoints={LOOP: [spec]})
    assert stops_until_exit(dap, "i") == (["4"], 0)  # i = 0, 2, 4: the third even one


def test_python_logpoint(dap):
    spec = {"line": BODY, "logMessage": "i={i} square={i * i} {nope}"}
    dap.launch(LOOP, dap.python, breakpoints={LOOP: [spec]})
    assert stops_until_exit(dap, "i") == ([], 0)
    for i in range(10):
        assert "i=%d square=%d {NameError: name 'nope' is not defined}\n" % (i, i * i) \
            in dap.output
    assert "total 285" in dap.output


def test_python_logpoint_with_a_hit_count(dap):
    spec = {"line": BODY, "logMessage": "seen {i}", "hitCondition": "%5"}
    dap.launch(LOOP, dap.python, breakpoints={LOOP: [spec]})
    assert stops_until_exit(dap, "i") == ([], 0)
    assert [line for line in dap.output.splitlines() if line.startswith("seen")] == \
        ["seen 4", "seen 9"]


def test_stepping_over_a_call_that_contains_a_logpoint(dap):
    call = marker_line(LOOP, "loop-call")
    dap.launch(LOOP, dap.python, breakpoints={
        LOOP: [call, {"line": BODY, "logMessage": "inside {i}"}]})
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(LOOP, [{"line": BODY, "logMessage": "inside {i}"}])
    # The logpoint fires inside the call being stepped over; the step still ends where
    # a step over ends.
    stop = dap.step("next", tid)
    assert stop["reason"] == "step"
    assert py_frames(dap.stack(tid))[0][0] == "main"
    assert "inside 0\n" in dap.output
    assert dap.status()["agent"]["stepping"] is None
    dap.cont()
    assert dap.wait_exit() == 0
    assert "inside 9\n" in dap.output


def test_a_hit_count_that_makes_no_sense_is_refused(dap):
    dap.launch(LOOP, dap.python, stopOnEntry=True)
    dap.wait_stopped()
    answers = dap.set_breakpoints(LOOP, [{"line": BODY, "hitCondition": "often"},
                                         {"line": BODY + 1, "hitCondition": "2"}])
    assert answers[0]["verified"] is False and "often" in answers[0]["message"]
    assert answers[1] == {"line": BODY + 1, "verified": True}
    answers = dap.set_breakpoints(CAPI_SRC, [{"line": NATIVE, "hitCondition": "%0"}])
    assert answers[0]["verified"] is False
    assert stops_until_exit_after_continue(dap) == (["1"], 0)


def stops_until_exit_after_continue(dap):
    dap.cont()
    return stops_until_exit(dap, "i")


@pytest.mark.parametrize("hit, expected", [
    ("4", ["3"]),
    (">=9", ["8", "9"]),
    ("<=2", ["0", "1"]),
    ("%3", ["2", "5", "8"]),
])
def test_native_hit_count(dap, capi, hit, expected):
    dap.launch(COUNTING, dap.python, env=capi.env,
               breakpoints={CAPI_SRC: [{"line": NATIVE, "hitCondition": hit}]})
    # `i` is read from the Python frame below the native ones: main().
    seen = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"])
        if name == "exited":
            break
        stack = dap.stack(body["threadId"])
        main = [f for f in stack if f["name"] == "main"][0]
        seen.append(dap.scope(main["id"])["i"]["value"])
        dap.cont(body["threadId"])
    assert (seen, body["exitCode"]) == (expected, 0)
    assert "total 45" in dap.output


def test_native_breakpoint_in_a_loop_stops_once_per_iteration(dap, capi, iteration):
    """Continuing from a breakpoint to the same breakpoint, ten times over.

    This is where LLDB's leftover stops show up (see Adapter._is_leftover): without the
    check, about one run in 25 had an extra stop one instruction past the breakpoint.
    """
    dap.launch(COUNTING, dap.python, env=capi.env, breakpoints={CAPI_SRC: [NATIVE]})
    seen = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"])
        if name == "exited":
            break
        assert body["reason"] == "breakpoint"
        stack = dap.stack(body["threadId"])
        assert stack[0]["name"] == "add_impl"
        if capi.opt == "O0":
            assert stack[0]["line"] == NATIVE
        main = [f for f in stack if f["name"] == "main"][0]
        seen.append(dap.scope(main["id"])["i"]["value"])
        dap.cont(body["threadId"])
    assert seen == [str(i) for i in range(10)]
    assert body["exitCode"] == 0


def set_function_breakpoints(dap, *items):
    wanted = [item if isinstance(item, dict) else {"name": item} for item in items]
    return dap.request("setFunctionBreakpoints", {"breakpoints": wanted})["breakpoints"]


def test_function_breakpoint_on_a_python_function(dap):
    dap.launch(LOOP, dap.python, stopOnEntry=True)
    dap.wait_stopped()
    assert set_function_breakpoints(dap, {"name": "work", "condition": "i == 4"}) == \
        [{"verified": True}]
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack)[0] == ("work", BODY - 1)  # the `def` line: the function's entry
    assert dap.scope(stack[0]["id"])["i"]["value"] == "4"
    set_function_breakpoints(dap)
    status = dap.status()["agent"]
    assert status["function_breakpoints"] == [] and status["global_events"] == 0
    dap.cont()
    assert dap.wait_exit() == 0


def test_function_breakpoint_by_qualified_name(dap, capi):
    dap.launch(VARIABLES, dap.python, env=capi.env, stopOnEntry=True)
    dap.wait_stopped()
    set_function_breakpoints(dap, "Point.__init__", "__main__.compute")
    dap.cont()
    stop = dap.wait_stopped()
    assert dap.stack(stop["threadId"])[0]["name"] == "compute"
    dap.cont()
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert [f["name"] for f in stack[:2]] == ["Point.__init__", "compute"]
    assert dap.scope(stack[0]["id"])["x"]["value"] == "3"
    set_function_breakpoints(dap)
    dap.cont()
    assert dap.wait_exit() == 0


def test_function_breakpoint_on_a_native_function(dap, capi):
    dap.launch(COUNTING, dap.python, env=capi.env, stopOnEntry=True)
    dap.wait_stopped()
    # The extension is not loaded yet; the breakpoint takes effect when it is.
    set_function_breakpoints(dap, {"name": "add_impl", "hitCondition": "%4"})
    dap.cont()
    seen = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"])
        if name == "exited":
            break
        stack = dap.stack(body["threadId"])
        assert stack[0]["name"] == "add_impl"
        main = [f for f in stack if f["name"] == "main"][0]
        seen.append(dap.scope(main["id"])["i"]["value"])
        dap.cont(body["threadId"])
    assert (seen, body["exitCode"]) == (["3", "7"], 0)


@pytest.mark.parametrize("hit, expected", [(None, [1, 2, 3, 4, 5]), ("%2", [2, 4])])
def test_data_breakpoint_on_a_native_variable(dap, capi, hit, expected):
    line = marker_line(CAPI_SRC, "bump-here")
    dap.launch(WATCH, dap.python, env=capi.env, breakpoints={CAPI_SRC: [line]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    scopes = {s["name"]: s["variablesReference"]
              for s in dap.request("scopes", {"frameId": stack[0]["id"]})["scopes"]}
    assert dap.variables(scopes["Globals"])["bump_count"]["value"] == "0"

    info = dap.request("dataBreakpointInfo", {"variablesReference": scopes["Globals"],
                                              "name": "bump_count"})
    assert info["dataId"] and "write" in info["accessTypes"]
    wanted = {"dataId": info["dataId"], "accessType": "write"}
    if hit:
        wanted["hitCondition"] = hit
    answers = dap.request("setDataBreakpoints", {"breakpoints": [wanted]})["breakpoints"]
    assert answers == [{"verified": True}], answers

    # A Python variable has no fixed place in memory to watch, and Seam says so.
    main = [f for f in stack if f["name"] == "main"][0]
    python_scope = [s for s in dap.request("scopes", {"frameId": main["id"]})["scopes"]
                    if s["name"] == "Locals"][0]["variablesReference"]
    refused = dap.request("dataBreakpointInfo", {"variablesReference": python_scope,
                                                 "name": "i"})
    assert refused["dataId"] is None and "Python variable" in refused["description"]

    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    seen = []
    while True:
        name, body = dap.wait_any(["stopped", "exited"])
        if name == "exited":
            break
        assert body["reason"] == "data breakpoint", body
        stack = dap.stack(body["threadId"])
        assert stack[0]["name"] == "st_bump" and "main" in [f["name"] for f in stack]
        seen.append(body["description"])
        dap.cont(body["threadId"])
    assert seen == ["bump_count is now %d" % n for n in expected]
    assert body["exitCode"] == 0 and "bumped 5" in dap.output


def test_native_logpoint(dap, capi):
    spec = {"line": NATIVE, "logMessage": "adding {b}: {a + b}"}
    dap.launch(COUNTING, dap.python, env=capi.env, breakpoints={CAPI_SRC: [spec]})
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 0, body
    logged = [line for line in dap.output.splitlines() if line.startswith("adding")]
    assert len(logged) == 10
    if capi.opt == "O0":
        totals = [sum(range(i + 1)) for i in range(10)]
        assert logged == ["adding %d: %d" % (i, totals[i]) for i in range(10)]
    assert "total 45" in dap.output
