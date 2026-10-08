"""Process events: deciding what a stop is, and reporting it or carrying on."""
import signal
import struct
import sys
import time

import lldb

from .common import (
    DapError, FAULT_SIGNALS, FRAMEWORK_FUNCTIONS, GLUE, LOG_FLAG, R_BREAKPOINT, R_EXCEPTION,
    R_RETURN_NATIVE,
    R_UNCAUGHT,
)

# What a thread that has just started a child process reports. Never a stop to show.
FORK_STOPS = tuple(getattr(lldb, name) for name in
                   ("eStopReasonFork", "eStopReasonVFork", "eStopReasonVForkDone")
                   if hasattr(lldb, name))


class StopsMixin:
    @staticmethod
    def _breakpoint_id(value):
        # Stop data is unsigned 64-bit, but LLDB's internal breakpoint IDs are
        # signed 32-bit. SWIG rejects the unconverted value at FindBreakpointByID.
        value &= 0xffffffff
        return value - (1 << 32) if value >= 1 << 31 else value

    def _visible_breakpoint_data(self, thread):
        private = getattr(self, "fork_observer_internal_ids", ())
        for index in range(0, thread.GetStopReasonDataCount(), 2):
            bp_id = thread.GetStopReasonDataAtIndex(index)
            if bp_id not in private:
                return self._breakpoint_id(bp_id), thread.GetStopReasonDataAtIndex(index + 1)
        return (self._breakpoint_id(thread.GetStopReasonDataAtIndex(0)),
                thread.GetStopReasonDataAtIndex(1))

    def _entry_argument(self, frame, index):
        """Integer/pointer argument at a function's entry, before its prologue runs."""
        triple = self.target.GetTriple() or ""
        name = ("x%d" % index if triple.startswith(("arm64", "aarch64"))
                else ("rdi", "rsi", "rdx", "rcx", "r8", "r9")[index])
        register = frame.FindRegister(name)
        error = lldb.SBError()
        value = register.GetValueAsUnsigned(error)
        if not register.IsValid() or not error.Success():
            raise DapError("cannot read argument register %s: %s" % (name, error.GetCString()))
        return value

    def _on_event(self, ev):
        if lldb.SBBreakpoint.EventIsBreakpointEvent(ev):
            self._refresh_native_bp_status()
            return
        if lldb.SBTarget.EventIsTargetEvent(ev):
            self._on_modules_loaded(ev)
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
        if state == lldb.eStateRunning and getattr(self, "internal_signal_policy", None):
            number, stop, notify, suppress = self.internal_signal_policy
            signals = self.process.GetUnixSignals()
            signals.SetShouldStop(number, stop)
            signals.SetShouldNotify(number, notify)
            signals.SetShouldSuppress(number, suppress)
            self.internal_signal_policy = None
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
            passing = getattr(self, "macos_fault_pass", None)
            if passing is not None:
                number, tid = passing
                thread = self._thread(tid)
                signals = self.process.GetUnixSignals()
                signals.SetShouldStop(number, True)
                signals.SetShouldNotify(number, True)
                self.macos_fault_pass = None
                if (thread.GetStopReason() in (lldb.eStopReasonTrace, lldb.eStopReasonPlanComplete)
                        and not self.pause_requested):
                    self._discard_plans(thread)
                    self._continue()
                    return
            self._on_stop()

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
        if self.process is not None:
            self.last_resume_stop_id = self.process.GetStopID()
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

    @staticmethod
    def _pc(thread):
        """Where the thread really is: its PC register, which stays current when its
        frame list does not. Right after a stop the register can be unreadable for a
        moment (it reads as zero); then the frame's own PC is the best there is."""
        frame = thread.GetFrameAtIndex(0)
        register = "pc" if frame.GetThread().GetProcess().GetTarget().GetTriple().startswith(
            ("arm64", "aarch64")) else "rip"
        rip = frame.FindRegister(register)
        if rip.IsValid():
            error = lldb.SBError()
            value = rip.GetValueAsUnsigned(error)
            if error.Success() and value:
                return value
        return frame.GetPC()

    def _is_leftover(self, thread):
        """True if this thread's stop reason describes a stop that is already over.

        To resume a thread that sits on a breakpoint, LLDB steps it one instruction past
        the breakpoint first. On a busy machine (seen with LLDB 18, 19 and 20; never on
        an idle one) that internal step sometimes surfaces as a public stop, with the
        thread still described as having hit the breakpoint it has just left: same stop
        reason, the old frame list, even the breakpoint's hit count bumped. The program
        has not come round again; the registers say where it really is. A thread that
        has genuinely hit a breakpoint has its PC on one of the breakpoint's locations.
        """
        reason = thread.GetStopReason()
        if reason == lldb.eStopReasonTrace:
            return self.native_stepping is None  # a single step nobody asked Seam for
        if reason != lldb.eStopReasonBreakpoint:
            return False
        pc = self._pc(thread)
        for i in range(0, thread.GetStopReasonDataCount(), 2):
            bp = self.target.FindBreakpointByID(
                self._breakpoint_id(thread.GetStopReasonDataAtIndex(i)))
            if bp.IsValid() and bp.FindLocationByAddress(pc).IsValid():
                return False
        self.log("thread", thread.GetThreadID(), "reports a breakpoint it is no longer at",
                 "(pc %#x):" % pc, thread.GetStopDescription(80))
        return True

    def _interesting(self, thread):
        reason = thread.GetStopReason()
        return (reason not in (lldb.eStopReasonNone, lldb.eStopReasonInvalid)
                and reason not in FORK_STOPS
                and not self._is_leftover(thread) and not self.traps.handled(thread))

    def _trap_thread(self):
        if self.bp_trap is None:
            return None
        for thread in self.process:
            if (thread.GetStopReason() == lldb.eStopReasonBreakpoint
                    and thread.GetStopReasonDataAtIndex(0) == self.bp_trap.GetID()
                    and not self._is_leftover(thread)):
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
        def picture(thread):
            frame = thread.GetFrameAtIndex(0)
            return "reason %s (%s) frame pc %#x rip %#x" % (
                thread.GetStopReason(), thread.GetStopDescription(60), frame.GetPC(),
                self._pc(thread))

        for thread in self.process:
            # Only a PC register that can be read, and differs, shows a stale list. Right
            # after a stop the register is sometimes unreadable (it reads as zero) while
            # the frames are fine; calling into the process then is pointless, and is the
            # likely cause of a breakpoint hit that lost its stop reason (decisions §17).
            if self._pc(thread) != thread.GetFrameAtIndex(0).GetPC():
                self.log("stale frame list on thread", thread.GetThreadID(), "- refreshing;",
                         picture(thread))
                try:
                    self._call(thread, "((int(*)(void))%d)()" % getpid, timeout_s=5)
                except DapError as exc:
                    self.log("refresh failed:", exc)
                self.log("  after the call:", picture(thread))
                return

    def _on_trap(self, thread):
        frame = thread.GetFrameAtIndex(0)
        reason = self._entry_argument(frame, 2)
        tid = thread.GetThreadID()
        self.safe_tid = tid
        self.stop_is_trap = True
        self.process.SetSelectedThread(thread)
        if self.pending_sync:
            self._sync_py_bps()
        if reason & LOG_FLAG:
            for message in self.agent("logs"):
                self.event("output", {"category": "console", "output": message + "\n"})
            reason &= ~LOG_FLAG
            if not reason:
                # Only a logpoint: carry on, leaving whatever a step has armed alone.
                self._new_stop()
                self._continue()
                return
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
                if info["mode"] == "userUnhandled":
                    # The frame on top is on its way out: nothing to step through.
                    self.throw_stop = True
        self.event("stopped", body)

    def _on_stop(self):
        passed = getattr(self, "macos_passed_fault", None)
        if passed is not None:
            signals = self.process.GetUnixSignals()
            signals.SetShouldStop(passed, True)
            signals.SetShouldNotify(passed, True)
            self.macos_passed_fault = None
        self._new_stop()
        # Entry traps come out of the process before anything looks at it; a thread that
        # ran into one is put back on the instruction (see entrytraps.py).
        trapped = self.traps.stopped()
        landed, self.traps.landed = self.traps.landed, None
        # Then make LLDB's picture of the threads current: the checks below read it.
        self._fix_stale_frames()
        for candidate in self.process:
            if self._fork_observer_stop(candidate):
                self._continue()
                return
        if (not self.pause_requested and landed is None
                and not any(self._interesting(t) for t in self.process)):
            if any(t.GetStopReason() in FORK_STOPS for t in self.process):
                # The program started a child process. LLDB deals with that and carries
                # on by itself; should such a stop ever be left standing, it is not one
                # to show (it used to be reported as a pause).
                self.log("stop for a child process; resuming")
            else:
                # No thread has a current reason to be stopped: the stop is a leftover
                # of stepping off a breakpoint (see _is_leftover), or other threads ran
                # into entry traps meant for the stepping one. Nothing is reported.
                if not trapped:
                    self.leftover_stops += 1
                self.log("stop without a current reason; resuming")
            self._continue()
            return
        self._drain_output()
        self._refresh_native_bp_status()
        thread = self._trap_thread()
        if thread is not None:
            self._on_trap(thread)
            return
        if landed is not None:
            # Step-in from Python reached the first user native function of a large module.
            thread = self._thread(landed)
            self.process.SetSelectedThread(thread)
            self._finish_steps(thread)
            self._report_native_stop(thread, {"threadId": landed, "allThreadsStopped": True,
                                              "reason": "step"})
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
        bp_id, location_id = self._visible_breakpoint_data(thread)
        self.log("stop: thread", thread.GetThreadID(), "reason", reason,
                 thread.GetStopDescription(80), "pc %#x" % self._pc(thread))
        internal_step_stop = (reason == lldb.eStopReasonBreakpoint and bp_id < 0
                              and self.native_stepping is not None)
        if internal_step_stop:
            reason = lldb.eStopReasonPlanComplete

        if (reason == lldb.eStopReasonBreakpoint and bp_id
                in [bp.GetID() for bp in self.user_bps.values()] + self.traps.breakpoint_ids()):
            # Step-in from Python reached the first user native function.
            self._finish_steps(thread)
            body["reason"] = "step"
            self._report_native_stop(thread, body)
            return
        if reason == lldb.eStopReasonBreakpoint and not self.pause_requested:
            hit = bp_id
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
            bp = self.target.FindBreakpointByID(bp_id)
            if bp.IsValid() and any(bp.GetID() == b.GetID()
                                    for group in self.native_bps.values() for b in group):
                # Read the hit location first: LLDB derives it from the live site owners,
                # so it reads as 0 once the location has been disabled.
                location = bp.FindLocationByID(location_id)
                self._drop_glue_locations(bp)
                if ((location.IsValid() and not location.IsEnabled())
                        or self._is_same_line_rehit(thread)):
                    self.log("skipping redundant breakpoint hit in",
                             thread.GetFrameAtIndex(0).GetFunctionName())
                    self._new_stop()
                    self._continue()
                    return
                if not self._native_breakpoint_wants_a_stop(thread, bp):
                    # A hit that does not count yet, or a logpoint. Remember the place
                    # all the same, so the line's other address ranges are not counted
                    # as further hits.
                    top = thread.GetFrameAtIndex(0)
                    self.last_native_stop[thread.GetThreadID()] = (
                        self._line_key(top), top.GetPC())
                    self._new_stop()
                    self._continue()
                    return
            elif (bp.IsValid() and any(bp.GetID() == b.GetID() for b in self.function_bps)
                    and not self._native_breakpoint_wants_a_stop(thread, bp)):
                self._new_stop()  # a function breakpoint whose hit count says "not yet"
                self._continue()
                return
        watched = {}
        if reason == lldb.eStopReasonWatchpoint and not self.pause_requested:
            if not self._watchpoint_stop(thread, watched):
                self._new_stop()
                self._continue()
                return
        returned = False
        if (self.stepout is not None and reason == lldb.eStopReasonBreakpoint
                and bp_id == self.stepout["bp"].GetID()):
            if thread.GetFrameAtIndex(0).GetSP() < self.stepout["sp"]:
                # The same return address, deeper in the stack (recursion): not ours yet.
                self._new_stop()
                self._continue()
                return
            self._clear_stepout()
            returned = True
        if ((returned or reason == lldb.eStopReasonPlanComplete) and self.native_stepping
                and not self.native_stepping.get("instruction")):  # that ends where it ends
            reason = lldb.eStopReasonPlanComplete
            kind = self._landing_class(thread)
            self.log("native step ended in", thread.GetFrameAtIndex(0).GetFunctionName(),
                     "class", kind, "hops", self.native_stepping["hops"])
            if kind == "interp" and self._resume_in_python(thread):
                return
            if kind == "nodebug" and self.native_stepping["hops"] < 64:
                frame = thread.GetFrameAtIndex(0)
                function = frame.GetFunction()
                source = frame.GetCompileUnit().GetFileSpec().fullpath or ""
                # Clang can leave a few instructions without a line after an
                # inlined call. A user function with DWARF is still the function
                # being stepped; get through the gap instead of stepping out of it.
                if (function.IsValid() and source and not self._is_glue_path(source)
                        and frame.GetFunctionName() == function.GetName()
                        and not FRAMEWORK_FUNCTIONS.search(frame.GetFunctionName() or "")):
                    self.native_stepping["hops"] += 1
                    self._new_stop()
                    error = lldb.SBError()
                    thread.StepInstruction(True, error)
                    if not error.Success():
                        raise DapError("could not step through an optimized line gap: %s"
                                       % error.GetCString())
                    self.running = True
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
            again = self.native_stepping.get("again")
            hops = self.native_stepping["hops"]
            if (kind == "user" and again and not self.pause_requested and hops < 64
                    and (hops or internal_step_stop
                         or self._classify_frame(thread.GetFrameAtIndex(0)) in GLUE)
                    and self._visible_position(thread) == self.native_stepping["from"]):
                # The step went into a piece of glue inlined into the user's function, or
                # through one and back, and the user's own line has not changed. Optimised
                # code is full of these (seven on the line of pydantic-core that calls a
                # Python validator); to the user none of them is a step. Take the same
                # step again.
                self.native_stepping["hops"] += 1
                self._new_stop()
                again(thread)
                self.running = True
                return
        self._finish_steps(thread)
        if self.pause_requested:
            self.pause_requested = False
            body["reason"] = "pause"
        elif reason == lldb.eStopReasonBreakpoint:
            body["reason"] = "breakpoint"
        elif reason == lldb.eStopReasonPlanComplete:
            body["reason"] = "step"
        elif reason == lldb.eStopReasonWatchpoint:
            body.update(watched)
        elif reason in (lldb.eStopReasonSignal, lldb.eStopReasonException):
            body["reason"] = "exception"
            body["description"] = thread.GetStopDescription(200)
            body["text"] = body["description"]
            name = "exception"
            if reason == lldb.eStopReasonSignal:
                name = self._signal_name(thread.GetStopReasonDataAtIndex(0))
            else:
                for mach, unix in (("EXC_BAD_ACCESS", "SIGSEGV"),
                                   ("EXC_BAD_INSTRUCTION", "SIGILL"),
                                   ("EXC_ARITHMETIC", "SIGFPE")):
                    if body["description"].startswith(mach):
                        name = unix
                        body["description"] = unix + ": " + body["description"]
                        body["text"] = body["description"]
                        break
            self.fault_stop = name in FAULT_SIGNALS or reason == lldb.eStopReasonException
            self.exception_info[thread.GetThreadID()] = {
                "exceptionId": name, "description": body["description"],
                "breakMode": "always"}
        else:
            body["reason"] = "pause"
        self._report_native_stop(thread, body)

    def _thrown_type(self, thread):
        """Name of the C++ type being thrown, at a stop on entry to `__cxa_throw`.

        Its second argument is the `std::type_info`. The type's name is that object's
        symbol ("typeinfo for T"); failing that, the mangled name it points to.
        """
        frame = thread.GetFrameAtIndex(0)
        tinfo = self._entry_argument(frame, 1)
        symbol = self.target.ResolveLoadAddress(tinfo).GetSymbol().GetName() or ""
        mangled = ""
        if not symbol.startswith("typeinfo for "):
            try:
                pointer = struct.unpack("<Q", self._read(tinfo + 8, 8))[0]
                mangled = self.process.ReadCStringFromMemory(pointer, 256, lldb.SBError()) or ""
                for ctx in self.target.FindSymbols("_ZTI" + mangled):
                    symbol = ctx.GetSymbol().GetName() or ""
                    if symbol.startswith("typeinfo for "):
                        break
            except (ValueError, struct.error):
                pass
        self.log("throw: pc %#x in %s, type_info %#x, symbol %r, mangled %r"
                 % (frame.GetPC(), frame.GetFunctionName(), tinfo, symbol, mangled))
        if symbol.startswith("typeinfo for "):
            return symbol[len("typeinfo for "):]
        return mangled

    def _report_native_exception(self, thread, kind, body):
        """A stop at a C++ `throw` or a Rust panic (exception breakpoint filters)."""
        if kind == "cpp_throw":
            name = self._thrown_type(thread)
            message = self._thrown_message(thread)
            what = "C++ exception thrown" + (": " + message if message else "")
            name = name or "C++ exception"
        else:
            name, what = "Rust panic", "Rust panic"
        body.update({"reason": "exception", "description": what, "text": name})
        self.throw_stop = True
        self.exception_info[thread.GetThreadID()] = {
            "exceptionId": name, "description": what, "breakMode": "always"}
        self._report_native_stop(thread, body)

    def _thrown_message(self, thread):
        """Read what() only for a known single-inheritance std::exception object.

        The object and RTTI registers are intact at __cxa_throw's first instruction.
        Itanium RTTI describes each single base at +16; other RTTI kinds are not walked.
        This evaluates a native method, never Python (decisions §32).
        """
        frame = thread.GetFrameAtIndex(0)
        pointer = self._entry_argument(frame, 0)
        info = self._entry_argument(frame, 1)
        seen = set()
        try:
            while info and info not in seen and len(seen) < 32:
                seen.add(info)
                vtable, name_pointer = struct.unpack("<QQ", self._read(info, 16))
                name = self.process.ReadCStringFromMemory(name_pointer, 256, lldb.SBError())
                if (name or "").lstrip("*") == "St9exception":
                    value, problem = self._evaluate(
                        frame, "((const std::exception*)%d)->what()" % pointer, 2)
                    if problem is not None and any(text in problem for text in (
                            "no type named", "unknown type name", "undeclared identifier 'std'",
                            "incomplete type", "no member named 'what'")):
                        # A stripped libstdc++ often has no std::exception declaration
                        # available to Clang. Its Itanium vtable has two destructor
                        # entries followed by what(); single inheritance needs no
                        # adjustment of `this`.
                        methods = struct.unpack("<Q", self._read(pointer, 8))[0]
                        what = struct.unpack("<Q", self._read(methods + 16, 8))[0]
                        value, problem = self._evaluate(
                            frame, "((const char*(*)(const void*))%d)((const void*)%d)"
                            % (what, pointer), 2)
                    if problem is None:
                        address = value.GetValueAsUnsigned()
                        error = lldb.SBError()
                        message = self.process.ReadCStringFromMemory(address, 4096, error)
                        if error.Success():
                            return message
                    self.log("could not read C++ exception message:", problem)
                    return None
                # Read the RTTI class itself. LLDB 18 does not reliably resolve a
                # symbol at the vtable's address point (16 bytes past its start).
                kind_info = struct.unpack("<Q", self._read(vtable - 8, 8))[0]
                kind_name = struct.unpack("<Q", self._read(kind_info + 8, 8))[0]
                kind = self.process.ReadCStringFromMemory(kind_name, 256, lldb.SBError()) or ""
                if kind.lstrip("*") != "N10__cxxabiv120__si_class_type_infoE":
                    break
                info = struct.unpack("<Q", self._read(info + 16, 8))[0]
        except (ValueError, struct.error):
            pass
        return None

    def _report_native_stop(self, thread, body):
        frame = thread.GetFrameAtIndex(0)
        self.last_native_stop[thread.GetThreadID()] = (self._line_key(frame), frame.GetPC())
        self.event("stopped", body)

    def _continue(self):
        if self.user_bps_on:
            self.traps.arm()  # they are out at every stop; in again while the step lasts
        self._sync_fork_cleanup()
        self.last_resume_stop_id = self.process.GetStopID()
        err = self.process.Continue()
        if not err.Success():
            self.traps.disarm()
            raise DapError("could not resume: %s" % err.GetCString())
        self.running = True

    def _interrupt(self):
        """Stop a running process for internal work. Returns False if it stopped by itself."""
        # Not SBProcess.Stop(): on LLDB 18 the stop it produces leaves the thread's frame
        # list cached, so the *next* stop shows the frames of this one.
        if sys.platform == "darwin":
            self.log("internal pause: state", self.process.GetState(), "stop-id",
                     self.process.GetStopID(), "resumed from", self.last_resume_stop_id)
            # Older Apple LLDB can immediately resume SendAsyncInterrupt's SIGINT
            # when the program's signal policy passes SIGINT. Halt forces a stop.
            # Continue is asynchronous and LLDB updates the public state when its
            # event is removed from our listener. Drain the queued resume event:
            # polling GetState alone cannot make the stale stopped state advance.
            deadline = time.monotonic() + 1
            resume_event = lldb.SBEvent()
            while (self.process.GetState() == lldb.eStateStopped
                   and self.process.GetStopID() == getattr(self, "last_resume_stop_id", None)
                   and time.monotonic() < deadline):
                if self.listener.WaitForEvent(1, resume_event):
                    if (lldb.SBProcess.EventIsProcessEvent(resume_event)
                            and resume_event.GetType() & (
                                lldb.SBProcess.eBroadcastBitSTDOUT
                                | lldb.SBProcess.eBroadcastBitSTDERR)):
                        self._drain_output()
            if (self.process.GetState() == lldb.eStateStopped
                    and self.process.GetStopID() != getattr(self, "last_resume_stop_id", None)):
                self.running = False
                self.traps.stopped()
                return False
            # Apple debugserver uses SIGINT for its interrupt. Force this one stop
            # and swallow the debugger-generated signal; restore the user's policy
            # after the subsequent resume is observed.
            number = int(signal.SIGINT)
            signals = self.process.GetUnixSignals()
            self.internal_signal_policy = (
                number, signals.GetShouldStop(number), signals.GetShouldNotify(number),
                signals.GetShouldSuppress(number))
            signals.SetShouldStop(number, True)
            signals.SetShouldNotify(number, True)
            signals.SetShouldSuppress(number, True)
            error = self.process.Stop()
            if not error.Success():
                _, stop, notify, suppress = self.internal_signal_policy
                signals.SetShouldStop(number, stop)
                signals.SetShouldNotify(number, notify)
                signals.SetShouldSuppress(number, suppress)
                self.internal_signal_policy = None
                raise DapError("could not interrupt the process: %s" % error.GetCString())
        else:
            self.process.SendAsyncInterrupt()
        state = self._wait_stop(10)
        if state != lldb.eStateStopped:
            self._on_exit()
            raise DapError("the process exited")
        self.running = False
        self.traps.stopped()
        if self.traps.landed is not None:
            return False  # a step-in reached its function at this very moment
        for thread in self.process:
            reason = thread.GetStopReason()
            if reason in (lldb.eStopReasonBreakpoint, lldb.eStopReasonException,
                          lldb.eStopReasonPlanComplete) and not self.traps.handled(thread):
                return False
        return True

    def _paused(self):
        return _Paused(self)


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
