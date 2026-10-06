// Runs inside VS Code's extension host (see run.js), with the packaged extension
// installed and a small project open: app.py, the native module it calls, and a virtual
// environment. Everything is done through VS Code's own debug API and commands, and
// checked against what VS Code itself ends up showing: the stopped frame, the file it
// opened, the session's end. With SEAM_SHOTS set, pictures of the window are kept too.
const assert = require("assert");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const vscode = require("vscode");

const TIMEOUT_MS = 90000;
const SHOTS = process.env.SEAM_SHOTS;
const SCREEN = (process.env.SEAM_SCREEN || "1600x1000").split("x");

const project = process.env.SEAM_PROJECT;
const app = path.join(project, "app.py");
const venvPython = path.join(project, ".venv", "bin", "python");
const rust = path.join(process.env.SEAM_DEMO_DIR, "src", "lib.rs");
const attachTarget = path.join(process.env.SEAM_REPOSITORY, "tests", "targets", "attach_target.py");

function lineOf(file, marker) {
  const lines = fs.readFileSync(file, "utf8").split("\n");
  const index = lines.findIndex((text) => text.includes(marker));
  assert.ok(index >= 0, `marker ${marker} in ${file}`);
  return index + 1;
}

function log(message) {
  console.log(`[seam editor check] ${message}`);
}

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

// Every DAP message the adapter sends, as VS Code received it.
const messages = [];
let wake = () => {};
let cursor = 0;

async function nextMessage(what, predicate) {
  const deadline = Date.now() + TIMEOUT_MS;
  for (;;) {
    while (cursor < messages.length) {
      const message = messages[cursor++];
      if (predicate(message)) {
        return message;
      }
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      const recent = messages.slice(-8).map((m) => JSON.stringify(m).slice(0, 300));
      throw new Error(`timed out waiting for ${what}; last messages:\n${recent.join("\n")}`);
    }
    await new Promise((resolve) => {
      const timer = setTimeout(resolve, Math.min(remaining, 1000));
      wake = () => {
        clearTimeout(timer);
        resolve();
      };
    });
  }
}

const nextEvent = (name) =>
  nextMessage(`the ${name} event`, (m) => m.type === "event" && m.event === name);

const refusal = (request) =>
  nextMessage(`${request} to be refused`,
    (m) => m.type === "response" && m.command === request && !m.success);

async function activeEditorIs(file) {
  const deadline = Date.now() + 20000;
  for (;;) {
    const editor = vscode.window.activeTextEditor;
    if (editor && editor.document.uri.fsPath === file) {
      return editor;
    }
    if (Date.now() > deadline) {
      const shown = editor ? editor.document.uri.fsPath : "nothing";
      throw new Error(`VS Code did not open ${file}; it shows ${shown}`);
    }
    await sleep(100);
  }
}

// VS Code has put the cursor on a line of a file: where it shows the program stopped.
// It does that a moment after the adapter's stop event, having asked for the stack.
async function cursorIsAt(file, line) {
  const deadline = Date.now() + 20000;
  for (;;) {
    const editor = vscode.window.activeTextEditor;
    const shown = editor
      ? `${editor.document.uri.fsPath} line ${editor.selection.active.line + 1}` : "nothing";
    if (shown === `${file} line ${line}`) {
      return;
    }
    if (Date.now() > deadline) {
      throw new Error(`VS Code did not go to ${file} line ${line}; it shows ${shown}`);
    }
    await sleep(100);
  }
}

async function stackOf(session, threadId) {
  const reply = await session.customRequest("stackTrace", { threadId });
  return { frames: reply.stackFrames, top: reply.stackFrames[0] };
}

// The value of a Python expression in a frame, as the debug console would print it.
async function evaluate(session, frame, expression) {
  const reply = await session.customRequest("evaluate",
    { expression, frameId: frame.id, context: "repl" });
  return reply.result;
}

function sessionStarted() {
  return new Promise((resolve) => {
    const subscription = vscode.debug.onDidStartDebugSession((session) => {
      subscription.dispose();
      resolve(session);
    });
  });
}

function sessionEnded(session) {
  return new Promise((resolve) => {
    const subscription = vscode.debug.onDidTerminateDebugSession((ended) => {
      if (ended.id === session.id) {
        subscription.dispose();
        resolve();
      }
    });
  });
}

async function start(folder, configuration) {
  cursor = messages.length;   // what earlier sessions left unread is not about this one
  const started = sessionStarted();
  assert.ok(await vscode.debug.startDebugging(folder, configuration), "the session started");
  return started;
}

