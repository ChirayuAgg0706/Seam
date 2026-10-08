// Starting the debug adapter that travels inside the extension: which Python runs it,
// the command line, and the child process. Nothing here needs VS Code.
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const processes = require("./processes");

// The seam package declares requires-python >= 3.12, and its launcher is part of it.
const MINIMUM = [3, 12];
const PROBE = "import sys; print('seam-python %d %d' % sys.version_info[:2])";
const STARTUP_REQUESTS = ["initialize", "launch", "attach"];
// `seam dap` gives LLDB ten seconds to leave after the session; then it is our turn.
const LEAVE_TIMEOUT_MS = 15000;
// How much of the adapter's stderr is kept to explain its end (LLDB's last lines, a traceback).
const STDERR_KEPT = 4000;

function probePython(python) {
  return new Promise((resolve) => {
    cp.execFile(python, ["-I", "-c", PROBE], { timeout: 10000 }, (error, stdout) => {
      const match = /seam-python (\d+) (\d+)/.exec(String(stdout || ""));
      if (match) {
        resolve({ version: [Number(match[1]), Number(match[2])] });
      } else if (error && error.code === "ENOENT") {
        resolve({ problem: "not found" });
      } else {
        resolve({ problem: "did not run" });
      }
    });
  });
}

// Interpreters that could run the adapter, best first. The one being debugged leads:
// if Seam can debug it at all it is a CPython 3.12+, whatever the system's python3 is.
function launcherCandidates(configuration, readlink = fs.readlinkSync) {
  const candidates = [];
  if (configuration.request === "launch" && configuration.python) {
    candidates.push(configuration.python);
  }
  if (configuration.request === "attach" && Number(configuration.pid) > 0) {
    try {
      if (process.platform === "darwin" && readlink === fs.readlinkSync) {
        const target = processes.listPythonProcesses().find(
          (entry) => entry.pid === Number(configuration.pid));
        if (target) {
          candidates.push(target.exe);
        }
      } else {
        candidates.push(readlink(`/proc/${Number(configuration.pid)}/exe`));
      }
    } catch (err) {
      // Not ours to read, or gone: the adapter will say so.
    }
  }
  candidates.push("python3", "python", "/usr/bin/python3");
  return [...new Set(candidates)];
}

async function findLauncherPython(candidates, probe = probePython) {
  const tried = [];
  for (const python of candidates) {
    const result = await probe(python);
    const version = result.version;
    if (version && (version[0] > MINIMUM[0]
        || (version[0] === MINIMUM[0] && version[1] >= MINIMUM[1]))) {
      return python;
    }
    tried.push(`${python}: ${version ? "Python " + version.join(".") : result.problem}`);
  }
  const wanted = MINIMUM.join(".");
  throw new Error(
    `Seam needs Python ${wanted} or newer to run its debug adapter and found none `
    + `(${tried.join("; ")}). Seam debugs CPython ${wanted}+ programs: select such an `
    + "interpreter for this project, or name one as \"python\" in the launch configuration.");
}

// `python <directory>` runs the directory's __main__.py with the directory itself on
// sys.path, so the seam package next to it is found without PYTHONPATH, which LLDB and
// the debugged program would inherit. -I keeps the user's PYTHONPATH and PYTHONSTARTUP
// (meant for their program) from getting in the adapter's way.
function bundledCommand(python, extensionPath) {
  return [python, "-I", path.join(extensionPath, "bundled"), "dap"];
}

function hasBundle(extensionPath) {
  return fs.existsSync(path.join(extensionPath, "bundled", "seam", "cli.py"));
}

