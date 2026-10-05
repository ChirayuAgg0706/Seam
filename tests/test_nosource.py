"""Code without source: a stripped library, libc, a wheel from PyPI, the interpreter itself."""
import os
import signal
import subprocess
import sysconfig

import pytest

from conftest import CAPI_SRC, EXT, _run, marker_line, target

pytestmark = pytest.mark.smoke

NOSOURCE = target("nosource.py")
CRASH = target("crash.py")
WHEELS = target("wheels.py")
LIBRARY = "seam_nosource.abi3.so"


def names(stack):
    return [f["name"] for f in stack]


def is_python(frame):
    return frame.get("source", {}).get("path", "").endswith(".py")


def assert_no_source(frame, library=None):
    """What a native frame without source must look like to the editor."""
    assert "path" not in frame.get("source", {}), frame
    assert frame.get("presentationHint") == "subtle", frame
    assert int(frame["instructionPointerReference"], 16) > 0, frame
    if "source" in frame:
        # Debug info naming a file that is not here: its name only, played down.
        assert frame["source"]["presentationHint"] == "deemphasize", frame
    else:
        assert frame["line"] == 0, frame
    if library:
        assert frame["name"].startswith(library + "!") or \
            frame["name"].startswith(library + "+0x"), frame


@pytest.fixture(scope="session")
def stripped(tmp_path_factory):
    """The no-source test extension: optimised, no debug info, stripped."""
    out = str(tmp_path_factory.mktemp("nosource"))
    library = os.path.join(out, LIBRARY)
    _run(["gcc", "-shared", "-fPIC", "-O2", "-I", sysconfig.get_paths()["include"],
          os.path.join(EXT, "nosource", "seam_nosource.c"), "-o", library])
    _run(["strip", library])
    return {"PYTHONPATH": out}


def stop_in_work(dap, stripped):
    """Launch and stop on a function breakpoint in the stripped library."""
    dap.launch(NOSOURCE, dap.python, env=stripped, stopOnEntry=True)
    dap.wait_stopped()
    dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "nosource_work"}]})
    dap.cont()
    stop = dap.wait_stopped()
    assert stop["reason"] == "breakpoint"
    dap.request("setFunctionBreakpoints", {"breakpoints": []})
    return stop["threadId"]


def test_stripped_library_explains_unbound_source_breakpoint(dap, stripped):
    tid = stop_in_work(dap, stripped)
    assert dap.output.count(LIBRARY + " has no debug info") == 1
    assert "cannot be stepped into" in dap.output and "build it with -g" in dap.output
    source = os.path.join(EXT, "nosource", "seam_nosource.c")
    answer = dap.set_breakpoints(source, [marker_line(source, "work-loop")])[0]
    assert answer["verified"] is False
    assert LIBRARY in answer["message"] and "no debug info" in answer["message"]
    dap.set_breakpoints(source, [])
    dap.cont(tid)
    assert dap.wait_exit() == 0