// A start that must fail. VS Code would show the reason in a dialog; under test it
// refuses to open dialogs, so the reason is read where it came from.
async function startRefused(folder, configuration) {
  cursor = messages.length;
  let started = false;
  try {
    started = await vscode.debug.startDebugging(folder, configuration);
  } catch (err) {
    log(`startDebugging: ${String(err.message || err).split("\n")[0]}`);
  }
  assert.ok(!started, `"${configuration.name}" must not start`);
}

function breakAt(file, line) {
  vscode.debug.addBreakpoints([new vscode.SourceBreakpoint(
    new vscode.Location(vscode.Uri.file(file), new vscode.Position(line - 1, 0)))]);
}

async function command(name, ...args) {
  try {
    return await vscode.commands.executeCommand(name, ...args);
  } catch (err) {
    log(`command ${name}: ${String(err.message || err).split("\n")[0]}`);
    return undefined;
  }
}

// ---------------------------------------------------------------- pictures

function tool(program, args) {
  try {
    return cp.execFileSync(program, args, { encoding: "utf8", timeout: 20000 }).trim();
  } catch (err) {
    log(`${program} ${args.join(" ")}: ${String(err.message).split("\n")[0]}`);
    return undefined;
  }
}

// There is no window manager on CI's display: nothing has sized the window to the
// screen or given it the keyboard.
async function prepareWindow() {
  if (!SHOTS) {
    return;
  }
  fs.mkdirSync(SHOTS, { recursive: true });
  const found = tool("xdotool", ["search", "--onlyvisible", "--name", "Visual Studio Code"])
    || tool("xdotool", ["search", "--onlyvisible", "--class", "code"]) || "";
  const windows = found.split("\n").filter(Boolean);
  for (const id of windows) {
    tool("xdotool", ["windowmove", id, "0", "0"]);
    tool("xdotool", ["windowsize", id, ...SCREEN]);
  }
  if (windows.length) {
    tool("xdotool", ["windowfocus", windows[0]]);
  }
  await sleep(1500);
  for (const id of windows) {
    const geometry = (tool("xdotool", ["getwindowgeometry", id]) || "").replace(/\s+/g, " ");
    log(`window ${id} "${tool("xdotool", ["getwindowname", id])}": ${geometry}`);
  }
  if (!windows.length) {
    log("no VS Code window found on the display; pictures will show what there is");
  }
}

async function shot(name) {
  if (!SHOTS) {
    return;
  }
  await sleep(2500);   // let the views finish drawing; nothing is asserted about this
  const file = path.join(SHOTS, `${name}.png`);
  if (tool("import", ["-window", "root", file]) === undefined) {
    tool("scrot", ["--overwrite", file]);
  }
  log(fs.existsSync(file) ? `picture: ${name}.png (${fs.statSync(file).size} bytes)`
    : `picture ${name}.png was not taken`);
}

// Open one variable of the top frame in the Variables view, then one of its members.
// The view has no API; it is driven as a keyboard user would, with list commands.
async function expandInVariablesView(session, frame, name, member) {
  if (!SHOTS) {
    return;
  }
  const scopes = (await session.customRequest("scopes", { frameId: frame.id })).scopes;
  const locals = (await session.customRequest("variables",
    { variablesReference: scopes[0].variablesReference })).variables;
  const row = locals.findIndex((variable) => variable.name === name);
  if (row < 0) {
    log(`no local called ${name} to expand: ${locals.map((v) => v.name).join()}`);
    return;
  }
  const members = (await session.customRequest("variables",
    { variablesReference: locals[row].variablesReference })).variables;
  const inner = members.findIndex((variable) => variable.name === member);
  await sleep(1500);   // the view fills in after the stop; nothing is asserted about this
  await command("workbench.debug.action.focusVariablesView");
  await sleep(500);
  await command("list.focusFirst");            // the Locals scope
  for (let i = 0; i <= row; i++) {
    await command("list.focusDown");
  }
  await command("list.expand");
  await sleep(800);
  if (inner >= 0) {
    for (let i = 0; i <= inner; i++) {
      await command("list.focusDown");
    }
    await command("list.expand");
  }
}

// ---------------------------------------------------------------- the checks

let seam;     // what the extension exports for this check
let folder;

