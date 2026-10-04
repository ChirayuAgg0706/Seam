// Seam's VS Code extension is a thin shim: it starts the Seam debug adapter that comes
// with it and lets VS Code talk the Debug Adapter Protocol to it. All debugging logic
// lives in the adapter. What is decided here is what only the editor knows: which
// interpreter the project uses, and which process the user wants to attach to.
const path = require("path");
const vscode = require("vscode");

const adapter = require("./lib/adapter");
const interpreter = require("./lib/interpreter");
const processes = require("./lib/processes");

const PYTHON_EXTENSION = "ms-python.python";
// Activating the Python extension can take a while on a cold start; F5 must not hang on it.
const PYTHON_EXTENSION_PATIENCE_MS = 15000;
const NO_BUNDLE = "Seam: this copy of the extension has no debug adapter inside (it is "
  + "put there by scripts/build-vsix.sh). Install the packaged .vsix, or set "
  + "\"seam.adapterCommand\" to your own installation, e.g. [\"seam\", \"dap\"].";

let output;            // the "Seam" output channel
const problems = [];   // every message shown to the user, newest last (read by the editor check)

function log(text) {
  output.appendLine(text);
}

// Tell the user about a problem without waiting for them to dismiss the message.
function tell(message) {
  problems.push(message);
  log(message);
  vscode.window.showErrorMessage(message);
}

function within(milliseconds, promise) {
  let timer;
  const late = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`no answer in ${milliseconds / 1000} s`)),
      milliseconds);
  });
  return Promise.race([promise, late]).finally(() => clearTimeout(timer));
}

// The interpreter selected in the Python extension, if that extension is installed.
// Seam does not depend on it: VSCodium users may well not have it.
async function selectedInterpreter(folder) {
  const extension = vscode.extensions.getExtension(PYTHON_EXTENSION);
  if (!extension) {
    return undefined;
  }
  const resource = folder ? folder.uri : undefined;
  const ask = async () => {
    const api = extension.isActive ? extension.exports : await extension.activate();
    if (!api) {
      return undefined;
    }
    await api.ready;
    if (api.environments) {
      const active = api.environments.getActiveEnvironmentPath(resource);
      const environment = await api.environments.resolveEnvironment(active);
      const uri = environment && environment.executable && environment.executable.uri;
      if (uri) {
        return uri.fsPath;
      }
    }
    // Versions of the Python extension from before its environments API.
    const details = api.settings && api.settings.getExecutionDetails
      && api.settings.getExecutionDetails(resource);
    const command = details && details.execCommand;
    return command && command.length === 1 && command[0] !== "python" ? command[0] : undefined;
  };
  try {
    return await within(PYTHON_EXTENSION_PATIENCE_MS, ask());
  } catch (err) {
    log(`the Python extension gave no interpreter: ${err.message}`);
    return undefined;
  }
}

function findInterpreter(folder) {
  return interpreter.findInterpreter({
    pythonExtension: () => selectedInterpreter(folder),
    setting: vscode.workspace.getConfiguration("python", folder ? folder.uri : undefined)
      .get("defaultInterpreterPath"),
    folder: folder ? folder.uri.fsPath : undefined,
  });
}

class SeamAdapterFactory {
  constructor(extensionPath) {
    this.extensionPath = extensionPath;
  }

  async createDebugAdapterDescriptor(session, _executable) {
    const settings = vscode.workspace.getConfiguration("seam");
    // The program inherits this environment, so nothing is added to it but the log.
    const env = Object.assign({}, process.env);
    if (settings.get("logFile")) {
      env.SEAM_LOG = settings.get("logFile");
    }
    let command = settings.get("adapterCommand");
    if (!Array.isArray(command) || !command.length) {
      try {
        if (!adapter.hasBundle(this.extensionPath)) {
          throw new Error(NO_BUNDLE);
        }
        const python = await adapter.findLauncherPython(
          adapter.launcherCandidates(session.configuration));
        command = adapter.bundledCommand(python, this.extensionPath);
      } catch (err) {
        // VS Code shows what is thrown here as the reason the session did not start.
        problems.push(err.message);
        log(err.message);
        throw err;
      }
    }
    log(`adapter: ${command.join(" ")}`);
    return new vscode.DebugAdapterInlineImplementation(
      new adapter.AdapterProcess(command, env, new vscode.EventEmitter(), {
        log: (text) => output.append(text),
        failed: (text) => tell(`Seam: ${text}`),
      }));
  }
}

