"""Source paths: extensions whose debug info names files that are somewhere else.

Each build below is made from a copy of the sources in a scratch directory, which is then
removed. What is left is what a user has after a build in a container, in CI or with
remapped paths: a library whose debug info names files that are not there, and the sources
somewhere else (here: in the repository).
"""
import os
import shutil
import sys
import sysconfig

import pytest

from conftest import BUILD, EXT, LAYERS, _run, _unavailable, marker_line, target

pytestmark = pytest.mark.smoke

SOURCES = os.path.join(EXT, "sourcemap")
MAPPED_C = os.path.join(SOURCES, "src", "mapped.c")
MAPPED_H = os.path.join(SOURCES, "src", "mapped_math.h")
SHIM_C = os.path.join(SOURCES, "vendor", "shim.c")
THROWING = os.path.join(SOURCES, "throwing.cpp")
TARGET = target("sourcemap.py")
BINDING = target("binding.py")
THROWS = target("throws.py")
LIBRARY = "seam_mapped.abi3.so"


def names(stack):
    return [f["name"] for f in stack]


def path_of(frame):
    return frame.get("source", {}).get("path")


class Build:
    """One build: where the library is, and the prefix its debug info has for SOURCES."""

    def __init__(self, out, debug, opt):
        self.env = {"PYTHONPATH": out}
        self.debug = debug
        self.opt = opt

    def at(self, actual, source, marker):
        """Exact native line check at -O0 only (see conftest.at_line)."""
        return self.opt != "O0" or actual == marker_line(source, marker)


def build_c(kind, opt, root):
    where = os.path.join(root, "A")
    out = os.path.join(root, "out")
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


def fake_map(sources=SOURCES):
    return {"/build/seam": sources, "/third/v": os.path.join(sources, "vendor")}


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
    dap.cont()
    assert dap.wait_exit() == 0
    assert "total 18" in dap.output and "shim 10" in dap.output
    assert "sourceMap" not in dap.output


@pytest.mark.parametrize("form", ["object", "pairs", "trailing slashes"])
def test_step_in_from_python_lands_in_mapped_user_code(dap, built, form):
    ext = built("fake")
    mapping = fake_map()
    if form == "pairs":
        mapping = [list(pair) for pair in mapping.items()]
    elif form == "trailing slashes":
        mapping = {prefix + "/": local + "/" for prefix, local in mapping.items()}
    call = marker_line(TARGET, "scale-call")
    dap.launch(TARGET, dap.python, env=ext.env, sourceMap=mapping, breakpoints={TARGET: [call]})
    tid = dap.wait_stopped()["threadId"]
    dap.set_breakpoints(TARGET, [])
    stop = dap.step("stepIn", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert "scale" in stack[0]["name"] and path_of(stack[0]) == MAPPED_C, stack
    assert ext.opt != "O0" or (
        stack[0]["name"] == "mp_scale"
        and marker_line(MAPPED_C, "scale-first") - 2 <= stack[0]["line"]
        <= marker_line(MAPPED_C, "scale-first"))
    assert names(stack)[-3:] == ["compute", "main", "<module>"]
    dap.step("stepOut", tid)
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"], path_of(stack[0])) == ("compute", call, TARGET)
    dap.cont()
    assert dap.wait_exit() == 0
    assert "sourceMap" not in dap.output