// Someone opens the project, opens app.py and presses F5. No launch.json, no setting,
// no Seam installed: the extension has to bring the adapter and find the interpreter.
async function pressF5() {
  const pyLine = lineOf(app, "# step in here");
  const rustLine = lineOf(rust, "// break here");
  const failLine = lineOf(app, "# raises here");

  const found = await seam.findInterpreter(folder);
  assert.deepStrictEqual(found,
    { python: venvPython, source: "the virtual environment in the workspace folder" });
  assert.deepStrictEqual(vscode.workspace.getConfiguration("seam").get("adapterCommand"), [],
    "no adapter command is configured: the extension uses the adapter it carries");

  await vscode.window.showTextDocument(vscode.Uri.file(app));
  breakAt(app, pyLine);
  await command("workbench.view.debug");
  await prepareWindow();
  await shot("0-before-f5");

  cursor = messages.length;
  const started = sessionStarted();
  // Not awaited: what matters is that a session appears.
  command("workbench.action.debug.start");
  const session = await Promise.race([started, sleep(30000)]);
  assert.ok(session, "F5 on a Python file started a session");
  assert.strictEqual(session.type, "seam");
  assert.strictEqual(session.configuration.python, venvPython,
    "the project's virtual environment is what gets debugged");
  assert.strictEqual(session.configuration.console, "integratedTerminal",
    "the extension defaults to the integrated terminal");
  assert.strictEqual(session.configuration.internalConsoleOptions, "neverOpen",
    "and leaves that terminal in view");
  const ended = sessionEnded(session);

  let stop = (await nextEvent("stopped")).body;
  assert.strictEqual(stop.reason, "breakpoint");
  let stack = await stackOf(session, stop.threadId);
  assert.deepStrictEqual(stack.frames.map((frame) => frame.name), ["total", "main", "<module>"]);
  assert.strictEqual(stack.top.line, pyLine);
  await cursorIsAt(app, pyLine);
  // The program itself says which interpreter it is.
  assert.strictEqual(await evaluate(session, stack.top, "__import__('sys').prefix"),
    `'${path.join(project, ".venv")}'`);
  assert.strictEqual(await evaluate(session, stack.top, "__import__('sys').executable"),
    `'${venvPython}'`);
  assert.strictEqual(await evaluate(session, stack.top, "__import__('os').environ.get('PYTHONPATH')"),
    "None", "the extension adds nothing to the program's environment");
  assert.ok(vscode.window.terminals.some((terminal) => terminal.name.includes("Seam")),
    "VS Code created a terminal for the program");
  log(`F5 debugs ${venvPython}; stopped at the Python breakpoint, app.py line ${pyLine}`);
  try {
    await expandInVariablesView(session, stack.top, "order", "quantities");
  } catch (err) {
    log(`the Variables view was not expanded for the picture: ${err.message}`);
  }
  await shot("1-python-stop");

  await command("workbench.action.debug.stepInto");
  stop = (await nextEvent("stopped")).body;
  stack = await stackOf(session, stop.threadId);
  assert.strictEqual(stack.top.source.path, rust);
  assert.ok(stack.top.name.includes("sum_squares"), stack.top.name);
  assert.deepStrictEqual(stack.frames.slice(1).map((frame) => frame.name),
    ["total", "main", "<module>"]);
  await activeEditorIs(rust);
  log(`Step Into landed in ${stack.top.name}; VS Code opened src/lib.rs; stack: `
    + stack.frames.map((frame) => frame.name).join(" < "));

  // A few turns of the loop, so that the Rust variables have something to show.
  breakAt(rust, rustLine);
  await nextMessage("the Rust breakpoint to be set",
    (m) => m.type === "response" && m.command === "setBreakpoints");
  for (let turn = 0; turn < 3; turn++) {
    await command("workbench.action.debug.continue");
    stop = (await nextEvent("stopped")).body;
  }
  stack = await stackOf(session, stop.threadId);
  assert.deepStrictEqual([stack.top.source.path, stack.top.line], [rust, rustLine]);
  await shot("2-native-stop");
  if (SHOTS) {
    // The Python frame under the Rust one: its variables are read from memory.
    await command("workbench.action.debug.callStackDown");
    await shot("3-native-stop-python-frame");
  }

  vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
  await command("workbench.action.debug.continue");
  stop = (await nextEvent("stopped")).body;
  assert.strictEqual(stop.reason, "exception");
  assert.ok(stop.description.startsWith("ZeroDivisionError"), stop.description);
  stack = await stackOf(session, stop.threadId);
  // The frames the exception passed through, from where it was raised outwards.
  assert.deepStrictEqual([stack.top.name, stack.top.line], ["check", failLine]);
  assert.ok(stack.frames.some((frame) => frame.name === "main"),
    stack.frames.map((frame) => frame.name).join());
  await cursorIsAt(app, failLine);
  log(`stopped on the uncaught exception: ${stop.description}; stack: `
    + stack.frames.map((frame) => frame.name).join(" < "));
  await shot("4-uncaught-exception");

  await command("workbench.action.debug.continue");
  assert.strictEqual((await nextEvent("exited")).body.exitCode, 1);
  await ended;
  const terminal = vscode.window.terminals.find((candidate) => candidate.name.includes("Seam"));
  terminal.show(true);
  await shot("5-terminal-after-the-run");
  log("the program ran in VS Code's integrated terminal, to its end");
}

