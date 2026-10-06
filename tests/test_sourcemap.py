"""Source paths: extensions whose debug info names files that are somewhere else.

Each build below is made from a copy of the sources in a scratch directory, which is then
removed. What is left is what a user has after a build in a container, in CI or with
remapped paths: a library whose debug info names files that are not there, and the sources
somewhere else (here: in the repository).
"""
import os
import shutil
import signal
import subprocess
import sys
import sysconfig

import pytest

from conftest import BUILD, EXT, LAYERS, _run, _unavailable, marker_line, pid_alive, target

pytestmark = pytest.mark.smoke

SOURCES = os.path.join(EXT, "sourcemap")
MAPPED_C = os.path.join(SOURCES, "src", "mapped.c")
MAPPED_H = os.path.join(SOURCES, "src", "mapped_math.h")
SHIM_C = os.path.join(SOURCES, "vendor", "shim.c")
THROWING = os.path.join(SOURCES, "throwing.cpp")
TARGET = target("sourcemap.py")
ATTACH = target("sourcemap_attach.py")
BINDING = target("binding.py")
THROWS = target("throws.py")
LIBRARY = "seam_mapped.abi3.so"


def names(stack):
    return [f["name"] for f in stack]


def path_of(frame):
    return frame.get("source", {}).get("path")


class Build:
    """One build: where the library is, and the prefix its debug info has for the sources."""

    def __init__(self, out, debug, opt):
        self.env = {"PYTHONPATH": out}
        self.debug = debug
        self.opt = opt

    def at(self, actual, source, marker):
        """Exact native line check at -O0 only (see conftest.at_line)."""
        return self.opt != "O0" or actual == marker_line(source, marker)


def build_c(kind, opt, root, sources=None):
    """Build the C and C++ test extensions from a copy of their sources in `root`/A.

    The copy is removed afterwards unless `sources` says where to build instead.
    """
    where = sources or os.path.join(root, "A")
    out = os.path.join(root, "out")
    if not sources:
        shutil.copytree(SOURCES, where)
    os.makedirs(out)
    common = ["-shared", "-fPIC", "-g", "-" + opt, "-I", sysconfig.get_paths()["include"]]
    files = ["src/mapped.c", os.path.join(where, "vendor", "shim.c")]
    if kind == "moved":
        # Full paths on the compiler's command line, as CMake and Meson pass them.
        flags, debug = [], where
        files = [os.path.join(where, "src", "mapped.c"), files[1]]
    elif kind == "fake":
        # What a container or CI build leaves; the vendored directory mapped separately.
        flags, debug = ["-fdebug-prefix-map=%s=/build/seam" % where,
                        "-fdebug-prefix-map=%s/vendor=/third/v" % where], "/build/seam"
    else:
        # Relative names, as distribution packages and reproducible builds have them.
        flags, debug = ["-ffile-prefix-map=%s=." % where], "."
    _run(["gcc", *common, *flags, *files, "-o", os.path.join(out, LIBRARY)], cwd=where)
    _run(["g++", *common, *flags, "throwing.cpp", "-o",
          os.path.join(out, "seam_throwing.abi3.so")], cwd=where)
    if not sources:
        shutil.rmtree(where)
    return Build(out, debug, opt)


@pytest.fixture(scope="session")
def built(request, tmp_path_factory):
    """The C and C++ test extensions, built so that their sources are not where the
    debug info says: "moved", "fake" (an invented prefix) or "relative"."""
    cache = {}

    def build(kind):
        if kind not in cache:
            cache[kind] = build_c(kind, request.config.getoption("--opt"),
                                  str(tmp_path_factory.mktemp(kind)))
        return cache[kind]
    return build


