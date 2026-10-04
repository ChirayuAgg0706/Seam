// Seam's VS Code extension is a thin shim: it starts `seam dap` and lets VS Code talk
// the Debug Adapter Protocol to it. All debugging logic lives in the adapter.
const vscode = require("vscode");

class SeamAdapterFactory {
  createDebugAdapterDescriptor(_session, _executable) {
    const config = vscode.workspace.getConfiguration("seam");
    const command = config.get("adapterCommand") || ["seam", "dap"];
    const logFile = config.get("logFile");
    const env = Object.assign({}, process.env);
    if (logFile) {
      env.SEAM_LOG = logFile;
    }
    return new vscode.DebugAdapterExecutable(command[0], command.slice(1), { env });
  }
}

class SeamConfigurationProvider {
  resolveDebugConfiguration(folder, config) {
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
        config.python = "python3";
      }
      if (!config.console) {
        // In VS Code the program gets a real terminal unless told otherwise, so that
        // input() works. (The adapter's own default is the debug console.)
        config.console = "integratedTerminal";
      }
    }
    return config;
  }
}

function activate(context) {
  context.subscriptions.push(
    vscode.debug.registerDebugAdapterDescriptorFactory("seam", new SeamAdapterFactory()),
    vscode.debug.registerDebugConfigurationProvider("seam", new SeamConfigurationProvider())
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
