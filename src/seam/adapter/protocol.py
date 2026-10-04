"""The DAP connection, request dispatch, and access to the target's memory and agent."""
import json
import os
import struct
import threading
import time
import traceback

import lldb

from .common import DapError, EXCEPTION_FILTERS, UNSAFE_MESSAGE, _Arguments

# SEAM_LOG_TIMES=1 starts every log line with the seconds since the adapter was loaded, to
# see where a slow request spends its time (the scale measurements use it).
LOG_EPOCH = time.monotonic() if os.environ.get("SEAM_LOG_TIMES") else None


class ProtocolMixin:
    def _note(self, text):
        if self.note_fd is not None:
            try:
                os.write(self.note_fd, text.encode() + b"\n")
            except OSError:
                pass

    def log(self, *parts):
        if self.logfile:
            stamp = "" if LOG_EPOCH is None else "%9.4f " % (time.monotonic() - LOG_EPOCH)
            self.logfile.write(stamp + " ".join(str(p) for p in parts) + "\n")
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
        if req.get("type") == "response":
            return  # a late answer to a reverse request nobody is waiting for any more
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
        # Another Python thread may be waiting for the GIL and have asked this one to give
        # it up. The interpreter would honour that in the middle of the agent's code and
        # then wait for somebody to take the GIL, which nobody can: every other thread is
        # stopped. So the request is withdrawn; the thread that made it makes it again as
        # soon as it runs.
        request = self.py.gil_drop_request(self.safe_tid)
        if request:
            self.log("withdrawing a request for the GIL before running the agent")
            self._write(*request)
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

    def req_initialize(self, args):
        self.client = dict(args)
        return {
            "supportsConfigurationDoneRequest": True,
            "supportsConditionalBreakpoints": True,
            "supportsHitConditionalBreakpoints": True,
            "supportsLogPoints": True,
            "supportsFunctionBreakpoints": True,
            "supportsEvaluateForHovers": True,
            "supportsTerminateRequest": True,
            "supportsExceptionInfoRequest": True,
            "supportsSetVariable": True,
            "supportsVariablePaging": True,
            "supportsDataBreakpoints": True,
            "exceptionBreakpointFilters": EXCEPTION_FILTERS,
        }

    def _reverse_request(self, command, arguments, timeout=30):
        """Ask the client to do something and wait for its answer.

        Called while a request is being handled, so the main loop is not running: the
        answer is picked out of the incoming queue, and everything else stays queued.
        """
        message = {"type": "request", "command": command, "arguments": arguments}
        self._send(message)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for queued in list(self.requests):
                if (queued.get("type") == "response"
                        and queued.get("request_seq") == message["seq"]):
                    self.requests.remove(queued)
                    self.log("<-", json.dumps(queued)[:600])
                    if not queued.get("success"):
                        raise DapError("the client refused %s: %s"
                                       % (command, queued.get("message")))
                    return queued.get("body") or {}
            time.sleep(0.02)
        raise DapError("the client did not answer %s within %d s" % (command, timeout))

    def _new_id(self):
        self.next_id += 1
        return self.next_id

    def _require_stopped(self):
        if self.process is None or self.running or self.exited:
            raise DapError("the process is not stopped")

    def _frame_record(self, frame_id):
        record = self.frames.get(frame_id)
        if record is None:
            raise DapError("unknown or stale frame id %s" % frame_id)
        return record

    def _new_ref(self, record):
        ref = self._new_id()
        self.refs[ref] = record
        return ref

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
            "stepInBreakpointsEnabled": (any(bp.IsEnabled() for bp in self.user_bps.values())
                                         or self.traps.pending or self.traps.armed),
            "entryTrapModules": sorted(self.traps.regions),
            "nativeStepInProgress": self.native_stepping is not None,
            "pythonStepArmed": self.py_step_armed,
            "leftoverStops": self.leftover_stops,
        }
        if self.safe_tid is not None:
            body["agent"] = self.agent("status")
        return body
