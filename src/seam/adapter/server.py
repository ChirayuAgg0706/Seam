"""Seam's debug adapter. Runs inside LLDB's embedded Python interpreter (stdlib only).

One controller owns the process: LLDB. Python-level stops arrive as native stops in
`seam_trap`; only there (and at other known safe points) does the adapter execute code
in the target. At every other stop it only reads memory.
"""
import collections
import json
import os
import shutil
import struct
import termios
import threading
import traceback

import lldb

from . import pyread

TARGET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_target")
PY_SUFFIXES = (".py", ".pyw", ".pyi")
EVAL_FRAME = "_PyEval_EvalFrameDefault"

R_BREAKPOINT = 1
R_STEP = 2
R_RETURN_NATIVE = 3

HELPER_SYMBOLS = (
    "seam_trap", "seam_dispatch", "seam_pending", "seam_req_buf", "seam_req_len",
    "seam_resp_ptr", "seam_resp_len", "seam_req_cap", "seam_pend_buf", "seam_pend_len",
)

UNSAFE_MESSAGE = (
    "Seam cannot run Python here: the process is stopped in native code, where the "
    "interpreter may be in an inconsistent state. Python can be evaluated at Python "
    "breakpoints and steps."
)


class DapError(Exception):
    pass


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
            result = handler(req.get("arguments") or {})
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
                return  # stale event from an internal interrupt
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
        self.event("exited", {"exitCode": self.process.GetExitStatus()})
        self.event("terminated")

    def _wait_stop(self, timeout=30):
        """Block until the process stops or exits. Returns the new state."""
        ev = lldb.SBEvent()
        waited = 0
        while waited < timeout:
            if not self.listener.WaitForEvent(1, ev):
                waited += 1
                if self.process.GetState() == lldb.eStateStopped:
                    return lldb.eStateStopped
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

    def _on_stop(self):
        self._new_stop()
        self._fix_stale_frames()
        self._drain_output()
        thread = self._trap_thread()
        if thread is not None:
            frame = thread.GetFrameAtIndex(0)
            reason = frame.FindRegister("rdx").GetValueAsUnsigned()
            self.safe_tid = thread.GetThreadID()
            self.stop_is_trap = True
            self.process.SetSelectedThread(thread)
            if self.pending_sync:
                self._sync_py_bps()
            self.event("stopped", {
                "reason": "breakpoint" if reason == R_BREAKPOINT else "step",
                "threadId": self.safe_tid, "allThreadsStopped": True})
            return
        thread = None
        for candidate in self.process:
            if self._interesting(candidate):
                thread = candidate
                break
        if thread is None:
            thread = self.process.GetSelectedThread()
        self.process.SetSelectedThread(thread)
        reason = thread.GetStopReason()
        body = {"threadId": thread.GetThreadID(), "allThreadsStopped": True}
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
        else:
            body["reason"] = "pause"
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
        if self.process is not None and self.process.IsValid() and not self.exited:
            self.process.Kill()
            self.exited = True

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
        }

    def req_launch(self, args):
        python = self._resolve_python(args.get("python") or "python3")
        self.cwd = args.get("cwd") or os.getcwd()
        argv = list(args.get("pythonArgs") or [])
        if args.get("module"):
            argv += ["-m", args["module"]]
        elif args.get("program"):
            argv.append(args["program"])
        else:
            raise DapError("launch needs either 'program' or 'module'")
        argv += [str(a) for a in args.get("args") or []]

        if not args.get("debugInfoLookup", True):
            self.dbg.HandleCommand("settings set symbols.enable-external-lookup false")
        err = lldb.SBError()
        self.target = self.dbg.CreateTarget(python, None, None, False, err)
        if not self.target or not self.target.IsValid():
            raise DapError("cannot create a target for %s: %s" % (python, err.GetCString()))
        bp_main = self.target.BreakpointCreateByName("Py_RunMain")
        self.bp_trap = self.target.BreakpointCreateByName("seam_trap")

        info = lldb.SBLaunchInfo(argv)
        info.SetWorkingDirectory(self.cwd)
        env = ["%s=%s" % kv for kv in (args.get("env") or {}).items()]
        info.SetEnvironmentEntries(env, True)
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
        self._inject(thread)
        self.safe_tid = thread.GetThreadID()
        self._sync_native_bps()
        if args.get("stopOnEntry"):
            self.agent("step", mode="entry")
        return None, lambda: self.event("initialized")

    def _inject(self, thread):
        """Load the agent. The caller guarantees `thread` is at a safe point."""
        run, module = self._symbol("PyRun_SimpleStringFlags")
        runtime, _ = self._symbol("_PyRuntime")
        version_addr, _ = self._symbol("Py_Version")
        if not run or not runtime or not version_addr:
            raise DapError("this does not look like CPython 3.12+: required symbols are missing")
        self.interp_module = module.GetFileSpec().fullpath
        hexversion = struct.unpack("<I", self._read(version_addr, 4))[0]
        version = (hexversion >> 24, (hexversion >> 16) & 0xFF)
        if version < (3, 12):
            raise DapError("Seam needs CPython 3.12 or newer; this is %d.%d" % version)
        code = ("import sys; sys.path.insert(0, %r)\n"
                "try:\n    import seam_agent\n"
                "finally:\n    sys.path.remove(%r)\n" % (TARGET_DIR, TARGET_DIR))
        rc = self._call(thread, "((int(*)(const char*, void*))%d)(%s, (void*)0)"
                        % (run, json.dumps(code)))
        self._drain_output()
        if rc != 0:
            raise DapError("could not load the Seam agent into the process (see its output)")
        for name in HELPER_SYMBOLS:
            addr, module = self._symbol(name)
            if not addr:
                raise DapError("Seam helper symbol %s not found after injection" % name)
            self.sym[name] = addr
        self.helper_module = module.GetFileSpec().fullpath
        self.sym["Py_AddPendingCall"] = self._symbol("Py_AddPendingCall")[0]
        self.sym["getpid"] = self._symbol("getpid")[0]
        self.sym["cap"] = struct.unpack("<q", self._read(self.sym["seam_req_cap"], 8))[0]
        self.py = pyread.PyReader(self._read, runtime, version)

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
                created.append(bp)
                answer = {"verified": bp.GetNumLocations() > 0, "line": b["line"]}
                if not answer["verified"]:
                    answer["message"] = "pending: no loaded module contains this line yet"
                answers.append(answer)
            self.native_bps[path] = created
        return {"breakpoints": answers}

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
        return {"breakpoints": []}

    def _sync_native_bps(self):
        pass  # native breakpoints are created directly on the target

    def _sync_py_bps(self):
        """Push the full Python breakpoint table to the agent, now or at the next safe point."""
        if self.process is None or self.exited:
            return None
        if self.safe_tid is not None:
            if self.pending_sync:
                self._write(self.sym["seam_pend_len"], struct.pack("<q", 0))
                self.pending_sync = False
            return self.agent("sync_breakpoints", files=self.py_bps)
        with self._paused():
            self._agent_pending("sync_breakpoints", files=self.py_bps)
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
        groups = []
        for native_tid, tstate in self.py.thread_states():
            if native_tid == tid:
                groups = self.py.thread_groups(tstate)
                break
        natives = [thread.GetFrameAtIndex(i) for i in range(thread.GetNumFrames())]
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
                    if sps[i] <= entry and (uppers[i] is None or entry < uppers[i]):
                        found = i
                        break
            if found is None:
                unmatched.append((entry, frames))
            else:
                anchors[found] = frames
                search_from = found + 1
        groups = unmatched

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
                        "path": path, "line": entry_line.GetLine() if path else 0})
        for _, frames in groups:  # could not be matched to a C frame; show them anyway
            for pf in frames:
                out.append({"kind": "py", "tid": tid, "index": py_index, "name": pf.name,
                            "path": self._py_path(pf.filename), "line": pf.line,
                            "pf": pf})
                py_index += 1
            last_python = len(out)
        if last_python is not None:
            del out[last_python:]  # thread bootstrap frames below the oldest Python frame
        for record in out:
            record["id"] = self._new_id()
            self.frames[record["id"]] = record
        self.stacks[tid] = out
        return out

    def req_stackTrace(self, args):
        self._require_stopped()
        stack = self._merged_stack(self._thread(args["threadId"]))
        start = args.get("startFrame") or 0
        levels = args.get("levels") or len(stack)
        frames = []
        for record in stack[start:start + levels]:
            frame = {"id": record["id"], "name": record["name"], "line": record["line"],
                     "column": 0}
            if record["path"]:
                frame["source"] = {"name": os.path.basename(record["path"]),
                                   "path": record["path"]}
            else:
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
                                       index=frame["index"])
                    return {"variables": [self._py_var(i) for i in items]}
                except DapError:
                    if scope != "locals":
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
            item = self.agent("evaluate", tid=record["tid"], index=record["index"], expr=expr)
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
        stack = self._merged_stack(thread)
        top = stack[0] if stack else None
        if top is not None and top["kind"] == "py":
            if self.safe_tid is None:
                raise DapError("stepping Python code from a native stop is not supported yet")
            self.agent("step", mode=mode, tid=top["tid"], index=top["index"])
            self._new_stop()
            self._continue()
            return None
        self._new_stop()
        self.process.SetSelectedThread(thread)
        if mode == "over":
            thread.StepOver()
        elif mode == "in":
            thread.StepInto()
        else:
            thread.StepOut()
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
    sock = socket.socket(fileno=fd)
    log_path = os.environ.get("SEAM_LOG")
    log = open(log_path, "a") if log_path else None
    adapter = Adapter(debugger, sock, log)
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