// The debug console, and stepping across the boundary with VS Code's own commands.
async function stepInTheDebugConsole() {
  const pyLine = lineOf(app, "# step in here");
  breakAt(app, pyLine);
  const session = await start(folder, {
    type: "seam", request: "launch", name: "editor check (debug console)",
    program: app, args: ["--no-check"], console: "internalConsole",
  });
  assert.strictEqual(session.configuration.python, venvPython);
  const ended = sessionEnded(session);
  let stop = (await nextEvent("stopped")).body;
  assert.strictEqual(stop.reason, "breakpoint");
  let { top } = await stackOf(session, stop.threadId);
  assert.deepStrictEqual([top.name, top.line], ["total", pyLine]);
  await cursorIsAt(app, pyLine);

  await command("workbench.action.debug.stepInto");
  stop = (await nextEvent("stopped")).body;
  ({ top } = await stackOf(session, stop.threadId));
  assert.strictEqual(top.source.path, rust);
  await activeEditorIs(rust);

  await command("workbench.action.debug.stepOut");
  stop = (await nextEvent("stopped")).body;
  ({ top } = await stackOf(session, stop.threadId));
  assert.deepStrictEqual([top.name, top.line], ["total", pyLine]);
  await cursorIsAt(app, pyLine);
  log("Step Into went into Rust, Step Out returned to the Python line");

  vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
  await command("workbench.action.debug.continue");
  assert.strictEqual((await nextEvent("exited")).body.exitCode, 0);
  await ended;
  const output = messages
    .filter((m) => m.type === "event" && m.event === "output")
    .map((m) => m.body.output).join("");
  assert.ok(output.includes(`interpreter: ${venvPython}`) && output.includes("total: 30"),
    `program output in the debug console: ${output}`);
  log("the program ran to its end; its output reached the debug console");
}

// "pid": "${command:seam.pickProcess}": the list, the picker, and an attach through it.
async function attachThroughThePicker() {
  const line = lineOf(attachTarget, "# tick-body");
  const target = cp.spawn(venvPython, [attachTarget], { stdio: ["ignore", "pipe", "inherit"] });
  let printed = "";
  target.stdout.on("data", (chunk) => {
    printed += chunk;
  });
  const exited = new Promise((resolve) => target.on("exit", resolve));
  try {
    for (const deadline = Date.now() + 20000; !printed.includes("ready");) {
      assert.ok(Date.now() < deadline, "the program to attach to started");
      await sleep(100);
    }
    const listed = seam.listProcesses();
    log(`Python processes listed: ${listed.map((p) => `${p.pid} ${p.argv.join(" ")}`).join(" | ")}`);
    const entry = listed.find((candidate) => candidate.pid === target.pid);
    assert.ok(entry, "the picker's list has the program");
    assert.deepStrictEqual(entry.argv.slice(1), [attachTarget]);
    assert.strictEqual(listed[0].pid, target.pid, "the newest process comes first");

    breakAt(attachTarget, line);
    cursor = messages.length;
    const started = sessionStarted();
    const starting = vscode.debug.startDebugging(folder, {
      type: "seam", request: "attach", name: "editor check (attach)",
      pid: "${command:seam.pickProcess}",
    });
    // The picker is open now and the start waits for an answer. Its first entry is the
    // program; accept it as Enter would. (Accepting before it has opened does nothing.)
    await shot("6-process-picker");
    let session;
    for (const deadline = Date.now() + 30000; !session;) {
      assert.ok(Date.now() < deadline, "the picker's answer started a session");
      await command("workbench.action.acceptSelectedQuickOpenItem");
      session = await Promise.race([started, sleep(500)]);
    }
    assert.ok(await starting, "the attach session started");
    assert.strictEqual(session.configuration.pid, target.pid, "the picked process, as a number");
    const ended = sessionEnded(session);

    const stop = (await nextEvent("stopped")).body;
    const { top } = await stackOf(session, stop.threadId);
    assert.deepStrictEqual([top.name, top.line], ["tick", line]);
    await cursorIsAt(attachTarget, line);
    log(`attached to pid ${target.pid} through the picker; stopped in ${top.name}`);
    await shot("7-attached");
    // Tell the program to finish, let go of it, and see it end on its own.
    await evaluate(session, top, "globals().update(STOP=True)");
    vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
    await vscode.debug.stopDebugging(session);
    await ended;
    assert.strictEqual(await exited, 0, `the program carried on after the detach: ${printed}`);
    assert.ok(printed.includes("stopped"), printed);
    log("detached; the program ran on and ended by itself");
  } finally {
    target.kill("SIGKILL");
  }
}

