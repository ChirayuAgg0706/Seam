"""The merged call stack, variables and expressions."""
import os

import lldb

from .common import DapError, GLUE, UNSAFE_MESSAGE


class StackMixin:
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

    def _native_variable(self, record, name):
        """The SBValue a variables record and a child name refer to, or None."""
        kind = record[0]
        if kind in ("native", "statics"):
            frame = self._native_frame(record[1])
            values = (frame.GetVariables(True, True, False, True) if kind == "native"
                      else frame.GetVariables(False, False, True, True))
            for value in values:
                if value.GetName() == name:
                    return value
            return None
        if kind == "sb":
            value = record[1].GetChildMemberWithName(name)
            if not value.IsValid() and name.startswith("["):
                value = record[1].GetChildAtIndex(int(name.strip("[]")))
            return value if value.IsValid() else None
        return None

    def req_threads(self, args):
        if self.process is None:
            return {"threads": []}
        threads = []
        for thread in self.process:
            name = thread.GetName() or "Thread"
            threads.append({"id": thread.GetThreadID(),
                            "name": "%s (%d)" % (name, thread.GetThreadID())})
        return {"threads": threads}

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

    def _native_frame(self, record):
        return self._thread(record["tid"]).GetFrameAtIndex(record["index"])

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
            # The statics and globals of the frame's source file.
            {"name": "Globals", "expensive": True,
             "variablesReference": self._new_ref(("statics", record))},
        ]}

    def _py_var(self, item, frame):
        """DAP form of a variable from the agent. `frame` is the Python frame record it
        was reached from: expressions that change its children are evaluated there."""
        ref = self._new_ref(("pyref", item["ref"], frame)) if item.get("ref") else 0
        var = {"name": item["name"], "value": item["value"], "type": item["type"],
               "variablesReference": ref}
        if item.get("expr"):
            var["evaluateName"] = item["expr"]
        if item.get("indexed"):
            var["indexedVariables"] = item["indexed"]
        return var

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
                    return {"variables": [self._py_var(i, frame) for i in items]}
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
            # Long sequences are fetched in the pages the client asks for.
            items = self.agent("variables", kind="ref", ref=record[1],
                               start=args.get("start"), count=args.get("count") or None)
            return {"variables": [self._py_var(i, record[2]) for i in items]}
        if kind in ("native", "statics"):
            frame = self._native_frame(record[1])
            values = (frame.GetVariables(True, True, False, True) if kind == "native"
                      else frame.GetVariables(False, False, True, True))
            return {"variables": [self._sb_var(v) for v in values]}
        value = record[1]
        count = min(value.GetNumChildren(), 500)
        return {"variables": [self._sb_var(value.GetChildAtIndex(i)) for i in range(count)]}

    def req_setVariable(self, args):
        """Change a variable: a Python local, global or member, or a native variable."""
        self._require_stopped()
        record = self.refs.get(args["variablesReference"])
        if record is None:
            raise DapError("unknown or stale variablesReference")
        name, text = args["name"], str(args["value"])
        kind = record[0]
        if kind in ("py", "pyref"):
            if self.safe_tid is None:
                raise DapError(UNSAFE_MESSAGE)
            frame = record[2]
            request = {"kind": record[1], "name": name} if kind == "py" else {
                "kind": "ref", "ref": record[1], "name": name}
            item = self.agent("set_variable", tid=frame["tid"], index=frame["index"],
                              pm=frame.get("pm"), value=text, **request)
            var = self._py_var(item, frame)
        else:
            value = self._native_variable(record, name)
            if value is None:
                raise DapError("no variable called %s here" % name)
            error = lldb.SBError()
            if not value.SetValueFromCString(text, error):
                raise DapError("cannot set %s: %s" % (name, error.GetCString()
                                                       or "the value was not accepted"))
            var = self._sb_var(value)
        return {"value": var["value"], "type": var["type"],
                "variablesReference": var["variablesReference"]}

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
            var = self._py_var(item, record)
            return {"result": var["value"], "type": var["type"],
                    "variablesReference": var["variablesReference"]}
        value = self._native_frame(record).EvaluateExpression(expr, self._expr_options(10))
        if not value.GetError().Success():
            raise DapError(value.GetError().GetCString() or "evaluation failed")
        var = self._sb_var(value)
        return {"result": var["value"], "type": var["type"],
                "variablesReference": var["variablesReference"]}
