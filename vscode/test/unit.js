// Checks of the extension's own logic that need no VS Code:
//
//   node vscode/test/unit.js            which interpreter, which processes, the adapter's
//                                       command line, the protocol framing
//   node vscode/test/unit.js --adapter  also run the adapter that scripts/build-vsix.sh
//                                       put into vscode/bundled through the process
//                                       wrapper the extension uses (needs LLDB)
//
// SEAM_TEST_PYTHON names the interpreter to debug with (default python3).
const assert = require("assert");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const adapter = require("../lib/adapter");
const interpreter = require("../lib/interpreter");
const processes = require("../lib/processes");
const { nativeFocus } = require("../lib/native-focus");

const ROOT = path.resolve(__dirname, "..", "..");
const EXTENSION = path.resolve(__dirname, "..");
const PYTHON = process.env.SEAM_TEST_PYTHON || "python3";
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "seam-unit-"));

const tests = [];
function test(name, body) {
  tests.push([name, body]);
}

function touch(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "");
  return file;
}

// What vscode.EventEmitter is to the process wrapper.
function emitter() {
  const listeners = [];
  return {
    event: (listener) => listeners.push(listener),
    fire: (message) => listeners.forEach((listener) => listener(message)),
  };
}

// ---------------------------------------------------------------- the interpreter

test("native focus is repaired once per stop, ignoring stale responses and other sessions", () => {
  const session = { id: "seam-session" };
  const calls = [];
  let changed;
  let disposed = false;
  const vscode = {
    debug: {
      activeStackItem: undefined,
      onDidChangeActiveStackItem: (listener) => {
        changed = listener;
        return { dispose() { disposed = true; } };
      },
    },
    commands: { executeCommand: async (name) => { calls.push(name); } },
  };
  const tracker = nativeFocus(vscode, session, assert.fail);
  const stopped = () => tracker.onDidSendMessage({ type: "event", event: "stopped",
    body: { threadId: 7 } });
  const request = (seq) => tracker.onWillReceiveMessage({ type: "request", command: "stackTrace",
    seq, arguments: { threadId: 7, startFrame: 0 } });
  const reply = (seq) => tracker.onDidSendMessage({ type: "response", command: "stackTrace",
    request_seq: seq, success: true, body: { stackFrames: [
      { id: 1, instructionPointerReference: "0x1234" },
      { id: 2, presentationHint: "subtle" },
      { id: 3, source: { path: "/app.py" } },
    ] } });
  const select = (frameId, targetSession = session) => {
    vscode.debug.activeStackItem = { session: targetSession, threadId: 7, frameId };
    changed();
  };
  stopped();
  request(1);
  reply(1);
  select(3, { id: "other-session" });
  assert.deepStrictEqual(calls, []);
  select(3);
  assert.deepStrictEqual(calls, ["workbench.action.debug.callStackUp"]);
  select(1);
  select(3); // the user deliberately inspects the caller: do not move them back
  assert.strictEqual(calls.length, 1);
  stopped();
  request(2);
  tracker.onWillReceiveMessage({ type: "request", command: "continue" });
  reply(2);
  select(3);
  assert.strictEqual(calls.length, 1);
  stopped();
  request(3);
  stopped(); // a later stop makes the earlier stack response stale
  reply(3);
  assert.strictEqual(calls.length, 1);
  tracker.onWillStopSession();
  assert.ok(disposed);
});

test("a virtual environment in the folder is found, .venv before venv", () => {
  const folder = fs.mkdtempSync(path.join(scratch, "project-"));
  assert.strictEqual(interpreter.venvInterpreter(folder), undefined);
  const second = touch(path.join(folder, "venv", "bin", "python"));
  assert.strictEqual(interpreter.venvInterpreter(folder), second);
  const first = touch(path.join(folder, ".venv", "bin", "python"));
  assert.strictEqual(interpreter.venvInterpreter(folder), first);
});

test("python.defaultInterpreterPath: unset values, ~, relative paths, variables", () => {
  const read = (value) => interpreter.settingInterpreter(value, "/work/project", "/home/me");
  for (const unset of [undefined, null, "", "  ", "python", 42]) {
    assert.strictEqual(read(unset), undefined, `unset: ${unset}`);
  }
  assert.strictEqual(read("~/envs/a/bin/python"), "/home/me/envs/a/bin/python");
  assert.strictEqual(read("env/bin/python"), "/work/project/env/bin/python");
  assert.strictEqual(read("/opt/py/bin/python3"), "/opt/py/bin/python3");
  assert.strictEqual(read("python3.13"), "python3.13");
  assert.strictEqual(read("${workspaceFolder}/.venv/bin/python"),
    "${workspaceFolder}/.venv/bin/python");
  assert.strictEqual(interpreter.settingInterpreter("env/bin/python", undefined),
    "env/bin/python");
});

