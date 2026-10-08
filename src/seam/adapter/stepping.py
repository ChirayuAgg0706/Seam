"""Stepping across the Python/native boundary, and what counts as user code."""
import struct

import lldb

from .common import DapError, FRAMEWORK_FUNCTIONS, GLUE, MAX_STEP_IN_LOCATIONS, SYSTEM_LIB_PREFIXES


class SteppingMixin:
    def _classify_address(self, address):
        """'user', 'framework' (binding glue) or 'nodebug' for a code address."""
        entry = address.GetLineEntry()
        spec = entry.GetFileSpec()
        if not entry.IsValid() or not spec.IsValid() or not entry.GetLine():
            return "nodebug"
        if self._is_glue_path(spec.fullpath or ""):
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
        if self._is_glue_path(spec.fullpath or ""):
            return "framework"
        if FRAMEWORK_FUNCTIONS.search(frame.GetFunctionName() or ""):
            return "framework"
        return "user"

    def _landing_class(self, thread):
        """Class of the place a thread is stopped at, looking through inlined glue.

        If the newest frame is glue that was inlined into a user function, the thread is
        physically in user code and that is where a step should end.
        """
        first = thread.GetFrameAtIndex(0)
        kind = self._classify_frame(first)
        if kind in GLUE:
            for i in range(1, thread.GetNumFrames()):
                frame = thread.GetFrameAtIndex(i)
                if not self._same_function_body(frame, first):
                    break
                if self._classify_frame(frame) == "user":
                    return "user"
        return kind

    def _visible_position(self, thread):
        """Where the user sees a thread in native code: the newest frame, or the user
        function it is inlined into, as (function, file, line, stack pointer)."""
        first = thread.GetFrameAtIndex(0)
        frame = first
        if self._classify_frame(first) in GLUE:
            for i in range(1, thread.GetNumFrames()):
                candidate = thread.GetFrameAtIndex(i)
                if not self._same_function_body(candidate, first):
                    break
                if self._classify_frame(candidate) == "user":
                    frame = candidate
                    break
        entry = frame.GetLineEntry()
        return (frame.GetFunctionName(), entry.GetFileSpec().fullpath, entry.GetLine(),
                frame.GetSP())

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
            if self.traps.covers(path, module):
                continue  # a large module: entry traps do the same job (entrytraps.py)
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
        self.traps.begin(tid)
        self.user_bps_on = True

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
        target_index = min(target_index, len(natives) - 1)
        target = natives[target_index]
        # Unless the call was made by code inlined into the target: then the return
        # address belongs to the innermost frame of that function's body, and the target's
        # own PC is merely where the inlined code starts.
        inner = target_index
        while inner > 1 and self._same_function_body(natives[inner - 1], target):
            inner -= 1
        address = natives[inner].GetPC()
        self._clear_stepout()
        self._discard_plans(thread)
        bp = self.target.BreakpointCreateByAddress(address)
        bp.SetThreadID(thread.GetThreadID())
        self.stepout = {"bp": bp, "sp": target.GetSP()}
        self.log("running until return to", target.GetFunctionName(), hex(address))
        self._sync_macos_fork_table()
        err = self.process.Continue()
        if not err.Success():
            self._clear_stepout()
            raise DapError("could not resume: %s" % err.GetCString())

    def _clear_stepout(self):
        if self.stepout is not None:
            self.target.BreakpointDelete(self.stepout["bp"].GetID())
            self.stepout = None

    @staticmethod
    def _same_function_body(frame, other):
        """True if two frames of a thread are one function's code, one inlined into the other.

        They share the stack pointer; a real caller's is always higher. Their PCs differ
        in general: LLDB gives the function a frame was inlined into the address where the
        inlined code starts, and the two only coincide on its first instruction.
        """
        return frame.GetSP() == other.GetSP()

    def _step_out_of_glue(self, thread, above=0):
        """Step out to the nearest frame that is user code or the interpreter.

        `above`: the frame being left, when it is not the newest one (it has glue inlined
        into it on top).
        """
        natives = self._native_frames(thread)
        # A frame with the newest frame's stack pointer is the function that one was
        # inlined into. The thread is in it right now and its PC is no return address:
        # running "until return to it" runs until the program comes by again, or to its
        # end (seen in pydantic-core: stepping out of a function with `map_err` inlined at
        # the current line ran the program to completion).
        target_index = above + 1
        while (target_index < len(natives)
               and (self._classify_frame(natives[target_index]) in GLUE
                    or self._same_function_body(natives[target_index], natives[0]))):
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
        if not self.py_step_armed:
            self.safe_tid = tid
            try:
                self.agent("step", mode="caller", tid=tid, just_my_code=self.just_my_code)
            finally:
                self.safe_tid = None
        # else a step in from native code has a Python step armed already: it ends on the
        # next Python line this thread runs, in a callback or back in the caller. Arming
        # "stop in the caller" over it would run straight through the callback.
        self._finish_steps(thread, cancel_py=False)
        self.py_step_armed = True
        self._sync_macos_fork_table()
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
            self.traps.finish()
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
            self.agent("step", mode="caller", tid=tid, just_my_code=self.just_my_code)
        except DapError as exc:
            self.log("cannot hand the step to Python:", exc)
            self.safe_tid = None
            return False
        self._finish_steps(thread, cancel_py=False)
        self.py_step_armed = True
        self._new_stop()
        self._continue()
        return True

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
        if (args.get("granularity") == "instruction" and mode != "out"
                and not (self.stop_is_trap and tid == self.safe_tid)):
            # The disassembly view steps by machine instruction. At a Python stop there
            # is no machine code of the user's to step through; the step is by line.
            self._step_instruction(thread, mode == "over")
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
            # justMyCode travels with the step: the agent decides where a step may end.
            request = {"mode": mode, "tid": tid, "index": top["index"],
                       "native_return": native_return, "just_my_code": self.just_my_code}
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
        host = 0
        if 0 < start < len(natives) and self._same_function_body(natives[start], natives[0]):
            # The frames above the user's are glue inlined into it: the thread is in the
            # user's function itself, not in a call it made. Step from where it is.
            host, start = start, 0
        if mode == "in" and self._control_safe(thread):
            # If the stepped statement calls back into Python, stop on its first line.
            self.safe_tid = tid
            try:
                self.agent("step", mode="any", tid=tid, just_my_code=self.just_my_code)
                self.py_step_armed = True
            except DapError as exc:
                self.log("cannot arm a Python step from native code:", exc)
        self.native_stepping = {"tid": tid, "hops": 0}
        if start == 0 and mode != "out":
            # What the step is repeated with if it only gets through inlined glue, and
            # where the user is now (see the end of _on_stop).
            self.native_stepping["again"] = (lldb.SBThread.StepOver if mode == "over"
                                             else lldb.SBThread.StepInto)
            self.native_stepping["from"] = self._visible_position(thread)
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
            self._step_out_of_glue(thread, above=host)
        self.running = True
        return None

    def req_next(self, args):
        return self._step(args, "over")

    def req_stepIn(self, args):
        return self._step(args, "in")

    def req_stepOut(self, args):
        return self._step(args, "out")
