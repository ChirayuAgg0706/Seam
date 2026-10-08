// Editor check: install the packaged extension into a real VS Code and use it the way
// someone does who has just installed the .vsix: a project folder with a virtual
// environment, no Seam installed anywhere else, no "python" in any configuration.
// Run under a display (CI uses xvfb):
//
//   cd vscode/test && npm install --no-save @vscode/test-electron
//   SEAM_VSIX=../seam-debugger-linux-x64-0.1.0.vsix SEAM_DEMO_DIR=../../examples/pyo3-demo \
//     xvfb-run -a -s "-screen 0 1600x1000x24" node run.js
//
// SEAM_DEMO_DIR is the built examples/pyo3-demo (its seam_demo.so is the project's
// native module). SEAM_SHOTS=<directory> keeps pictures of the window at a few stops
// (needs xdotool and ImageMagick's `import`). `node run.js python-extension` installs
// the Python extension from the Marketplace as well and checks that the interpreter
// selected there is the one debugged.
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const {
  downloadAndUnzipVSCode,
  resolveCliArgsFromVSCodeExecutablePath,
  runTests,
} = require("@vscode/test-electron");

// What a first look at the editor does not need: tips, recommendations, the chat pane.
const SETTINGS = {
  "workbench.startupEditor": "none",
  "workbench.tips.enabled": false,
  "workbench.secondarySideBar.defaultVisibility": "hidden",
  "chat.disableAIFeatures": true,
  "chat.commandCenter.enabled": false,
  "extensions.ignoreRecommendations": true,
  "extensions.autoUpdate": false,
  "update.mode": "none",
  "telemetry.telemetryLevel": "off",
  "git.enabled": false,
  "editor.minimap.enabled": false,
  "python.experiments.enabled": false,
};

function makeProject(demo) {
  const project = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "seam-check-")), "orders");
  fs.mkdirSync(project);
  fs.copyFileSync(path.join(__dirname, "project", "app.py"), path.join(project, "app.py"));
  fs.copyFileSync(path.join(demo, "seam_demo.so"), path.join(project, "seam_demo.so"));
  const include = cp.execFileSync("python3", ["-c",
    "import sysconfig; print(sysconfig.get_paths()['include'])"], { encoding: "utf8" }).trim();
  const stripped = path.join(project, "seam_nosource.abi3.so");
  const shared = process.platform === "darwin"
    ? ["-bundle", "-undefined", "dynamic_lookup"] : ["-shared", "-fPIC"];
  cp.execFileSync("gcc", [...shared, "-O2", "-I", include,
    path.resolve(__dirname, "../../tests/ext/nosource/seam_nosource.c"), "-o", stripped]);
  cp.execFileSync("strip", [...(process.platform === "darwin" ? ["-x"] : []), stripped]);
  fs.writeFileSync(path.join(project, "disassembly.py"),
    "import seam_nosource\nprint(seam_nosource.work(2))\n");
  // The project's own environment, and a second one to select in the Python extension.
  for (const name of [".venv", "env-b"]) {
    cp.execFileSync("python3", ["-m", "venv", "--without-pip", path.join(project, name)],
      { stdio: "inherit" });
  }
  return project;
}

function install(cli, args, extension) {
  const done = cp.spawnSync(cli, [...args, "--install-extension", extension], { stdio: "inherit" });
  if (done.status !== 0) {
    throw new Error(`could not install ${extension}`);
  }
}

async function main() {
  for (const name of ["SEAM_VSIX", "SEAM_DEMO_DIR"]) {
    if (!process.env[name]) {
      throw new Error(`set ${name}`);
    }
  }
  const suite = process.argv[2] || "first-run";
  const demo = path.resolve(process.env.SEAM_DEMO_DIR);
  const project = makeProject(demo);
  const vscodeExecutablePath = await downloadAndUnzipVSCode("stable");
  const [cli, ...args] = resolveCliArgsFromVSCodeExecutablePath(vscodeExecutablePath);
  const userData = args.find((arg) => arg.startsWith("--user-data-dir="));
  if (!userData) {
    throw new Error(`no profile directory among ${args.join(" ")}`);
  }
  const settings = path.join(userData.slice("--user-data-dir=".length), "User", "settings.json");
  fs.mkdirSync(path.dirname(settings), { recursive: true });
  fs.writeFileSync(settings, JSON.stringify(SETTINGS, null, 2));

  install(cli, args, path.resolve(process.env.SEAM_VSIX));
  if (suite === "python-extension") {
    install(cli, args, "ms-python.python");
  }
  // The extension under test is the installed .vsix. The "development" extension is an
  // empty driver whose only job is to carry the test into the extension host.
  await runTests({
    vscodeExecutablePath,
    extensionDevelopmentPath: path.resolve(__dirname, "driver"),
    extensionTestsPath: path.resolve(__dirname, "suite.js"),
    extensionTestsEnv: {
      SEAM_SUITE: suite,
      SEAM_PROJECT: project,
      SEAM_DEMO_DIR: demo,
      SEAM_REPOSITORY: path.resolve(__dirname, "..", ".."),
    },
    // Software rendering: what a hardware-less display can show, and a screenshot see.
    launchArgs: [project, "--disable-workspace-trust", "--disable-gpu"],
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
