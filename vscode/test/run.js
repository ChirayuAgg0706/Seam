// Editor check: install the packaged extension into a real VS Code and debug
// examples/pyo3-demo with it. Run under a display (CI uses xvfb):
//
//   cd vscode/test && npm install --no-save @vscode/test-electron
//   SEAM_VSIX=../seam-debugger-0.1.0.vsix SEAM_DEMO_DIR=../../examples/pyo3-demo \
//     SEAM_BIN=~/.venvs/seam/bin/seam xvfb-run -a node run.js
const cp = require("child_process");
const path = require("path");
const {
  downloadAndUnzipVSCode,
  resolveCliArgsFromVSCodeExecutablePath,
  runTests,
} = require("@vscode/test-electron");

async function main() {
  for (const name of ["SEAM_VSIX", "SEAM_DEMO_DIR", "SEAM_BIN"]) {
    if (!process.env[name]) {
      throw new Error(`set ${name}`);
    }
  }
  const vscodeExecutablePath = await downloadAndUnzipVSCode("stable");
  const [cli, ...args] = resolveCliArgsFromVSCodeExecutablePath(vscodeExecutablePath);
  const install = cp.spawnSync(
    cli, [...args, "--install-extension", path.resolve(process.env.SEAM_VSIX)],
    { stdio: "inherit" });
  if (install.status !== 0) {
    throw new Error("could not install the .vsix");
  }
  // The extension under test is the installed .vsix. The "development" extension is an
  // empty driver whose only job is to carry the test into the extension host.
  await runTests({
    vscodeExecutablePath,
    extensionDevelopmentPath: path.resolve(__dirname, "driver"),
    extensionTestsPath: path.resolve(__dirname, "suite.js"),
    launchArgs: [path.resolve(process.env.SEAM_DEMO_DIR), "--disable-workspace-trust"],
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
