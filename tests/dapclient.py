"""A scripted DAP client that drives `seam dap` exactly as an editor would."""
import json
import os
import queue
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DapFailure(AssertionError):
    pass


class DapClient:
    def __init__(self, log_path=None, command=None):
        """`command` runs an installed adapter; by default the source tree's is used."""
        env = dict(os.environ)
        if command is None:
            command = [sys.executable, "-m", "seam", "dap"]
            env["PYTHONPATH"] = (os.path.join(ROOT, "src") + os.pathsep
                                 + env.get("PYTHONPATH", ""))
        if log_path:
            env["SEAM_LOG"] = log_path
        self.log_path = log_path
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, env=env)
        self.seq = 0
        self.inbox = queue.Queue()
        self.events = []
        self.output = ""
        self.target_pid = None
        threading.Thread(target=self._reader, daemon=True).start()

    # ---------------------------------------------------------- transport

    def _reader(self):
        out = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        raise EOFError
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":")[1])
                self.inbox.put(json.loads(out.read(length)))
        except (EOFError, OSError, ValueError):
            self.inbox.put(None)

    def _next(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DapFailure("timed out waiting for the adapter\n" + self.tail_log())
        try:
            msg = self.inbox.get(timeout=remaining)
        except queue.Empty:
            raise DapFailure("timed out waiting for the adapter\n" + self.tail_log())
        if msg is None:
            raise DapFailure("the adapter closed the connection\n" + self.tail_log())
        if msg.get("type") == "event":
            if msg["event"] == "output":
                self.output += msg["body"].get("output", "")
            else:
                self.events.append(msg)
        return msg

    def tail_log(self, lines=40):
        """The end of the adapter's protocol log and of LLDB's own output (tracebacks)."""
        out = ""
        for title, path in (("adapter log", self.log_path),
                            ("lldb output", (self.log_path or "") + ".lldb")):
            if path and os.path.exists(path):
                with open(path, errors="replace") as fh:
                    out += "--- %s ---\n%s" % (title, "".join(fh.readlines()[-lines:]))
        return out

    def send(self, command, arguments=None):
        self.seq += 1
        data = json.dumps({"seq": self.seq, "type": "request", "command": command,
                           "arguments": arguments or {}}).encode()
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.proc.stdin.flush()
        return self.seq

    def request(self, command, arguments=None, timeout=60, check=True):
        seq = self.send(command, arguments)
        deadline = time.monotonic() + timeout
        while True:
            msg = self._next(deadline)
            if msg.get("type") == "response" and msg.get("request_seq") == seq:
                if check and not msg.get("success"):
                    raise DapFailure("%s failed: %s" % (command, msg.get("message")))
                return msg if not check else msg.get("body") or {}

    def wait_event(self, name, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            for i, ev in enumerate(self.events):
                if ev["event"] == name:
                    del self.events[i]
                    return ev["body"]
            self._next(deadline)

    def wait_any(self, names, timeout=30):
        """Wait for the first of several events; returns (name, body)."""
        deadline = time.monotonic() + timeout
        while True:
            for i, ev in enumerate(self.events):
                if ev["event"] in names:
                    del self.events[i]
                    return ev["event"], ev["body"]
            self._next(deadline)

    def drain(self, seconds, name):
        """Read messages for `seconds`; return the queued events called `name`."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = self.inbox.get(timeout=remaining)
            except queue.Empty:
                break
            if msg is None:
                raise DapFailure("the adapter closed the connection\n" + self.tail_log())
            if msg.get("type") == "event":
                if msg["event"] == "output":
                    self.output += msg["body"].get("output", "")
                else:
                    self.events.append(msg)
        return [e for e in self.events if e["event"] == name]

    # ------------------------------------------------------------ helpers

    def launch(self, program, python, args=(), breakpoints=None, **extra):
        self.request("initialize", {"adapterID": "seam", "clientID": "tests"})
        launch = {"program": program, "python": python, "args": list(args),
                  "cwd": os.path.dirname(program)}
        if os.environ.get("SEAM_TEST_DEBUGINFO") == "0":
            # Matrix cell: pretend the interpreter's separate debug info is not installed.
            launch["debugInfoLookup"] = False
        launch.update(extra)
        self.request("launch", launch)
        self.wait_event("initialized")
        for path, lines in (breakpoints or {}).items():
            self.set_breakpoints(path, lines)
        self.request("configurationDone")

    def set_breakpoints(self, path, lines):
        bps = [b if isinstance(b, dict) else {"line": b} for b in lines]
        return self.request("setBreakpoints", {"source": {"path": path},
                                               "breakpoints": bps})["breakpoints"]

    def wait_stopped(self, timeout=30):
        return self.wait_event("stopped", timeout)

    def stack(self, thread_id):
        return self.request("stackTrace", {"threadId": thread_id})["stackFrames"]

    def variables(self, ref):
        body = self.request("variables", {"variablesReference": ref})
        return {v["name"]: v for v in body["variables"]}

    def scope(self, frame_id, name="Locals"):
        for scope in self.request("scopes", {"frameId": frame_id})["scopes"]:
            if scope["name"] == name:
                return self.variables(scope["variablesReference"])
        raise DapFailure("no scope %r" % name)

    def evaluate(self, expression, frame_id=None, check=True):
        args = {"expression": expression, "context": "repl"}
        if frame_id is not None:
            args["frameId"] = frame_id
        return self.request("evaluate", args, check=check)

    def step(self, command, thread_id):
        self.request(command, {"threadId": thread_id})
        return self.wait_stopped()

    def cont(self, thread_id=0):
        self.request("continue", {"threadId": thread_id})

    def status(self):
        body = self.request("seam/status")
        self.target_pid = body.get("pid")
        return body

    def wait_exit(self, timeout=30):
        body = self.wait_event("exited", timeout)
        return body["exitCode"]

    def close(self):
        if self.proc.poll() is None:
            try:
                self.request("disconnect", timeout=10, check=False)
            except (DapFailure, OSError):
                pass
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                raise DapFailure("the adapter did not exit after disconnect")
