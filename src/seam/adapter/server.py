"""Seam's debug adapter. Runs inside LLDB's embedded Python interpreter (stdlib only).

One controller owns the process: LLDB. Python-level stops arrive as native stops in
`seam_trap`; only there (and at other known safe points) does the adapter execute code
in the target. At every other stop it only reads memory.
"""
import collections
import json
import os
import re
import shutil
import signal
import struct
import tempfile
import termios
import threading
import time
import traceback

import lldb

from . import pyread

TARGET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_target")
PY_SUFFIXES = (".py", ".pyw", ".pyi")
EVAL_FRAME = "_PyEval_EvalFrameDefault"

R_BREAKPOINT = 1
R_STEP = 2
R_RETURN_NATIVE = 3
R_ATTACHED = 5
R_EXCEPTION = 6
R_UNCAUGHT = 7
EVAL_PLEASE_STOP_BIT = 1 << 5   # _PY_EVAL_PLEASE_STOP_BIT in CPython 3.14's pycore_ceval.h

EXCEPTION_FILTERS = [
    {"filter": "uncaught", "label": "Uncaught Python exceptions", "default": True,
     "description": "Stop when an exception nobody handled is about to end the program "
                    "or a thread. The frames it passed through can still be inspected."},
    {"filter": "raised", "label": "Raised Python exceptions", "default": False,
     "description": "Stop when an exception is raised in your code, or first reaches it "
                    "from a library, even if it is handled afterwards."},
    {"filter": "cpp_throw", "label": "C++ throw", "default": False,
     "description": "Stop when native code throws a C++ exception."},
    {"filter": "rust_panic", "label": "Rust panic", "default": False,
     "description": "Stop when Rust code panics."},
]
PYTHON_EXCEPTION_FILTERS = ("uncaught", "raised")

HELPER_SYMBOLS = (
    "seam_trap", "seam_dispatch", "seam_pending", "seam_req_buf", "seam_req_len",
    "seam_resp_ptr", "seam_resp_len", "seam_req_cap", "seam_pend_buf", "seam_pend_len",
    "seam_step_gen",
)

# Source paths that mark a native frame as binding-layer glue rather than user code.
FRAMEWORK_PATHS = (
    "/include/pybind11/", "/include/nanobind/", "/nanobind/src/", "/.cargo/registry/",
    "/rustc/", "/usr/include/", "/usr/lib/", "/usr/local/include/",
    "/include/python3",  # CPython's own header inlines (Py_INCREF, vectorcall helpers)
)
# Function names that are glue even when their line info points into user files
# (macro-generated trampolines, Cython argument-parsing wrappers, module init).
FRAMEWORK_FUNCTIONS = re.compile(
    r"^(__pyx_pw_|__pyx_pymod_|__Pyx_|__pyx_tp_|PyInit_|_GLOBAL__sub_I_|pybind11::|"
    r"nanobind::|pyo3::|core::|alloc::|std::)"
    r"|__pyfunction_|__pymethod_|__pyo3_|_PYO3_DEF|::trampoline")
CYTHON_GLUE = re.compile(r"^(__pyx_pw_|__pyx_pymod_|__Pyx_|__pyx_tp_)")
SYSTEM_LIB_PREFIXES = ("/usr/lib/", "/lib/", "/usr/lib64/", "/lib64/", "[")
# Frame classes a step never ends in: Seam keeps going until user code or Python.
GLUE = ("framework", "nodebug", "system")
MAX_STEP_IN_LOCATIONS = 20000

# Signals that mean the program has gone wrong. They stop the debugger by default, and at
# such a stop Seam runs nothing in the process, not even its own bookkeeping.
FAULT_SIGNALS = ("SIGSEGV", "SIGBUS", "SIGILL", "SIGFPE", "SIGABRT")
# LLDB uses these itself (breakpoints, interrupting); their handling is left alone.
LLDB_SIGNALS = ("SIGTRAP", "SIGSTOP")
# The final stop reply of the debug-server protocol: W<code> exited, X<signal> killed.
EXIT_PACKET = re.compile(r"read packet: \$([WX])([0-9a-fA-F]{2})")

UNSAFE_MESSAGE = (
    "Seam cannot run Python here: the process is stopped in native code, where the "
    "interpreter may be in an inconsistent state. Python can be evaluated at Python "
    "breakpoints and steps."
)


class DapError(Exception):
    pass


class _Arguments(dict):
    """Request arguments: a missing required one is the client's error, not a Seam bug."""

    def __missing__(self, key):
        raise DapError("missing argument '%s'" % key)