test("the interpreter comes from the first source that has one", async () => {
  const folder = fs.mkdtempSync(path.join(scratch, "project-"));
  const venv = touch(path.join(folder, ".venv", "bin", "python"));
  const find = (sources) => interpreter.findInterpreter(Object.assign({ folder }, sources));
  let found = await find({ pythonExtension: async () => "/selected/python",
                           setting: "/setting/python" });
  assert.deepStrictEqual(found, { python: "/selected/python", source: "the Python extension" });
  found = await find({ pythonExtension: async () => undefined, setting: "/setting/python" });
  assert.strictEqual(found.python, "/setting/python");
  assert.ok(found.source.includes("python.defaultInterpreterPath"));
  found = await find({ setting: "python" });
  assert.strictEqual(found.python, venv);
  assert.ok(found.source.includes("virtual environment"));
  found = await interpreter.findInterpreter({ folder: path.join(scratch, "nothing-here") });
  assert.deepStrictEqual(found, { python: "python3", source: "the default" });
  found = await interpreter.findInterpreter({});
  assert.strictEqual(found.python, "python3");
});

// ------------------------------------------------------------------ the processes

test("which program names are Python", () => {
  for (const name of ["python", "python3", "/usr/bin/python3.12", "python3.13t", "python3.14d"]) {
    assert.ok(processes.isPython(name), name);
  }
  for (const name of ["", undefined, "pythonista", "ipython", "node", "python3.12-config",
                      "/usr/bin/python3/thing"]) {
    assert.ok(!processes.isPython(name), String(name));
  }
});

function fakeProcess(root, pid, entry) {
  const dir = path.join(root, String(pid));
  fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, "cmdline"), entry.argv.map((a) => a + "\0").join(""));
  const after = ["S", "1", ...Array(17).fill("0"), String(entry.start)].join(" ");
  fs.writeFileSync(path.join(dir, "stat"), `${pid} (${entry.name || "python3"}) ${after} 0 0\n`);
  fs.writeFileSync(path.join(dir, "status"),
    `Name:\tpython3\nPid:\t${pid}\nTracerPid:\t${entry.tracer || 0}\nUid:\t0\n`);
  if (entry.exe) {
    fs.symlinkSync(entry.exe, path.join(dir, "exe"));
  }
  if (entry.cwd) {
    fs.symlinkSync(entry.cwd, path.join(dir, "cwd"));
  }
}

test("processes are read from /proc: Python only, newest first", () => {
  const root = fs.mkdtempSync(path.join(scratch, "proc-"));
  fakeProcess(root, 100, { argv: ["python3", "old.py"], exe: "/usr/bin/python3.12", start: 500,
                           cwd: "/srv/old" });
  // A name with spaces and brackets must not shift the fields that follow it.
  fakeProcess(root, 200, { argv: ["/venv/bin/python", "-m", "http.server"], start: 900,
                           exe: "/usr/bin/python3.13 (deleted)", name: "my (odd) name",
                           tracer: 77 });
  fakeProcess(root, 300, { argv: ["bash", "-c", "python3 x.py"], exe: "/usr/bin/bash", start: 950 });
  fakeProcess(root, 400, { argv: [], exe: "/usr/bin/python3.12", start: 990 });   // a zombie
  // Started through a wrapper script: the kernel's view of the executable decides.
  fakeProcess(root, 500, { argv: ["gunicorn: worker"], exe: "/usr/bin/python3.12", start: 700 });
  fs.mkdirSync(path.join(root, "self"));
  fs.writeFileSync(path.join(root, "uptime"), "1 1\n");

  const found = processes.listPythonProcesses({ procRoot: root });
  assert.deepStrictEqual(found.map((entry) => entry.pid), [200, 500, 100]);
  assert.deepStrictEqual(found[0], {
    pid: 200, argv: ["/venv/bin/python", "-m", "http.server"], exe: "/usr/bin/python3.13",
    cwd: "", startTime: 900, tracer: 77,
  });
  assert.strictEqual(found[2].cwd, "/srv/old");
  assert.deepStrictEqual(processes.listPythonProcesses({ procRoot: root, uid: 4000000 }), []);
  const kept = processes.listPythonProcesses({
    procRoot: root, ignore: (entry) => entry.argv.includes("old.py") });
  assert.deepStrictEqual(kept.map((entry) => entry.pid), [200, 500]);
});