@pytest.fixture(scope="session")
def moved_cython(request, tmp_path_factory, pyinfo):
    """The Cython test extension with line directives, built in a directory now gone."""
    try:
        import Cython  # noqa: F401
    except ImportError:
        _unavailable("Cython is not installed in the test environment")
    opt = request.config.getoption("--opt")
    root = str(tmp_path_factory.mktemp("cython"))
    where, out = os.path.join(root, "A"), os.path.join(root, "out")
    os.makedirs(where)
    os.makedirs(out)
    shutil.copy(LAYERS["cython"][1], where)
    _run([sys.executable, "-m", "cython", "-3", "--line-directives", "seam_cython.pyx",
          "-o", "seam_cython.c"], cwd=where)
    _run(["gcc", "-shared", "-fPIC", "-g", "-" + opt, "-I", pyinfo["include"],
          "seam_cython.c", "-o", os.path.join(out, "seam_cython.so")], cwd=where)
    shutil.rmtree(where)
    return Build(out, where, opt)


@pytest.fixture(scope="session")
def remapped_rust(request, tmp_path_factory):
    """The PyO3 test crate built with --remap-path-prefix, from a copy of the crate.

    The copy and the cargo target directory are this test's own, so the crate the other
    tests build is not disturbed. The dependencies are taken over from the ordinary
    build's target directory when there is one: the flag is passed to the crate itself
    only (`cargo rustc`), so they are the same.
    """
    if not shutil.which("cargo"):
        _unavailable("cargo is not installed")
    opt = request.config.getoption("--opt")
    crate = os.path.join(BUILD, "sourcemap-pyo3")
    shutil.rmtree(crate, ignore_errors=True)
    shutil.copytree(os.path.join(EXT, "pyo3"), crate, ignore=shutil.ignore_patterns("target"))
    target_dir = os.path.expanduser("~/.cache/seam/cargo-target-sourcemap")
    ordinary = os.environ.get("CARGO_TARGET_DIR",
                              os.path.expanduser("~/.cache/seam/cargo-target"))
    if not os.path.isdir(target_dir) and os.path.isdir(ordinary):
        shutil.copytree(ordinary, target_dir)
    cmd = ["cargo", "rustc", "--quiet", "--lib"] + (["--release"] if opt != "O0" else [])
    _run(cmd + ["--", "--remap-path-prefix=%s=/remapped/seam" % crate], cwd=crate,
         env=dict(os.environ, CARGO_TARGET_DIR=target_dir, PYO3_NO_PYTHON="1"))
    out = str(tmp_path_factory.mktemp("rust"))
    shutil.copy2(os.path.join(target_dir, "debug" if opt == "O0" else "release",
                              "libseam_pyo3.so"), os.path.join(out, "seam_pyo3.so"))
    shutil.rmtree(crate)
    return Build(out, "/remapped/seam", opt)


def fake_map(sources=SOURCES):
    return {"/build/seam": sources, "/third/v": os.path.join(sources, "vendor")}


def step_into(dap, ext, marker="scale-call", **options):
    """Launch, stop on a Python line that calls into the extension, and step in."""
    call = marker_line(TARGET, marker)
    dap.launch(TARGET, dap.python, env=ext.env, breakpoints={TARGET: [call]}, **options)
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(TARGET, [])
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    return tid, dap.stack(tid)


def finish(dap, code=0):
    dap.cont()
    assert dap.wait_exit() == code
    if code == 0:
        assert "total 18" in dap.output and "shim 10" in dap.output


# ------------------------------------------------------- native code built elsewhere