def test_attach_notices_already_loaded_stripped_library(dap, stripped, python, tmp_path):
    script = tmp_path / "attach_stripped.py"
    script.write_text(
        "import ctypes, os, time\n"
        "import seam_nosource\n"
        "ctypes.CDLL(None).prctl(0x59616d61, -1, 0, 0, 0)\n"
        "print('ready', flush=True)\n"
        "while True:\n"
        "    time.sleep(0.02)\n")
    proc = subprocess.Popen([python, str(script)], env=dict(os.environ, **stripped),
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        dap.request("initialize", {"adapterID": "seam"})
        dap.request("attach", {"pid": proc.pid})
        dap.wait_event("initialized")
        assert dap.output.count(LIBRARY + " has no debug info") == 1
        assert "build it with -g" in dap.output
        dap.close()
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def disassemble(dap, reference, count, **more):
    body = dap.request("disassemble", {"memoryReference": reference,
                                       "instructionCount": count, **more})
    return body["instructions"]


def addresses(instructions):
    return [int(i["address"], 16) for i in instructions]


def test_function_breakpoint_in_a_stripped_library_and_its_disassembly(dap, stripped):
    tid = stop_in_work(dap, stripped)
    assert dap.capabilities["supportsDisassembleRequest"] is True
    stack = dap.stack(tid)
    top = stack[0]
    assert top["name"] == LIBRARY + "!nosource_work" and "source" not in top, top
    assert_no_source(top)
    # The library's other frames are glue to Seam (no debug info) and stay hidden; the
    # Python frames below are complete, and are not machine code.
    assert names(stack)[1:] == ["run", "main", "<module>"]
    assert stack[1]["line"] == marker_line(NOSOURCE, "work-call")
    assert all("instructionPointerReference" not in f for f in stack[1:])

    reference = top["instructionPointerReference"]
    pc = int(reference, 16)
    listing = disassemble(dap, reference, 10)
    assert len(listing) == 10 and addresses(listing)[0] == pc
    assert all(i["instructionBytes"] and i["instruction"] != "??" for i in listing), listing
    assert listing[0]["symbol"] == "nosource_work" and "location" not in listing[0]
    assert addresses(listing) == sorted(set(addresses(listing)))
    # The length of each instruction is the distance to the next.
    for this, following in zip(listing, listing[1:]):
        size = len(this["instructionBytes"].split())
        assert int(following["address"], 16) - int(this["address"], 16) == size

    # A byte offset moves the start; a negative instruction offset looks back.
    skipped = disassemble(dap, reference, 3, offset=len(listing[0]["instructionBytes"].split()))
    assert addresses(skipped) == addresses(listing)[1:4]
    around = disassemble(dap, reference, 10, instructionOffset=-5)
    assert len(around) == 10 and addresses(around)[5:] == addresses(listing)[:5]
    assert addresses(around) == sorted(set(addresses(around)))
    assert all(i.get("presentationHint") != "invalid" for i in around), around

    # What VS Code asks for when its disassembly view opens: 400 around the address.
    wide = disassemble(dap, reference, 400, instructionOffset=-200)
    assert len(wide) == 400 and addresses(wide)[200] == pc
    assert addresses(wide) == sorted(set(addresses(wide)))
    assert addresses(wide)[200:210] == addresses(listing)
    # This library has far fewer instructions than that. The ones it has are in one
    # block around the address; before and after it are placeholders.
    readable = [n for n, i in enumerate(wide) if i.get("presentationHint") != "invalid"]
    assert readable == list(range(readable[0], readable[-1] + 1)) and len(readable) > 40
    assert readable[0] < 190 and readable[-1] > 210, (readable[0], readable[-1])

    # Addresses with nothing to read: placeholders, not a failure.
    nothing = disassemble(dap, "0x10", 6, instructionOffset=-3)
    assert len(nothing) == 6
    assert all(i["presentationHint"] == "invalid" for i in nothing), nothing
    refused = dap.request("disassemble", {"memoryReference": "nowhere",
                                          "instructionCount": 4}, check=False)
    assert not refused["success"] and "memoryReference" in refused["message"]

    dap.cont()
    assert dap.wait_exit() == 0
    assert "work 29" in dap.output and "done" in dap.output


def test_stepping_by_instruction_in_a_stripped_library(dap, stripped):
    tid = stop_in_work(dap, stripped)
    assert dap.capabilities["supportsSteppingGranularity"] is True

    def here():
        top = dap.stack(tid)[0]
        return top, disassemble(dap, top["instructionPointerReference"], 2)

    # Step over, one instruction at a time, until the next one is a call.
    stepped = 0
    top, (this, following) = here()
    while not this["instruction"].startswith("call"):
        assert not this["instruction"].startswith(("j", "ret")), this
        dap.request("next", {"threadId": tid, "granularity": "instruction"})
        assert dap.wait_stopped()["reason"] == "step"
        top, (this, following_now) = here()
        assert top["instructionPointerReference"] == following["address"], (top, following)
        assert top["name"] == LIBRARY + "!nosource_work"
        following = following_now
        stepped += 1
        assert stepped < 20, "no call instruction found"
    # Step in at the call: the next instruction executed is not the one after it.
    dap.request("stepIn", {"threadId": tid, "granularity": "instruction"})
    assert dap.wait_stopped()["reason"] == "step"
    stack = dap.stack(tid)
    assert stack[0]["instructionPointerReference"] != following["address"], stack[0]
    assert_no_source(stack[0])
    # An ordinary step from here is relative to the Python frame that made the call, as
    # at any stop inside a library: over it, to that function's next line.
    assert dap.status()["nativeStepInProgress"] is False
    stop = dap.step("next", tid)
    assert stop["reason"] == "step"
    stack = dap.stack(tid)
    assert (stack[0]["name"], stack[0]["line"]) == \
        ("run", marker_line(NOSOURCE, "work-call") + 1)
    dap.cont()
    assert dap.wait_exit() == 0
    assert "work 29" in dap.output


def test_crash_in_a_stripped_library(dap, stripped):
    dap.launch(NOSOURCE, dap.python, args=["crash"], env=stripped)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGSEGV" in stop["description"]
    stack = dap.stack(stop["threadId"])
    position = names(stack).index("main")
    # Every frame of the library between the fault and the Python code is shown.
    assert position >= 1 and stack[0]["name"] == LIBRARY + "!nosource_crash", names(stack)
    for frame in stack[:position]:
        assert_no_source(frame, LIBRARY)
    assert stack[position]["line"] == marker_line(NOSOURCE, "crash-call")
    assert names(stack)[position:] == ["main", "<module>"]
    listing = disassemble(dap, stack[0]["instructionPointerReference"], 1)
    assert listing[0]["instruction"].startswith("mov"), listing  # the faulting store
    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGSEGV


def test_crash_inside_the_interpreter_called_from_a_library(dap, stripped):
    dap.launch(NOSOURCE, dap.python, args=["bad-object"], env=stripped)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGSEGV" in stop["description"]
    stack = dap.stack(stop["threadId"])
    # The interpreter function that faulted is shown, then the library that called it;
    # the interpreter's frames between the library and the Python code stay hidden.
    # (With the interpreter's debug info installed the name is that of the inlined
    # accessor the fault is in.)
    library, _, function = stack[0]["name"].partition("!")
    assert library.startswith(("python", "libpython")), names(stack)
    assert function in ("PyObject_Repr", "Py_TYPE"), names(stack)
    assert_no_source(stack[0])
    assert stack[1]["name"] == LIBRARY + "!nosource_bad_object", names(stack)
    assert names(stack)[2:] == ["main", "<module>"]
    assert stack[2]["line"] == marker_line(NOSOURCE, "bad-call")
    assert dap.scope(stack[2]["id"])["mode"]["value"] == "'bad-object'"
    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGSEGV


def test_crash_raised_by_the_interpreter_itself(dap, stripped):
    dap.launch(NOSOURCE, dap.python, args=["sigsegv"], env=stripped)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGSEGV" in stop["description"]
    stack = dap.stack(stop["threadId"])
    position = names(stack).index("main")
    assert stack[position]["line"] == marker_line(NOSOURCE, "sigsegv-call")
    # libc's frames (raise and what it calls), then the interpreter's function that
    # called it, which is normally hidden.
    for frame in stack[:position]:
        assert_no_source(frame)
    libraries = [f["name"].split("!")[0].split("+")[0] for f in stack[:position]]
    assert libraries[0].startswith("libc."), names(stack)
    assert libraries[-1].startswith(("python", "libpython")), names(stack)
    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGSEGV


def test_libc_frames_carry_no_source_that_is_not_there(dap, capi):
    dap.launch(CRASH, dap.python, args=["abort"], env=capi.env)
    stop = dap.wait_stopped()
    assert stop["reason"] == "exception" and "SIGABRT" in stop["description"]
    stack = dap.stack(stop["threadId"])
    position = names(stack).index("st_do_abort")
    assert position >= 1
    for frame in stack[:position]:
        assert_no_source(frame, "libc.so.6")
    # A frame that has source keeps its plain name and its path.
    assert stack[position]["source"]["path"] == CAPI_SRC
    assert "presentationHint" not in stack[position]
    assert int(stack[position]["instructionPointerReference"], 16) > 0
    assert "sourceMap" not in dap.output  # libc's missing source is nobody's mistake
    dap.cont()
    assert dap.wait_exit() == 128 + signal.SIGABRT


def test_function_breakpoint_in_a_stripped_wheel(dap, wheels_python):
    dap.launch(WHEELS, wheels_python, args=["callbacks"], stopOnEntry=True)
    dap.wait_stopped()
    dap.request("setFunctionBreakpoints", {"breakpoints": [{"name": "dumps"}]})
    dap.cont()
    # A Python function called `dumps` would stop here too; the one wanted is orjson's.
    for _ in range(20):
        stop = dap.wait_stopped()
        stack = dap.stack(stop["threadId"])
        if not is_python(stack[0]):
            break
        dap.cont()
    library, _, function = stack[0]["name"].partition("!")
    assert library.startswith("orjson") and library.endswith(".so"), stack[0]
    assert function == "dumps", stack[0]
    assert_no_source(stack[0])
    python = [(f["name"], f["line"]) for f in stack if is_python(f)]
    assert python[0] == ("use_orjson", marker_line(WHEELS, "orjson-call")), python
    # A real library has enough code around the address for all the editor asks for.
    listing = disassemble(dap, stack[0]["instructionPointerReference"], 400,
                          instructionOffset=-200)
    assert len(listing) == 400
    assert listing[200]["address"] == stack[0]["instructionPointerReference"]
    assert listing[200]["symbol"] == "dumps"
    assert all(i.get("presentationHint") != "invalid" for i in listing)
    assert addresses(listing) == sorted(set(addresses(listing)))
    dap.request("setFunctionBreakpoints", {"breakpoints": []})
    dap.cont()
    assert dap.wait_exit() == 0
    assert 'orjson {"value":{"odd":"Odd"}}' in dap.output