test("a real Python process of this user is listed, with what it runs", async () => {
  const child = cp.spawn(PYTHON, ["-c", "import time; time.sleep(60)", "seam unit"],
    { cwd: scratch, stdio: "ignore" });
  try {
    await new Promise((resolve, reject) => {
      child.on("spawn", resolve);
      child.on("error", reject);
    });
    const found = processes.listPythonProcesses();
    const entry = found.find((candidate) => candidate.pid === child.pid);
    assert.ok(entry, `pid ${child.pid} among ${found.map((e) => e.pid).join()}`);
    assert.deepStrictEqual(entry.argv.slice(1), ["-c", "import time; time.sleep(60)", "seam unit"]);
    assert.ok(processes.isPython(entry.exe), entry.exe);
    assert.strictEqual(entry.cwd, fs.realpathSync(scratch));
    assert.strictEqual(entry.tracer, 0);
    assert.ok(entry.startTime > 0);
    assert.ok(!found.some((candidate) => candidate.pid === process.pid), "not this process");
    const starts = found.map((candidate) => candidate.startTime);
    assert.deepStrictEqual(starts, [...starts].sort((a, b) => b - a), "newest first");
  } finally {
    child.kill("SIGKILL");
  }
});

test("what the picker shows for a process", () => {
  const item = processes.pickItem({
    pid: 4242, argv: ["/venv/bin/python", "serve.py", "--name", "two words", "it's"],
    exe: "/usr/bin/python3.12", cwd: "/srv/app", startTime: 1, tracer: 0 });
  assert.deepStrictEqual(item, {
    label: "serve.py --name 'two words' 'it'\\''s'",
    description: "pid 4242",
    detail: "/venv/bin/python  in /srv/app",   // the environment, not the interpreter behind it
    pid: 4242,
  });
  const bare = processes.pickItem({ pid: 7, argv: ["python3"], exe: "/usr/bin/python3.12",
                                    cwd: "", startTime: 1, tracer: 9 });
  assert.strictEqual(bare.label, "python3.12 (no arguments)");
  assert.strictEqual(bare.description, "pid 7, already being debugged");
  assert.strictEqual(bare.detail, "/usr/bin/python3.12");
});

// ------------------------------------------------------- the adapter's command line

test("the bundled adapter is run as a directory, in isolated mode", () => {
  assert.deepStrictEqual(adapter.bundledCommand("/v/bin/python", "/ext"),
    ["/v/bin/python", "-I", "/ext/bundled", "dap"]);
  assert.strictEqual(adapter.hasBundle(path.join(scratch, "no-extension")), false);
});

test("the interpreter being debugged is the first choice to run the adapter", () => {
  const launch = { request: "launch", python: "/v/bin/python" };
  assert.deepStrictEqual(adapter.launcherCandidates(launch),
    ["/v/bin/python", "python3", "python", "/usr/bin/python3"]);
  assert.deepStrictEqual(adapter.launcherCandidates({ request: "launch", python: "python3" }),
    ["python3", "python", "/usr/bin/python3"]);
  const read = (link) => {
    assert.strictEqual(link, "/proc/321/exe");
    return "/opt/py/bin/python3.14";
  };
  assert.deepStrictEqual(adapter.launcherCandidates({ request: "attach", pid: 321 }, read),
    ["/opt/py/bin/python3.14", "python3", "python", "/usr/bin/python3"]);
  const gone = () => {
    throw new Error("ENOENT");
  };
  assert.deepStrictEqual(adapter.launcherCandidates({ request: "attach", pid: 321 }, gone),
    ["python3", "python", "/usr/bin/python3"]);
});

test("the adapter runs on the first candidate that is Python 3.12 or newer", async () => {
  const versions = { old: [3, 11], good: [3, 13], newer: [4, 0] };
  const probe = async (python) => (versions[python]
    ? { version: versions[python] } : { problem: "not found" });
  assert.strictEqual(await adapter.findLauncherPython(["missing", "old", "good", "newer"], probe),
    "good");
  assert.strictEqual(await adapter.findLauncherPython(["newer"], probe), "newer");
  await assert.rejects(adapter.findLauncherPython(["missing", "old"], probe), (error) => {
    assert.ok(error.message.includes("Seam needs Python 3.12 or newer"), error.message);
    assert.ok(error.message.includes("missing: not found; old: Python 3.11"), error.message);
    return true;
  });
});

