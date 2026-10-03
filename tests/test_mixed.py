"""Mixed Python + native scenarios against the plain C-API extension."""
import ast
import json
import time

from conftest import CAPI_SRC, marker_line, target
from test_python import ground_truth, py_frames

MIXED = target("mixed.py")
RUNNING = target("running.py")
THREADS = target("threads.py")


def names(stack):
    return [f["name"] for f in stack]


def test_native_breakpoint_shows_one_merged_stack(dap, capi):
    line = marker_line(CAPI_SRC, "add-impl-return")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={CAPI_SRC: [line]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert names(stack) == ["add_impl", "st_add", "leaf", "st_call_back", "middle", "main",
                            "<module>"]
    assert [f["line"] for f in stack] == [
        line,
        marker_line(CAPI_SRC, "add-call"),
        marker_line(MIXED, "leaf-add"),
        marker_line(CAPI_SRC, "callback-call"),
        marker_line(MIXED, "middle-callback"),
        marker_line(MIXED, "main-middle"),
        marker_line(MIXED, "module-main"),
    ]
    assert stack[0]["source"]["path"] == CAPI_SRC and stack[2]["source"]["path"] == MIXED

    if capi.opt == "O0":
        native = dap.scope(stack[0]["id"])
        assert (native["a"]["value"], native["b"]["value"], native["sum"]["value"]) == \
            ("20", "20", "40")
        assert dap.evaluate("a + b", stack[0]["id"])["result"] == "40"

    # Python locals are readable at a native stop, from memory alone.
    assert dap.scope(stack[2]["id"])["v"]["value"] == "20"
    middle = dap.scope(stack[4]["id"])
    assert middle["label"]["value"] == "'middle'" and middle["v"]["value"] == "20"
    assert dap.scope(stack[5]["id"])["numbers"]["value"] == "[1, 2.5, 'three', None, True]"

    # Running Python here would be unsafe: Seam refuses, and the process stays healthy.
    refused = dap.evaluate("v + 1", stack[2]["id"], check=False)
    assert not refused["success"] and "native code" in refused["message"]
    assert dap.status()["safe"] is False

    dap.set_breakpoints(CAPI_SRC, [])
    dap.cont()
    assert dap.wait_exit() == 0
    for text in ("total 41", "caught native failure", "done"):
        assert text in dap.output


def test_python_breakpoint_below_native_frames(dap, capi):
    line = marker_line(MIXED, "leaf-return")
    dap.launch(MIXED, dap.python, env=capi.env, breakpoints={MIXED: [line]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert names(stack) == ["leaf", "st_call_back", "middle", "main", "<module>"]
    python_part = [(f["name"], f["line"]) for f in stack if f["name"] != "st_call_back"]
    assert python_part == ground_truth(dap, stack[0]["id"])
    assert stack[1]["line"] == marker_line(CAPI_SRC, "callback-call")
    assert dap.evaluate("doubled", stack[0]["id"])["result"] == "40"
    assert dap.scope(stack[2]["id"])["label"]["value"] == "'middle'"
    if capi.opt == "O0":
        assert dap.scope(stack[1]["id"])["depth"]["value"] == "1"
    dap.cont()
    assert dap.wait_exit() == 0


def test_python_and_native_breakpoints_in_one_session(dap, capi):
    native = marker_line(CAPI_SRC, "callback-after")
    python = marker_line(MIXED, "main-print")
    dap.launch(MIXED, dap.python, env=capi.env,
               breakpoints={CAPI_SRC: [native], MIXED: [python]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert names(stack)[:2] == ["st_call_back", "middle"] and stack[0]["line"] == native
    dap.cont()
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack)[0] == ("main", python)
    assert dap.scope(stack[0]["id"])["total"]["value"] == "41"
    dap.cont()
    assert dap.wait_exit() == 0


def test_breakpoints_added_and_removed_while_running(dap, capi, iteration):
    py_line = marker_line(RUNNING, "tick-return")
    c_line = marker_line(CAPI_SRC, "add-impl-return")
    dap.launch(RUNNING, dap.python, env=capi.env)
    time.sleep(0.3)

    # The target sleeps 0.4 s per loop, so after each `continue` there is a window in
    # which the breakpoint cannot be hit and can be removed while the process runs.
    dap.set_breakpoints(RUNNING, [py_line])
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert py_frames(stack)[0] == ("tick", py_line)
    dap.cont()
    dap.set_breakpoints(RUNNING, [])
    assert not dap.drain(1.2, "stopped"), "stopped at a Python breakpoint removed while running"

    dap.set_breakpoints(CAPI_SRC, [c_line])
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert names(stack)[:3] == ["add_impl", "st_add", "tick"]
    dap.cont()
    dap.set_breakpoints(CAPI_SRC, [])
    assert not dap.drain(1.2, "stopped"), "stopped at a native breakpoint removed while running"

    dap.set_breakpoints(RUNNING, [py_line])
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    dap.evaluate("globals().update(STOP=True)", stack[0]["id"])
    dap.set_breakpoints(RUNNING, [])
    status = dap.status()
    assert status["agent"]["global_events"] == 0 and status["agent"]["local_events"] == {}
    assert status["nativeBreakpoints"] == 0
    dap.cont()
    assert dap.wait_exit() == 0
    assert "stopped" in dap.output


THREAD_TRUTH = (
    "__import__('json').dumps({str(t.native_id): [[f.filename, f.name, f.lineno] for f in "
    "__import__('traceback').extract_stack(__import__('sys')._current_frames()[t.ident])] "
    "for t in __import__('threading').enumerate()})")


def test_threads_each_have_a_correct_merged_stack(dap, capi):
    line = marker_line(THREADS, "work-return")
    dap.launch(THREADS, dap.python, env=capi.env, breakpoints={THREADS: [line]})
    stop = dap.wait_stopped()
    tid = stop["threadId"]
    stack = dap.stack(tid)
    assert names(stack)[:3] == ["work", "st_call_back", "worker"]

    truth = json.loads(ast.literal_eval(dap.evaluate(THREAD_TRUTH, stack[0]["id"])["result"]))
    threads = {t["id"] for t in dap.request("threads")["threads"]}
    assert len(truth) == 3 and set(map(int, truth)) <= threads
    native_seen = {}
    for native_id, frames in truth.items():
        expected = [(name, lineno) for filename, name, lineno in reversed(frames)
                    if "seam" not in filename.split("/")[-1]]
        merged = dap.stack(int(native_id))
        # Seam shows qualified names; traceback only has the bare co_name.
        python = [(f["name"].split(".")[-1], f["line"]) for f in merged
                  if f.get("source", {}).get("path", "").endswith(".py")]
        assert python == expected, "thread %s" % native_id
        native_seen[expected[0][0]] = names(merged)
    # The sleeping thread is inside the C function, with its Python caller below it.
    sleeper = native_seen["sleeper"]
    assert "st_sleep_nogil" in sleeper
    assert sleeper.index("st_sleep_nogil") < sleeper.index("sleeper")
    dap.cont()
    assert dap.wait_exit() == 0
    assert "threads done" in dap.output