def test_breakpoints_in_sources_that_moved(dap, built):
    ext = built("moved")
    line = marker_line(MAPPED_C, "scale-return")
    dap.launch(TARGET, dap.python, env=ext.env, sourceMap=[[ext.debug, SOURCES]],
               breakpoints={MAPPED_C: [line]})
    # Set before the library was loaded: bound when it is, and the client is told.
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    assert [e["body"]["breakpoint"]["verified"] for e in dap.events
            if e["event"] == "breakpoint"] == [True]
    stack = dap.stack(stop["threadId"])
    assert "scale" in stack[0]["name"] and path_of(stack[0]) == MAPPED_C, stack[0]
    assert ext.at(stack[0]["line"], MAPPED_C, "scale-return")
    assert names(stack)[-3:] == ["compute", "main", "<module>"]
    assert [path_of(f) for f in stack[-3:]] == [TARGET] * 3
    if ext.opt == "O0":
        assert names(stack)[:2] == ["scale", "mp_scale"]
        assert path_of(stack[1]) == MAPPED_C
        assert stack[1]["line"] == marker_line(MAPPED_C, "scale-call")
        assert dap.scope(stack[0]["id"])["scaled"]["value"] == "6"

    # Set after the library was loaded, in a header: bound at once.
    answers = dap.set_breakpoints(MAPPED_H, [marker_line(MAPPED_H, "halve-return")])
    assert answers[0]["verified"] is True, answers
    dap.set_breakpoints(MAPPED_C, [])
    dap.cont()
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert "halve" in stack[0]["name"] and path_of(stack[0]) == MAPPED_H, stack[0]
    dap.set_breakpoints(MAPPED_H, [])
    finish(dap)
    assert "sourceMap" not in dap.output