// The adapter as a child process, in the shape of a vscode.DebugAdapter.
//
// VS Code can run the command itself (DebugAdapterExecutable), but then it discards
// whatever the adapter writes to stderr: an adapter that cannot start, because LLDB is
// missing for instance, shows up as "terminated unexpectedly (read error)". Here the
// adapter's own words are kept and given to VS Code as the answer to the request it is
// waiting on, which it shows to the user.
//
// `emitter` is a vscode.EventEmitter. `hooks.log(text)` receives the adapter's stderr;
// `hooks.failed(text)` is called when the adapter dies in the middle of a session.
class AdapterProcess {
  constructor(command, env, emitter, hooks = {}) {
    this.onDidSendMessage = emitter.event;
    this.emitter = emitter;
    this.hooks = hooks;
    this.buffer = Buffer.alloc(0);
    this.stderr = "";
    this.pending = new Map();   // seq -> command of requests the adapter has not answered
    this.heard = false;         // VS Code has sent something
    this.over = false;          // the child has gone
    this.lastWords = "";        // why, once it has
    this.disposed = false;      // VS Code has ended the session
    this.finished = false;      // the adapter itself said the session is over
    this.child = cp.spawn(command[0], command.slice(1), { env, stdio: ["pipe", "pipe", "pipe"] });
    this.child.on("error", (error) => this._ended(`cannot start ${command[0]}: ${error.message}`));
    if (!this.child.stdout) {
      return;   // it could not be started at all; "error" says why
    }
    this.child.stdout.on("data", (chunk) => this._read(chunk));
    this.child.stderr.on("data", (chunk) => {
      this.stderr = (this.stderr + chunk).slice(-STDERR_KEPT);
      if (hooks.log) {
        hooks.log(String(chunk));
      }
    });
    // Writing to an adapter that has just gone; its end is reported by "close".
    this.child.stdin.on("error", () => {});
    // "close", not "exit": everything the adapter wrote has been read by then.
    this.child.on("close", (code, signal) => {
      const how = signal ? `signal ${signal}` : `status ${code}`;
      this._ended(code === 0 ? undefined : `Seam's debug adapter ended unexpectedly (${how}).`);
    });
  }

  _read(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (!this.disposed) {
      const headerEnd = this.buffer.indexOf("\r\n\r\n");
      if (headerEnd < 0) {
        return;
      }
      const header = this.buffer.toString("ascii", 0, headerEnd);
      const length = /content-length: *(\d+)/i.exec(header);
      const start = headerEnd + 4;
      const end = start + (length ? Number(length[1]) : 0);
      if (this.buffer.length < end) {
        return;
      }
      const body = this.buffer.toString("utf8", start, end);
      this.buffer = this.buffer.subarray(end);
      let message;
      try {
        message = JSON.parse(body);
      } catch (err) {
        if (this.hooks.log) {
          this.hooks.log(`not a protocol message from the adapter: ${body.slice(0, 200)}\n`);
        }
        continue;
      }
      if (message.type === "response") {
        this.pending.delete(message.request_seq);
        this.finished = this.finished || message.command === "disconnect";
      } else if (message.type === "event" && message.event === "terminated") {
        this.finished = true;
      }
      this.emitter.fire(message);
    }
  }

  _refuse(seq, command) {
    this.emitter.fire({ seq: 0, type: "response", request_seq: seq, command, success: false,
                        message: this.lastWords });
  }

  _ended(failure) {
    if (this.over) {
      return;
    }
    this.over = true;
    clearTimeout(this.leaveTimer);
    if (this.disposed) {
      return;
    }
    // What the adapter said on its way out explains more than its exit status.
    const said = this.stderr.trim().replace(/^seam: /, "");
    this.lastWords = said || failure || "Seam's debug adapter is no longer running.";
    // Before the program is running, VS Code is waiting for an answer and shows a
    // refusal as the reason the session did not start; that also ends the session.
    const starting = !this.heard
      || [...this.pending.values()].some((command) => STARTUP_REQUESTS.includes(command));
    for (const [seq, command] of this.pending) {
      this._refuse(seq, command);
    }
    this.pending.clear();
    if (starting) {
      return;
    }
    // Later on nobody shows a refused request, so the extension has to speak up, and
    // the session has to be ended for VS Code.
    if (failure && this.hooks.failed) {
      this.hooks.failed(this.lastWords);
    }
    if (!this.finished) {
      this.emitter.fire({ seq: 0, type: "event", event: "terminated", body: {} });
    }
  }

  handleMessage(message) {
    this.heard = true;
    if (this.over) {
      if (message.type === "request") {
        this._refuse(message.seq, message.command);
      }
      return;
    }
    if (message.type === "request") {
      this.pending.set(message.seq, message.command);
    }
    if (this.child.stdin) {
      const data = Buffer.from(JSON.stringify(message), "utf8");
      this.child.stdin.write(`Content-Length: ${data.length}\r\n\r\n`);
      this.child.stdin.write(data);
    }
  }

  dispose() {
    this.disposed = true;
    if (this.over || !this.child.stdin) {
      return;
    }
    // Closing its input is how a client tells `seam dap` that it has left: the adapter
    // detaches from or kills the program, and both processes leave on their own.
    this.child.stdin.end();
    this.leaveTimer = setTimeout(() => this.child.kill("SIGTERM"), LEAVE_TIMEOUT_MS);
    this.leaveTimer.unref();
  }
}

module.exports = {
  AdapterProcess, bundledCommand, findLauncherPython, hasBundle, launcherCandidates, probePython,
};
