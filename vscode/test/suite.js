// Runs inside VS Code's extension host (see run.js). Drives two debug sessions on
// examples/pyo3-demo through VS Code's own debug API and commands, and checks what VS Code
// itself ends up showing: the stopped frame, the file it opened, the session's end.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vscode = require("vscode");

const TIMEOUT_MS = 90000;

function lineOf(file, marker) {
  const lines = fs.readFileSync(file, "utf8").split("\n");
  const index = lines.findIndex((text) => text.includes(marker));
  assert.ok(index >= 0, `marker ${marker} in ${file}`);
  return index + 1;
}

function log(message) {
  console.log(`[seam editor check] ${message}`);
}

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
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

async function topFrame(session, threadId) {
  const reply = await session.customRequest("stackTrace", { threadId });
  return { frames: reply.stackFrames, top: reply.stackFrames[0] };
}

async function start(folder, configuration) {
  const started = new Promise((resolve) => {
    const subscription = vscode.debug.onDidStartDebugSession((session) => {
      subscription.dispose();
      resolve(session);
    });
  });
  assert.ok(await vscode.debug.startDebugging(folder, configuration), "the session started");
  return started;
}

exports.run = async function run() {
  const demo = process.env.SEAM_DEMO_DIR && path.resolve(process.env.SEAM_DEMO_DIR);
  const script = path.join(demo, "demo.py");
  const rust = path.join(demo, "src", "lib.rs");
  const pyLine = lineOf(script, "# step in here");

  const extension = vscode.extensions.getExtension("seam.seam-debugger");
  assert.ok(extension, "the Seam extension is installed");
  log(`extension ${extension.id} ${extension.packageJSON.version}`);

  const settings = vscode.workspace.getConfiguration("seam");
  await settings.update("adapterCommand", [process.env.SEAM_BIN, "dap"],
    vscode.ConfigurationTarget.Global);
  if (process.env.SEAM_LOG) {
    await settings.update("logFile", process.env.SEAM_LOG, vscode.ConfigurationTarget.Global);
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

  const folder = vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders[0];
  assert.ok(folder, "the demo folder is open as the workspace");
  vscode.debug.addBreakpoints([
    new vscode.SourceBreakpoint(
      new vscode.Location(vscode.Uri.file(script), new vscode.Position(pyLine - 1, 0))),
  ]);
  const python = process.env.SEAM_DEMO_PYTHON || "/usr/bin/python3";

  // ---- Session 1: the debug console, stepping across the boundary with VS Code's
  // own commands.
  let session = await start(folder, {
    type: "seam", request: "launch", name: "editor check (debug console)",
    program: script, python, console: "internalConsole",
  });
  let stop = (await nextEvent("stopped")).body;
  assert.strictEqual(stop.reason, "breakpoint");
  let { top } = await topFrame(session, stop.threadId);
  assert.deepStrictEqual([top.name, top.line], ["report", pyLine]);
  let editor = await activeEditorIs(script);
  assert.strictEqual(editor.selection.active.line, pyLine - 1, "cursor on the stopped line");
  log("stopped at the Python breakpoint; VS Code shows demo.py at that line");

  await vscode.commands.executeCommand("workbench.action.debug.stepInto");
  stop = (await nextEvent("stopped")).body;
  let stack = await topFrame(session, stop.threadId);
  assert.strictEqual(stack.top.source.path, rust);
  assert.ok(stack.top.name.includes("sum_squares"), stack.top.name);
  const names = stack.frames.map((frame) => frame.name);
  assert.ok(names.includes("report") && names[names.length - 1] === "<module>", names.join());
  await activeEditorIs(rust);
  log(`Step Into landed in ${stack.top.name}; VS Code opened src/lib.rs; stack: ${names.join(" < ")}`);

  await vscode.commands.executeCommand("workbench.action.debug.stepOut");
  stop = (await nextEvent("stopped")).body;
  ({ top } = await topFrame(session, stop.threadId));
  assert.deepStrictEqual([top.name, top.line], ["report", pyLine]);
  await activeEditorIs(script);
  log("Step Out returned to the Python line");

  vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
  await vscode.commands.executeCommand("workbench.action.debug.continue");
  const exited = (await nextEvent("exited")).body;
  assert.strictEqual(exited.exitCode, 0);
  await nextEvent("terminated");
  const output = messages
    .filter((m) => m.type === "event" && m.event === "output")
    .map((m) => m.body.output).join("");
  assert.ok(output.includes("squares(5) = 30"), `program output in the debug console: ${output}`);
  log("the program ran to its end; its output reached the debug console");

  // ---- Session 2: the extension's default, the integrated terminal. VS Code has to
  // answer the adapter's runInTerminal request for the program to start at all.
  vscode.debug.addBreakpoints([
    new vscode.SourceBreakpoint(
      new vscode.Location(vscode.Uri.file(script), new vscode.Position(pyLine - 1, 0))),
  ]);
  session = await start(folder, {
    type: "seam", request: "launch", name: "editor check (terminal)", program: script, python,
  });
  assert.strictEqual(session.configuration.console, "integratedTerminal",
    "the extension defaults to the integrated terminal");
  stop = (await nextEvent("stopped")).body;
  ({ top } = await topFrame(session, stop.threadId));
  assert.deepStrictEqual([top.name, top.line], ["report", pyLine]);
  assert.ok(vscode.window.terminals.some((terminal) => terminal.name.includes("Seam")),
    "VS Code created a terminal for the program");
  vscode.debug.removeBreakpoints(vscode.debug.breakpoints);
  await vscode.commands.executeCommand("workbench.action.debug.continue");
  assert.strictEqual((await nextEvent("exited")).body.exitCode, 0);
  log("the program ran in VS Code's integrated terminal and stopped at the breakpoint");
};
