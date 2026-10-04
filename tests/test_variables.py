"""Looking into variables and changing them: Python locals, members, native locals."""
import pytest

from conftest import CAPI_SRC, marker_line, target

pytestmark = pytest.mark.smoke

VARS = target("variables.py")


def set_variable(dap, reference, name, value, check=True):
    return dap.request("setVariable", {"variablesReference": reference, "name": name,
                                       "value": value}, check=check)


def scope_reference(dap, frame_id, name="Locals"):
    return [s for s in dap.request("scopes", {"frameId": frame_id})["scopes"]
            if s["name"] == name][0]["variablesReference"]


def test_python_variables_can_be_inspected_and_changed(dap, capi):
    line = marker_line(VARS, "before-call")
    dap.launch(VARS, dap.python, env=capi.env, breakpoints={VARS: [line]})
    stop = dap.wait_stopped()
    frame = dap.stack(stop["threadId"])[0]["id"]
    local_ref = scope_reference(dap, frame)
    local = dap.variables(local_ref)
    assert (local["count"]["value"], local["scale"]["value"]) == ("5", "2")
    assert local["box"]["evaluateName"] == "box"

    # A local, from an expression evaluated in the frame.
    reply = set_variable(dap, local_ref, "scale", "count * 2")
    assert (reply["value"], reply["type"]) == ("10", "int")
    assert dap.variables(local_ref)["scale"]["value"] == "10"

    # Members: an attribute, a list item, a dict entry, a slot.
    box = dap.variables(local["box"]["variablesReference"])
    assert box["items"]["evaluateName"] == "box.items"
    assert set_variable(dap, local["box"]["variablesReference"], "label",
                        "'renamed'")["value"] == "'renamed'"
    items = dap.variables(box["items"]["variablesReference"])
    assert items["[0]"]["evaluateName"] == "box.items[0]"
    set_variable(dap, box["items"]["variablesReference"], "[0]", "50")
    table = dap.variables(local["table"]["variablesReference"])
    assert table["'a'"]["evaluateName"] == "table['a']"
    set_variable(dap, local["table"]["variablesReference"], "'a'", "7")
    point = dap.variables(local["point"]["variablesReference"])
    assert (point["x"]["value"], point["y"]["value"]) == ("3", "4")  # __slots__, no __dict__
    set_variable(dap, local["point"]["variablesReference"], "x", "30")

    # A global, and an assignment typed into the debug console.
    set_variable(dap, scope_reference(dap, frame, "Globals"), "LIMIT", "100")
    dap.evaluate("count = 6", frame)
    assert dap.variables(local_ref)["count"]["value"] == "6"

    # A long list arrives in the pages the client asks for.
    assert local["big"]["indexedVariables"] == 1000
    page = dap.request("variables", {"variablesReference": local["big"]["variablesReference"],
                                     "filter": "indexed", "start": 990, "count": 5})["variables"]
    assert [(v["name"], v["value"]) for v in page] == \
        [("[%d]" % i, str(i)) for i in range(990, 995)]

    # What cannot be done says why, and changes nothing.
    bad = set_variable(dap, local_ref, "scale", "no_such_name + 1", check=False)
    assert not bad["success"] and "NameError" in bad["message"]
    frozen = dap.evaluate("(1, 2)", frame)
    bad = set_variable(dap, frozen["variablesReference"], "[0]", "9", check=False)
    assert not bad["success"] and "cannot be changed" in bad["message"]
    assert dap.variables(local_ref)["scale"]["value"] == "10"

    # The program carries on with the new values:
    # add(6, 10) = 16; 16 * 10 + 30 + 50 + 7 + 100 = 347.
    dap.cont()
    assert dap.wait_exit() == 0
    assert "result 347 renamed 1000" in dap.output


def test_native_variable_can_be_changed(dap, capi):
    if capi.opt != "O0":
        pytest.skip("optimised code keeps no addressable copy of the local to change")
    line = marker_line(CAPI_SRC, "add-impl-return")
    dap.launch(VARS, dap.python, env=capi.env, breakpoints={CAPI_SRC: [line]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    native_ref = scope_reference(dap, stack[0]["id"])
    assert dap.variables(native_ref)["sum"]["value"] == "7"
    assert set_variable(dap, native_ref, "sum", "40")["value"] == "40"
    assert dap.variables(native_ref)["sum"]["value"] == "40"
    bad = set_variable(dap, native_ref, "sum", "not a number", check=False)
    assert not bad["success"] and "cannot set sum" in bad["message"]

    # Python variables of the frames below cannot be changed from a native stop.
    compute = [f for f in stack if f["name"] == "compute"][0]
    refused = set_variable(dap, scope_reference(dap, compute["id"]), "scale", "3", check=False)
    assert not refused["success"] and "native code" in refused["message"]

    # add() now returns 40: 40 * 2 + 3 + 1 + 1 + 10 = 95.
    dap.cont()
    assert dap.wait_exit() == 0
    assert "result 95 box 1000" in dap.output
