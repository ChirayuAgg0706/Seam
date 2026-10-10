# Seam extension 0.1.5

Fix **Seam: Check This Machine** incorrectly reporting that Python is missing when
`python.defaultInterpreterPath` uses `${workspaceFolder}`. The command now resolves
the project directory before checking the interpreter.

Both Linux x64 and Apple Silicon macOS ARM64 packages include the fix. They keep
the released Seam debugger 0.1.2 and its platform helpers unchanged. No PyPI update
is needed.

For manual installation, download the VSIX for your platform and run **Extensions:
Install from VSIX...** in VS Code. On Windows, open a WSL window and install the
Linux package on the WSL side.

Marketplace availability depends on upload and validation of both platform files.