test("probing a real interpreter, and one that does not exist", async () => {
  const real = await adapter.probePython(PYTHON);
  assert.ok(real.version && real.version[0] === 3 && real.version[1] >= 12, JSON.stringify(real));
  assert.deepStrictEqual(await adapter.probePython("/no/such/python"), { problem: "not found" });
  const broken = touch(path.join(scratch, "broken-python"));
  fs.writeFileSync(broken, "#!/bin/sh\nexit 3\n");
  fs.chmodSync(broken, 0o755);
  assert.deepStrictEqual(await adapter.probePython(broken), { problem: "did not run" });
});

// ----------------------------------------------------------- the process wrapper

// A stand-in for the adapter: answers every request, and misbehaves on demand.
const FAKE_ADAPTER = `
let buffer = Buffer.alloc(0);
function send(message, pieces) {
  const data = Buffer.from(JSON.stringify(message));
  const framed = Buffer.concat([Buffer.from("Content-Length: " + data.length + "\\r\\n\\r\\n"), data]);
  if (!pieces) {
    process.stdout.write(framed);
    return;
  }
  // One message in several writes, the cut falling inside the header and the body.
  process.stdout.write(framed.subarray(0, 9));
  setTimeout(() => process.stdout.write(framed.subarray(9, 40)), 30);
  setTimeout(() => process.stdout.write(framed.subarray(40)), 60);
}
if (process.argv[2] === "refuse") {
  process.stderr.write("seam: no debugger here\\n");
  process.exit(1);
}
process.stdin.on("data", (chunk) => {
  buffer = Buffer.concat([buffer, chunk]);
  for (;;) {
    const at = buffer.indexOf("\\r\\n\\r\\n");
    if (at < 0) return;
    const length = Number(/Content-Length: (\\d+)/.exec(buffer.toString("ascii", 0, at))[1]);
    if (buffer.length < at + 4 + length) return;
    const request = JSON.parse(buffer.toString("utf8", at + 4, at + 4 + length));
    buffer = buffer.subarray(at + 4 + length);
    const reply = { type: "response", request_seq: request.seq, command: request.command,
                    success: true, body: { echo: request.arguments } };
    if (request.arguments.unanswered) {
      continue;
    } else if (request.command === "die") {
      process.stderr.write("seam: LLDB went away\\n");
      process.exit(1);
    } else if (request.command === "crash") {
      process.kill(process.pid, "SIGKILL");
    } else if (request.command === "split") {
      send(reply, true);
    } else if (request.command === "burst") {
      const data = [reply, { type: "event", event: "output", body: { output: "é ✓" } }]
        .map((m) => Buffer.from(JSON.stringify(m)))
        .map((d) => Buffer.concat([Buffer.from("Content-Length: " + d.length + "\\r\\n\\r\\n"), d]));
      process.stdout.write(Buffer.concat(data));
    } else if (request.command === "garbage") {
      process.stdout.write("Content-Length: 5\\r\\n\\r\\nnope!");
      send(reply);
    } else {
      send(reply);
    }
  }
});
process.stdin.on("end", () => process.exit(0));
`;
const fakeAdapter = path.join(scratch, "fake-adapter.js");
fs.writeFileSync(fakeAdapter, FAKE_ADAPTER);

// A small protocol client on top of the wrapper, as VS Code is.
class Client {
  constructor(command, env) {
    this.messages = [];
    this.cursor = 0;
    this.log = "";
    this.failures = [];
    this.wake = () => {};
    this.seq = 0;
    const events = emitter();
    this.adapter = new adapter.AdapterProcess(command, env || process.env, events, {
      log: (text) => {
        this.log += text;
      },
      failed: (text) => this.failures.push(text),
    });
    this.adapter.onDidSendMessage((message) => {
      this.messages.push(message);
      this.wake();
    });
    this.closed = new Promise((resolve) => this.adapter.child.on("close", resolve));
  }

  send(command, args) {
    this.seq += 1;
    this.adapter.handleMessage({ seq: this.seq, type: "request", command, arguments: args || {} });
    return this.seq;
  }

