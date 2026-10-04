# Seam for VS Code

Mixed-mode debugging for Python programs with native (C, C++, Rust) extensions on Linux:
one call stack with Python and native frames interleaved, breakpoints on both sides, and
stepping across the boundary.

This extension only starts the Seam debug adapter. Install Seam itself first (see the
main README) so that `seam dap` works in a terminal, or point the `seam.adapterCommand`
setting at it. `seam doctor` checks the installation.

Minimal `launch.json` entry:

```json
{
  "type": "seam",
  "request": "launch",
  "name": "Seam: current file",
  "program": "${file}",
  "python": "${workspaceFolder}/.venv/bin/python"
}
```

- Set breakpoints in `.py`, `.c`, `.cpp`, `.rs` and `.pyx` files as usual. Conditions, hit
  counts and log messages work on both sides.
- The Breakpoints view offers four exception stops: uncaught Python exceptions (on by
  default), raised Python exceptions, C++ throw and Rust panic.
- The program runs in the integrated terminal, so it can read input. Set
  `"console": "internalConsole"` to send its output to the Debug Console instead.
- A crash in native code (segfault, abort) stops at the faulting line, with the Python
  frames that led there.

If something misbehaves, set `seam.logFile` to a file path, reproduce the problem, and
include that file (and the one next to it ending in `.lldb`) in the report.
