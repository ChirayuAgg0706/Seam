"""Regressions found by a developer trying Seam without its documentation."""
import os
import subprocess

import pybind11
import pytest

from conftest import EXT, SHARED_FLAGS, Extension, _run, marker_line, target

pytestmark = pytest.mark.smoke
SCRIPT = target("inspection.py")
SOURCE = os.path.join(EXT, "pybind11", "inspection.cpp")


@pytest.fixture(scope="session")
def inspection(tmp_path_factory, pyinfo, request):
    directory = str(tmp_path_factory.mktemp("inspection"))
    opt = request.config.getoption("--opt")
    _run(["g++", *SHARED_FLAGS, "-g", "-" + opt, "-std=c++17",
          "-I", pyinfo["include"], "-I", pybind11.get_include(), SOURCE,
          "-o", os.path.join(directory, "seam_inspection.so")])
    return Extension(directory, opt, SOURCE, "seam_inspection", "pybind11")


def test_launch_does_not_add_imports_to_main(dap):
    dap.launch(target("clean_main.py"), dap.python)
    assert dap.wait_exit() == 0
    assert "clean []" in dap.output and "leaked" not in dap.output


@pytest.mark.parametrize("function", [False, True])
def test_invalid_python_condition_is_reported_once(dap, function):
    loop = target("loop.py")
    spec = {"condition": "no_such_name"}
    if function:
        dap.launch(loop, dap.python, stopOnEntry=True)
        dap.wait_stopped()
        dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "work", **spec}]})
        dap.cont()
    else:
        dap.launch(loop, dap.python, breakpoints={loop: [
            {"line": marker_line(loop, "work-body"), **spec}]})
    for _ in range(10):
        assert dap.wait_stopped()["reason"] == "breakpoint"
        dap.cont()
    assert dap.wait_exit() == 0
    assert dap.output.count("could not be evaluated") == 1
    assert "no_such_name" in dap.output and "NameError" in dap.output


def test_invalid_cpp_string_condition_is_reported_once(dap, inspection):
    # At -O2 name is optimised out; either failure still needs an explanation.
    dap.launch(SCRIPT, dap.python, env=inspection.env, breakpoints={SOURCE: [
        {"line": marker_line(SOURCE, "inspection-return"), "condition": 'name == 42'}]})
    lines = []
    for _ in range(2):
        stop = dap.wait_stopped()
        assert stop["reason"] == "breakpoint"
        lines.append(dap.stack(stop["threadId"])[0]["line"])
        dap.cont()
    assert dap.wait_exit() == 0
    assert dap.output.count("could not be evaluated") == 1
    changed = [e["body"]["breakpoint"] for e in dap.events if e["event"] == "breakpoint"]
    assert any("condition could not be evaluated" in b.get("message", "") for b in changed)
    errors = [b for b in changed if "condition could not be evaluated" in b.get("message", "")]
    assert len(errors) == 1 and errors[0]["line"] == lines[0]


def test_invalid_condition_reports_before_its_hit_count_is_reached(dap):
    loop = target("loop.py")
    dap.launch(loop, dap.python, breakpoints={loop: [
        {"line": marker_line(loop, "work-body"), "condition": "no_such_name",
         "hitCondition": "20"}]})
    assert dap.wait_exit() == 0
    assert dap.output.count("could not be evaluated") == 1


def test_globals_and_native_expression_hint(dap, inspection):
    dap.launch(SCRIPT, dap.python, env=inspection.env,
               breakpoints={SOURCE: [marker_line(SOURCE, "inspection-return")]})
    tid = dap.wait_stopped()["threadId"]
    frame = dap.stack(tid)[0]["id"]
    variables = dap.scope(frame, "Globals")
    assert set(variables) == {"LIMIT", "calls"}, variables
    assert variables["LIMIT"]["value"] == "17"
    refused = dap.evaluate("mixer.__class__", frame, check=False)
    assert not refused["success"] and "native frame" in refused["message"]
    assert "Python line" in refused["message"]
    dap.set_breakpoints(SOURCE, [])
    dap.cont(tid)
    assert dap.wait_exit() == 0


@pytest.mark.parametrize("use_frame", [False, True])
def test_local_watchpoint_expires_when_stack_slot_is_reused(dap, inspection, use_frame):
    dap.launch(SCRIPT, dap.python, env=inspection.env,
               breakpoints={SOURCE: [marker_line(SOURCE, "watch-local")]})
    tid = dap.wait_stopped()["threadId"]
    frame = dap.stack(tid)[0]["id"]
    scope = dap.request("scopes", {"frameId": frame})["scopes"][0]["variablesReference"]
    context = {"frameId": frame} if use_frame else {"variablesReference": scope}
    info = dap.request("dataBreakpointInfo", {"name": "slot", **context})
    assert len(info["dataId"].split("/")) == 6, info
    answer = dap.request("setDataBreakpoints", {"breakpoints": [{"dataId": info["dataId"]}]})
    assert answer["breakpoints"] == [{"verified": True}]
    dap.set_breakpoints(SOURCE, [])
    dap.cont(tid)
    stops = []
    while True:
        event, body = dap.wait_any(["stopped", "exited"])
        if event == "exited":
            break
        assert body["reason"] == "data breakpoint"
        stops.append(body["description"])
        dap.cont(body["threadId"])
    assert stops == ["slot is now 11", "slot is now 12"]
    assert body["exitCode"] == 0
    assert "removed the data breakpoint on slot" in dap.output