// "Seam: Check This Machine" runs the bundled `seam doctor` for the project's interpreter.
async function checkThisMachine() {
  const doctor = await vscode.commands.executeCommand("seam.checkMachine");
  assert.ok(Array.isArray(doctor), "the command reports what it ran");
  assert.deepStrictEqual(doctor.slice(1),
    ["-I", path.join(vscode.extensions.getExtension("seam.seam-debugger").extensionPath, "bundled"),
      "doctor", "--python", venvPython]);
  // The terminal's text cannot be read from here, so the same command is run again.
  const result = cp.spawnSync(doctor[0], doctor.slice(1), { encoding: "utf8", timeout: 120000 });
  assert.strictEqual(result.status, 0, result.stdout + result.stderr);
  assert.ok(result.stdout.trimEnd().endsWith("Seam is ready to use."), result.stdout);
  await sleep(3000);  // let the terminal show its copy before the picture
  await shot("9-check-this-machine");
  log("\"Seam: Check This Machine\" runs the bundled doctor for the project's interpreter");
}

// What the user is told when a session cannot start.
async function refusals() {
  const base = { type: "seam", request: "launch", program: app, console: "internalConsole" };

  // No LLDB. (The extension host's environment is the adapter's.)
  process.env.SEAM_LLDB = "/no/such/lldb";
  try {
    await startRefused(folder, Object.assign({ name: "editor check (no LLDB)" }, base));
    const reply = await refusal("initialize");
    assert.ok(reply.message.startsWith("LLDB 18 or newer is required"), reply.message);
    log(`without LLDB the session is refused: ${reply.message}`);
  } finally {
    delete process.env.SEAM_LLDB;
  }

  // An interpreter that is not there: the adapter runs on another one and says so.
  await startRefused(folder, Object.assign(
    { name: "editor check (no such interpreter)", python: "/no/such/python" }, base));
  let reply = await refusal("launch");
  assert.strictEqual(reply.message, "Python interpreter not found: /no/such/python");

  // seam.adapterCommand is used when set, and a command that cannot run is named.
  const settings = vscode.workspace.getConfiguration("seam");
  await settings.update("adapterCommand", ["/no/such/seam", "dap"], vscode.ConfigurationTarget.Global);
  try {
    await startRefused(folder, Object.assign({ name: "editor check (own adapter)" }, base));
    reply = await refusal("initialize");
    assert.ok(reply.message.includes("cannot start /no/such/seam"), reply.message);
  } finally {
    await settings.update("adapterCommand", undefined, vscode.ConfigurationTarget.Global);
  }

  // A pid that is not one never reaches the adapter.
  const before = seam.problems.length;
  await startRefused(folder, { type: "seam", request: "attach", name: "editor check (bad pid)",
                               pid: "not a pid" });
  assert.deepStrictEqual(seam.problems.slice(before),
    ["Seam: \"pid\" must be the id of a running process; it is \"not a pid\"."]);
  log("a missing LLDB, a missing interpreter, a broken adapter command and a bad pid are explained");
}

