# Seam for VS Code

Debug Python and C, C++ or Rust together on Linux x86-64, including WSL.

Set breakpoints on both sides of a native call, step across the boundary, and inspect
Python and native frames in one call stack. Supports C API, PyO3, pybind11, nanobind
and Cython extension modules.

![Rust locals and Python callers at a native breakpoint](images/python-rust-stack.png)

An actual VS Code session from the packaged-extension acceptance tests.

## What you need

- Linux x86-64 with glibc. On Windows, open the project in WSL: the extension then runs on the
  Linux side.
- LLDB with its Python scripting support; prefer 19 or 20 (Debian/Ubuntu:
  `apt install lldb-19`). Seam tries `lldb-20`, then `lldb-19`, before plain `lldb`.
  An explicit `SEAM_LLDB` overrides this choice. LLDB 18 remains supported but can
  lose sessions when threaded programs start child processes; the machine check warns.
- CPython 3.12, 3.13 or 3.14 for your program. Python 3.15.0rc3 also passes the
  compatibility tests; later 3.15 builds have not been validated for this release.
- Build your native extension with debug information (`-g`; for Rust use a debug
  build or `[profile.release] debug = true`) for source breakpoints and stepping.

Seam itself needs no separate pip installation or compiler. The extension carries its debug adapter and the
small compiled helper it loads into your program, and runs the adapter with a Python it
finds on the machine: the interpreter being debugged, else `python3`. If no Python 3.12+
or no LLDB is found, the session does not start and VS Code shows the reason.

To check a machine before the first session, run **Seam: Check This Machine** from the
Command Palette. It looks at LLDB, the helper, ptrace permission and the project's
interpreter, runs one real debug session, and says what to fix.

## Start debugging

Install LLDB first (Ubuntu 24.04: `sudo apt-get install -y lldb-19`). Download the Linux
VSIX from the [0.1.0 release](https://github.com/ChirayuAgg0706/Seam/releases/tag/v0.1.0)
and run **Extensions: Install from VSIX…**. Windows users do this in a WSL window.

Run **Seam: Check This Machine**, open a Python file, set a breakpoint on a native call
and press F5. Step Into enters native code and Step Out returns to Python;
no `launch.json` is needed. To keep a configuration:

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
- The Breakpoints view offers five exception stops: uncaught Python exceptions (on by
  default), raised Python exceptions, user-unhandled Python exceptions, C++ throw and Rust panic.
- The program runs in the integrated terminal, so it can read input. Set
  `"console": "internalConsole"` to send its output to the Debug Console instead.
- A crash in native code (segfault, abort) stops at the faulting line, with the Python
  frames that led there.

At a native stop, select a Python caller to inspect its locals and globals without
running Python in the paused process:

![Python locals and globals while the program is stopped in Rust](images/python-variables-at-native-stop.png)

Without native source, open **Disassembly View** on the native frame and step by instruction:

![VS Code disassembly at a native breakpoint](images/native-disassembly.png)

The [main README](https://github.com/ChirayuAgg0706/Seam#readme) lists every launch option.

## Limitations

- Linux x86-64 is the supported platform, including WSL. Native Windows, macOS,
  ARM, Alpine/musl, free-threaded Python, PyPy and the experimental JIT are outside
  the validated scope.
- At native stops, Python values are read from memory. Simple built-in types show
  values; other objects show their type and address. Python evaluation, object
  expansion and mutation require a safe Python stop.
- Optimised native code can lose source lines, locals or frames. A debug build gives
  the most reliable source stepping.
- Child processes run but are not debugged. Use LLDB 19 or 20 to avoid LLDB 18's
  threaded-child-process session failure.
- An expression that crashes or times out can damage the target; restart the session.
- Attach needs ptrace permission and a responsive interpreter; blocked-process
  attach and remote debugging are outside the supported scope.
- Disassembly breakpoints are not supported. Thread-heavy workloads can run about
  twice as slowly; there is no universal low-overhead guarantee.

See the [complete limitations](https://github.com/ChirayuAgg0706/Seam#limitations).

## Settings

| Setting | Meaning |
|---|---|
| `seam.adapterCommand` | Empty (the default): use the adapter inside the extension. Set it to run a Seam you installed yourself, for example `["/path/to/venv/bin/seam", "dap"]`. |
| `seam.logFile` | If set, the adapter writes a protocol log to this file. |

If something misbehaves, set `seam.logFile` to a file path, reproduce the problem, and
include that file (and the one next to it ending in `.lldb`) in a
[bug report](https://github.com/ChirayuAgg0706/Seam/issues), together with
what the "Seam" output channel shows.

## Building from source

`scripts/build-vsix.sh` builds `seam-debugger-linux-x64-<version>.vsix`. The package is
specific to Linux x86-64 because of the compiled helper inside. Given a wheel
(`scripts/build-vsix.sh dist/x.whl`),
the script takes the adapter and helper from it instead of compiling; the release
workflow passes the manylinux wheel.
