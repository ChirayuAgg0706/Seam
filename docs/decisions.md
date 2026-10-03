# Design decisions

One short entry per significant decision, newest last.

## 1. Seam runs inside LLDB's embedded Python interpreter

LLDB's Python module cannot be imported from a normal Python process on Ubuntu 24.04
(broken `_lldb` symlink, then `PyImport_AppendInittab` abort under 3.12). Seam's
controller ("the adapter") is therefore a script loaded with
`lldb --batch -o "command script import …"`. It implements its own DAP server; a small
`seam` launcher starts LLDB and bridges stdio to it.

- Rejected: proxying `lldb-dap` (no control over stops or stack frames).
- Rejected: bundling LLDB (about 100 MB and a build pipeline).
- Cost: adapter code is stdlib-only and must run on whatever Python LLDB embeds;
  users need `lldb` 18 or newer.

## 2. The in-process helper is one stable-ABI C extension plus a Python agent

`seam/_trap.abi3.so` is built against the limited API (3.12), so one binary serves
3.12–3.14. It contains `seam_trap()`, the hot `LINE` callback, and the request/response
buffers LLDB uses to talk to the agent. Stepping logic lives in Python
(`seam/target/agent.py`); it only runs while a step is in progress, so its cost is not
on the hot path.

## 3. The helper is injected at `Py_RunMain`, not by wrapping the script

On launch the adapter sets a one-shot breakpoint on `Py_RunMain`. At that point the
interpreter is initialised, the main thread holds the GIL and no Python code is running,
so it is a safe point. The adapter calls `PyRun_SimpleStringFlags` there to import the
agent. `sys.argv`, `__main__` and `python -m`/`-c` behave exactly as without Seam.

## 4. Adapter ⇄ agent calls go through memory buffers, not expression strings

The adapter writes a JSON request into a buffer owned by the helper, calls the no-argument
C function `seam_dispatch()` on the trapped thread, and reads the JSON reply back from
memory. This avoids quoting problems and keeps LLDB expressions trivial (they need no
debug info).

## 4a. The debugged program runs on a pty owned by the adapter

LLDB's command-line driver consumes the inferior's stdout itself, so output never reached
the DAP client. The adapter now opens a pty, hands the slave to the target as
stdin/stdout/stderr and forwards the master as `output` events. The program sees a
terminal (line-buffered output, `isatty()` true), as it would when run by hand.
Cost: stdout and stderr are not distinguished.

## 4b. Code run on the debugger's behalf never stops the debugger

While the agent is serving a request, its monitoring handlers return immediately. Without
this, arming a step outside a monitoring callback (stop-on-entry) stopped inside the
agent's own JSON encoding.

## 4c. A step that leaves a function stops in the caller on the calling line

When the stepped frame returns, the agent arms a one-shot `INSTRUCTION` event on the
caller and stops at its next instruction, i.e. mid-line on the line that made the call.
This matches what debugpy and Visual Studio show, and it is the same place a native
step-out lands.

## 5. Toolchain for development

`uv` provides virtual environments (the system Python has no `ensurepip`) and stripped
standalone CPython 3.12/3.13/3.14 builds, which double as the "no debug info" test
targets. Commits are made from the Windows side, where `gh` is authenticated.

## 6. Licence

Apache-2.0, chosen by the project owner.