// With the Python extension installed, the interpreter selected there is the one
// debugged, whatever else lies around in the folder.
async function interpreterFromThePythonExtension() {
  const selected = path.join(project, "env-b", "bin", "python");
  const python = vscode.extensions.getExtension("ms-python.python");
  assert.ok(python, "the Python extension is installed");
  const api = await python.activate();
  await api.ready;
  log(`Python extension ${python.packageJSON.version}; active before: `
    + api.environments.getActiveEnvironmentPath(folder.uri).path);
  // What "Python: Select Interpreter" does.
  await api.environments.updateActiveEnvironmentPath(selected, folder.uri);
  assert.deepStrictEqual(await seam.findInterpreter(folder),
    { python: selected, source: "the Python extension" });

  const pyLine = lineOf(app, "# step in here");
  breakAt(app, pyLine);
  await command("workbench.view.debug");
  await prepareWindow();
  const session = await start(folder, {
    type: "seam", request: "launch", name: "editor check (Python extension)",
    program: app, args: ["--no-check"], console: "internalConsole",
  });
  assert.strictEqual(session.configuration.python, selected);
  const ended = sessionEnded(session);
  const stop = (await nextEvent("stopped")).body;
  const { top } = await stackOf(session, stop.threadId);
  assert.deepStrictEqual([top.name, top.line], ["total", pyLine]);
  assert.strictEqual(await evaluate(session, top, "__import__('sys').prefix"),
    `'${path.join(project, "env-b")}'`);
  log(`the interpreter selected in the Python extension is debugged: ${selected}`);
  await shot("8-with-the-python-extension");
  vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
  await command("workbench.action.debug.continue");
  assert.strictEqual((await nextEvent("exited")).body.exitCode, 0);
  await ended;
  log(`the Python extension's active interpreter afterwards: `
    + api.environments.getActiveEnvironmentPath(folder.uri).path);
}

async function disassemblyAtTheNativeStop() {
  vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
  vscode.debug.addBreakpoints([new vscode.FunctionBreakpoint("nosource_work")]);
  const session = await start(folder, {
    type: "seam", request: "launch", name: "editor check (disassembly)",
    program: path.join(project, "disassembly.py"), console: "internalConsole",
  });
  const ended = sessionEnded(session);
  const stop = (await nextEvent("stopped")).body;
  const { top } = await stackOf(session, stop.threadId);
  assert.ok(top.name.endsWith("!nosource_work"), top.name);
  assert.ok(top.instructionPointerReference);
  assert.notStrictEqual(top.presentationHint, "subtle");
  // Check the frame the editor actually focused, not just the protocol response.
  const deadline = Date.now() + 20000;
  while (vscode.debug.activeStackItem?.frameId !== top.id && Date.now() < deadline) {
    await sleep(100);
  }
  assert.strictEqual(vscode.debug.activeStackItem?.frameId, top.id,
    "VS Code must focus the stopped native frame instead of the Python caller");
  await vscode.commands.executeCommand("debug.action.openDisassemblyView");
  const listing = await nextMessage("disassembly in the editor", (m) =>
    m.type === "response" && m.command === "disassemble" && m.success);
  assert.ok(listing.body.instructions.some((i) => i.instruction && i.instruction !== "??"));
  await shot("9-native-disassembly");
  await vscode.commands.executeCommand("workbench.action.debug.stepOver");
  const stepped = (await nextEvent("stopped")).body;
  const after = (await stackOf(session, stepped.threadId)).top;
  assert.ok(after.instructionPointerReference, "F10 in disassembly steps native code");
  assert.notStrictEqual(after.instructionPointerReference, top.instructionPointerReference);
  vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
  await command("workbench.action.debug.continue");
  assert.strictEqual((await nextEvent("exited")).body.exitCode, 0);
  await ended;
  log("the native stop is focused, opens disassembly, and steps an instruction");
}

exports.run = async function run() {
  const extension = vscode.extensions.getExtension("seam.seam-debugger");
  assert.ok(extension, "the Seam extension is installed");
  seam = await extension.activate();
  log(`extension ${extension.id} ${extension.packageJSON.version} in ${extension.extensionPath}`);
  folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  assert.ok(folder && folder.uri.fsPath === project, "the project is open as the workspace");
  if (process.env.SEAM_LOG) {
    await vscode.workspace.getConfiguration("seam")
      .update("logFile", process.env.SEAM_LOG, vscode.ConfigurationTarget.Global);
  }
  vscode.debug.registerDebugAdapterTrackerFactory("seam", {
    createDebugAdapterTracker() {
      return {
        onDidSendMessage(message) {
          messages.push(message);
          wake();
        },
      };
    },
  });

  try {
    if (process.env.SEAM_SUITE === "python-extension") {
      await interpreterFromThePythonExtension();
    } else {
      await pressF5();
      await stepInTheDebugConsole();
      await attachThroughThePicker();
      await disassemblyAtTheNativeStop();
      await checkThisMachine();
      await refusals();
    }
  } catch (err) {
    await shot(`failed-${process.env.SEAM_SUITE || "first-run"}`);
    throw err;
  }
};