  async wait(what, find, timeout = 60000) {
    const deadline = Date.now() + timeout;
    for (;;) {
      const found = find();
      if (found) {
        return found;
      }
      const left = deadline - Date.now();
      if (left <= 0) {
        const recent = this.messages.slice(-6).map((m) => JSON.stringify(m).slice(0, 240));
        throw new Error(`timed out waiting for ${what}; last messages:\n${recent.join("\n")}`
          + `\nstderr: ${this.log}`);
      }
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, Math.min(left, 500));
        this.wake = () => {
          clearTimeout(timer);
          resolve();
        };
      });
    }
  }

  answer(seq, what) {
    return this.wait(what, () => this.messages.find(
      (m) => m.type === "response" && m.request_seq === seq));
  }

  request(command, args) {
    return this.answer(this.send(command, args), `the answer to ${command}`);
  }

  // The next event of that name not yet handed out.
  event(name) {
    return this.wait(`the ${name} event`, () => {
      while (this.cursor < this.messages.length) {
        const message = this.messages[this.cursor++];
        if (message.type === "event" && message.event === name) {
          return message;
        }
      }
      return undefined;
    });
  }

  events(name) {
    return this.messages.filter((m) => m.type === "event" && m.event === name);
  }
}

const fake = (...args) => new Client([process.execPath, fakeAdapter, ...args]);

test("messages are framed both ways, whole, split and several at once", async () => {
  const client = fake();
  let reply = await client.request("initialize", { text: "naïve ✓", big: "x".repeat(200000) });
  assert.strictEqual(reply.success, true);
  assert.strictEqual(reply.body.echo.text, "naïve ✓");
  assert.strictEqual(reply.body.echo.big.length, 200000);
  reply = await client.request("split", { n: 1 });
  assert.deepStrictEqual(reply.body.echo, { n: 1 });
  reply = await client.request("burst");
  assert.strictEqual((await client.event("output")).body.output, "é ✓");
  reply = await client.request("garbage");
  assert.strictEqual(reply.success, true);
  assert.ok(client.log.includes("not a protocol message"), client.log);
  assert.strictEqual(client.adapter.pending.size, 0);
  client.adapter.dispose();
  await client.closed;
  assert.deepStrictEqual(client.events("terminated"), [], "a session ended by VS Code is silent");
  assert.deepStrictEqual(client.failures, []);
});

test("an adapter that cannot start: its own words answer the first request", async () => {
  // Whether VS Code asks before or after the adapter has gone, the answer is the same.
  for (const wait of [false, true]) {
    const client = fake("refuse");
    if (wait) {
      await client.closed;
    }
    const reply = await client.request("initialize");
    assert.strictEqual(reply.success, false);
    assert.strictEqual(reply.message, "no debugger here");
    assert.strictEqual(reply.command, "initialize");
    await client.closed;
    // VS Code ends a session whose start was refused; nothing more is said.
    assert.deepStrictEqual(client.events("terminated"), []);
    assert.deepStrictEqual(client.failures, []);
    assert.ok(client.log.includes("seam: no debugger here"));
  }
});

test("an adapter that dies during launch: the launch is refused with the reason", async () => {
  const client = fake();
  await client.request("initialize");
  client.send("launch", { unanswered: true });
  const seq = client.send("die");
  const refused = await client.answer(seq, "the refusals");
  assert.strictEqual(refused.message, "LLDB went away");
  assert.ok(client.messages.some((m) => m.command === "launch" && m.success === false
                                        && m.message === "LLDB went away"));
  await client.closed;
  assert.deepStrictEqual(client.failures, []);
  assert.deepStrictEqual(client.events("terminated"), []);
});

test("an adapter that dies mid-session: the user is told and the session is ended", async () => {
  for (const [command, expected] of [["die", "LLDB went away"],
                                     ["crash", "Seam's debug adapter ended unexpectedly (signal SIGKILL)."]]) {
    const client = fake();
    await client.request("initialize");
    await client.request("launch");
    const reply = await client.request(command);
    assert.strictEqual(reply.success, false);
    assert.strictEqual(reply.message, expected);
    await client.event("terminated");
    assert.deepStrictEqual(client.failures, [expected]);
    // Whatever VS Code still asks is refused, not left hanging.
    const late = await client.request("threads");
    assert.deepStrictEqual([late.success, late.message], [false, expected]);
    assert.strictEqual(client.events("terminated").length, 1);
    client.adapter.dispose();
  }
});