def test_python_thread_name_and_type_survive_native_stop(dap, inspection):
    line = marker_line(SCRIPT, "inspection-python")
    dap.launch(SCRIPT, dap.python, env=inspection.env, args=["thread"],
               breakpoints={SCRIPT: [line], SOURCE: [marker_line(SOURCE, "inspection-return")]})
    tid = dap.wait_stopped()["threadId"]
    assert next(t for t in dap.request("threads")["threads"] if t["id"] == tid)["name"] \
        == "worker-0 (%d)" % tid
    assert dap.scope(dap.stack(tid)[0]["id"])["mixer"]["type"] == "Mixer"
    dap.cont(tid)
    assert dap.wait_stopped()["threadId"] == tid
    assert next(t for t in dap.request("threads")["threads"] if t["id"] == tid)["name"] \
        == "worker-0 (%d)" % tid
    python = next(f for f in dap.stack(tid) if f["name"] == "run")
    assert dap.scope(python["id"])["mixer"]["type"] == "Mixer"
    dap.set_breakpoints(SOURCE, [])
    dap.cont(tid)
    assert dap.wait_exit() == 0


@pytest.mark.parametrize("just_my_code", [True, False])
def test_python_library_frames_are_deemphasized(dap, inspection, just_my_code):
    dap.launch(SCRIPT, dap.python, env=inspection.env, args=["library"], justMyCode=just_my_code,
               breakpoints={SCRIPT: [marker_line(SCRIPT, "inspection-python")]})
    stack = dap.stack(dap.wait_stopped()["threadId"])
    library = [f for f in stack if "/unittest/" in f.get("source", {}).get("path", "")]
    assert library
    for frame in library:
        assert (frame.get("presentationHint") == "subtle") == just_my_code
        if just_my_code:
            assert frame["source"]["presentationHint"] == "deemphasize"
            assert "Python library" in frame["source"]["origin"]
    assert "presentationHint" not in stack[0]
    dap.cont()
    assert dap.wait_exit() == 0


def test_optimized_locals_have_an_explanation(dap, inspection):
    if inspection.opt != "O2":
        pytest.skip("requires optimised native code")
    dap.launch(SCRIPT, dap.python, env=inspection.env,
               breakpoints={SOURCE: [marker_line(SOURCE, "inspection-return")]})
    frame = dap.stack(dap.wait_stopped()["threadId"])[0]["id"]
    variables = dap.scope(frame)
    # GCC and Clang retain different optimized variables. A retained value must be
    # correct; an unavailable value must have an explanation and no expandable ref.
    for name, retained in (("unused", "100"), ("name", '"wrong"')):
        variable = variables[name]
        if variable["value"].startswith(("<optimized out>", "<unavailable:")):
            assert variable["variablesReference"] == 0, variable
        else:
            assert variable["value"] == retained, variable
    dap.set_breakpoints(SOURCE, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_help_points_to_requirements_editors_and_docs(python):
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(EXT), "..", "src"))
    help_text = subprocess.check_output([python, "-m", "seam", "--help"], env=env, text=True)
    for text in ("LLDB 18+", "CPython 3.12+", "VS Code", "Neovim", "#readme", "doctor"):
        assert text in help_text


@pytest.mark.parametrize("kind", ["derived", "integer"])
def test_cpp_exception_message_and_nonstandard_throw(dap, inspection, tmp_path, kind):
    script = tmp_path / "throw.py"
    script.write_text(
        "import seam_inspection\n"
        "try:\n"
        "    seam_inspection.fail(%r)\n"
        "except BaseException:\n"
        "    print('caught')\n" % ("integer" if kind == "integer" else "derived message"))
    dap.launch(str(script), dap.python, env=inspection.env, exceptions=["cpp_throw"])
    stop = dap.wait_stopped()
    info = dap.request("exceptionInfo", {"threadId": stop["threadId"]})
    if kind == "derived":
        assert stop["description"] == "C++ exception thrown: derived message", dap.tail_log()
        assert info["exceptionId"] == "TrialError"
    else:
        assert stop["description"] == "C++ exception thrown"
        assert info["exceptionId"] == "int"
    dap.cont()
    assert dap.wait_exit() == 0 and "caught" in dap.output
