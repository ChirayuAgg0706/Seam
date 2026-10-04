"""Breakpoints of every kind: source lines, functions, data, exceptions."""
import struct

import lldb

from .common import (
    CYTHON_GLUE, DapError, EXCEPTION_FILTERS, PYTHON_EXCEPTION_FILTERS, PY_SUFFIXES,
    fill_log_message, hit_condition_met, parse_hit_condition,
)


class BreakpointsMixin:
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

    def _throw_breakpoint(self):
        """Breakpoint on the first instruction of `__cxa_throw`.

        Not LLDB's own C++ exception breakpoint: that one stops after the function's
        prologue, where the argument registers are no longer guaranteed to hold the
        arguments. The C++ runtime is usually loaded later, with the extension module,
        so the breakpoint has to be set by name; the command line is the only place
        where a name breakpoint can be told not to skip the prologue.
        """
        before = {self.target.GetBreakpointAtIndex(i).GetID()
                  for i in range(self.target.GetNumBreakpoints())}
        result = lldb.SBCommandReturnObject()
        self.dbg.GetCommandInterpreter().HandleCommand(
            "breakpoint set --name __cxa_throw --skip-prologue false", result)
        for i in range(self.target.GetNumBreakpoints()):
            bp = self.target.GetBreakpointAtIndex(i)
            if bp.GetID() not in before:
                return bp
        self.log("could not set the throw breakpoint:", result.GetError())
        return self.target.BreakpointCreateForException(
            lldb.eLanguageTypeC_plus_plus, False, True)

    def req_setBreakpoints(self, args):
        path = (args.get("source") or {}).get("path")
        if not path:
            raise DapError("setBreakpoints needs source.path")
        wanted = args.get("breakpoints") or []
        # A hit-count condition that cannot be understood makes that one breakpoint
        # unverified, with the reason; the others are set as usual.
        hits = []
        for b in wanted:
            try:
                hits.append(parse_hit_condition(b.get("hitCondition")))
            except DapError as exc:
                hits.append(exc)
        refused = [{"line": b["line"], "verified": False, "message": str(hit)}
                   if isinstance(hit, DapError) else None for b, hit in zip(wanted, hits)]
        if path.endswith(PY_SUFFIXES):
            items = [{"line": b["line"], "condition": b.get("condition"), "hit": hit,
                      "log": b.get("logMessage")}
                     for b, hit in zip(wanted, hits) if not isinstance(hit, DapError)]
            if items:
                self.py_bps[path] = items
            else:
                self.py_bps.pop(path, None)
            result = self._sync_py_bps()
            accepted = iter((result or {}).get(path)
                            or [{"line": i["line"], "verified": True} for i in items])
            return {"breakpoints": [r or next(accepted) for r in refused]}
        with self._paused():
            for bp in self.native_bps.pop(path, []):
                self.native_bp_specs.pop(bp.GetID(), None)
                self.target.BreakpointDelete(bp.GetID())
            created = []
            answers = []
            for b, hit, problem in zip(wanted, hits, refused):
                if problem:
                    answers.append(problem)
                    continue
                bp = self.target.BreakpointCreateByLocation(path, b["line"])
                if b.get("condition"):
                    bp.SetCondition(b["condition"])
                if hit or b.get("logMessage") is not None:
                    self._set_native_hit_condition(bp, hit, b.get("logMessage"))
                self._drop_glue_locations(bp)
                created.append(bp)
                self.native_bp_lines[bp.GetID()] = b["line"]
                answer = self._native_bp_answer(bp)
                self.native_bp_state[bp.GetID()] = (answer["verified"], answer["line"])
                answers.append(answer)
            self.native_bps[path] = created
        return {"breakpoints": answers}

    def _native_breakpoint_wants_a_stop(self, thread, bp):
        """Apply a native breakpoint's hit-count condition and log message.

        Returns False if the program should simply carry on (the hit does not count,
        or the breakpoint is a logpoint and its message has been printed).
        """
        spec = self.native_bp_specs.get(bp.GetID())
        if spec is None:
            return True
        spec["hits"] += 1
        self.log("native breakpoint", bp.GetID(), "hit", spec["hits"], "lldb count",
                 bp.GetHitCount(), "pc %#x" % thread.GetFrameAtIndex(0).GetPC(),
                 "stop-id", self.process.GetStopID())
        if spec["hit"]:
            operator, number = spec["hit"]
            last = {"==": number, "<=": number, "<": number - 1}.get(operator)
            if last is not None and spec["hits"] >= last:
                bp.SetEnabled(False)  # no later hit can count: stop paying for the stops
            if not hit_condition_met(spec["hit"], spec["hits"]):
                return False
        if spec["log"] is None:
            return True
        frame = thread.GetFrameAtIndex(0)

        def value_of(expression):
            value = frame.EvaluateExpression(expression, self._expr_options(5))
            if not value.GetError().Success():
                return "{%s}" % (value.GetError().GetCString() or "error").strip()
            return value.GetSummary() or value.GetValue() or ""

        self.event("output", {"category": "console",
                              "output": fill_log_message(spec["log"], value_of) + "\n"})
        return False

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
        """Break on entry to a function, by name, whichever side it lives on.

        The name is given to LLDB (a native function, now or when its module loads) and
        to the agent (a Python function: bare name, qualified name, or module.qualname).
        A condition is evaluated in the language of the function that is entered.
        """
        if self.process is None:
            raise DapError("nothing has been launched")
        answers = []
        python_side = []
        with self._paused():
            for bp in self.function_bps:
                self.native_bp_specs.pop(bp.GetID(), None)
                self.target.BreakpointDelete(bp.GetID())
            self.function_bps = []
            for b in args.get("breakpoints") or []:
                try:
                    hit = parse_hit_condition(b.get("hitCondition"))
                except DapError as exc:
                    answers.append({"verified": False, "message": str(exc)})
                    continue
                bp = self.target.BreakpointCreateByName(b["name"])
                if b.get("condition"):
                    bp.SetCondition(b["condition"])
                if hit:
                    self._set_native_hit_condition(bp, hit, None)
                self.function_bps.append(bp)
                python_side.append({"name": b["name"], "condition": b.get("condition"),
                                    "hit": hit})
                answers.append({"verified": True})
        self.py_function_bps = python_side
        self._sync_py_bps()
        return {"breakpoints": answers}

    def _set_native_hit_condition(self, bp, hit, log):
        """Remember a native breakpoint's hit-count condition and log message."""
        skipped = 0
        if hit and hit[0] in ("==", ">=", ">"):
            # LLDB skips the hits before the interesting one without stopping; Seam
            # only counts from there on.
            skipped = max(hit[1] if hit[0] == ">" else hit[1] - 1, 0)
            bp.SetIgnoreCount(skipped)
        self.native_bp_specs[bp.GetID()] = {"hit": hit, "log": log, "hits": skipped}

    def req_dataBreakpointInfo(self, args):
        """Can a variable be watched? Native variables of 1, 2, 4 or 8 bytes can."""
        self._require_stopped()
        name = args["name"]
        record = self.refs.get(args.get("variablesReference"))
        value = None
        if record is not None:
            if record[0] in ("py", "pyref"):
                return {"dataId": None, "description":
                        "Data breakpoints work on native variables; %s is a Python "
                        "variable." % name}
            value = self._native_variable(record, name)
        elif args.get("frameId") is not None:
            frame = self._frame_record(args["frameId"])
            if frame["kind"] == "native":
                value = self._native_frame(frame).EvaluateExpression(
                    name, self._expr_options(5))
                if not value.GetError().Success():
                    value = None
        address = value.GetLoadAddress() if value is not None else lldb.LLDB_INVALID_ADDRESS
        size = value.GetByteSize() if value is not None else 0
        if address == lldb.LLDB_INVALID_ADDRESS or size not in (1, 2, 4, 8):
            return {"dataId": None, "description":
                    "%s cannot be watched: it has no address in memory, or is not 1, 2, 4 "
                    "or 8 bytes long." % name}
        return {"dataId": "%x/%d/%s" % (address, size, name), "description": name,
                "accessTypes": ["write", "readWrite", "read"], "canPersist": False}

    def req_setDataBreakpoints(self, args):
        if self.process is None:
            raise DapError("nothing has been launched")
        answers = []
        with self._paused():
            for watch_id in self.watchpoints:
                self.target.DeleteWatchpoint(watch_id)
            self.watchpoints = {}
            for b in args.get("breakpoints") or []:
                try:
                    address, size, name = str(b.get("dataId")).split("/", 2)
                    address, size = int(address, 16), int(size)
                    hit = parse_hit_condition(b.get("hitCondition"))
                except (ValueError, DapError) as exc:
                    answers.append({"verified": False, "message": str(exc)})
                    continue
                access = b.get("accessType") or "write"
                error = lldb.SBError()
                watch = self.target.WatchAddress(address, size, access != "write",
                                                 access != "read", error)
                if not error.Success() or not watch.IsValid():
                    answers.append({"verified": False, "message":
                                    "cannot watch %s: %s" % (name, error.GetCString()
                                                             or "no hardware watchpoint free")})
                    continue
                if b.get("condition"):
                    watch.SetCondition(b["condition"])
                self.watchpoints[watch.GetID()] = {
                    "name": name, "address": address, "size": size, "hit": hit, "hits": 0}
                answers.append({"verified": True})
        return {"breakpoints": answers}

    def _watchpoint_stop(self, thread, body):
        """A watched variable was touched. Returns False if the hit does not count."""
        watch = self.watchpoints.get(thread.GetStopReasonDataAtIndex(0))
        if watch is None:
            body.update({"reason": "data breakpoint",
                         "description": thread.GetStopDescription(120)})
            return True
        watch["hits"] += 1
        if watch["hit"] and not hit_condition_met(watch["hit"], watch["hits"]):
            return False
        text = "%s was accessed" % watch["name"]
        try:
            raw = self._read(watch["address"], watch["size"])
            text = "%s is now %d" % (watch["name"], int.from_bytes(raw, "little", signed=True))
        except ValueError:
            pass
        body.update({"reason": "data breakpoint", "description": text, "text": text})
        return True

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
                        self.native_exc_bps[name] = self._throw_breakpoint()
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
            return self.agent("sync_breakpoints", files=self.py_bps, exceptions=exceptions,
                              functions=self.py_function_bps)
        with self._paused():
            self._agent_pending("sync_breakpoints", files=self.py_bps, exceptions=exceptions,
                                functions=self.py_function_bps)
            self.pending_sync = True
        return None
