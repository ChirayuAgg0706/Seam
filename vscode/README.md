# Seam for VS Code

Mixed-mode debugging for Python programs with native (C, C++, Rust) extensions on Linux.

This extension only starts the Seam debug adapter; install Seam itself first (see the
main README) so that `seam dap` works in a terminal, or point the `seam.adapterCommand`
setting at it.

Minimal `launch.json` entry:

```json
{
  "type": "seam",
  "request": "launch",
  "name": "Seam: current file",
  "program": "${file}",
  "python": "python3"
}
```

Set breakpoints in `.py`, `.c`, `.cpp`, `.rs` and `.pyx` files as usual.