class Adapter:
    def __init__(self, debugger, sock, log=None):
        self.dbg = debugger
        self.dbg.SetAsync(True)
        self.sock = sock
        self.rfile = sock.makefile("rb")
        self.wlock = threading.Lock()
        self.logfile = log
        self.listener = lldb.SBListener("seam")
        self.wake = lldb.SBBroadcaster("seam.wake")
        self.listener.StartListeningForEvents(self.wake, 1)
        self.requests = collections.deque()
        self.seq = 0
        self.done = False

        self.target = None
        self.process = None
        self.running = False
        self.exited = False
        self.cwd = os.getcwd()
        self.sym = {}
        self.py = None
        self.interp_module = None
        self.helper_module = None
        self.bp_trap = None

        self.epoch = 0
        self.safe_tid = None          # thread stopped at a safe point, if any
        self.stop_is_trap = False
        self.frames = {}              # frame id -> record
        self.stacks = {}              # tid -> merged stack for the current stop
        self.refs = {}                # variablesReference -> record
        self.next_id = 1
        self.py_bps = {}              # path -> [{line, condition}]
        self.native_bps = {}          # path -> [SBBreakpoint]
        self.function_bps = []
        self.pending_sync = False
        self.pause_requested = False
        self.output_thread = None
        self.framework_paths = FRAMEWORK_PATHS
        self.show_glue_frames = False
        self.user_bps = {}            # module path -> breakpoint on all its user functions
        self.user_bps_on = False
        self.unwind_warnings = set()  # functions LLDB failed to unwind (warned once each)
        self.last_native_stop = {}    # tid -> (line key, pc) of the last reported stop
        self.native_bp_lines = {}     # breakpoint id -> line the user asked for
        self.native_bp_state = {}     # breakpoint id -> (verified, line) last reported
        self.attached = False         # attached to an existing process (detach, don't kill)
        self.temp_files = []
        self.native_stepping = None   # {"tid", "hops"} while an LLDB step plan is running
        self.stepout = None           # {"bp", "sp"}: Seam's own run-until-return
        self.py_step_armed = False    # the agent has a Python-level step armed
        self.exception_info = {}      # tid -> exceptionInfo body for the current stop
        self.fault_stop = False       # stopped at a fault signal: run nothing in the process
        self.exit_packet = None       # ("W" | "X", number) from the final stop reply
        self.note_fd = None           # side channel to `seam dap` (see cli.py)
        self.exc_filters = []         # exception breakpoint filters in force
        self.native_exc_bps = {}      # filter -> SBBreakpoint (cpp_throw, rust_panic)
        self.just_my_code = True
        self.post_mortem = {}         # tid -> frames of the uncaught exception shown there
        self.throw_stop = False       # stopped at a C++ throw or Rust panic
        self._watch_exit_packets()

    def _note(self, text):
        if self.note_fd is not None:
            try:
                os.write(self.note_fd, text.encode() + b"\n")
            except OSError:
                pass

    def _watch_exit_packets(self):
        """Learn whether the program exited or was killed by a signal.

        LLDB's API reports both as an exit status (SIGKILL and `sys.exit(9)` both read 9,
        with no description). The difference survives in one place only: the last packet
        of the debug-server protocol. So that channel is logged to a callback which keeps
        nothing but that packet.
        """
        def on_log(line):
            match = EXIT_PACKET.search(line)
            if match:
                self.exit_packet = (match.group(1), int(match.group(2), 16))

        self._on_log = on_log  # LLDB does not keep the callable alive
        self.dbg.SetLoggingCallback(on_log)
        self.dbg.HandleCommand("log enable gdb-remote packets")

    # ------------------------------------------------------------ plumbing

    def log(self, *parts):
        if self.logfile:
            self.logfile.write(" ".join(str(p) for p in parts) + "\n")
            self.logfile.flush()

    def _send(self, msg):
        with self.wlock:
            self.seq += 1
            msg["seq"] = self.seq
            data = json.dumps(msg).encode()
            self.sock.sendall(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.log("->", data.decode()[:600])

    def event(self, name, body=None):
        self._send({"type": "event", "event": name, "body": body or {}})

    def _reader(self):
        try:
            while True:
                length = None
                while True:
                    line = self.rfile.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":")[1])
                body = self.rfile.read(length)
                self.requests.append(json.loads(body))
                self.wake.BroadcastEventByType(1)
        except (EOFError, OSError, ValueError, TypeError):
            self.requests.append({"type": "request", "command": "disconnect", "seq": 0,
                                  "arguments": {}, "_eof": True})
            self.wake.BroadcastEventByType(1)

    def run(self):
        threading.Thread(target=self._reader, daemon=True).start()
        ev = lldb.SBEvent()
        while not self.done:
            while self.requests and not self.done:
                self._handle(self.requests.popleft())
            if self.done:
                break
            if self.listener.WaitForEvent(1, ev):
                self._on_event(ev)
        self._kill()

    def _handle(self, req):
        self.log("<-", json.dumps(req)[:600])
        cmd = req.get("command", "")
        resp = {"type": "response", "request_seq": req.get("seq", 0), "command": cmd,
                "success": True}
        handler = getattr(self, "req_" + cmd.replace("/", "_"), None)
        after = None
        try:
            if handler is None:
                raise DapError("unsupported request: %s" % cmd)
            result = handler(_Arguments(req.get("arguments") or {}))
            if isinstance(result, tuple):
                result, after = result
            if result is not None:
                resp["body"] = result
        except DapError as exc:
            resp["success"] = False
            resp["message"] = str(exc)
        except Exception as exc:  # a bug in Seam must not kill the session silently
            self.log(traceback.format_exc())
            resp["success"] = False
            resp["message"] = "Seam internal error: %s: %s" % (type(exc).__name__, exc)
        if not req.get("_eof"):
            self._send(resp)
        if after:
            after()

    # ------------------------------------------------------ process events

    def _on_event(self, ev):
        if lldb.SBBreakpoint.EventIsBreakpointEvent(ev):
            self._refresh_native_bp_status()
            return
        if not lldb.SBProcess.EventIsProcessEvent(ev):
            return
        kind = ev.GetType()
        if kind & (lldb.SBProcess.eBroadcastBitSTDOUT | lldb.SBProcess.eBroadcastBitSTDERR):
            self._drain_output()
        if not kind & lldb.SBProcess.eBroadcastBitStateChanged:
            return
        state = lldb.SBProcess.GetStateFromEvent(ev)
        self.log("event: state", state, "restarted", lldb.SBProcess.GetRestartedFromEvent(ev),
                 "running", self.running, "stop-id", self.process.GetStopID())
        if state == lldb.eStateExited:
            self._on_exit()
        elif state == lldb.eStateStopped:
            if lldb.SBProcess.GetRestartedFromEvent(ev) or not self.running:
                return
            if self.process.GetState() != lldb.eStateStopped:
                # LLDB can deliver a stop event a moment before its public process state
                # says "stopped" (seen about once in 30 step-ins on large modules).
                # Dropping the event here loses a real stop and hangs the session, so
                # give the state a moment to catch up before deciding it is stale.
                deadline = time.monotonic() + 1.0
                while (time.monotonic() < deadline
                       and self.process.GetState() != lldb.eStateStopped):
                    time.sleep(0.01)
                self.log("stop event arrived before the public state; state is now",
                         self.process.GetState())
                if self.process.GetState() != lldb.eStateStopped:
                    return
            self.running = False
            self._on_stop()

    def _drain_output(self):
        for getter, category in ((self.process.GetSTDOUT, "stdout"),
                                 (self.process.GetSTDERR, "stderr")):
            while True:
                text = getter(4096)
                if not text:
                    break
                self.event("output", {"category": category, "output": text})

    def _pump_output(self, master):
        """Forward everything the target writes to its pty as DAP output events."""
        try:
            while True:
                data = os.read(master, 65536)
                if not data:
                    break
                self.event("output", {"category": "stdout",
                                      "output": data.decode("utf-8", "replace")})
        except OSError:
            pass  # EIO: every writer has closed the pty
        finally:
            os.close(master)

    def _on_exit(self):
        if self.exited:
            return
        self.exited = True
        self.running = False
        if self.output_thread is not None:
            self.output_thread.join(2)
        self._drain_output()
        code = self.process.GetExitStatus()
        if self.exit_packet == ("X", code):
            self.event("output", {"category": "console", "output":
                       "Seam: the program was terminated by signal %s.\n"
                       % self._signal_name(code)})
            code += 128  # what a shell would report
        self.event("exited", {"exitCode": code})
        self.event("terminated")

    @staticmethod
    def _signal_name(number):
        try:
            return signal.Signals(number).name
        except ValueError:
            return str(number)

    def _wait_stop(self, timeout=30):
        """Block until the process stops or exits. Returns the new state."""
        ev = lldb.SBEvent()
        waited = 0
        while waited < timeout:
            if not self.listener.WaitForEvent(1, ev):
                # Only events count. The public state is not consulted here: right after
                # a resume it can still read "stopped" (see docs/decisions.md §10).
                waited += 1
                continue
            if not lldb.SBProcess.EventIsProcessEvent(ev):
                continue
            if ev.GetType() & (lldb.SBProcess.eBroadcastBitSTDOUT
                               | lldb.SBProcess.eBroadcastBitSTDERR):
                self._drain_output()
            if not ev.GetType() & lldb.SBProcess.eBroadcastBitStateChanged:
                continue
            state = lldb.SBProcess.GetStateFromEvent(ev)
            self.log("wait: state", state, "restarted",
                     lldb.SBProcess.GetRestartedFromEvent(ev), "stop-id",
                     self.process.GetStopID())
            if state == lldb.eStateStopped and not lldb.SBProcess.GetRestartedFromEvent(ev):
                return state
            if state in (lldb.eStateExited, lldb.eStateCrashed, lldb.eStateDetached):
                return state
        raise DapError("timed out waiting for the process to stop")

    def _new_stop(self):
        self.epoch += 1
        self.frames.clear()
        self.stacks.clear()
        self.refs.clear()
        self.safe_tid = None
        self.stop_is_trap = False
        self.exception_info.clear()
        self.post_mortem.clear()
        self.fault_stop = False
        self.throw_stop = False
        if self.py:
            self.py.new_stop()

    def _interesting(self, thread):
        reason = thread.GetStopReason()
        return reason not in (lldb.eStopReasonNone, lldb.eStopReasonInvalid)

    def _trap_thread(self):
        if self.bp_trap is None:
            return None
        for thread in self.process:
            if (thread.GetStopReason() == lldb.eStopReasonBreakpoint
                    and thread.GetStopReasonDataAtIndex(0) == self.bp_trap.GetID()):
                return thread
        return None

    def _fix_stale_frames(self):
        """Work around LLDB 18 serving the previous stop's frames after an interrupt.

        Once any expression has been evaluated, the stop that follows an interrupt-and-
        continue keeps the interrupt stop's cached frame list (LLDB's own `bt` shows it).
        The live registers are right, so the condition is detectable, and running any
        function call makes LLDB rebuild the list. getpid() is async-signal-safe and takes
        no locks, so it is harmless at any stop.
        """
        getpid = self.sym.get("getpid")
        if not getpid:
            return
        for thread in self.process:
            frame = thread.GetFrameAtIndex(0)
            rip = frame.FindRegister("rip")
            if rip.IsValid() and rip.GetValueAsUnsigned() != frame.GetPC():
                self.log("stale frame list on thread", thread.GetThreadID(), "- refreshing")
                try:
                    self._call(thread, "((int(*)(void))%d)()" % getpid, timeout_s=5)
                except DapError as exc:
                    self.log("refresh failed:", exc)
                return

    # ----------------------------------------------- stepping across the boundary

    def _classify_address(self, address):
        """'user', 'framework' (binding glue) or 'nodebug' for a code address."""
        entry = address.GetLineEntry()
        spec = entry.GetFileSpec()
        if not entry.IsValid() or not spec.IsValid() or not entry.GetLine():
            return "nodebug"
        path = spec.fullpath or ""
        if any(part in path for part in self.framework_paths):
            return "framework"
        if FRAMEWORK_FUNCTIONS.search(self._function_name(address)):
            return "framework"
        return "user"

    @staticmethod
    def _function_name(address):
        """Name of the innermost function at `address`, looking through inlining.

        With optimisation a user function is often inlined into binding glue; the code is
        still the user's, so judge it by the inlined function's name, not its host's.
        """
        block = address.GetBlock()
        if block.IsValid():
            inlined = block if block.IsInlined() else block.GetContainingInlinedBlock()
            if inlined.IsValid() and inlined.GetInlinedName():
                return inlined.GetInlinedName()
        return address.GetFunction().GetName() or address.GetSymbol().GetName() or ""

    def _classify_frame(self, frame):
        module = frame.GetModule().GetFileSpec().fullpath
        if module == self.interp_module:
            return "interp"
        if module == self.helper_module:
            return "seam"
        if not module or module.startswith(SYSTEM_LIB_PREFIXES):
            return "system"  # libc and friends, even when their debug info is installed
        # Judge the frame by its own name and line, not by whatever is innermost at its
        # PC: with inlining several frames share one PC and they are not the same thing.
        entry = frame.GetLineEntry()
        spec = entry.GetFileSpec()
        if not entry.IsValid() or not spec.IsValid() or not entry.GetLine():
            return "nodebug"
        path = spec.fullpath or ""
        if any(part in path for part in self.framework_paths):
            return "framework"
        if FRAMEWORK_FUNCTIONS.search(frame.GetFunctionName() or ""):
            return "framework"
        return "user"

    def _native_frames(self, thread, retry=True):
        """The thread's native frames, cut where LLDB's unwinder went wrong.

        When LLDB cannot unwind a function it tends to end the backtrace with a frame
        whose PC is not code at all (LLDB 20 does this under nanobind's optimised
        library, ending in `_PyRuntime + N`). Such a frame must never be used as a place
        to run to.

        Sometimes the bad frame is a glitch rather than a real limit: about once in 40
        stops in `seam_trap`, LLDB 20 produced a two-frame backtrace whose second frame
        was in a data section. Making LLDB rebuild its frame list (the same harmless
        `getpid()` call used for stale frames) is tried once before giving up.
        """
        frames = []
        for i in range(thread.GetNumFrames()):
            frame = thread.GetFrameAtIndex(i)
            if i:
                section = frame.GetPCAddress().GetSection()
                if (not section.IsValid()
                        or not section.GetPermissions() & lldb.ePermissionsExecutable):
                    self.log("native frames cut at", i, "of", thread.GetNumFrames(),
                             "pc %#x" % frame.GetPC(), "name", frame.GetFunctionName(),
                             "rsp %#x" % thread.GetFrameAtIndex(0).GetSP())
                    if retry and self.sym.get("getpid"):
                        try:
                            self._call(thread, "((int(*)(void))%d)()" % self.sym["getpid"],
                                       timeout_s=5)
                        except DapError as exc:
                            self.log("refresh failed:", exc)
                        again = self._native_frames(thread, retry=False)
                        self.log("after refresh:", len(again), "frames")
                        return again
                    break
            frames.append(frame)
        return frames

    def _warn_truncated(self, frame):
        name = frame.GetFunctionName() or "%#x" % frame.GetPC()
        if name not in self.unwind_warnings:
            self.unwind_warnings.add(name)
            self.event("output", {"category": "console", "output":
                       "Seam: LLDB could not unwind the native stack past `%s`. Native "
                       "frames below it are missing from the call stack; Python frames "
                       "are still complete.\n" % name[:120]})

    def _landing_class(self, thread):
        """Class of the place a thread is stopped at, looking through inlined glue.

        If the newest frame is glue that was inlined into a user function (same PC and
        SP), the thread is physically in user code and that is where a step should end.
        """
        first = thread.GetFrameAtIndex(0)
        kind = self._classify_frame(first)
        if kind in GLUE:
            for i in range(1, thread.GetNumFrames()):
                frame = thread.GetFrameAtIndex(i)
                if frame.GetSP() != first.GetSP() or frame.GetPC() != first.GetPC():
                    break
                if self._classify_frame(frame) == "user":
                    return "user"
        return kind

    def _user_modules(self):
        """Loaded modules that carry debug info and are not the interpreter or system libs."""
        for module in self.target.module_iter():
            path = module.GetFileSpec().fullpath or ""
            if (path in (self.interp_module, self.helper_module)
                    or not path.startswith("/")  # LLDB's own JIT modules have no file
                    or path.startswith(SYSTEM_LIB_PREFIXES)
                    or module.GetNumCompileUnits() == 0):
                continue
            yield path, module

    def _enable_user_bps(self, tid):
        """Arm a breakpoint on every user function of every user module, for one thread.

        This is how "step in" from Python lands in user native code whatever the binding
        layer: the first user function this thread enters wins, and glue is never a
        candidate. The breakpoints are created once per module and kept disabled.
        """
        for path, module in self._user_modules():
            bp = self.user_bps.get(path)
            if bp is None:
                modules = lldb.SBFileSpecList()
                modules.Append(module.GetFileSpec())
                bp = self.target.BreakpointCreateByRegex(
                    ".", lldb.eLanguageTypeUnknown, modules, lldb.SBFileSpecList())
                count = bp.GetNumLocations()
                usable = 0
                for i in range(count):
                    location = bp.GetLocationAtIndex(i)
                    if (count <= MAX_STEP_IN_LOCATIONS
                            and self._classify_address(location.GetAddress()) == "user"):
                        usable += 1
                    else:
                        location.SetEnabled(False)
                if count > MAX_STEP_IN_LOCATIONS:
                    self.event("output", {"category": "console", "output":
                               "Seam: %s has %d functions; stepping into it from Python is "
                               "disabled (limit %d).\n" % (path, count, MAX_STEP_IN_LOCATIONS)})
                self.log("user breakpoints for", path, usable, "of", count)
                self.user_bps[path] = bp
            bp.SetThreadID(tid)
            bp.SetEnabled(True)
        self.user_bps_on = True

    def _drop_glue_locations(self, bp):
        """Disable locations of a source-line breakpoint that sit in Cython's generated glue.

        With line directives Cython attributes parts of its module-init and argument-
        parsing code to the user's .pyx lines, so a breakpoint on a statement would also
        stop during import. Those functions are never the user's code.
        """
        for i in range(bp.GetNumLocations()):
            location = bp.GetLocationAtIndex(i)
            if (location.IsEnabled()
                    and CYTHON_GLUE.search(self._function_name(location.GetAddress()))):
                location.SetEnabled(False)

    @staticmethod
    def _line_key(frame):
        """Identity of "this invocation of this function, on this line"."""
        entry = frame.GetLineEntry()
        return (frame.GetCFA(), frame.GetFunctionName(), entry.GetFileSpec().fullpath,
                entry.GetLine())

    def _is_same_line_rehit(self, thread):
        """True if a breakpoint hit is just another address range of the line we were on.

        LLDB resolves a line to every address range that carries it. Rust's `?`, Cython's
        generated code and optimised C give one line several ranges in one function, so
        continuing or stepping from a stop on that line would hit "the same" breakpoint
        again without the program having gone anywhere. Coming back to the *same* address
        (a loop) is a real hit.
        """
        last = self.last_native_stop.get(thread.GetThreadID())
        frame = thread.GetFrameAtIndex(0)
        return (last is not None and last[0] == self._line_key(frame)
                and last[1] != frame.GetPC())

    def _step_out_to(self, thread, natives, target_index):
        """Run until natives[target_index] is the newest frame.

        Stepping out of an *inlined* frame does not execute anything in LLDB (it only
        changes which inlined scope is shown), so step out of the nearest real frame
        above the target instead.
        """
        # LLDB's own step-out plan is not used for this. With inlined frames (where it only
        # changes the displayed scope), artificial tail-call frames and frames whose
        # "is inlined" answer depends on the PC rather than the frame, it either did
        # nothing or ran the program to completion (seen with optimised Cython and Rust).
        # A frame's PC is its return address and its SP is the stack pointer right after
        # the return, so a breakpoint there plus a stack-depth check is exact.
        target = natives[min(target_index, len(natives) - 1)]
        self._clear_stepout()
        self._discard_plans(thread)
        bp = self.target.BreakpointCreateByAddress(target.GetPC())
        bp.SetThreadID(thread.GetThreadID())
        self.stepout = {"bp": bp, "sp": target.GetSP()}
        self.log("running until return to", target.GetFunctionName(), hex(target.GetPC()))
        err = self.process.Continue()
        if not err.Success():
            self._clear_stepout()
            raise DapError("could not resume: %s" % err.GetCString())

    def _clear_stepout(self):
        if self.stepout is not None:
            self.target.BreakpointDelete(self.stepout["bp"].GetID())
            self.stepout = None

    def _step_out_of_glue(self, thread):
        """Step out to the nearest frame that is user code or the interpreter."""
        natives = self._native_frames(thread)
        target_index = 1
        while (target_index < len(natives)
               and self._classify_frame(natives[target_index]) in GLUE):
            target_index += 1
        if target_index < len(natives):
            self._step_out_to(thread, natives, target_index)
            return
        # Nowhere to run to: every remaining frame is glue, which means LLDB's backtrace
        # ended early. If Python called this code, hand the step to the Python side
        # instead: stop when the calling Python frame resumes.
        tid = thread.GetThreadID()
        if not self.py.holds_gil(tid):
            raise DapError("cannot step out: LLDB could not unwind the native stack here")
        self._warn_truncated(natives[-1])
        self.safe_tid = tid
        try:
            self.agent("step", mode="caller", tid=tid)
        finally:
            self.safe_tid = None
        self._finish_steps(thread, cancel_py=False)
        self.py_step_armed = True
        err = self.process.Continue()
        if not err.Success():
            raise DapError("could not resume: %s" % err.GetCString())

    def _discard_plans(self, thread):
        """Drop LLDB step plans so a later `continue` does not stop where a step would."""
        self.process.SetSelectedThread(thread)
        result = lldb.SBCommandReturnObject()
        self.dbg.GetCommandInterpreter().HandleCommand("thread plan discard 1", result)

    def _cancel_py_step(self):
        """Cancel the agent's armed step with a memory write (legal at any stop)."""
        addr = self.sym["seam_step_gen"]
        gen = struct.unpack("<q", self._read(addr, 8))[0]
        self._write(addr, struct.pack("<q", gen + 1))
        self.py_step_armed = False

    def _finish_steps(self, thread, cancel_py=True):
        """Tear down everything a step may have armed. Called at every reported stop."""
        self._clear_stepout()
        if self.user_bps_on:
            for bp in self.user_bps.values():
                bp.SetEnabled(False)
            self.user_bps_on = False
        if self.native_stepping:
            self._discard_plans(thread)
            self.native_stepping = None
        if cancel_py and self.py_step_armed:
            self._cancel_py_step()
        self.py_step_armed = False

    def _control_safe(self, thread):
        """True if Seam may run its own control requests on this thread right now.

        The thread must hold the GIL and be stopped at a statement boundary in user
        native code, where calling into the interpreter is as legal as it would be for
        the user's own function. User-supplied Python is never run here.
        """
        return (not self.fault_stop and self._landing_class(thread) == "user"
                and self.py.holds_gil(thread.GetThreadID()))

    def _resume_in_python(self, thread):
        """Native code has returned into the interpreter: stop when its Python caller resumes."""
        tid = thread.GetThreadID()
        if not self.py.holds_gil(tid):
            return False
        self.safe_tid = tid
        try:
            self.agent("step", mode="caller", tid=tid)
        except DapError as exc:
            self.log("cannot hand the step to Python:", exc)
            self.safe_tid = None
            return False
        self._finish_steps(thread, cancel_py=False)
        self.py_step_armed = True
        self._new_stop()
        self._continue()
        return True

    def _on_trap(self, thread):
        frame = thread.GetFrameAtIndex(0)
        reason = frame.FindRegister("rdx").GetValueAsUnsigned()
        tid = thread.GetThreadID()
        self.safe_tid = tid
        self.stop_is_trap = True
        self.process.SetSelectedThread(thread)
        if self.pending_sync:
            self._sync_py_bps()
        if reason == R_RETURN_NATIVE:
            # The stepped Python function is returning to native code that called it:
            # finish with a native step-out into the nearest user frame.
            natives = self._native_frames(thread)
            for i, native in enumerate(natives):
                if i and self._classify_frame(native) == "user":
                    self._finish_steps(thread, cancel_py=False)
                    self.native_stepping = {"tid": tid, "hops": 0}
                    self._new_stop()
                    self._step_out_to(thread, natives, i)
                    self.running = True
                    return
        self._finish_steps(thread, cancel_py=False)
        self.last_native_stop.pop(tid, None)
        body = {"reason": "breakpoint" if reason == R_BREAKPOINT else "step",
                "threadId": tid, "allThreadsStopped": True}
        if reason in (R_EXCEPTION, R_UNCAUGHT):
            body["reason"] = "exception"
            try:
                info = self.agent("exception")
            except DapError as exc:
                self.log("no exception details:", exc)
            else:
                summary = info["type"] + (": " + info["message"] if info["message"] else "")
                body["description"] = summary[:300]
                body["text"] = info["type"]
                self.exception_info[tid] = {
                    "exceptionId": info["full_type"], "description": info["message"],
                    "breakMode": info["mode"],
                    "details": {"message": info["message"], "typeName": info["type"],
                                "fullTypeName": info["full_type"],
                                "stackTrace": info["trace"]}}
                if info.get("frames"):
                    self.post_mortem[tid] = info["frames"]
        self.event("stopped", body)

    def _on_stop(self):
        self._new_stop()
        self._fix_stale_frames()
        self._drain_output()
        self._refresh_native_bp_status()
        thread = self._trap_thread()
        if thread is not None:
            self._on_trap(thread)
            return
        thread = None
        stepping_tid = self.native_stepping["tid"] if self.native_stepping else None
        for candidate in self.process:
            if self._interesting(candidate) and (
                    thread is None or candidate.GetThreadID() == stepping_tid):
                thread = candidate
        if thread is None:
            thread = self.process.GetSelectedThread()
        self.process.SetSelectedThread(thread)
        reason = thread.GetStopReason()
        body = {"threadId": thread.GetThreadID(), "allThreadsStopped": True}

        if (reason == lldb.eStopReasonBreakpoint and thread.GetStopReasonDataAtIndex(0)
                in [bp.GetID() for bp in self.user_bps.values()]):
            # Step-in from Python reached the first user native function.
            self._finish_steps(thread)
            body["reason"] = "step"
            self._report_native_stop(thread, body)
            return
        if reason == lldb.eStopReasonBreakpoint and not self.pause_requested:
            hit = thread.GetStopReasonDataAtIndex(0)
            for name, bp in self.native_exc_bps.items():
                if bp.GetID() == hit:
                    self._finish_steps(thread)
                    self._report_native_exception(thread, name, body)
                    return
        if reason == lldb.eStopReasonBreakpoint and not self.pause_requested:
            # A source-line breakpoint can also resolve into generated glue that carries
            # the user's line numbers (Cython's module-init code does). Never stop there.
            # Locations are resolved lazily (the module may load after the breakpoint was
            # set), so the clean-up happens here too.
            bp = self.target.FindBreakpointByID(thread.GetStopReasonDataAtIndex(0))
            if bp.IsValid() and any(bp.GetID() == b.GetID()
                                    for group in self.native_bps.values() for b in group):
                # Read the hit location first: LLDB derives it from the live site owners,
                # so it reads as 0 once the location has been disabled.
                location = bp.FindLocationByID(thread.GetStopReasonDataAtIndex(1))
                self._drop_glue_locations(bp)
                if ((location.IsValid() and not location.IsEnabled())
                        or self._is_same_line_rehit(thread)):
                    self.log("skipping redundant breakpoint hit in",
                             thread.GetFrameAtIndex(0).GetFunctionName())
                    self._new_stop()
                    self._continue()
                    return
        returned = False
        if (self.stepout is not None and reason == lldb.eStopReasonBreakpoint
                and thread.GetStopReasonDataAtIndex(0) == self.stepout["bp"].GetID()):
            if thread.GetFrameAtIndex(0).GetSP() < self.stepout["sp"]:
                # The same return address, deeper in the stack (recursion): not ours yet.
                self._new_stop()
                self._continue()
                return
            self._clear_stepout()
            returned = True
        if (returned or reason == lldb.eStopReasonPlanComplete) and self.native_stepping:
            reason = lldb.eStopReasonPlanComplete
            kind = self._landing_class(thread)
            self.log("native step ended in", thread.GetFrameAtIndex(0).GetFunctionName(),
                     "class", kind, "hops", self.native_stepping["hops"])
            if kind == "interp" and self._resume_in_python(thread):
                return
            if kind in GLUE and self.native_stepping["hops"] < 64:
                # Returned into binding glue: keep going until user code or the interpreter.
                self.native_stepping["hops"] += 1
                self._new_stop()
                try:
                    self._step_out_of_glue(thread)
                    self.running = True
                    return
                except DapError as exc:
                    self.log("cannot leave glue:", exc)  # report the stop where it is
        self._finish_steps(thread)
        if self.pause_requested:
            self.pause_requested = False
            body["reason"] = "pause"
        elif reason == lldb.eStopReasonBreakpoint:
            body["reason"] = "breakpoint"
        elif reason == lldb.eStopReasonPlanComplete:
            body["reason"] = "step"
        elif reason in (lldb.eStopReasonSignal, lldb.eStopReasonException):
            body["reason"] = "exception"
            body["description"] = thread.GetStopDescription(200)
            body["text"] = body["description"]
            name = "exception"
            if reason == lldb.eStopReasonSignal:
                name = self._signal_name(thread.GetStopReasonDataAtIndex(0))
            self.fault_stop = name in FAULT_SIGNALS or reason == lldb.eStopReasonException
            self.exception_info[thread.GetThreadID()] = {
                "exceptionId": name, "description": body["description"],
                "breakMode": "always"}
        else:
            body["reason"] = "pause"
        self._report_native_stop(thread, body)

    def _report_native_exception(self, thread, kind, body):
        """A stop at a C++ `throw` or a Rust panic (exception breakpoint filters)."""
        if kind == "cpp_throw":
            # Stopped on entry to __cxa_throw(object, type_info, destructor).
            tinfo = thread.GetFrameAtIndex(0).FindRegister("rsi").GetValueAsUnsigned()
            symbol = self.target.ResolveLoadAddress(tinfo).GetSymbol().GetName() or ""
            name = symbol[len("typeinfo for "):] if symbol.startswith("typeinfo for ") else ""
            what = "C++ exception thrown" + (": " + name if name else "")
            name = name or "C++ exception"
        else:
            name, what = "Rust panic", "Rust panic"
        body.update({"reason": "exception", "description": what, "text": name})
        self.throw_stop = True
        self.exception_info[thread.GetThreadID()] = {
            "exceptionId": name, "description": what, "breakMode": "always"}
        self._report_native_stop(thread, body)

    def _report_native_stop(self, thread, body):
        frame = thread.GetFrameAtIndex(0)
        self.last_native_stop[thread.GetThreadID()] = (self._line_key(frame), frame.GetPC())
        self.event("stopped", body)

    def _continue(self):
        err = self.process.Continue()
        if not err.Success():
            raise DapError("could not resume: %s" % err.GetCString())
        self.running = True

    def _interrupt(self):
        """Stop a running process for internal work. Returns False if it stopped by itself."""
        # Not SBProcess.Stop(): on LLDB 18 the stop it produces leaves the thread's frame
        # list cached, so the *next* stop shows the frames of this one.
        self.process.SendAsyncInterrupt()
        state = self._wait_stop(10)
        if state != lldb.eStateStopped:
            self._on_exit()
            raise DapError("the process exited")
        self.running = False
        for thread in self.process:
            reason = thread.GetStopReason()
            if reason in (lldb.eStopReasonBreakpoint, lldb.eStopReasonException,
                          lldb.eStopReasonPlanComplete):
                return False
        return True

    def _kill(self):
        """End the session: kill a program Seam launched, detach from one it attached to."""
        if self.process is not None and self.process.IsValid() and not self.exited:
            if self.attached:
                self._detach()
            else:
                self.process.Kill()
                self.exited = True
        for path in self.temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.temp_files = []

    # ------------------------------------------------------ target access

    def _read(self, addr, size):
        err = lldb.SBError()
        data = self.process.ReadMemory(addr, size, err)
        if not err.Success() or data is None or len(data) != size:
            raise ValueError("cannot read %d bytes at %#x" % (size, addr))
        return data

    def _write(self, addr, data):
        err = lldb.SBError()
        self.process.WriteMemory(addr, data, err)
        if not err.Success():
            raise DapError("cannot write target memory: %s" % err.GetCString())

    def _symbol(self, name):
        for ctx in self.target.FindSymbols(name):
            addr = ctx.GetSymbol().GetStartAddress().GetLoadAddress(self.target)
            if addr != lldb.LLDB_INVALID_ADDRESS:
                return addr, ctx.GetModule()
        return None, None

    def _expr_options(self, timeout_s=30):
        opts = lldb.SBExpressionOptions()
        opts.SetLanguage(lldb.eLanguageTypeC)
        opts.SetStopOthers(True)
        opts.SetTryAllThreads(False)
        opts.SetIgnoreBreakpoints(True)
        opts.SetUnwindOnError(True)
        opts.SetSuppressPersistentResult(True)
        opts.SetTimeoutInMicroSeconds(int(timeout_s * 1000000))
        return opts

    def _call(self, thread, expr, timeout_s=30):
        value = thread.GetFrameAtIndex(0).EvaluateExpression(expr, self._expr_options(timeout_s))
        err = value.GetError()
        if not err.Success():
            raise DapError("call into the target failed: %s" % err.GetCString())
        return value.GetValueAsSigned()

    def _thread(self, tid):
        thread = self.process.GetThreadByID(tid)
        if not thread.IsValid():
            raise DapError("unknown thread %s" % tid)
        return thread

    def agent(self, cmd, **kw):
        """Run one request through the in-process agent. Only legal at a safe point."""
        if self.safe_tid is None:
            raise DapError(UNSAFE_MESSAGE)
        kw["cmd"] = cmd
        kw["epoch"] = self.epoch
        data = json.dumps(kw).encode()
        if len(data) >= self.sym["cap"]:
            raise DapError("request too large for the agent buffer")
        self._write(self.sym["seam_req_buf"], data)
        self._write(self.sym["seam_req_len"], struct.pack("<q", len(data)))
        rc = self._call(self._thread(self.safe_tid),
                        "((int(*)(void))%d)()" % self.sym["seam_dispatch"])
        if rc != 0:
            raise DapError("the Seam agent failed to answer (code %d)" % rc)
        ptr = struct.unpack("<Q", self._read(self.sym["seam_resp_ptr"], 8))[0]
        size = struct.unpack("<q", self._read(self.sym["seam_resp_len"], 8))[0]
        reply = json.loads(self._read(ptr, size)) if size else {}
        self.log("agent", data.decode()[:300], "=>", json.dumps(reply)[:300])
        if not reply.get("ok"):
            self.log(reply.get("trace", ""))
            raise DapError(reply.get("error", "agent error"))
        return reply["result"]

    def _agent_pending(self, cmd, **kw):
        """Queue a request to run at the main thread's next safe point (unsafe stop)."""
        if self.fault_stop:
            # Queueing means calling into the interpreter, and this process has crashed.
            self.log("fault stop: not queueing", cmd)
            return
        kw["cmd"] = cmd
        kw["epoch"] = -1
        data = json.dumps(kw).encode()
        self._write(self.sym["seam_pend_buf"], data)
        self._write(self.sym["seam_pend_len"], struct.pack("<q", len(data)))
        thread = self.process.GetSelectedThread()
        self._call(thread, "((int(*)(int(*)(void*), void*))%d)((int(*)(void*))%d, (void*)0)"
                   % (self.sym["Py_AddPendingCall"], self.sym["seam_pending"]), timeout_s=3)

    # --------------------------------------------------------------- launch

    def _resolve_python(self, name):
        path = name if os.sep in name else shutil.which(name)
        if not path or not os.path.exists(path):
            raise DapError("Python interpreter not found: %s" % name)
        return os.path.abspath(path)

    def req_initialize(self, args):
        return {
            "supportsConfigurationDoneRequest": True,
            "supportsConditionalBreakpoints": True,
            "supportsFunctionBreakpoints": True,
            "supportsEvaluateForHovers": True,
            "supportsTerminateRequest": True,
            "supportsExceptionInfoRequest": True,
            "exceptionBreakpointFilters": EXCEPTION_FILTERS,
        }

    def _apply_settings(self, args):
        unknown = [str(s) for s in args.get("stopOnSignals") or ()
                   if str(s).upper() not in signal.Signals.__members__]
        if unknown:
            raise DapError("stopOnSignals: unknown signal %s" % ", ".join(unknown))
        if not args.get("debugInfoLookup", True):
            self.dbg.HandleCommand("settings set symbols.enable-external-lookup false")
        # A native step that leaves user code must stop as soon as it is back in the
        # interpreter, so Seam can hand the step over to the Python side.
        self.dbg.HandleCommand(
            "settings set target.process.thread.step-out-avoid-nodebug false")
        self.framework_paths = FRAMEWORK_PATHS + tuple(args.get("frameworkPaths") or ())
        self.show_glue_frames = bool(args.get("showGlueFrames"))
        self.just_my_code = bool(args.get("justMyCode", True))

    def _apply_signal_policy(self, args):
        """Stop on the signals that mean a crash; hand every other signal to the program.

        LLDB's defaults suit C programs: it stops on SIGUSR1, SIGTERM, SIGPIPE and friends
        and swallows SIGINT. Python programs use those routinely (handlers, timers,
        KeyboardInterrupt), so by default only fault signals stop the debugger.
        """
        wanted = args.get("stopOnSignals")
        wanted = FAULT_SIGNALS if wanted is None else tuple(str(s).upper() for s in wanted)
        signals = self.process.GetUnixSignals()
        known = {}
        for i in range(signals.GetNumSignals()):
            number = signals.GetSignalAtIndex(i)
            known[signals.GetSignalAsCString(number)] = number
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise DapError("stopOnSignals: unknown signal %s" % ", ".join(unknown))
        for name, number in known.items():
            if name in LLDB_SIGNALS:
                continue
            signals.SetShouldStop(number, name in wanted)
            signals.SetShouldNotify(number, name in wanted)
            signals.SetShouldSuppress(number, False)

    def _require_no_session(self):
        if self.target is not None:
            raise DapError("this session is already debugging a program")

    def req_launch(self, args):
        self._require_no_session()
        python = self._resolve_python(args.get("python") or "python3")
        self.cwd = args.get("cwd") or os.getcwd()
        if not os.path.isdir(self.cwd):
            raise DapError("working directory does not exist: %s" % self.cwd)
        argv = list(args.get("pythonArgs") or [])
        if args.get("module"):
            argv += ["-m", args["module"]]
        elif args.get("program"):
            argv.append(args["program"])
        else:
            raise DapError("launch needs either 'program' or 'module'")
        argv += [str(a) for a in args.get("args") or []]

        self._apply_settings(args)
        err = lldb.SBError()
        self.target = self.dbg.CreateTarget(python, None, None, False, err)
        if not self.target or not self.target.IsValid():
            raise DapError("cannot create a target for %s: %s" % (python, err.GetCString()))
        bp_main = self._entry_breakpoint("Py_RunMain")

        info = lldb.SBLaunchInfo(argv)
        info.SetWorkingDirectory(self.cwd)
        # The program inherits the environment `seam dap` was started in, plus launch "env".
        env = {k: v for k, v in os.environ.items() if k != "SEAM_DAP_FD"}
        env.update({str(k): str(v) for k, v in (args.get("env") or {}).items()})
        info.SetEnvironmentEntries(["%s=%s" % kv for kv in env.items()], False)
        info.SetListener(self.listener)
        # The target gets its own pty: LLDB's driver would otherwise swallow its output.
        master, slave = os.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[1] &= ~termios.ONLCR
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        tty = os.ttyname(slave)
        info.AddOpenFileAction(0, tty, True, False)
        info.AddOpenFileAction(1, tty, False, True)
        info.AddOpenFileAction(2, tty, False, True)
        self.process = self.target.Launch(info, err)
        os.close(slave)
        if not err.Success() or not self.process or not self.process.IsValid():
            os.close(master)
            raise DapError("launch failed: %s" % err.GetCString())
        self._note("launched %d" % self.process.GetProcessID())
        self.output_thread = threading.Thread(target=self._pump_output, args=(master,),
                                              daemon=True)
        self.output_thread.start()
        state = self._wait_stop()
        if state != lldb.eStateStopped:
            self._on_exit()
            raise DapError("the process exited before reaching Py_RunMain; is %s a "
                           "CPython 3.12+ interpreter?" % python)
        thread = self.process.GetSelectedThread()
        if (thread.GetStopReason() != lldb.eStopReasonBreakpoint
                or thread.GetStopReasonDataAtIndex(0) != bp_main.GetID()):
            raise DapError("unexpected stop before Py_RunMain: %s" % thread.GetStopDescription(200))
        self.target.BreakpointDelete(bp_main.GetID())
        self._apply_signal_policy(args)
        self._inject(thread)
        self.safe_tid = thread.GetThreadID()
        self._sync_native_bps()
        if args.get("stopOnEntry"):
            self.agent("step", mode="any")
            self.py_step_armed = True
        return None, lambda: self.event("initialized")

    def _watch_breakpoints(self):
        """Receive LLDB's breakpoint events (locations resolving when a module loads)."""
        self.target.GetBroadcaster().AddListener(
            self.listener, lldb.SBTarget.eBroadcastBitBreakpointChanged)

    def _entry_breakpoint(self, name):
        """Breakpoint on a function's first instruction, located through the symbol table.

        Seam's own breakpoints are not set by function name. LLDB 20 resolves a name
        breakpoint through the debug info and skips the prologue; with the interpreter's
        debug info in a separate file it lands at a wrong address (`Py_RunMain` ended up
        inside a data table), so the breakpoint was never hit. The symbol table is right.
        """
        for ctx in self.target.FindSymbols(name):
            address = ctx.GetSymbol().GetStartAddress()
            if address.IsValid():
                return self.target.BreakpointCreateBySBAddress(address)
        # Not in any module yet (an interpreter linked against libpython): by name.
        return self.target.BreakpointCreateByName(name)

    def _find_python(self):
        """Locate the interpreter in the process and set up the raw-memory reader."""
        self._watch_breakpoints()
        run, module = self._symbol("PyRun_SimpleStringFlags")
        runtime, _ = self._symbol("_PyRuntime")
        version_addr, _ = self._symbol("Py_Version")
        if not run or not runtime or not version_addr:
            raise DapError("this does not look like a CPython 3.12+ process: the "
                           "interpreter's symbols are missing")
        self.interp_module = module.GetFileSpec().fullpath
        hexversion = struct.unpack("<I", self._read(version_addr, 4))[0]
        version = (hexversion >> 24, (hexversion >> 16) & 0xFF)
        if version < (3, 12):
            raise DapError("Seam needs CPython 3.12 or newer; this is %d.%d" % version)
        self.sym["PyRun_SimpleStringFlags"] = run
        self.sym["PyRun_SimpleString"] = self._symbol("PyRun_SimpleString")[0]
        self.sym["Py_AddPendingCall"] = self._symbol("Py_AddPendingCall")[0]
        self.sym["getpid"] = self._symbol("getpid")[0]
        try:
            self.py = pyread.PyReader(self._read, runtime, version)
        except (ValueError, NotImplementedError) as exc:
            raise DapError("unsupported interpreter: %s" % exc)

    def _load_helper(self):
        """Resolve the helper's symbols once the agent has been imported."""
        module = None
        for name in HELPER_SYMBOLS:
            addr, module = self._symbol(name)
            if not addr:
                raise DapError("Seam helper symbol %s not found after injection" % name)
            self.sym[name] = addr
        self.helper_module = module.GetFileSpec().fullpath
        self.sym["cap"] = struct.unpack("<q", self._read(self.sym["seam_req_cap"], 8))[0]
        if self.bp_trap is None:
            # Launch: set by address (see _entry_breakpoint). On attach the breakpoint
            # was set by name before the helper loaded and the thread is stopped at it
            # right now, so it is left alone.
            self.bp_trap = self.target.BreakpointCreateByAddress(self.sym["seam_trap"])

    def _inject(self, thread):
        """Load the agent. The caller guarantees `thread` is at a safe point."""
        self._find_python()
        code = ("import sys; sys.path.insert(0, %r)\n"
                "try:\n    import seam_agent\n"
                "finally:\n    sys.path.remove(%r)\n" % (TARGET_DIR, TARGET_DIR))
        rc = self._call(thread, "((int(*)(const char*, void*))%d)(%s, (void*)0)"
                        % (self.sym["PyRun_SimpleStringFlags"], json.dumps(code)))
        self._drain_output()
        if rc != 0:
            raise DapError("could not load the Seam agent into the process (see its output)")
        self._load_helper()

    # --------------------------------------------------------------- attach

    def req_attach(self, args):
        self._require_no_session()
        pid = int(args.get("pid") or 0)
        if pid <= 0:
            raise DapError("attach needs a 'pid'")
        self._apply_settings(args)
        err = lldb.SBError()
        self.target = self.dbg.CreateTarget("")
        self.process = self.target.AttachToProcessWithID(self.listener, pid, err)
        if not err.Success() or not self.process or not self.process.IsValid():
            self.process = None
            raise DapError("cannot attach to pid %d: %s (is ptrace allowed? see "
                           "/proc/sys/kernel/yama/ptrace_scope)" % (pid, err.GetCString()))
        self.attached = True
        try:
            self._wait_attached(pid)
            self._apply_signal_policy(args)
            try:
                self.cwd = os.readlink("/proc/%d/cwd" % pid)
            except OSError:
                pass
            self._find_python()
            # The helper is not loaded yet, so this breakpoint can only be set by name.
            # (The helper carries its own debug info, so the LLDB 20 problem with name
            # breakpoints and separate debug files does not apply to it.)
            self.bp_trap = self.target.BreakpointCreateByName("seam_trap")
            # Evaluate one harmless call now. On 3.14 the PEP 768 path would otherwise
            # make its first expression at the helper's trap, right after the helper
            # library was loaded, and LLDB 18 crashed or hung there in about 1 attach
            # in 13 on CI. The 3.12/3.13 path already evaluates an expression here.
            try:
                self._call(self.process.GetSelectedThread(),
                           "((int(*)(void))%d)()" % self.sym["getpid"], timeout_s=5)
            except DapError as exc:
                self.log("warm-up call failed:", exc)
            method = self._request_agent_load()
            thread = self._wait_for_attach_trap(float(args.get("timeout") or 15))
        except DapError:
            self._abandon()
            raise
        self._load_helper()
        self.safe_tid = thread.GetThreadID()
        self.stop_is_trap = True
        self.event("output", {"category": "console", "output":
                   "Seam: attached to pid %d (helper loaded via %s).\n" % (pid, method)})
        return None, lambda: self.event("initialized")

    def _wait_attached(self, pid, timeout=30):
        """Wait for the stop that completes an attach.

        LLDB does not always deliver a stop event for it, so the public state is polled
        as well. That is safe only here: the process has never been resumed by Seam, so
        "stopped" cannot be a stale reading from before a resume.
        """
        ev = lldb.SBEvent()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.listener.WaitForEvent(1, ev) and lldb.SBProcess.EventIsProcessEvent(ev):
                state = lldb.SBProcess.GetStateFromEvent(ev)
                if state in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateCrashed):
                    raise DapError("pid %d exited while attaching" % pid)
                if state == lldb.eStateStopped:
                    return
            if self.process.GetState() == lldb.eStateStopped:
                return
        raise DapError("timed out attaching to pid %d" % pid)

    def _request_agent_load(self):
        """Ask the stopped process to import the agent at its main thread's next safe point."""
        code = (
            "import sys\n"
            "try:\n"
            "    sys.path.insert(0, %r)\n"
            "    try:\n"
            "        import seam_agent\n"
            "    finally:\n"
            "        sys.path.remove(%r)\n"
            "    seam_agent.attached()\n"
            "except BaseException:\n"
            "    import traceback\n"
            "    traceback.print_exc()\n" % (TARGET_DIR, TARGET_DIR))
        remote = self.py.L.remote
        if remote is not None:
            # PEP 768 (3.14+): three memory writes, no code run by the debugger.
            interp = self.py.u64(self.py.runtime + self.py.L.runtime_interp_head)
            enabled = struct.unpack("<i", self._read(interp + remote["enabled"], 4))[0]
            tstate = self.py.u64(interp + remote["threads_main"])
            if enabled and tstate:
                fd, script = tempfile.mkstemp(prefix="seam-attach-", suffix=".py")
                with os.fdopen(fd, "w") as fh:
                    fh.write(code)
                self.temp_files.append(script)
                path = script.encode() + b"\0"
                if len(path) <= remote["path_size"]:
                    support = tstate + remote["support"]
                    self._write(support + remote["path"], path)
                    self._write(support + remote["pending"], struct.pack("<i", 1))
                    breaker = self.py.u64(tstate + remote["eval_breaker"])
                    self._write(tstate + remote["eval_breaker"],
                                struct.pack("<Q", breaker | EVAL_PLEASE_STOP_BIT))
                    return "PEP 768 remote exec"
        # 3.12/3.13 (or remote debugging disabled): queue a pending call. This runs
        # Py_AddPendingCall in the stopped process, which is not a safe point; the call
        # only takes a short internal lock, and it is abandoned if it does not return.
        if not self.sym.get("PyRun_SimpleString") or not self.sym.get("Py_AddPendingCall"):
            raise DapError("this interpreter does not export the functions attach needs")
        err = lldb.SBError()
        data = code.encode() + b"\0"
        addr = self.process.AllocateMemory(
            len(data), lldb.ePermissionsReadable | lldb.ePermissionsWritable, err)
        if not err.Success():
            raise DapError("cannot allocate memory in the process: %s" % err.GetCString())
        self._write(addr, data)
        self._call(self.process.GetSelectedThread(),
                   "((int(*)(int(*)(void*), void*))%d)((int(*)(void*))%d, (void*)%d)"
                   % (self.sym["Py_AddPendingCall"], self.sym["PyRun_SimpleString"], addr),
                   timeout_s=3)
        return "a pending call"

    def _wait_for_attach_trap(self, timeout):
        """Run until the agent reports in from `seam_agent.attached()`."""
        deadline = time.monotonic() + timeout
        while True:
            err = self.process.Continue()
            if not err.Success():
                raise DapError("could not resume the process: %s" % err.GetCString())
            remaining = int(max(1, deadline - time.monotonic()))
            try:
                state = self._wait_stop(remaining)
            except DapError:
                self.process.SendAsyncInterrupt()
                self._wait_stop(10)
                raise DapError(
                    "the process did not load the Seam helper within %d s. Its main "
                    "thread never reached a safe point; it is probably blocked in a "
                    "system call or a long native call." % timeout)
            if state != lldb.eStateStopped:
                raise DapError("the process exited while attaching")
            thread = self._trap_thread()
            if thread is not None:
                return thread
            if time.monotonic() > deadline:
                raise DapError("the process did not load the Seam helper in time")

    def _abandon(self):
        """Give up on a process we attached to, leaving it running."""
        if self.process is not None and self.process.IsValid():
            self.target.DeleteAllBreakpoints()
            self.process.Detach()
        self.exited = True

    def _detach(self):
        """Remove everything Seam armed and let the process carry on."""
        if self.exited or self.process is None:
            return
        try:
            if self.running:
                self._interrupt()
            self._finish_steps(self.process.GetSelectedThread())
            if self.helper_module:
                self.py_bps = {}
                if self.safe_tid is not None:
                    self.agent("shutdown")
                else:
                    self._agent_pending("shutdown")
        except (DapError, ValueError) as exc:
            self.log("detach clean-up failed:", exc)
        self.target.DeleteAllBreakpoints()
        self.process.Detach()
        self.exited = True

    def req_configurationDone(self, args):
        if self.process is None:
            raise DapError("nothing has been launched")
        self._new_stop()
        self._continue()
        return None

    def req_disconnect(self, args):
        self.done = True
        return None

    def req_terminate(self, args):
        self._kill()
        self.event("terminated")
        return None

    # ---------------------------------------------------------- breakpoints

    def req_setBreakpoints(self, args):
        path = (args.get("source") or {}).get("path")
        if not path:
            raise DapError("setBreakpoints needs source.path")
        wanted = args.get("breakpoints") or []
        if path.endswith(PY_SUFFIXES):
            items = [{"line": b["line"], "condition": b.get("condition")} for b in wanted]
            if items:
                self.py_bps[path] = items
            else:
                self.py_bps.pop(path, None)
            result = self._sync_py_bps()
            answers = (result or {}).get(path) or [{"line": i["line"], "verified": True}
                                                   for i in items]
            return {"breakpoints": answers}
        with self._paused():
            for bp in self.native_bps.pop(path, []):
                self.target.BreakpointDelete(bp.GetID())
            created = []
            answers = []
            for b in wanted:
                bp = self.target.BreakpointCreateByLocation(path, b["line"])
                if b.get("condition"):
                    bp.SetCondition(b["condition"])
                self._drop_glue_locations(bp)
                created.append(bp)
                self.native_bp_lines[bp.GetID()] = b["line"]
                answer = self._native_bp_answer(bp)
                self.native_bp_state[bp.GetID()] = (answer["verified"], answer["line"])
                answers.append(answer)
            self.native_bps[path] = created
        return {"breakpoints": answers}

    def _native_bp_answer(self, bp):
        """DAP description of a native breakpoint: where it really is, if anywhere."""
        line = self.native_bp_lines.get(bp.GetID(), 0)
        best = None
        for i in range(bp.GetNumLocations()):
            location = bp.GetLocationAtIndex(i)
            if location.IsEnabled():
                address = location.GetAddress()
                if best is None or address.GetFileAddress() < best.GetFileAddress():
                    best = address
        answer = {"id": bp.GetID(), "verified": best is not None, "line": line}
        if best is None:
            answer["message"] = ("no code for this line yet: its module is not loaded, or "
                                 "the compiler left the line with no code of its own")
        elif best.GetLineEntry().IsValid() and best.GetLineEntry().GetLine():
            answer["line"] = best.GetLineEntry().GetLine()
        return answer

    def _refresh_native_bp_status(self):
        """Tell the client when a pending native breakpoint resolves (or moves)."""
        for group in self.native_bps.values():
            for bp in group:
                answer = self._native_bp_answer(bp)
                state = (answer["verified"], answer["line"])
                if self.native_bp_state.get(bp.GetID()) != state:
                    self.native_bp_state[bp.GetID()] = state
                    self.event("breakpoint", {"reason": "changed", "breakpoint": answer})

    def req_setFunctionBreakpoints(self, args):
        with self._paused():
            for bp in self.function_bps:
                self.target.BreakpointDelete(bp.GetID())
            self.function_bps = []
            answers = []
            for b in args.get("breakpoints") or []:
                bp = self.target.BreakpointCreateByName(b["name"])
                self.function_bps.append(bp)
                answers.append({"verified": bp.GetNumLocations() > 0})
        return {"breakpoints": answers}

    def req_setExceptionBreakpoints(self, args):
        wanted = list(args.get("filters") or [])
        wanted += [option.get("filterId") for option in args.get("filterOptions") or []]
        known = [f["filter"] for f in EXCEPTION_FILTERS]
        self.exc_filters = [name for name in known if name in wanted]
        if self.process is not None and not self.exited:
            with self._paused():
                for name in ("cpp_throw", "rust_panic"):
                    bp = self.native_exc_bps.pop(name, None)
                    if bp is not None:
                        self.target.BreakpointDelete(bp.GetID())
                    if name == "cpp_throw" and name in self.exc_filters:
                        self.native_exc_bps[name] = self.target.BreakpointCreateForException(
                            lldb.eLanguageTypeC_plus_plus, False, True)
                    elif name in self.exc_filters:
                        self.native_exc_bps[name] = self.target.BreakpointCreateByName(
                            "rust_panic")
            self._sync_py_bps()
        return {"breakpoints": [{"verified": name in known} for name in wanted]}

    def req_exceptionInfo(self, args):
        self._require_stopped()
        info = self.exception_info.get(args.get("threadId"))
        if info is None:
            raise DapError("this thread is not stopped at an exception")
        return info

    def _sync_native_bps(self):
        pass  # native breakpoints are created directly on the target

    def _sync_py_bps(self):
        """Push the full Python breakpoint table to the agent, now or at the next safe point."""
        if self.process is None or self.exited:
            return None
        exceptions = {"filters": [name for name in self.exc_filters
                                  if name in PYTHON_EXCEPTION_FILTERS],
                      "just_my_code": self.just_my_code}
        if self.safe_tid is not None:
            if self.pending_sync:
                self._write(self.sym["seam_pend_len"], struct.pack("<q", 0))
                self.pending_sync = False
            return self.agent("sync_breakpoints", files=self.py_bps, exceptions=exceptions)
        with self._paused():
            self._agent_pending("sync_breakpoints", files=self.py_bps, exceptions=exceptions)
            self.pending_sync = True
        return None

    def _paused(self):
        return _Paused(self)

    # ---------------------------------------------------------------- stack

    def req_threads(self, args):
        if self.process is None:
            return {"threads": []}
        threads = []
        for thread in self.process:
            name = thread.GetName() or "Thread"
            threads.append({"id": thread.GetThreadID(), "name": "%s (%d)" % (name, thread.GetThreadID())})
        return {"threads": threads}

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def _py_path(self, filename):
        if not filename or filename.startswith("<"):
            return None
        if not os.path.isabs(filename):
            filename = os.path.join(self.cwd, filename)
        return os.path.realpath(filename)

    def _merged_stack(self, thread):
        tid = thread.GetThreadID()
        cached = self.stacks.get(tid)
        if cached is not None:
            return cached
        tstate, _ = self.py.find_thread(tid)
        groups = self.py.thread_groups(tstate) if tstate else []
        natives = self._native_frames(thread)
        sps = [f.GetSP() for f in natives]
        if self.logfile:
            self.log("native frames of", tid, "stop reason", thread.GetStopReason(),
                     thread.GetStopDescription(80))
            for f in natives:
                self.log("   %#x sp=%#x %s [%s]" % (f.GetPC(), f.GetSP(), f.GetFunctionName(),
                                                    f.GetModule().GetFileSpec().GetFilename()))
            self.log("python groups", groups)
        # Each group's entry frame lives in the C frame of the eval loop running it. Find
        # that C frame by address only: its name is not reliable (it may be inlined into
        # its caller, renamed by LTO, or replaced by tail-call handlers on 3.14).
        # A frame's stack area runs from its SP to the next older frame with a higher SP.
        uppers = [None] * len(natives)
        for i in range(len(natives) - 2, -1, -1):
            uppers[i] = sps[i + 1] if sps[i + 1] > sps[i] else uppers[i + 1]
        anchors = {}
        unmatched = []
        search_from = 0
        for entry, frames in groups:
            found = None
            if entry is not None:
                for i in range(search_from, len(natives)):
                    # The oldest frame has no known upper bound, so it never matches:
                    # if the backtrace was cut short it would claim every older group.
                    if uppers[i] is not None and sps[i] <= entry < uppers[i]:
                        found = i
                        break
            if found is None:
                unmatched.append((entry, frames))
            else:
                anchors[found] = frames
                search_from = found + 1
        groups = unmatched
        if unmatched and natives:
            # Python frames whose eval loop is not on the native stack LLDB produced.
            self._warn_truncated(natives[-1])

        out = []
        hide_top = self.stop_is_trap and tid == self.safe_tid
        py_index = 0
        last_python = None
        for i, frame in enumerate(natives):
            name = frame.GetFunctionName() or ""
            module = frame.GetModule().GetFileSpec().fullpath
            if i in anchors:
                for pf in anchors[i]:
                    out.append({"kind": "py", "tid": tid, "index": py_index, "name": pf.name,
                                "path": self._py_path(pf.filename), "line": pf.line,
                                "pf": pf})
                    py_index += 1
                hide_top = False
                last_python = len(out)
                continue
            if hide_top or module in (self.interp_module, self.helper_module):
                continue
            entry_line = frame.GetLineEntry()
            spec = entry_line.GetFileSpec()
            path = spec.fullpath if spec.IsValid() else None
            out.append({"kind": "native", "tid": tid, "index": i,
                        "name": name or "%#x" % frame.GetPC(),
                        "path": path, "line": entry_line.GetLine() if path else 0,
                        "cls": self._classify_frame(frame),
                        "at": (frame.GetPC(), sps[i])})
        for _, frames in groups:  # could not be matched to a C frame; show them anyway
            for pf in frames:
                out.append({"kind": "py", "tid": tid, "index": py_index, "name": pf.name,
                            "path": self._py_path(pf.filename), "line": pf.line,
                            "pf": pf})
                py_index += 1
            last_python = len(out)
        if last_python is not None:
            del out[last_python:]  # thread bootstrap frames below the oldest Python frame
        # Glue inlined into a user function shares that function's PC and SP. The thread
        # is physically in the user function, so that is the frame to show on top.
        if out and out[0]["kind"] == "native" and out[0]["cls"] in GLUE:
            for position, record in enumerate(out):
                if record["kind"] != "native" or record["at"] != out[0]["at"]:
                    break
                if record["cls"] == "user":
                    del out[:position]
                    break
        if tid in self.post_mortem:
            # Stopped at an uncaught exception. The frames it passed through have already
            # unwound; the traceback keeps them alive, and they are what the user wants
            # to see. They go on top of whatever is still on the stack.
            out[:0] = [{"kind": "py", "tid": tid, "index": 0, "pm": position,
                        "name": frame["name"], "path": self._py_path(frame["filename"]),
                        "line": frame["line"], "pf": None}
                       for position, frame in enumerate(self.post_mortem[tid])]
        for record in out:
            record["id"] = self._new_id()
            self.frames[record["id"]] = record
        self.stacks[tid] = out
        return out

    def req_stackTrace(self, args):
        self._require_stopped()
        stack = self._merged_stack(self._thread(args["threadId"]))
        if not self.show_glue_frames:
            # Binding-layer trampolines between user code and Python are noise (PyO3 puts
            # ten of them under every function). The newest frame is always shown.
            stack = [r for i, r in enumerate(stack)
                     if i == 0 or r.get("cls") not in ("framework", "nodebug")]
        start = args.get("startFrame") or 0
        levels = args.get("levels") or len(stack)
        frames = []
        for record in stack[start:start + levels]:
            frame = {"id": record["id"], "name": record["name"], "line": record["line"],
                     "column": 0}
            if record["path"]:
                frame["source"] = {"name": os.path.basename(record["path"]),
                                   "path": record["path"]}
            if not record["path"] or record.get("cls") in GLUE:
                frame["presentationHint"] = "subtle"
            frames.append(frame)
        return {"stackFrames": frames, "totalFrames": len(stack)}

    def _require_stopped(self):
        if self.process is None or self.running or self.exited:
            raise DapError("the process is not stopped")

    def _frame_record(self, frame_id):
        record = self.frames.get(frame_id)
        if record is None:
            raise DapError("unknown or stale frame id %s" % frame_id)
        return record

    def _native_frame(self, record):
        return self._thread(record["tid"]).GetFrameAtIndex(record["index"])

    # ------------------------------------------------------------ variables

    def _new_ref(self, record):
        ref = self._new_id()
        self.refs[ref] = record
        return ref

    def req_scopes(self, args):
        self._require_stopped()
        record = self._frame_record(args["frameId"])
        if record["kind"] == "py":
            scopes = [{"name": "Locals", "presentationHint": "locals", "expensive": False,
                       "variablesReference": self._new_ref(("py", "locals", record))}]
            if self.safe_tid is not None:
                scopes.append({"name": "Globals", "expensive": True,
                               "variablesReference": self._new_ref(("py", "globals", record))})
            return {"scopes": scopes}
        return {"scopes": [
            {"name": "Locals", "presentationHint": "locals", "expensive": False,
             "variablesReference": self._new_ref(("native", record))},
        ]}

    def _py_var(self, item):
        ref = self._new_ref(("pyref", item["ref"])) if item.get("ref") else 0
        return {"name": item["name"], "value": item["value"], "type": item["type"],
                "variablesReference": ref}

    def _sb_var(self, value):
        text = value.GetSummary() or value.GetValue()
        expandable = value.MightHaveChildren()
        if text is None:
            text = "{...}" if expandable else ""
        return {"name": value.GetName() or "", "value": text, "type": value.GetTypeName() or "",
                "variablesReference": self._new_ref(("sb", value)) if expandable else 0}

    def req_variables(self, args):
        self._require_stopped()
        record = self.refs.get(args["variablesReference"])
        if record is None:
            raise DapError("unknown or stale variablesReference")
        kind = record[0]
        if kind == "py":
            _, scope, frame = record
            if self.safe_tid is not None:
                try:
                    items = self.agent("variables", kind=scope, tid=frame["tid"],
                                       index=frame["index"], pm=frame.get("pm"))
                    return {"variables": [self._py_var(i) for i in items]}
                except DapError:
                    if scope != "locals" or frame["pf"] is None:
                        raise
            elif scope != "locals":
                raise DapError(UNSAFE_MESSAGE)
            # Native stop (or a thread the agent cannot see): decode from memory only.
            return {"variables": [
                {"name": name, "value": text, "type": tname, "variablesReference": 0}
                for name, text, tname in self.py.frame_locals(frame["pf"])]}
        if kind == "pyref":
            items = self.agent("variables", kind="ref", ref=record[1])
            return {"variables": [self._py_var(i) for i in items]}
        if kind == "native":
            frame = self._native_frame(record[1])
            values = frame.GetVariables(True, True, False, True)
            return {"variables": [self._sb_var(v) for v in values]}
        value = record[1]
        count = min(value.GetNumChildren(), 500)
        return {"variables": [self._sb_var(value.GetChildAtIndex(i)) for i in range(count)]}

    def req_evaluate(self, args):
        self._require_stopped()
        expr = args.get("expression", "")
        frame_id = args.get("frameId")
        record = self._frame_record(frame_id) if frame_id is not None else None
        if record is None:
            stack = self._merged_stack(self.process.GetSelectedThread())
            record = stack[0] if stack else None
        if record is None:
            raise DapError("no frame to evaluate in")
        if record["kind"] == "py":
            if self.safe_tid is None:
                raise DapError(UNSAFE_MESSAGE)
            item = self.agent("evaluate", tid=record["tid"], index=record["index"],
                              pm=record.get("pm"), expr=expr)
            var = self._py_var(item)
            return {"result": var["value"], "type": var["type"],
                    "variablesReference": var["variablesReference"]}
        value = self._native_frame(record).EvaluateExpression(expr, self._expr_options(10))
        if not value.GetError().Success():
            raise DapError(value.GetError().GetCString() or "evaluation failed")
        var = self._sb_var(value)
        return {"result": var["value"], "type": var["type"],
                "variablesReference": var["variablesReference"]}

    # ------------------------------------------------------------- stepping

    def req_continue(self, args):
        self._require_stopped()
        self._new_stop()
        self._continue()
        return {"allThreadsContinued": True}

    def req_pause(self, args):
        if self.process is None or not self.running:
            raise DapError("the process is not running")
        self.pause_requested = True
        self.process.SendAsyncInterrupt()
        return None

    def _step(self, args, mode):
        self._require_stopped()
        thread = self._thread(args["threadId"])
        tid = thread.GetThreadID()
        if tid in self.post_mortem or self.throw_stop:
            # An uncaught exception (its frames are gone) or a native throw (control
            # leaves by unwinding, not by returning): there is nothing to step through.
            self._new_stop()
            self._continue()
            return None
        stack = self._merged_stack(thread)
        # Step relative to the newest frame the user cares about: a Python frame or user
        # native code. System-library and glue frames above it (e.g. being paused inside
        # nanosleep under time.sleep) do not count.
        position = next((i for i, r in enumerate(stack)
                         if r["kind"] == "py" or r["cls"] == "user"), None)
        top = stack[position] if position is not None else None
        if top is not None and top["kind"] == "py":
            # If this Python frame was called from user native code, leaving it must end
            # in that native frame rather than in the Python frame further down.
            native_return = False
            for record in stack[position + 1:]:
                if record["kind"] == "py":
                    break
                if record["cls"] == "user":
                    native_return = True
                    break
            request = {"mode": mode, "tid": tid, "index": top["index"],
                       "native_return": native_return}
            if self.safe_tid is not None:
                self.agent("step", **request)
            else:
                # Stopped inside the interpreter (e.g. after a pause): arm the step at
                # the main thread's next safe point.
                self._agent_pending("step", **request)
            if mode == "in":
                self._enable_user_bps(tid)
            self.py_step_armed = True
            self._new_stop()
            self._continue()
            return None

        self.process.SetSelectedThread(thread)
        natives = self._native_frames(thread)
        start = top["index"] if top is not None else 0
        if mode == "in" and self._control_safe(thread):
            # If the stepped statement calls back into Python, stop on its first line.
            self.safe_tid = tid
            try:
                self.agent("step", mode="any", tid=tid)
                self.py_step_armed = True
            except DapError as exc:
                self.log("cannot arm a Python step from native code:", exc)
        self.native_stepping = {"tid": tid, "hops": 0}
        self._new_stop()
        if start > 0:
            # Stopped inside a library call made by user code: any step returns to it.
            self._step_out_to(thread, natives, start)
        elif mode == "over":
            thread.StepOver()
        elif mode == "in":
            thread.StepInto()
        else:
            # Step out to the next frame worth showing: skip binding glue.
            self._step_out_of_glue(thread)
        self.running = True
        return None

    def req_next(self, args):
        return self._step(args, "over")

    def req_stepIn(self, args):
        return self._step(args, "in")

    def req_stepOut(self, args):
        return self._step(args, "out")

    # ----------------------------------------------------------- test hooks

    def req_seam_lldb(self, args):
        """Run a raw LLDB command (diagnostics only)."""
        result = lldb.SBCommandReturnObject()
        self.dbg.GetCommandInterpreter().HandleCommand(args["command"], result)
        return {"output": (result.GetOutput() or "") + (result.GetError() or "")}

    def req_seam_status(self, args):
        """Internal state, used by the test suite to check nothing is left behind."""
        self._require_stopped()
        body = {
            "safe": self.safe_tid is not None,
            "nativeBreakpoints": sum(len(v) for v in self.native_bps.values())
            + len(self.function_bps),
            "totalBreakpoints": self.target.GetNumBreakpoints(),
            "pid": self.process.GetProcessID(),
            "stepInBreakpointsEnabled": any(bp.IsEnabled() for bp in self.user_bps.values()),
            "nativeStepInProgress": self.native_stepping is not None,
            "pythonStepArmed": self.py_step_armed,
        }
        if self.safe_tid is not None:
            body["agent"] = self.agent("status")
        return body


class _Paused:
    """Context manager: make sure the process is stopped, then restore its state."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.resume = False

    def __enter__(self):
        a = self.adapter
        if a.process is not None and a.running and not a.exited:
            self.resume = a._interrupt()
            if not self.resume:
                self.real_stop = True
        return self

    def __exit__(self, *exc):
        a = self.adapter
        if self.resume:
            a._continue()
        elif getattr(self, "real_stop", False):
            a._on_stop()
        return False


def serve(debugger):
    import socket

    fd = int(os.environ["SEAM_DAP_FD"])
    os.set_inheritable(fd, False)  # neither channel is the debugged program's business
    sock = socket.socket(fileno=fd)
    log_path = os.environ.get("SEAM_LOG")
    log = open(log_path, "a") if log_path else None
    adapter = Adapter(debugger, sock, log)
    note_fd = os.environ.get("SEAM_NOTE_FD")
    if note_fd:
        os.set_inheritable(int(note_fd), False)
        adapter.note_fd = int(note_fd)
    try:
        adapter.run()
    except Exception:
        adapter.log(traceback.format_exc())
        raise
    finally:
        adapter._kill()
        try:
            sock.close()
        except OSError:
            pass
