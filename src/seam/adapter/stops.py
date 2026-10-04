"""Process events: deciding what a stop is, and reporting it or carrying on."""
import struct
import time

import lldb

from .common import (
    DapError, FAULT_SIGNALS, GLUE, LOG_FLAG, R_BREAKPOINT, R_EXCEPTION, R_RETURN_NATIVE,
    R_UNCAUGHT,
)


class StopsMixin:
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

    @staticmethod
    def _pc(thread):
        """Where the thread really is: its PC register, which stays current when its
        frame list does not. Right after a stop the register can be unreadable for a
        moment (it reads as zero); then the frame's own PC is the best there is."""
        frame = thread.GetFrameAtIndex(0)
        rip = frame.FindRegister("rip")
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
            bp = self.target.FindBreakpointByID(thread.GetStopReasonDataAtIndex(i))
            if bp.IsValid() and bp.FindLocationByAddress(pc).IsValid():
                return False
        self.log("thread", thread.GetThreadID(), "reports a breakpoint it is no longer at",
                 "(pc %#x):" % pc, thread.GetStopDescription(80))
        return True

    def _interesting(self, thread):
        reason = thread.GetStopReason()
        return (reason not in (lldb.eStopReasonNone, lldb.eStopReasonInvalid)
                and not self._is_leftover(thread))

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
                frame.FindRegister("rip").GetValueAsUnsigned())

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
        reason = frame.FindRegister("rdx").GetValueAsUnsigned()
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
        self.event("stopped", body)

    def _on_stop(self):
        self._new_stop()
        # First make LLDB's picture of the threads current: the checks below read it.
        self._fix_stale_frames()
        if not self.pause_requested and not any(self._interesting(t) for t in self.process):
            # No thread has a current reason to be stopped: the stop is a leftover of
            # stepping off a breakpoint (see _is_leftover). Nothing is reported.
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
        self.log("stop: thread", thread.GetThreadID(), "reason", reason,
                 thread.GetStopDescription(80), "pc %#x" % self._pc(thread))

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
        elif reason == lldb.eStopReasonWatchpoint:
            body.update(watched)
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

    def _thrown_type(self, thread):
        """Name of the C++ type being thrown, at a stop on entry to `__cxa_throw`.

        Its second argument is the `std::type_info`. The type's name is that object's
        symbol ("typeinfo for T"); failing that, the mangled name it points to.
        """
        frame = thread.GetFrameAtIndex(0)
        tinfo = frame.FindRegister("rsi").GetValueAsUnsigned()
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