class SeamConfigurationProvider {
  async resolveDebugConfiguration(folder, config) {
    // F5 with no launch.json: debug the active Python file.
    if (!config.type && !config.request && !config.name) {
      const editor = vscode.window.activeTextEditor;
      if (!editor || editor.document.languageId !== "python") {
        return vscode.window
          .showInformationMessage("Seam: open a Python file to debug it.")
          .then(() => undefined);
      }
      config.type = "seam";
      config.request = "launch";
      config.name = "Seam: current file";
      config.program = "${file}";
    }
    if (config.request === "launch") {
      if (!config.program && !config.module) {
        return vscode.window
          .showErrorMessage("Seam: set \"program\" or \"module\" in the launch configuration.")
          .then(() => undefined);
      }
      if (!config.cwd && folder) {
        config.cwd = folder.uri.fsPath;
      }
      if (!config.python) {
        // The project's interpreter, as the editor knows it. Decided before VS Code
        // substitutes variables, so a ${workspaceFolder} in the setting is filled in.
        const found = await findInterpreter(folder);
        config.python = found.python;
        log(`interpreter: ${found.python} (from ${found.source})`);
      }
      if (!config.console) {
        // In VS Code the program gets a real terminal unless told otherwise, so that
        // input() works. (The adapter's own default is the debug console.)
        config.console = "integratedTerminal";
      }
    }
    return config;
  }

  // By now ${command:seam.pickProcess} has been replaced by what the picker returned:
  // text, where a pid written into launch.json by hand is a number.
  resolveDebugConfigurationWithSubstitutedVariables(_folder, config) {
    if (config.request === "attach" && typeof config.pid === "string") {
      if (!/^\s*[1-9]\d*\s*$/.test(config.pid)) {
        tell(`Seam: "pid" must be the id of a running process; it is "${config.pid}".`);
        return undefined;
      }
      config.pid = Number(config.pid);
    }
    return config;
  }
}

// Seam's own processes are Python too (the adapter's launcher, the holder that sits in
// the program's terminal); attaching the debugger to itself is never what is wanted.
function isSeamItself(entry, extensionPath) {
  const [, first, second] = entry.argv;
  return entry.argv.some((argument) => argument.startsWith(extensionPath + path.sep)
                                       || argument.endsWith("/seam/terminal.py"))
    || (path.basename(first || "") === "seam" && second === "dap");
}

function listProcesses(extensionPath) {
  return processes.listPythonProcesses({ ignore: (entry) => isSeamItself(entry, extensionPath) });
}

// "pid": "${command:seam.pickProcess}" in an attach configuration.
async function pickProcess(extensionPath) {
  const found = listProcesses(extensionPath);
  if (!found.length) {
    tell("Seam: none of your running processes is a Python program.");
    return undefined;
  }
  const picked = await vscode.window.showQuickPick(found.map(processes.pickItem), {
    placeHolder: "Select the Python process to attach to",
    matchOnDescription: true,
    matchOnDetail: true,
  });
  // Nothing picked: VS Code abandons the session without a word, which is right.
  return picked ? String(picked.pid) : undefined;
}

function activate(context) {
  output = vscode.window.createOutputChannel("Seam");
  context.subscriptions.push(
    output,
    vscode.debug.registerDebugAdapterDescriptorFactory("seam",
      new SeamAdapterFactory(context.extensionPath)),
    vscode.debug.registerDebugConfigurationProvider("seam", new SeamConfigurationProvider()),
    vscode.commands.registerCommand("seam.pickProcess", () => pickProcess(context.extensionPath))
  );
  // Not an API for other extensions: this is what CI's editor check looks at.
  return {
    findInterpreter,
    listProcesses: () => listProcesses(context.extensionPath),
    problems,
  };
}

function deactivate() {}

module.exports = { activate, deactivate };