test("a command that cannot be run at all is reported the same way", async () => {
  const client = new Client([path.join(scratch, "no-such-adapter"), "dap"]);
  const reply = await client.request("initialize");
  assert.strictEqual(reply.success, false);
  assert.ok(reply.message.includes("cannot start") && reply.message.includes("no-such-adapter"),
    reply.message);
});

// -------------------------------------------------- the adapter inside the extension

const WITH_ADAPTER = process.argv.includes("--adapter");
const BASIC = path.join(ROOT, "tests", "targets", "basic.py");

function lineOf(file, marker) {
  const index = fs.readFileSync(file, "utf8").split("\n").findIndex((text) => text.includes(marker));
  assert.ok(index >= 0, `marker ${marker} in ${file}`);
  return index + 1;
}

async function bundled(env) {
  assert.ok(adapter.hasBundle(EXTENSION), "run scripts/build-vsix.sh first");
  const python = await adapter.findLauncherPython(
    adapter.launcherCandidates({ request: "launch", python: PYTHON }));
  const clean = Object.assign({}, process.env, env);
  delete clean.PYTHONPATH;
  return new Client(adapter.bundledCommand(python, EXTENSION), clean);
}

if (WITH_ADAPTER) {
  test("bundled adapter: without LLDB the session is refused in the adapter's words", async () => {
    const client = await bundled({ SEAM_LLDB: "/no/such/lldb" });
    const reply = await client.request("initialize", { adapterID: "seam" });
    assert.strictEqual(reply.success, false);
    assert.ok(reply.message.startsWith("No `lldb` was found on PATH"), reply.message);
    await client.closed;
  });

  test("bundled adapter: launch, breakpoint, variables, run to the end", async () => {
    const client = await bundled();
    const line = lineOf(BASIC, "# inner-first");
    let reply = await client.request("initialize", { adapterID: "seam", clientID: "unit" });
    assert.strictEqual(reply.success, true, reply.message);
    reply = await client.request("launch", { program: BASIC, python: PYTHON,
                                             cwd: path.dirname(BASIC) });
    assert.strictEqual(reply.success, true, reply.message);
    await client.event("initialized");
    reply = await client.request("setBreakpoints", { source: { path: BASIC },
                                                     breakpoints: [{ line }] });
    assert.strictEqual(reply.body.breakpoints[0].verified, true);
    await client.request("configurationDone");
    const stop = (await client.event("stopped")).body;
    assert.strictEqual(stop.reason, "breakpoint");
    const frames = (await client.request("stackTrace", { threadId: stop.threadId })).body.stackFrames;
    assert.deepStrictEqual(frames.map((frame) => frame.name), ["inner", "outer", "main", "<module>"]);
    assert.strictEqual(frames[0].line, line);
    // The helper the program loaded is the one inside the extension, found with nothing
    // set in the environment.
    const evaluate = async (expression) => (await client.request("evaluate",
      { expression, frameId: frames[0].id, context: "repl" })).body.result;
    assert.strictEqual(await evaluate("__import__('sys').modules['_seam_trap'].__file__"),
      `'${path.join(EXTENSION, "bundled", "seam", "_target", "_seam_trap.abi3.so")}'`);
    assert.strictEqual(await evaluate("__import__('os').environ.get('PYTHONPATH')"), "None");
    await client.request("setBreakpoints", { source: { path: BASIC }, breakpoints: [] });
    await client.request("continue", { threadId: stop.threadId });
    assert.strictEqual((await client.event("exited")).body.exitCode, 0);
    await client.event("terminated");
    assert.ok(client.events("output").map((m) => m.body.output).join("").includes("result 33"));
    await client.request("disconnect");
    client.adapter.dispose();
    // Its input closed, the adapter leaves by itself, well before the wrapper would kill it.
    const started = Date.now();
    assert.strictEqual(await client.closed, 0);
    assert.ok(Date.now() - started < 12000, "the adapter left on its own");
    assert.deepStrictEqual(client.failures, []);
  });
}

async function main() {
  let failed = 0;
  for (const [name, body] of tests) {
    try {
      await body();
      console.log(`ok    ${name}`);
    } catch (error) {
      failed += 1;
      console.log(`FAIL  ${name}\n${error.stack || error}`);
    }
  }
  fs.rmSync(scratch, { recursive: true, force: true });
  console.log(failed ? `${failed} of ${tests.length} failed` : `${tests.length} passed`);
  process.exit(failed ? 1 : 0);
}

main();
