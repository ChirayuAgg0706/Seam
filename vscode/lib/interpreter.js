// Which interpreter to debug with when the launch configuration does not name one.
// Nothing here needs VS Code: the caller passes in what it learned from the editor.
const fs = require("fs");
const os = require("os");
const path = require("path");

const VENV_DIRECTORIES = [".venv", "venv"];

function venvInterpreter(folder, exists = fs.existsSync) {
  for (const name of VENV_DIRECTORIES) {
    const python = path.join(folder, name, "bin", "python");
    if (exists(python)) {
      return python;
    }
  }
  return undefined;
}

// The value of python.defaultInterpreterPath as a path. "python" is what the Python
// extension ships as the default and says nothing about this project, so it counts as
// unset. Resolve the workspace path here: the machine check does not go through
// VS Code's debug-configuration variable substitution.
function settingInterpreter(value, folder, home = os.homedir()) {
  if (typeof value !== "string" || !value.trim() || value.trim() === "python") {
    return undefined;
  }
  value = value.trim();
  if (folder) {
    value = value.replace(/\$\{workspaceFolder\}/g, () => folder);
  }
  if (value === "~" || value.startsWith("~/")) {
    return path.join(home, value.slice(1));
  }
  // A bare name is looked up on PATH by the adapter; a relative path is relative to
  // the folder, as the Python extension reads it.
  if (folder && value.includes("/") && !path.isAbsolute(value) && !value.startsWith("${")) {
    return path.join(folder, value);
  }
  return value;
}

// The first rule that gives an answer wins. `sources` holds:
//   pythonExtension  async function: the Python extension's active interpreter for the
//                    folder, or undefined (not installed, nothing selected, no answer)
//   setting          the python.defaultInterpreterPath setting
//   folder           the workspace folder's path, if there is one
async function findInterpreter(sources) {
  const selected = sources.pythonExtension ? await sources.pythonExtension() : undefined;
  if (selected) {
    return { python: selected, source: "the Python extension" };
  }
  const setting = settingInterpreter(sources.setting, sources.folder);
  if (setting) {
    return { python: setting, source: "the python.defaultInterpreterPath setting" };
  }
  const venv = sources.folder && venvInterpreter(sources.folder, sources.exists);
  if (venv) {
    return { python: venv, source: "the virtual environment in the workspace folder" };
  }
  return { python: "python3", source: "the default" };
}

module.exports = { findInterpreter, settingInterpreter, venvInterpreter };