@pytest.mark.parametrize("form", ["object", "pairs", "trailing slashes"])
def test_step_in_from_python_lands_in_mapped_user_code(dap, built, form):
    ext = built("fake")
    mapping = fake_map()
    if form == "pairs":
        mapping = [list(pair) for pair in mapping.items()]
    elif form == "trailing slashes":
        mapping = {prefix + "/": local + "/" for prefix, local in mapping.items()}
    tid, stack = step_into(dap, ext, sourceMap=mapping)
    assert "scale" in stack[0]["name"] and path_of(stack[0]) == MAPPED_C, stack
    assert "presentationHint" not in stack[0]
    assert ext.opt != "O0" or (
        stack[0]["name"] == "mp_scale"
        and marker_line(MAPPED_C, "scale-first") - 2 <= stack[0]["line"]
        <= marker_line(MAPPED_C, "scale-first"))
    assert names(stack)[-3:] == ["compute", "main", "<module>"]
    dap.step("stepOut", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"], path_of(stack[0])) == \
        ("compute", marker_line(TARGET, "scale-call"), TARGET)
    finish(dap)
    assert "sourceMap" not in dap.output


@pytest.mark.parametrize("glue", ["not marked", "marked", "marked and shown"])
def test_glue_is_judged_by_the_path_on_this_machine_too(dap, built, glue):
    """`shim.c` is /third/v/shim.c in the debug info and .../vendor/shim.c here."""
    ext = built("fake")
    options = {"sourceMap": fake_map()}
    if glue != "not marked":
        options["frameworkPaths"] = ["/vendor/"]
    if glue == "marked and shown":
        options["showGlueFrames"] = True
    tid, stack = step_into(dap, ext, "shim-call", **options)
    if glue == "not marked":
        # The function Python calls is user code like any other: the step ends there.
        assert stack[0]["name"] == "shim_call" and path_of(stack[0]) == SHIM_C, stack
    else:
        # It is glue: the step goes through it into the user's function behind it.
        assert stack[0]["name"] == "scale_twice" and path_of(stack[0]) == MAPPED_C, stack
        shim = [f for f in stack if f["name"] == "shim_call"]
        if glue == "marked":
            assert not shim and names(stack)[1] == "through_glue", names(stack)
        else:
            assert path_of(shim[0]) == SHIM_C and shim[0]["presentationHint"] == "subtle"
    finish(dap)


def test_relative_names_with_a_mapping(dap, built):
    ext = built("relative")
    line = marker_line(MAPPED_C, "scale-return")
    dap.launch(TARGET, dap.python, env=ext.env, sourceMap={".": SOURCES},
               breakpoints={MAPPED_C: [line]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert "scale" in stack[0]["name"] and path_of(stack[0]) == MAPPED_C, stack[0]
    assert ext.at(stack[0]["line"], MAPPED_C, "scale-return")
    dap.set_breakpoints(MAPPED_C, [])
    finish(dap)
    assert "sourceMap" not in dap.output


def test_relative_names_are_looked_for_from_the_working_directory(dap, built):
    ext = built("relative")
    tid, stack = step_into(dap, ext, cwd=SOURCES)  # no mapping, and no breakpoint in it
    assert "scale" in stack[0]["name"] and path_of(stack[0]) == MAPPED_C, stack
    finish(dap)
    assert "sourceMap" not in dap.output


def test_a_frame_whose_source_is_not_on_this_machine(dap, built):
    ext = built("relative")
    tid, stack = step_into(dap, ext)  # run from tests/targets: src/mapped.c is not there
    top = stack[0]
    # User code still: the step ends in it. It is shown with what is known about it.
    assert top["name"].startswith(LIBRARY + "!") and "scale" in top["name"], top
    assert top["source"] == {
        "name": "mapped.c", "presentationHint": "deemphasize",
        "origin": "src/mapped.c is not on this machine (see the sourceMap option)"}, top
    assert top.get("presentationHint", "normal") == "normal" and top["line"] > 0
    assert int(top["instructionPointerReference"], 16) > 0
    said = [line for line in dap.output.splitlines() if line.startswith("Seam: the debug info")]
    assert len(said) == 1, dap.output
    assert "of %s names its source as src/mapped.c, which is not on this machine" % LIBRARY \
        in said[0]
    assert said[0].endswith('"sourceMap": {".": "/where/the/sources/are"}'), said[0]
    # An editor that asks for the text is told why there is none.
    refused = dap.request("source", {"sourceReference": 0, "source": top["source"]},
                          check=False)
    assert not refused["success"] and "src/mapped.c is not on this machine" in refused["message"]

    dap.step("stepOut", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], path_of(stack[0])) == ("compute", TARGET)
    finish(dap)
    assert dap.output.count("Seam: the debug info") == 1  # once per session


def test_a_mapping_that_does_not_fit_gets_a_concrete_suggestion(dap, built):
    ext = built("fake")
    tid, stack = step_into(dap, ext, cwd=SOURCES, sourceMap={"/build/wrong": SOURCES})
    assert "path" not in stack[0]["source"] and stack[0]["source"]["name"] == "mapped.c"
    said = [line for line in dap.output.splitlines() if line.startswith("Seam: the debug info")]
    assert len(said) == 1, dap.output
    assert "names its source as /build/seam/src/mapped.c" in said[0]
    assert MAPPED_C + " looks like the same file" in said[0]
    # The entry it offers is the one that works (see the tests above).
    assert said[0].endswith('"sourceMap": {"/build/seam": "%s"}' % SOURCES), said[0]
    finish(dap)


def test_a_mapping_to_a_directory_that_does_not_exist(dap, built):
    ext = built("fake")
    tid, stack = step_into(dap, ext, sourceMap={"/build/seam": "/nowhere/at/all"})
    assert "path" not in stack[0]["source"]
    assert "Seam: sourceMap maps /build/seam to /nowhere/at/all, which does not exist on " \
        "this machine." in dap.output
    assert ("Seam: sourceMap puts /build/seam/src/mapped.c (the name in the debug info of "
            "%s) at /nowhere/at/all/src/mapped.c, which does not exist." % LIBRARY) \
        in dap.output
    finish(dap)


def unbound_message(sources=SOURCES):
    return ('the breakpoint in %s has no code to stop at: %s was built from '
            '/build/seam/src/mapped.c. If that is the same file, add this to the launch '
            'configuration: "sourceMap": {"/build/seam": "%s"}' % (MAPPED_C, LIBRARY, sources))


def test_a_breakpoint_that_cannot_bind_says_what_the_library_was_built_from(dap, built):
    ext = built("fake")
    # Set before the library is loaded, with a mapping that does not fit it. The program
    # never stops, so the explanation has to come by itself when the library loads.
    dap.launch(TARGET, dap.python, env=ext.env, sourceMap={"/build/wrong": SOURCES},
               breakpoints={MAPPED_C: [marker_line(MAPPED_C, "scale-return")]})
    assert dap.wait_exit() == 0
    told = [e["body"]["breakpoint"] for e in dap.events if e["event"] == "breakpoint"]
    assert told and told[-1]["verified"] is False and told[-1]["message"] == unbound_message()
    assert dap.output.count("Seam: " + unbound_message()) == 1, dap.output


def test_a_breakpoint_set_after_loading_is_answered_with_the_reason(dap, built):
    ext = built("fake")
    dap.launch(TARGET, dap.python, env=ext.env,
               breakpoints={TARGET: [marker_line(TARGET, "scale-call")]})
    dap.wait_stopped()
    answers = dap.set_breakpoints(MAPPED_C, [marker_line(MAPPED_C, "scale-return"),
                                             marker_line(MAPPED_C, "crash-here")])
    assert [(a["verified"], a["message"]) for a in answers] == [(False, unbound_message())] * 2
    dap.set_breakpoints(TARGET, [])
    finish(dap)
    assert dap.output.count("has no code to stop at") == 1  # once per file


@pytest.mark.parametrize("value", ["/build=/src", [["/build"]], {"/build": 5},
                                   [["/build", ""]], 7])
def test_a_malformed_mapping_is_refused_and_the_session_survives(dap, built, value):
    dap.request("initialize", {"adapterID": "seam"})
    reply = dap.request("launch", {"program": TARGET, "python": dap.python,
                                   "sourceMap": value}, check=False)
    assert not reply["success"]
    assert reply["message"].startswith("sourceMap must be a list of"), reply["message"]
    dap.launch(TARGET, dap.python, env=built("fake").env, sourceMap=fake_map())
    assert dap.wait_exit() == 0


def test_function_and_data_breakpoints_in_a_mapped_file(dap, built):
    ext = built("fake")
    dap.launch(TARGET, dap.python, env=ext.env, sourceMap=fake_map(), stopOnEntry=True)
    dap.wait_stopped()
    dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "mp_scale"}]})
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert stack[0]["name"] == "mp_scale" and path_of(stack[0]) == MAPPED_C, stack[0]
    dap.request("setFunctionBreakpoints", {"breakpoints": []})

    # The Globals scope lists the statics of the frame's file, whatever it is called.
    scopes = {s["name"]: s["variablesReference"]
              for s in dap.request("scopes", {"frameId": stack[0]["id"]})["scopes"]}
    assert dap.variables(scopes["Globals"])["calls"]["value"] == "0"
    info = dap.request("dataBreakpointInfo", {"variablesReference": scopes["Globals"],
                                              "name": "calls"})
    answers = dap.request("setDataBreakpoints", {"breakpoints": [
        {"dataId": info["dataId"], "accessType": "write"}]})["breakpoints"]
    assert answers == [{"verified": True}], answers
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["reason"] == "data breakpoint" and stop["description"] == "calls is now 1"
    stack = dap.stack(stop["threadId"])
    assert stack[0]["name"] == "mp_scale" and path_of(stack[0]) == MAPPED_C, stack[0]
    dap.request("setDataBreakpoints", {"breakpoints": []})
    finish(dap)


