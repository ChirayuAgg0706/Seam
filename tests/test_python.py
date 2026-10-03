"""Pure-Python scenarios: breakpoints, stack, variables, evaluate, stepping."""
import ast
import json

from conftest import marker_line, target

BASIC = target("basic.py")
LATE = target("late_module.py")
LOOP = target("loop.py")

GROUND_TRUTH = ("__import__('json').dumps([[f.filename, f.name, f.lineno] "
                "for f in __import__('traceback').extract_stack()])")


def ground_truth(dap, frame_id):
    """The Python stack as Python itself reports it, newest first, Seam frames removed."""
    raw = dap.evaluate(GROUND_TRUTH, frame_id)["result"]
    frames = json.loads(ast.literal_eval(raw))
    return [(name, line) for filename, name, line in reversed(frames)
            if "seam" not in filename.replace("\\", "/").split("/")[-1]]


def py_frames(stack):
    return [(f["name"], f["line"]) for f in stack]


def test_breakpoint_stack_and_variables(dap):
    line = marker_line(BASIC, "inner-first")
    dap.launch(BASIC, dap.python, breakpoints={BASIC: [line]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack) == [
        ("inner", line),
        ("outer", marker_line(BASIC, "outer-call")),
        ("main", marker_line(BASIC, "main-call")),
        ("<module>", marker_line(BASIC, "module-call")),
    ]
    assert stack[0]["source"]["path"] == BASIC
    assert py_frames(stack) == ground_truth(dap, stack[0]["id"])

    local = dap.scope(stack[0]["id"])
    assert local["a"]["value"] == "5" and local["b"]["value"] == "10"
    assert "total" not in local
    outer = dap.scope(stack[1]["id"])
    assert outer["items"]["value"] == "[1, 2, 3]"
    children = dap.variables(outer["items"]["variablesReference"])
    assert [children["[%d]" % i]["value"] for i in range(3)] == ["1", "2", "3"]
    assert dap.scope(stack[2]["id"])["name"]["value"] == "'seam'"

    assert dap.evaluate("a + b", stack[0]["id"])["result"] == "15"
    assert dap.evaluate("len(items)", stack[1]["id"])["result"] == "3"
    failed = dap.evaluate("no_such_name", stack[0]["id"], check=False)
    assert not failed["success"] and "NameError" in failed["message"]

    dap.status()
    dap.cont()
    assert dap.wait_exit() == 0
    assert "result 33" in dap.output and "66" in dap.output


def test_breakpoint_in_module_imported_later(dap):
    line = marker_line(LATE, "late-body")
    dap.launch(BASIC, dap.python, breakpoints={LATE: [line]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack)[0] == ("late", line)
    assert stack[0]["source"]["path"] == LATE
    assert dap.scope(stack[0]["id"])["value"]["value"] == "33"
    dap.status()
    dap.cont()
    assert dap.wait_exit() == 0


def test_add_and_remove_breakpoints_while_stopped(dap):
    first = marker_line(BASIC, "main-call")
    dap.launch(BASIC, dap.python, breakpoints={BASIC: [first]})
    stop = dap.wait_stopped()
    assert py_frames(dap.stack(stop["threadId"]))[0] == ("main", first)
    ret = marker_line(BASIC, "inner-return")
    after = marker_line(BASIC, "outer-after")
    answers = dap.set_breakpoints(BASIC, [ret, after])
    assert [a["line"] for a in answers] == [ret, after] and all(a["verified"] for a in answers)
    dap.cont()
    stop = dap.wait_stopped()
    assert py_frames(dap.stack(stop["threadId"]))[0] == ("inner", ret)
    dap.set_breakpoints(BASIC, [])
    status = dap.status()
    assert status["agent"]["breakpoints"] == {}
    assert status["agent"]["global_events"] == 0
    assert status["agent"]["local_events"] == {}
    dap.cont()
    assert dap.wait_exit() == 0


def test_conditional_breakpoint(dap):
    body = marker_line(LOOP, "work-body")
    dap.launch(LOOP, dap.python, breakpoints={LOOP: [{"line": body, "condition": "i == 7"}]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack)[0] == ("work", body)
    assert dap.scope(stack[0]["id"])["i"]["value"] == "7"
    assert dap.scope(stack[1]["id"])["total"]["value"] == str(sum(i * i for i in range(7)))
    dap.cont()
    assert dap.wait_exit() == 0
    assert "total 285" in dap.output


def test_breakpoint_on_a_line_without_code_moves_to_the_next_line(dap):
    target_line = marker_line(BASIC, "outer-def")
    dap.launch(BASIC, dap.python, stopOnEntry=True)
    stop = dap.wait_stopped()
    answers = dap.set_breakpoints(BASIC, [target_line - 1])
    assert answers == [{"line": target_line, "verified": True}]
    dap.cont()
    stop = dap.wait_stopped()
    assert py_frames(dap.stack(stop["threadId"])) == [("<module>", target_line)]
    dap.set_breakpoints(BASIC, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_stepping(dap, iteration):
    call = marker_line(BASIC, "outer-call")
    dap.launch(BASIC, dap.python, breakpoints={BASIC: [call]})
    stop = dap.wait_stopped()
    tid = stop["threadId"]
    assert py_frames(dap.stack(tid))[0] == ("outer", call)

    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    assert py_frames(dap.stack(tid))[:2] == [("inner", marker_line(BASIC, "inner-first")),
                                             ("outer", call)]
    stop = dap.step("next", tid)
    assert py_frames(dap.stack(tid))[0] == ("inner", marker_line(BASIC, "inner-return"))
    assert dap.scope(dap.stack(tid)[0]["id"])["total"]["value"] == "15"

    # Stepping over the last line returns to the calling line.
    stop = dap.step("next", tid)
    assert py_frames(dap.stack(tid))[0] == ("outer", call)
    stop = dap.step("next", tid)
    assert py_frames(dap.stack(tid))[0] == ("outer", marker_line(BASIC, "outer-after"))
    assert dap.scope(dap.stack(tid)[0]["id"])["result"]["value"] == "30"

    stop = dap.step("stepOut", tid)
    assert py_frames(dap.stack(tid))[0] == ("main", marker_line(BASIC, "main-call"))
    stop = dap.step("next", tid)
    assert py_frames(dap.stack(tid))[0] == ("main", marker_line(BASIC, "main-print"))
    assert dap.scope(dap.stack(tid)[0]["id"])["x"]["value"] == "33"

    status = dap.status()
    assert status["agent"]["stepping"] is None
    assert status["agent"]["global_events"] == 1  # PY_START only, for the breakpoint
    dap.set_breakpoints(BASIC, [])
    status = dap.status()
    assert status["agent"]["global_events"] == 0 and status["agent"]["local_events"] == {}
    dap.cont()
    assert dap.wait_exit() == 0


def test_step_over_a_call_does_not_stop_inside(dap, iteration):
    call = marker_line(BASIC, "main-call")
    dap.launch(BASIC, dap.python, breakpoints={BASIC: [call]})
    tid = dap.wait_stopped()["threadId"]
    dap.step("next", tid)
    assert py_frames(dap.stack(tid))[0] == ("main", marker_line(BASIC, "main-print"))
    dap.set_breakpoints(BASIC, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_breakpoint_inside_a_stepped_over_call_wins(dap, iteration):
    call = marker_line(BASIC, "main-call")
    inner = marker_line(BASIC, "inner-first")
    dap.launch(BASIC, dap.python, breakpoints={BASIC: [call, inner]})
    tid = dap.wait_stopped()["threadId"]
    stop = dap.step("next", tid)
    assert stop["reason"] == "breakpoint"
    assert py_frames(dap.stack(tid))[0] == ("inner", inner)
    assert dap.status()["agent"]["stepping"] is None
    dap.cont()
    assert dap.wait_exit() == 0


def test_stop_on_entry(dap):
    dap.launch(BASIC, dap.python, stopOnEntry=True)
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack) == [("<module>", 1)]
    dap.cont()
    assert dap.wait_exit() == 0
