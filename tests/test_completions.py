"""What the debug console offers while an expression is being typed."""
import pytest

from conftest import CAPI_SRC, marker_line, target

pytestmark = pytest.mark.smoke

VARS = target("variables.py")


def complete(dap, frame, text, column=None):
    reply = dap.request("completions", {
        "frameId": frame, "text": text, "column": column or len(text) + 1})
    return reply["targets"]


def labels(dap, frame, text, column=None):
    return [t["label"] for t in complete(dap, frame, text, column)]


def test_python_names_and_attributes_are_completed(dap, capi):
    line = marker_line(VARS, "before-call")
    dap.launch(VARS, dap.python, env=capi.env, breakpoints={VARS: [line]})
    stop = dap.wait_stopped()
    frame = dap.stack(stop["threadId"])[0]["id"]

    # Locals, globals and built-ins; the part already typed is what gets replaced.
    found = complete(dap, frame, "bo")
    assert {"box", "bool"} <= {t["label"] for t in found}
    assert all((t["start"], t["length"]) == (1, 2) for t in found)
    assert "LIMIT" in labels(dap, frame, "LIM")
    assert "seamtest" in labels(dap, frame, "print(seam")

    # Attributes, of whatever the dotted name before the cursor is.
    assert labels(dap, frame, "print(box.") == ["items", "label"]
    assert labels(dap, frame, "point.") == ["x", "y"]                 # __slots__
    assert labels(dap, frame, "box.items.app") == ["append"]
    assert "add" in labels(dap, frame, "seamtest.a")
    # Underscore names only once an underscore has been typed.
    assert "__init__" in labels(dap, frame, "box.__")

    # The cursor can be in the middle of the text.
    found = complete(dap, frame, "box.la + 1", column=7)
    assert [(t["label"], t["start"], t["length"]) for t in found] == [("label", 5, 2)]

    # Nothing is offered, and nothing is called, where the object would have to be
    # computed first; and a name that does not exist is not an error.
    assert labels(dap, frame, "box.label.upper().") == []
    assert labels(dap, frame, "no_such_name.") == []
    assert labels(dap, frame, "'text'.up") == []

    # The session is as healthy as before.
    assert dap.evaluate("count + scale", frame)["result"] == "7"
    dap.cont()
    assert dap.wait_exit() == 0


def test_native_stop_completes_native_variables_only(dap, capi):
    line = marker_line(CAPI_SRC, "add-impl-return")
    dap.launch(VARS, dap.python, env=capi.env, breakpoints={CAPI_SRC: [line]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    if capi.opt == "O0":
        assert "sum" in labels(dap, stack[0]["id"], "su")
    assert labels(dap, stack[0]["id"], "sum.") == []

    # Python cannot be run at a native stop, so the Python frames offer nothing; that
    # is not an error, the editor asks on every keystroke.
    compute = [f for f in stack if f["name"] == "compute"][0]
    assert labels(dap, compute["id"], "bo") == []
    dap.cont()
    assert dap.wait_exit() == 0
