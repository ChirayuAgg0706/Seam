# Seam for VS Code

Mixed-mode debugging for Python programs with native (C, C++, Rust) extensions on Linux:
one call stack with Python and native frames interleaved, breakpoints on both sides, and
stepping across the boundary.

## What you need

- Linux x86-64. On Windows, open the project in WSL: the extension then runs on the
  Linux side.
- LLDB 18 or newer with its Python scripting support (Debian/Ubuntu: `apt install lldb`).
- A CPython 3.12 or newer for your program.

Nothing else has to be installed. The extension carries Seam's debug adapter and the
small compiled helper it loads into your program, and runs the adapter with a Python it
finds on the machine: the interpreter being debugged, else `python3`. If no Python 3.12+
or no LLDB is found, the session does not start and VS Code shows the reason.

To check a machine before the first session, run **Seam: Check This Machine** from the
Command Palette. It looks at LLDB, the helper, ptrace permission and the project's
interpreter, runs one real debug session, and says what to fix.

## Start debugging

Open a Python file and press F5; no `launch.json` is needed. To keep a configuration:

```json
{
  "type": "seam",
  "request": "launch",
  "name": "Seam: current file",
  "program": "${file}"
}
```

**Which interpreter.** Without `"python"` in the configuration, Seam debugs with the
project's interpreter: the one selected in the Python extension (`ms-python.python`) if
that extension is installed; otherwise the `python.defaultInterpreterPath` setting;
otherwise `.venv` or `venv` in the workspace folder; otherwise `python3`. Set `"python"`
to name one yourself. The choice is logged in the "Seam" output channel.

**Attach** to a running Python process of yours, picked from a list:

```json
{
  "type": "seam",
  "request": "attach",
  "name": "Seam: attach",
  "pid": "${command:seam.pickProcess}"
}
```

The list shows what each process runs, its pid, interpreter and working directory, newest
first. A number works as `"pid"` too. Attaching needs ptrace permission for a process that
is not a child (`/proc/sys/kernel/yama/ptrace_scope` must be 0).

## While debugging

- Set breakpoints in `.py`, `.c`, `.cpp`, `.rs` and `.pyx` files as usual. Conditions, hit
  counts and log messages work on both sides.
- The Breakpoints view offers four exception stops: uncaught Python exceptions (on by
  default), raised Python exceptions, C++ throw and Rust panic.
- The program runs in the integrated terminal, so it can read input. Set
  `"console": "internalConsole"` to send its output to the Debug Console instead.
- A crash in native code (segfault, abort) stops at the faulting line, with the Python
  frames that led there.

The main README lists every launch option.

## Settings

| Setting | Meaning |
|---|---|
| `seam.adapterCommand` | Empty (the default): use the adapter inside the extension. Set it to run a Seam you installed yourself, for example `["/path/to/venv/bin/seam", "dap"]`. |
| `seam.logFile` | If set, the adapter writes a protocol log to this file. |

If something misbehaves, set `seam.logFile` to a file path, reproduce the problem, and
include that file (and the one next to it ending in `.lldb`) in the report, together with
what the "Seam" output channel shows.

## The package

`scripts/build-vsix.sh` builds `seam-debugger-linux-x64-<version>.vsix`. The package is
specific to Linux x86-64 because of the compiled helper inside. The helper uses a handful
of C library functions; built on Ubuntu 24.04 it needs glibc 2.4 or newer (the script
prints what the one it built needs). Given a wheel (`scripts/build-vsix.sh dist/x.whl`),
the script takes the adapter and helper from it instead of compiling; the release
workflow passes the manylinux wheel.