def test_logpoint_and_hit_count_in_mapped_files(dap, built):
    ext = built("moved")
    log = {"line": marker_line(MAPPED_C, "scale-return"), "logMessage": "scaling {value}"}
    second = {"line": marker_line(MAPPED_H, "halve-return"), "hitCondition": "2"}
    dap.launch(TARGET, dap.python, env=ext.env, sourceMap=[[ext.debug, SOURCES]],
               breakpoints={MAPPED_C: [log], MAPPED_H: [second]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    assert "halve" in stack[0]["name"] and path_of(stack[0]) == MAPPED_H, stack[0]
    compute = [f for f in stack if f["name"] == "compute"][0]
    assert dap.scope(compute["id"])["value"]["value"] == "2"  # the second call only
    name, body = "stopped", None
    dap.cont()
    name, body = dap.wait_any(["stopped", "exited"])
    assert name == "exited" and body["exitCode"] == 0, body
    logged = [line for line in dap.output.splitlines() if line.startswith("scaling")]
    assert len(logged) == 4, dap.output  # three calls from Python, one through the shim
    if ext.opt == "O0":
        assert logged == ["scaling 1", "scaling 2", "scaling 3", "scaling 5"]


def test_crash_in_a_mapped_file(dap, built):
    ext = built("fake")
    dap.launch(TARGET, dap.python, args=["crash"], env=ext.env, sourceMap=fake_map())
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGSEGV" in stop["description"]
    stack = dap.stack(stop["threadId"])
    assert stack[0]["name"] == "mp_crash" and path_of(stack[0]) == MAPPED_C, stack[0]
    assert ext.at(stack[0]["line"], MAPPED_C, "crash-here")
    assert "presentationHint" not in stack[0]
    assert (stack[1]["name"], stack[1]["line"]) == ("main", marker_line(TARGET, "crash-call"))
    finish(dap, 128 + signal.SIGSEGV)


def test_cpp_throw_in_a_mapped_file(dap, built):
    ext = built("fake")
    dap.launch(TARGET, dap.python, args=["throw"], env=ext.env, sourceMap=fake_map(),
               exceptions=["cpp_throw"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception"
    assert stop["description"] == "C++ exception thrown: thrown on purpose"
    stack = dap.stack(stop["threadId"])
    thrower = [f for f in stack if path_of(f) == THROWING]
    assert thrower, stack
    if ext.opt == "O0":
        assert "thrower" in thrower[0]["name"]
        assert thrower[0]["line"] == marker_line(THROWING, "throw-here")
    # Above it, the C++ runtime's own frame: no source, and it says which library.
    assert stack[0]["name"].startswith("libstdc++") and "__cxa_throw" in stack[0]["name"]
    assert "path" not in stack[0].get("source", {})
    position = names(stack).index("throw")
    assert stack[position]["line"] == marker_line(TARGET, "throw-call")
    finish(dap)
    assert "caught thrown on purpose" in dap.output


def test_cython_line_directives_in_sources_that_moved(dap, moved_cython):
    ext = moved_cython
    source = LAYERS["cython"][1]
    line = marker_line(source, "add-body")
    dap.launch(BINDING, dap.python, args=["seam_cython"], env=ext.env,
               sourceMap=[[ext.debug, os.path.dirname(source)]], breakpoints={source: [line]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert "add" in stack[0]["name"] and path_of(stack[0]) == source, stack[0]
    assert ext.opt != "O0" or stack[0]["line"] == line
    assert [(f["name"], f["line"]) for f in stack if path_of(f) == BINDING] == [
        ("main", marker_line(BINDING, "bind-add")),
        ("<module>", marker_line(BINDING, "module-main"))]
    dap.set_breakpoints(source, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert "sum 42" in dap.output and "sourceMap" not in dap.output


def test_rust_built_with_remap_path_prefix(dap, remapped_rust):
    ext = remapped_rust
    source = LAYERS["pyo3"][1]
    mapping = {ext.debug: os.path.join(EXT, "pyo3")}
    call = marker_line(BINDING, "bind-add")
    dap.launch(BINDING, dap.python, args=["seam_pyo3"], env=ext.env, sourceMap=mapping,
               breakpoints={BINDING: [call]})
    tid = dap.wait_stopped()["threadId"]
    # Stepping in still ends in the user's function, and it is shown in the user's file.
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert "add" in stack[0]["name"] and path_of(stack[0]) == source, stack
    if ext.opt == "O0":
        assert [f["name"] for f in stack[1:]] == ["main", "<module>"], names(stack)
        # A breakpoint in the same file, set through the path on this machine.
        line = marker_line(source, "callback-call")
        answers = dap.set_breakpoints(source, [line])
        assert answers[0]["verified"] is True, answers
        dap.set_breakpoints(BINDING, [])
        dap.cont()
        stop = dap.wait_stopped()
        assert stop["reason"] == "breakpoint"
        stack = dap.stack(tid)
        assert (path_of(stack[0]), stack[0]["line"]) == (source, line), stack[0]
    dap.set_breakpoints(source, [])
    dap.set_breakpoints(BINDING, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert "back 6" in dap.output and "sourceMap" not in dap.output


def test_rust_panic_in_a_remapped_crate(dap, remapped_rust):
    ext = remapped_rust
    source = LAYERS["pyo3"][1]
    dap.launch(THROWS, dap.python, args=["seam_pyo3"], env=ext.env,
               sourceMap={ext.debug: os.path.join(EXT, "pyo3")}, exceptions=["rust_panic"])
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and stop["description"] == "Rust panic"
    stack = dap.stack(stop["threadId"])
    position = names(stack).index("call_native")
    if ext.opt == "O0":
        thrower = [f for f in stack[:position] if path_of(f) == source]
        assert thrower and thrower[0]["line"] == marker_line(source, "panic-here"), stack
    dap.cont()
    assert dap.wait_exit() == 0
    assert "caught PanicException" in dap.output


def test_attach_with_a_source_map(dap, python, built):
    ext = built("fake")
    proc = subprocess.Popen([python, ATTACH], stdout=subprocess.PIPE, text=True,
                            env=dict(os.environ, **ext.env))
    try:
        assert proc.stdout.readline().split()[:1] == ["ready"]
        dap.request("initialize", {"adapterID": "seam"})
        dap.request("attach", {"pid": proc.pid, "sourceMap": fake_map()})
        dap.wait_event("initialized")
        answers = dap.set_breakpoints(MAPPED_C, [marker_line(MAPPED_C, "scale-return")])
        assert answers[0]["verified"] is True, answers  # the library is already loaded
        dap.request("configurationDone")
        stop = dap.wait_stopped()
        assert stop["reason"] == "breakpoint"
        stack = dap.stack(stop["threadId"])
        assert "scale" in stack[0]["name"] and path_of(stack[0]) == MAPPED_C, stack[0]
        assert "tick" in names(stack)

        dap.set_breakpoints(MAPPED_C, [])
        dap.set_breakpoints(ATTACH, [marker_line(ATTACH, "tick-body")])
        dap.cont()
        stop = dap.wait_stopped()
        stack = dap.stack(stop["threadId"])
        dap.evaluate("globals().update(STOP=True)", stack[0]["id"])
        dap.set_breakpoints(ATTACH, [])
        dap.close()
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not pid_alive(proc.pid)


# ------------------------------------------------------------- symbolic links

MAIN_PY = """\
import helper


def main():
    value = helper.double(4)  # main-call
    print("value", value, flush=True)


main()  # module-main
"""
HELPER_PY = """\
import linked


def double(n):
    twice = n * 2  # double-body
    return linked.plus_one(twice)  # double-return
"""
LINKED_PY = """\
def plus_one(n):
    return n + 1  # plus-body
"""


@pytest.fixture
def project(tmp_path):
    """A small Python project. `link` is a symbolic link to its directory, and its
    module `linked.py` is itself a symbolic link to a file kept elsewhere."""
    real = tmp_path / "real" / "project"
    real.mkdir(parents=True)
    (real / "main.py").write_text(MAIN_PY)
    (real / "helper.py").write_text(HELPER_PY)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "original.py").write_text(LINKED_PY)
    (real / "linked.py").symlink_to(tmp_path / "elsewhere" / "original.py")
    (tmp_path / "link").symlink_to(real)

    class Project:
        def __init__(self, directory):
            self.main = os.path.join(directory, "main.py")
            self.helper = os.path.join(directory, "helper.py")
            self.linked = os.path.join(directory, "linked.py")
    return Project(str(real)), Project(str(tmp_path / "link"))


def test_python_project_opened_through_a_symbolic_link(dap, project):
    real, link = project
    line = marker_line(link.helper, "double-body")
    dap.launch(link.main, dap.python, breakpoints={link.helper: [line]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    tid = stop["threadId"]
    stack = dap.stack(tid)
    # The interpreter knows helper.py by its resolved path (sys.path[0] is resolved);
    # the editor has it open through the link, and that is the name it is given.
    assert [(f["name"], f["line"], path_of(f)) for f in stack] == [
        ("double", line, link.helper),
        ("main", marker_line(link.main, "main-call"), link.main),
        ("<module>", marker_line(link.main, "module-main"), link.main)]
    assert dap.evaluate("__file__", stack[0]["id"])["result"] == repr(real.helper)
    # A file no breakpoint was set in, entered by stepping: under the link as well.
    dap.step("next", tid)
    dap.step("stepIn", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], path_of(stack[0])) == ("plus_one", link.linked), stack[0]
    dap.set_breakpoints(link.helper, [])
    dap.cont()
    assert dap.wait_exit() == 0
    assert "value 9" in dap.output


def test_python_file_that_is_a_symbolic_link(dap, project):
    real, link = project
    line = marker_line(real.linked, "plus-body")
    dap.launch(real.main, dap.python, breakpoints={real.linked: [line]})
    stop = dap.wait_stopped()
    stack = dap.stack(stop["threadId"])
    # Shown as the file the editor has open, not as the file the link points to.
    assert (stack[0]["name"], stack[0]["line"], path_of(stack[0])) == \
        ("plus_one", line, real.linked)
    assert path_of(stack[1]) == real.helper
    dap.set_breakpoints(real.linked, [])
    dap.cont()
    assert dap.wait_exit() == 0


@pytest.mark.parametrize("spelling", ["dots", "doubled slash", "through the link"])
def test_python_breakpoint_path_spelled_differently(dap, project, spelling):
    real, link = project
    directory = os.path.dirname(real.helper)
    path = {"dots": os.path.join(directory, "..", "project", "helper.py"),
            "doubled slash": directory + "//helper.py",
            "through the link": link.helper}[spelling]
    line = marker_line(real.helper, "double-body")
    dap.launch(real.main, dap.python, stopOnEntry=True)
    dap.wait_stopped()
    answers = dap.set_breakpoints(path, [line])
    assert answers == [{"line": line, "verified": True}]
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    # The frame is reported under a tidy name: the client's own for the file where it
    # reached it through a link, the plain path otherwise.
    shown = link.helper if spelling == "through the link" else real.helper
    assert (stack[0]["name"], stack[0]["line"], path_of(stack[0])) == ("double", line, shown)
    dap.set_breakpoints(path, [])
    dap.cont()
    assert dap.wait_exit() == 0


def test_native_sources_reached_through_a_symbolic_link(dap, built, tmp_path):
    """The compiler recorded the real directory (as rustc and CMake do); the editor has
    the project open through a link, with no mapping given."""
    real = tmp_path / "real"
    shutil.copytree(SOURCES, real / "ext")
    (tmp_path / "link").symlink_to(real)
    ext = build_c("moved", built("moved").opt, str(tmp_path), sources=str(real / "ext"))
    mapped_c = str(tmp_path / "link" / "ext" / "src" / "mapped.c")
    line = marker_line(mapped_c, "scale-return")
    dap.launch(TARGET, dap.python, env=ext.env, breakpoints={mapped_c: [line]})
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    stack = dap.stack(stop["threadId"])
    assert "scale" in stack[0]["name"] and path_of(stack[0]) == mapped_c, stack[0]
    dap.set_breakpoints(mapped_c, [])
    finish(dap)
