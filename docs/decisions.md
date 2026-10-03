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

## 4e. Python frames are matched to C frames by address only, never by function name

The brief's rule was "the entry frame's address falls inside the stack area of the
`_PyEval_EvalFrameDefault` C frame". The uv build of 3.14 uses the tail-call interpreter:
`_PyEval_EvalFrameDefault` is inlined into its caller and the visible frames are
`_TAIL_CALL_*` handlers, so no frame carries that name. Seam now finds the C frame whose
stack area contains the entry frame's address, whatever it is called. Frames belonging to
the interpreter's own module are hidden from the merged stack.

## 4d. Workaround for stale frame lists in LLDB 18

Found while testing "add a native breakpoint while the program runs". Once any expression
has been evaluated in a session, the stop that follows an interrupt-and-continue shows
the *interrupt* stop's frames; LLDB's own `bt` is wrong too, while the registers are
right. Reproduced in pure LLDB 18.1.3 with no Seam code involved.

- Detection: frame 0's PC differs from the `rip` register.
- Repair: evaluate a call to `getpid()` on that thread, which makes LLDB rebuild the list.
  `getpid()` is async-signal-safe and lock-free, so it is the one function Seam will call
  at a stop that is not a Python safe point.
- Tried and rejected: `SBProcess.Stop()` instead of `SendAsyncInterrupt()` (same bug);
  poking a section load address to force a cache flush (no effect).
- Not yet checked against LLDB 19/20.

## 7. Stepping across the boundary

The brief left one problem open: stepping out of, or into Python from, native code that
Seam did not step into (for example after a direct C breakpoint), where
`sys.monitoring` cannot be called. What was built:

**Python → native (step in).** At the Python stop (a safe point) the agent arms an
ordinary Python step-in, and the adapter enables a breakpoint on every *user* function of
every extension module that has debug info, for the stepping thread only. Whichever fires
first wins. This replaces the brief's "read `ml_meth` from the callable, then native
step-in through the dispatcher": it needs no knowledge of how pybind11, nanobind, PyO3 or
Cython route a call, and binding glue is never a candidate because glue functions are
filtered out by source path and name when the breakpoints are created. The breakpoints
are created once per module and stay disabled between steps. Modules with more than
20,000 functions are excluded.

**Native → Python (step out, or step over past the end).** LLDB steps out natively with
`step-out-avoid-nodebug` off, so the step completes the moment control is back in the
interpreter. That moment *is* a safe point: a C function called by the interpreter has
just returned, the thread holds the GIL (checked from memory before anything runs), and
the interpreter is exactly as re-entrant as it was while the C function could still call
back into it. The adapter then asks the agent to stop when the calling Python frame
executes its next instruction, so the stop is on the calling line. If the native function
raised, the next instruction that has a line number is in the handler, or the frame
unwinds and the step follows it up. This is the brief's option 2.

**Native → Python callback (step in).** Arming a Python step from a native stop needs the
agent. Seam does this only when the thread holds the GIL and is stopped at a statement
boundary in user native code ("control-safe"), where calling into the interpreter is as
legal as it would be for the user's own next statement. Only Seam's own request runs; it
touches `sys.monitoring` state and nothing of the user's. Evaluating user expressions at
native stops stays refused. If the stop is not control-safe the step behaves as a plain
native step-in.

**Python callback → native caller (step out, or step over past the end).** The agent
traps on the frame's `PY_RETURN`; the adapter finishes with a native step-out into the
nearest user frame.

**Cancelling a step** is a memory write: the adapter bumps a generation counter in the
helper and the agent drops any step armed under an older generation. So ending a step at
a native stop runs no code in the target.

**Stopped inside the interpreter (pause).** A step is queued with `Py_AddPendingCall` and
armed at the main thread's next bytecode boundary (the brief's option 1, used here and for
breakpoint changes while running).

## 8. Attach

- **3.14:** PEP 768. The adapter writes a script path and the pending flag into the main
  thread state and sets the eval-breaker bit: three memory writes, no code run by the
  debugger. Falls back to the pending-call method if remote debugging is disabled.
- **3.12/3.13:** the adapter allocates memory in the target (an `mmap` system call made by
  LLDB, no libc locks), writes a bootstrap string there and calls
  `Py_AddPendingCall(PyRun_SimpleString, string)`. Caveat: `Py_AddPendingCall` is called
  at an arbitrary stop. It takes one short internal lock; if the stopped thread happens
  to hold it the call cannot return and is abandoned after 3 seconds.
- Either way the helper loads at the main thread's next safe point and reports in through
  `seam_trap`. A main thread blocked in a system call never gets there; attach then times
  out and detaches.
- On disconnect Seam removes its breakpoints and monitoring events and detaches; the
  helper library stays mapped.

## 9. Native source-line breakpoints do not re-fire within one line

LLDB resolves a line to every address range carrying it. Rust's `?`, Cython's line
directives and optimised C give one line several ranges in one function, so continuing
or stepping out of a call on that line hit "the same" breakpoint again.

- First attempt, rejected: keep only the lowest address per function (what gdb shows).
  With `-O2` the lowest address is not necessarily the first one executed, and breakpoints
  were hit after the interesting call, or never.
- What is done: all locations stay enabled. A hit is skipped when the thread's previous
  reported stop was in the same invocation of the same function on the same line, at a
  different address. Returning to the same address (a loop) still stops.
- Separately, locations inside Cython's generated glue (`__Pyx_*`, `__pyx_pymod_*`,
  `__pyx_pw_*`) are disabled: with line directives Cython attributes module-init code to
  user lines, which made a breakpoint on a statement fire during import.
- The client is told where a native breakpoint really is: unverified until its module
  loads, and with the actual line if the compiler left the requested one with no code.

## 10. A stop event can precede LLDB's public process state

Found as an intermittent hang (about 1 in 30 step-ins into large pybind11/nanobind
modules): Seam treated a stop event as stale when `SBProcess.GetState()` did not yet say
"stopped", and so dropped a real stop. Logs showed the state flipping to stopped
moments later with a genuine breakpoint stop reason. Seam now waits up to a second for
the state to catch up before discarding a stop event.

## 5. Toolchain for development

`uv` provides virtual environments (the system Python has no `ensurepip`) and stripped
standalone CPython 3.12/3.13/3.14 builds, which double as the "no debug info" test
targets. Commits are made from the Windows side, where `gh` is authenticated.

## 6. Licence

Apache-2.0, chosen by the project owner.
