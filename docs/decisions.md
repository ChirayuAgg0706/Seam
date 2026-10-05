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
- A refused attach takes its request back before detaching, while the process is still
  stopped. Left queued, the process acted on it once its main thread ran Python again,
  with the debugger gone: 3.14 printed "Can't open debugger script" and a traceback into
  the program's stderr (the script had been deleted), 3.12/3.13 imported a helper nobody
  was listening to. For PEP 768 the pending flag is cleared. A pending call cannot be
  taken out of the interpreter's queue, so the program text it will run is emptied
  instead (the memory it lives in stays mapped). If the main thread had already started
  on the request at that moment, the helper still loads and stays idle.
- Before the process is resumed to load the helper, the adapter evaluates one harmless
  call (`getpid()`). Without it, the PEP 768 path made the session's first LLDB
  expression at the helper's trap, immediately after the helper library was loaded, and
  LLDB 18 crashed or hung in `SBFrame::EvaluateExpression` in 3 of 40 attaches in a CI
  soak (never locally). With it the soak passed 80 of 80. Why LLDB fails there is not
  understood; the 3.12/3.13 path never showed it, and it already evaluated an expression
  at the attach stop.
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

The same lag works the other way: right after a resume the public state can still read
"stopped". The helper that waits for a stop used to fall back to that state when no event
had arrived for a second, which could report a stop that had not happened. (I first blamed
this for a CI failure where LLDB died after an attach; that was wrong, see §8.) The wait
now trusts events only. The single exception is the stop that completes
an attach, for which LLDB does not always send an event and before which nothing has been
resumed.

## 11. Seam does its own "run until return", not LLDB's step-out plan

LLDB's step-out was used first, to leave native frames and binding glue. It failed in
three ways on optimised code, the last of which only showed up in CI:

- stepping out of an inlined frame executes nothing (it only changes the displayed scope);
- `SBFrame.IsInlined()` answers for the PC, not the frame, so the real host of an inlined
  function also reports as inlined and could not be picked as "the real frame to leave";
- with artificial tail-call frames (optimised Rust) and with optimised Cython on 3.13/3.14,
  the plan's return breakpoint was hit but the plan did not recognise it and the program
  ran to completion.

Seam now sets a thread-specific breakpoint at the target frame's PC (its return address)
and accepts the hit only when the stack pointer is at or above that frame's SP, which
rules out recursion through the same return address. Related: a frame is classified as
user code or glue by its own name and line, not by the innermost function at its PC, and
when the newest frame is glue inlined into a user function the stop belongs to the user
function.

## 12. LLDB 20: internal breakpoints by symbol address, and truncated backtraces

Running the suite under LLDB 20.1.2 (after LLDB 18 was replaced on the development
machine) turned up two differences from 18.

**Function-name breakpoints land at the wrong address** when the interpreter's debug info
is in a separate file (`python3.x-dbg`). `breakpoint set -n Py_RunMain` resolved to an
address inside a data table (`0x8b89de` instead of `0x6bc920` on 3.12), so the launch
breakpoint was never hit and the program ran to completion. Symbol-table lookup gives the
right address. Seam now sets its own breakpoints (`Py_RunMain`, `seam_trap`) on the
symbol's start address. The one exception is `seam_trap` during attach, which has to be
set by name before the helper is loaded; it is replaced by an address breakpoint as soon
as the helper's symbols exist, and the helper carries its debug info in the same file, so
the bug does not apply to it. Breakpoints the user sets by function name still go
through LLDB's name resolution.

**LLDB 20 cannot unwind through nanobind's optimised library code.** Its own `bt` ends
there with a frame whose PC is inside `_PyRuntime` (data). LLDB 18 unwound the same binary
correctly. Seam cannot repair the unwinder, so it contains the damage:

- a frame whose PC is not in an executable section ends the native frame list;
- the oldest native frame never claims a group of Python frames (its stack area has no
  known upper bound), so a cut-short backtrace is recognised as such, the Python frames
  are still shown, and a console message names the function LLDB stopped at;
- stepping out when no frame is left to run to falls back to arming the Python-side step
  (the thread holds the GIL), instead of running to a bogus address.

Native frames below the point of failure, such as the user's function that called a
Python callback, are simply missing in that situation.

**A transient two-frame backtrace.** About once in 40 stops in `seam_trap`, LLDB 20
returned a backtrace of two frames whose second frame was in a data section. This showed
up as an intermittent test failure. When the native frame list ends in such a frame Seam
makes LLDB rebuild it once (the `getpid()` call from §4d) before treating it as
truncated; in 80 looped runs the glitch occurred twice and both times the rebuild
returned the full 24-frame stack. One unexplained failure seen earlier under LLDB 18 (a
nanobind callback scenario, see STATUS.md) has the same shape, but that run's output was
not kept, so this is a guess.

## 13. Signals, crashes and how the program ended

**Only fault signals stop the debugger by default.** LLDB's defaults suit C programs: it
stops on SIGUSR1, SIGTERM, SIGPIPE, SIGWINCH and others, and swallows SIGINT. A Python
program that installs handlers, uses timers or relies on `KeyboardInterrupt` would stop
in the debugger at each of them and never see its SIGINT. Seam stops on SIGSEGV, SIGBUS,
SIGILL, SIGFPE and SIGABRT and delivers every other signal straight to the program. The
launch/attach option `stopOnSignals` replaces that list (a program that hosts a JVM, which
uses SIGSEGV internally, can leave it out). SIGTRAP and SIGSTOP are LLDB's own and are
not touched.

**At a fault stop nothing is run in the process.** Everywhere else Seam may run its own
bookkeeping at chosen points (arming a step from user native code, queueing a breakpoint
change with `Py_AddPendingCall`). A process stopped at SIGSEGV may hold any lock and have
any structure half-written, so those are skipped: a breakpoint change made there is
accepted and not applied, and a step simply resumes. The merged stack and Python locals
come from memory as at any native stop. Native expressions the user types are still
evaluated by LLDB; the test checks that calling a function there does not deliver the
pending signal.

**Death by signal is reported as such.** LLDB's API gives an exit status and nothing
else: SIGKILL and `sys.exit(9)` both read 9 with an empty description. The distinction
exists in one place, the last packet of the debug-server protocol (`W<code>` for an exit,
`X<signal>` for a kill). Seam enables LLDB's `gdb-remote packets` log with a callback that
keeps only that packet. When it is an `X`, Seam prints "the program was terminated by
signal NAME" and reports exit code 128 + signal, as a shell would. If the packet is ever
not seen (a format change in a future LLDB), the raw status is reported unchanged.

**If LLDB itself dies**, the program it launched is not killed with it: the debug server
detaches and the program carries on as an orphan, with nobody reading its output. The
adapter therefore reports each launched pid to `seam dap` over a pipe. When LLDB exits
abnormally (killed, crashed, or the adapter inside it raised, which it turns into exit
status 70), `seam dap` kills those programs, sends the client an output event explaining
what happened followed by `terminated`, and exits with status 1. Programs Seam attached
to are never killed.

## 14. Exception breakpoints

**Uncaught exceptions are taken from the interpreter, not guessed.** Deciding at `raise`
time whether an exception will be caught is guesswork in pure Python and impossible in a
mixed program (native frames in between can clear or translate it). The interpreter
knows: it raises the audit event `sys.excepthook` immediately before reporting an
exception nobody handled, whatever `sys.excepthook` has been replaced with, and never for
`SystemExit`. The helper registers a C audit hook for that one event. A Python-level
audit hook would run for every audited operation in the program (`open`, `import`, every
`ctypes` call); the C hook costs a string comparison. `PySys_AddAuditHook` is outside the
stable ABI, the only such function the helper uses; it has been exported since 3.8. An
audit hook cannot be removed, so it is added the first time the filter is switched on and
is inert otherwise.

Threads never reach that path (`threading` catches the exception and calls
`threading.excepthook`), so the agent puts a C-level callable in front of that hook while
the filter is on and restores the original when it is switched off. A program that
replaces `threading.excepthook` afterwards hides thread exceptions from Seam.

**The stack at an uncaught exception comes from the traceback.** By the time the
interpreter reports it, every frame the exception passed through has unwound. The
traceback keeps those frame objects alive, so Seam shows them (newest first, at the line
each was on) on top of whatever is still on the real stack, and variables and expressions
work in them through the agent as in any Python frame. They are released at the next
stop. A step from such a stop continues the program: there is nothing left to step
through.

**Raised exceptions use `sys.monitoring`'s RAISE event**, which fires in every frame an
exception passes through. Seam stops once per exception: in the first frame of user code
it touches, judged from the traceback (no deeper traceback entry is user code). That
covers a `raise` in user code, an exception coming out of a library call, and one set by
native code, with nothing remembered between events. User code is anything outside the
standard library and `site-packages`/`dist-packages`; `justMyCode: false` widens it to
everything. Code Seam runs itself (expressions, breakpoint conditions) never triggers a
stop.

**C++ throw and Rust panic** are LLDB breakpoints (`__cxa_throw` through LLDB's exception
breakpoint, and the `rust_panic` symbol, which the Rust runtime keeps for debuggers). The
C++ exception type is read from the `type_info` argument's symbol name. Its message is
read through `std::exception::what()` when RTTI proves that base exists (see §32).

## 15. Running the program in the client's terminal

LLDB launches the program, so the usual DAP arrangement (the client's terminal runs the
program itself) is not available. Instead, for `console: integratedTerminal` or
`externalTerminal` the adapter sends the client a `runInTerminal` request for a small
holder program (`seam/terminal.py`, standard library only). The holder connects back over
a Unix socket, reports which terminal device it is on, and then waits without ever reading
from it. The adapter launches the program with that device as its standard input, output
and error.

The holder, not the program, is the terminal's foreground job, so the terminal's
signal keys reach the holder. It passes Ctrl-C, Ctrl-\ and a hang-up to the adapter, which
sends the same signal to the program; with the signal policy of §13 a SIGINT goes straight
through and becomes `KeyboardInterrupt`. The adapter hangs up on the holder when the
program exits or the session ends, which returns the terminal to its shell. Two things
are lost by not being the foreground job: the program gets no SIGWINCH on resize, and
Ctrl-Z does nothing.

In the debug console (`internalConsole`) nobody can type, so the program's standard input
is `/dev/null` rather than a terminal that never answers: a stray `input()` fails at once
with `EOFError` instead of hanging the session. A client that asks for a terminal without
supporting `runInTerminal` gets this mode and a message saying so.

## 16. Hit counts and logpoints

One syntax for both sides: a bare number means "that hit only", as Python users know it
from debugpy (LLDB's own tools read a bare number as "from that hit on"), and `>=`, `>`,
`<`, `<=`, `%` are available for the rest. The adapter parses it once; a hit count it
cannot parse makes that breakpoint unverified with the reason, and leaves the others
alone.

On Python lines the agent counts and decides inside its line callback, so a hit that does
not count costs no stop. A logpoint's message is built there too, and delivered by a trap
whose reason carries a "log" flag: the adapter collects the text, emits an output event
and resumes without touching whatever a step has armed. If a step ends on the same line,
the one trap carries both. This costs one stop and resume per message; the alternative, a
second channel out of the program, was not worth it for something meant for occasional
messages.

On native lines LLDB's condition is used as is. Hit counts are Seam's own (LLDB only has
"ignore the first N"): each hit stops, is counted, and resumes if it does not count.
Where the syntax allows, LLDB skips the leading hits itself (`ignore count`), and the
breakpoint is disabled once no later hit can count, so `==1000000` in a hot loop does not
mean a million stops. Message expressions are evaluated by LLDB in the stopped frame.
The line's other address ranges (§9) are not counted as further hits.

## 17. Stops that are already over

Found through a hit-count scenario (`%3` on a native breakpoint in a ten-iteration loop)
that stopped on the wrong iterations in 4 of 9 CI jobs and about 1 local run in 25.

To resume a thread that is sitting on a breakpoint, LLDB first steps it one instruction
past the breakpoint. Occasionally that internal step surfaces as a public stop, and the
thread is then still described as it was at the previous stop: stop reason "breakpoint N",
the old frame list, and LLDB even bumps the breakpoint's hit count a second time. The
program has not come round again. The adapter log of a failing run shows it exactly: the
stop id advanced by one instead of two, the frame list was stale, and the PC was four
bytes past the breakpoint, on the next line.

Before this was understood it had three effects, depending on where it happened: a native
breakpoint in a loop occasionally stopped an extra time, one line further on; a hit count
was counted twice; and the same thing at Seam's own trap would have run a request in the
agent from a stop that was not a trap.

The PC register is current when the frame list is not, so the check is cheap: a thread
that has really hit a breakpoint has its PC on one of that breakpoint's locations. A
thread whose stop reason names a breakpoint it is not at, or a single-step nobody asked
Seam for, has no current reason to be stopped. If no thread has one (and no pause was
requested), Seam resumes without reporting anything.

How often it happens depends on how busy the machine is. Looping the native breakpoint
scenarios locally under LLDB 20: none in 3,960 continues on an idle machine, 6 in 1,584
with half the cores kept busy, 46 in about 860 with every core busy. Every one had the
same picture (stop reason "breakpoint", frame PC on the breakpoint, PC register one
instruction past it), and after the check none of those runs failed.

Two mistakes made on the way, both found by the same loops:

- The first version trusted the PC register unconditionally. Right after a stop the
  register is sometimes unreadable and reads as zero, so a genuine hit was taken for a
  leftover and skipped. An unreadable register now falls back to the frame's PC.
- The stale-frame refresh of §4d (a `getpid()` call in the process) used to run whenever
  the register and the frame PC differed, which included the unreadable case, where the
  frames were fine. Once, under full load, such a refresh at a genuine hit was followed
  by no thread having a breakpoint stop reason any more, and the hit was lost. That run's
  log does not show what the thread looked like before the call, so that the call wiped
  the reason is the likely explanation, not an observed one. The refresh now runs only
  when the register can be read and really differs. The lost hit has not recurred in the
  loaded runs since, which is too few (22) to call it proven.

This is probably also what was behind the one-off failures recorded in STATUS.md, but
that is an inference from the similarity, not something those runs' logs can confirm.

## 18. Seam uses a debugger instance of its own, not the `lldb` program's

Seam's adapter runs inside the `lldb` program (§1) and for a long time used the
`SBDebugger` that program hands to scripts. That debugger has an event-handler thread of
its own. It is signed up for the state-change events of every process, so each stop
event went to two consumers: Seam's listener and that thread. Whichever took it first
did LLDB's stop processing (updating the public state, running the stop actions, deciding
whether to restart), while the other was already looking at the process.

It was found through data breakpoints. Plain LLDB reports a first, spurious watchpoint
hit ("value unchanged") and silently continues; Seam received that stop as a real one,
reported it, and then watched LLDB resume the program underneath it. The stop event Seam
held said "not restarted"; the restart was decided a moment later, on the other thread.

With a debugger created by Seam itself (`SBDebugger.Create()`), nothing else consumes its
events, and a whole family of earlier findings went away with it. Measured by looping the
native breakpoint scenarios under LLDB 20 with half the cores kept busy:

| | shared debugger | own debugger |
|---|---|---|
| continues from a breakpoint | 1,584 | 1,920 |
| stale frame lists (§4d) | 12 | 0 |
| leftover stops (§17) | 6 | 0 |

and with every core busy, 46 leftover stops in about 860 continues before, none in 384
after. So the stale frame lists of §4d, the stop events that preceded the public state
(§10), the unreadable PC register and the leftover stops of §17 were most likely all the
same race, seen from different angles. That is an inference from the measurements above;
the earlier diagnoses were each consistent with what was observed at the time.

LLDB 18 and 19 were then looped under load on CI (the `soak` job with `soak_load`): the
breakpoint scenarios 12 times over on a 2-core runner with both cores kept busy, which
is where the shared debugger had produced a hit count off by one in 4 of 9 jobs without
any extra load.

| | LLDB 18.1.3 | LLDB 19.1.1 |
|---|---|---|
| scenario runs, failures | 288, 0 | 288, 0 |
| continue requests | 2,448 | 2,448 |
| stale frame lists (§4d) | 0 | 0 |
| leftover stops (§17) | 0 | 0 |

The defences written for the old symptoms stay in place all the same. They cost nothing
when nothing is wrong, each writes a line to the log when it acts, and the soak job
counts those lines: if one ever becomes non-zero again, something is consuming Seam's
events again.

The transient two-frame backtrace of §12 and the attach crash of §8 (LLDB 18 on CI) may
have had the same cause. Neither has been re-tested without its workaround, so the
workarounds stay too.

## 19. Changing variables, function breakpoints, data breakpoints

**Set variable.** A Python local, global, attribute, list item or dict entry is assigned
by the agent, with the new value given as an expression evaluated in the frame. From
Python 3.13 `frame.f_locals` writes through to the running frame. In 3.12 it is a
snapshot, and the interpreter copies it back only for old-style trace functions, so the
agent calls `PyFrame_LocalsToFast` itself (through `ctypes`, imported only when a
variable is actually changed). The same applies to an assignment typed into the debug
console. At a native stop Python variables cannot be changed, for the same reason they
cannot be evaluated. Native variables are set through LLDB.

**Function breakpoints** take a name and nothing else, and the name may belong to either
side, so it is given to both: to LLDB as a function-name breakpoint (pending until the
module that has it loads) and to the agent, which stops on entry to a Python function
whose bare name, qualified name or `module.qualname` matches. A condition is evaluated in
the language of the function actually entered.

**Data breakpoints** are LLDB hardware watchpoints on native variables of 1, 2, 4 or 8
bytes; a native frame's "Globals" scope lists the statics of its source file so that they
can be chosen. Python variables are refused with an explanation: a Python name has no
fixed place in memory to watch.

## 20. The VS Code extension carries the adapter

A user had to `pip install` Seam and put `seam` on PATH before the extension could do
anything, and the extension debugged with `python3` whatever the project used. The first
F5 in a project with a virtual environment failed or ran the wrong interpreter.

**The package is inside the extension.** `scripts/build-vsix.sh` lays the `seam` package
out in `vscode/bundled/seam`, with the compiled helper, and the extension is packaged for
`linux-x64` only. The release workflow hands the script the manylinux wheel, so the
released extension carries the same adapter and helper as the wheel. (A helper built on
Ubuntu 24.04 needs glibc 2.4, so in practice either loads anywhere.)

**It is started as `python -I <extension>/bundled dap`.** Running a directory makes
Python put that directory on `sys.path`, so the package is importable with nothing in the
environment. `PYTHONPATH` was rejected: LLDB's embedded Python and the debugged program
inherit the adapter's environment. `-I` keeps the user's own `PYTHONPATH` and
`PYTHONSTARTUP`, which are meant for their program and are passed on untouched, from
interfering with the adapter.

**Which Python runs it.** The package declares `requires-python >= 3.12`, and the
launcher is part of it. The interpreter being debugged comes first (for attach, the
target's `/proc/<pid>/exe`): if Seam can debug it at all it is a CPython 3.12+, whatever
the distribution's `python3` is. Then `python3`, `python`, `/usr/bin/python3`. Each is
probed; if none is 3.12 or newer the session is refused with the list of what was tried.

**The extension owns the adapter process.** VS Code can run a debug adapter command
itself, but then it discards the adapter's stderr: a missing LLDB showed up as "Debug
adapter process has terminated unexpectedly (read error)". The extension now spawns the
process and passes messages through (a `DebugAdapterInlineImplementation`). When the
adapter dies while VS Code waits for `initialize`, `launch` or `attach`, that request is
answered with what the adapter wrote to stderr, which VS Code shows as the reason the
session did not start. When it dies later, the extension shows the text as an error and
ends the session. The session's end is signalled by closing the adapter's input, the
path the test suite's client uses, not by SIGTERM as VS Code would; SIGTERM follows after
15 seconds if the process is still there.

**The interpreter to debug, when the configuration names none:** the Python extension's
active environment for the workspace folder if that extension is installed (it is not a
dependency; a 15-second limit keeps F5 from hanging on its activation), else the
`python.defaultInterpreterPath` setting (the value `python`, its shipped default, counts
as unset), else `.venv` or `venv` in the folder, else `python3`. It is decided before
VS Code substitutes variables, so `${workspaceFolder}` in the setting works.

**With the program in a terminal, the Debug Console is not opened over it.** VS Code
opens the Debug Console when the first session starts, which showed an empty panel while
the program's output sat in the Terminal tab behind it. The extension sets
`internalConsoleOptions` to `neverOpen` unless the configuration says otherwise. Seam's
own messages and logpoint output are still in the Debug Console for whoever opens it.

**The attach picker reads `/proc`.** The extension only runs on Linux. A process counts
as Python by the name of its executable or of `argv[0]`. Seam's own launcher and terminal
holder are left out. The picker returns text, because VS Code substitutes
`${command:...}` as text; the extension turns it into a number, and the adapter accepts
either.

**`seam doctor` runs out of the extension too** ("Seam: Check This Machine"). Its live
check starts an adapter; started as `python -m seam` that only works where Seam is
installed, so when the doctor itself was run as a directory it starts the adapter the
same way.

## 21. Child processes

Seam debugs the program it launched or attached to and nothing else; following children
is out of scope for v1. What matters is that children run as if no debugger were there and
that the parent's session stays healthy.

**What LLDB does by itself.** The debug server traces forks. At a fork LLDB removes its
breakpoint instructions from the child's copy of memory and detaches from it; at a vfork
(`subprocess`, `os.system`, `os.posix_spawn`) it removes them from the memory parent and
child share and puts them back when the child has called `exec` or exited. Seam sees a
fork only as a stop event flagged "restarted", which it already ignored. Hardware
watchpoints are not inherited by a child. All of this was checked with scenarios under
LLDB 20 before anything in Seam was changed; LLDB 18 and 19 run them on CI only.

**The helper switches itself off in a forked child.** The child starts as a copy of the
parent: breakpoint tables, monitoring events, exception hooks, perhaps a step in progress.
Nobody would answer a trap there, logpoint messages would pile up, and the events would
cost time for nothing. `os.register_at_fork` runs the switch-off right after the fork: all
events off, callbacks unregistered, `threading.excepthook` restored, the debugger's slot in
`sys.monitoring` freed. A fork made by native code skips Python's at-fork hooks, so the C
helper also registers `pthread_atfork`: it sets a flag in the child that turns every entry
point into a no-op (never a trap) and runs the same switch-off the first time one is
reached. A request queued for the next safe point before the fork is not executed in the
child.

**The user is told once.** A breakpoint in a multiprocessing worker that never stops is
the first thing a user of other Python debuggers trips over. The debug server's stop reply
is the one place a fork shows (`fork:p<pid>`, `vfork:p<pid>`, later `vforkdone`), and Seam
already receives every packet of that protocol through the log callback it uses for the
exit packet (§13), so noticing costs a substring test per packet and no stop. Only children
that run Python are worth the message: a fork (a copy of the program), or a vfork child
whose executable is a Python interpreter when `vforkdone` arrives, which is after its
`exec`. `git`, `ls` and the like are not mentioned. One message per session. A Python child
that exits before `vforkdone` is processed is missed; that was accepted.

**Stopping the session ends the children with the program.** LLDB starts the program as
the leader of its own process group. When Seam kills the program (terminate, disconnect,
the client vanishing, LLDB dying) it kills that group, so the children that stayed in it go
too, as they would on Ctrl-C or a closed terminal when the program runs by hand. A child
that moved to a session or group of its own (a daemon, `start_new_session=True`) is left
alone. When the program ends by itself nothing is killed: its children are its own
business, as without a debugger. Rejected: killing only the program, which is what LLDB's
own front ends do; it leaves orphaned workers and servers holding ports after every stopped
session, and Python users know the other behaviour from debugpy. For the same reason the
signals the terminal holder passes on (Ctrl-C, Ctrl-\, hang-up) go to the group: before,
Ctrl-C did nothing while the program sat in `os.system()`, because the C library makes the
caller ignore SIGINT for the duration.

**The exit report does not wait for the pty to close.** The adapter used to wait up to two
seconds for the end of the program's pty before reporting the exit. A child that outlives
the program keeps the pty open, so every such exit took two seconds. The wait is now for
the pty to be empty: everything the program wrote has been forwarded. What a surviving
child writes later is still forwarded while the session lasts; after that its writes fail
with EIO, as on a closed terminal.

**Expressions that start a child process.** `subprocess.run(...)` typed into the debug
console ended the session. LLDB evaluates Seam's call into the helper as an expression; to
an expression a fork is a stop it has no explanation for, and with "unwind on error" it
gives up on the spot: registers put back (in the middle of the interpreter), the fork's own
handling skipped, the child left stopped under the debug server for ever, and the next
memory access fails. With "unwind on error" off the stop goes the normal way: LLDB lets the
child go, restarts, and the expression runs to its end. So calls that can run user code
(every request to the helper, native expressions typed by the user, native logpoint
expressions) are made with it off, and a call that really fails (a crash, a timeout) is
unwound by Seam with `SBThread.UnwindInnermostExpression`, which is what the option did;
the stop event LLDB then also sends to the listener is dropped. A forked child that returns
from such a call has nothing to return to (its caller was the debugger): the helper ends it
with `_exit(0)`.

Known limits. While a vfork child has not yet called `exec`, LLDB has every breakpoint out
of the program, so a breakpoint another thread reaches in that moment (normally well under
a millisecond) is missed.

**LLDB 18 and vfork with several threads.** The first CI run of these scenarios failed two
of them on every LLDB 18 job and on no LLDB 19 or 20 job.

- Several threads calling `os.system` at once: LLDB 18 loses the program (the process is
  reported as exited with status -1 and no exit packet). This is llvm-project #81564,
  fixed in 19. CPython's `subprocess` holds the GIL across its vfork, so it cannot do
  this; `os.system` and native code can. Seam now says what happened when an exit comes
  without an exit packet.
- One thread starting a child while another reaches a breakpoint in the same stop (the
  scenario has threads running over a conditional native breakpoint whose condition is
  false): LLDB 18 cannot evaluate the condition ("Couldn't allocate space for the stack
  frame: Couldn't malloc: address space is full"), so it stops. Three things were tried
  from Seam's side over six CI runs, each read from the adapter logs the soak job keeps:
  evaluating the condition again (fails the same way, and after the resume LLDB lost the
  program); passing such a hit over while a vfork child is under way (same); resuming
  without looking at anything (the program ran on, but a later expression failed with
  "memory write failed" and the adapter hung). LLDB 18's own state is wrong after the
  coincidence, and nothing done from outside cured it, so all three were taken out
  again. Re-evaluating conditions would also have run a condition with side effects
  twice.

What stays: a thread's fork, vfork or vfork-done stop reason is never a stop to show (it
had been reported as a pause); `seam doctor` notes the weakness when it finds LLDB 18;
the README says how to use LLDB 19 instead; and under LLDB 18 the first scenario skips
after asserting Seam's message, and the second runs without the breakpoint the other
threads keep reaching.

## 22. Debugging a pytest run

`"module": "pytest"` needed no change. Checked: breakpoints in tests, fixtures and a test
in a class; line numbers under assertion rewriting; the merged stack through pytest and
pluggy (compared with `traceback.extract_stack()`); stepping into an extension and back;
logpoints and the exit code with output capture on; the terminal; `-n 2`.

Two things worth knowing. pytest enables `faulthandler`; a segfault still stops in the
debugger first, with the merged stack. Continuing lets faulthandler write its report and
raise the signal again, which is a second stop (in the C library, same stack below);
continuing again ends the run. Seam does not hide the second stop: it is a real signal.
With pytest-xdist the tests run in worker processes, which are not debugged; the one-time
message of §21 says so.

## 23. The "user_unhandled" exception filter

Under a test runner or a framework the interesting exception is caught by the library, so
"uncaught" never fires, and "raised" fires for everything. This filter stops when a frame
of user code hands an exception to the library code that called it.

The helper decides at `sys.monitoring`'s PY_UNWIND event: the unwinding frame is user code
(§14's definition) and its caller, skipping frames of the import system and `runpy`, is
not. No caller at all means the exception is about to be uncaught, which is the other
filter's business, so the two never stop for the same exception of the main thread. Whether
user code further out would catch it later is not asked; that is what the filter means in
other Python debuggers too. Only errors count: exceptions outside `Exception` (exits,
cancellations, pytest's skip) and `StopIteration`/`StopAsyncIteration` never stop.

The unwinding frame is still on the stack, at the failing line and with its variables, so
it is shown live; frames the exception came through below it have unwound and are shown
from the traceback, as at an uncaught exception. A step from such a stop carries on.

PY_UNWIND is the event stepping uses to follow an exception upwards, and a tool has one
callback per event. While the filter is on, its check is chained in front of whatever
stepping registered, in C, because stepping's handler finds its frame by counting from
itself. Both callables are kept alive for good: the interpreter holds no reference to a
callback while it runs, and replacing the callback at a stop that was inside it freed it
under its own feet (a scenario caught this as an exit code of 1). The event is global, so
with the filter on every frame that exits by exception costs one call into the helper.

## 24. Stepping in coroutines and generators

Found by the first scenario written for it: stepping over `await asyncio.sleep(0.01)` ended
in `asyncio/events.py`. The agent treated `PY_YIELD` like `PY_RETURN`, so a suspending
coroutine "returned" to whatever had resumed it, and for a task that is the event loop.

**A suspension is not a return.** A step stays with the frame it follows until that frame
runs its next line, however often it is suspended and whatever runs in between.

- Step over and step out enable events only on the code object of the followed frame
  (`LINE`, `PY_RETURN`) plus `PY_UNWIND`, and accept them only from that frame object. The
  frame object of a generator or coroutine keeps its identity across suspensions (checked
  on 3.12 to 3.15), so nothing has to be known about tasks: another task running the same
  coroutine function is another frame. Nothing else is instrumented, so what runs while
  the frame is suspended runs at full speed and cannot end the step. `PY_YIELD` is simply
  not listened to.
- Step out therefore means "until it really returns or raises", not "until it yields".
- Step in listens to every line of the thread. When the followed coroutine suspends at an
  `await`, the agent stops listening to lines until that frame is resumed (`PY_RESUME`) or
  thrown into (`PY_THROW`): the tasks that run meanwhile are not something the stepped
  line called. So step in at an `await` that reaches no user code is step over.
- A `yield` is not an `await`: the value goes to a consumer that is on the stack and runs
  next. Step in at a `yield` follows it there. Plain generators are told apart by their
  code flags; in an async generator a yielded value reaches `PY_YIELD` wrapped in
  `async_generator_wrapped_value`, an awaited one does not (a CPython detail, checked on
  3.12 to 3.15). Step over a `yield` stays in the generator and ends on its next line when
  the consumer asks for the next value. That is what pdb does
  (`test_pdb_next_command_for_generator`), and it was chosen for that reason; "follow the
  value to the consumer" would also have been defensible for step over.
- A breakpoint anywhere still ends the step.

**When the frame finishes**, the step goes, as before, to the frame that resumed it, on
the calling line (§4c): the coroutine that awaited it, or the consumer of a generator. For
a task that frame is the event loop's; see §25.

**Thrown exceptions.** Cancellation and timeouts arrive as `PY_THROW`. If the frame handles
the exception the step ends on the handler's line; if not, `PY_UNWIND` moves the step to
the caller as for any exception. From 3.13 on the interpreter also reports the await's own
line once more when an exception is thrown into it: the event belongs to `CLEANUP_THROW`,
the hidden instruction that passes the exception to the awaited object. The agent's line
handler ignores a `LINE` event whose instruction is `CLEANUP_THROW`, for steps and for the
breakpoints it decides itself, so every version steps alike. (The C fast path for plain
breakpoints does not; see STATUS.)

**Two differences between interpreters are left as they are**; the tests assert them per
version.

- When the generator of a `for` loop is finished, "the consumer's next instruction" is the
  loop's clean-up on the `for` line from 3.13 on. 3.12 has nothing left to run on that
  line, so the step ends on the statement after the loop.
- `gen.close()` (or garbage collection) of a generator suspended outside any `try`: 3.12.3
  discards it without running it, so a step waiting in it never ends and the program runs
  on to the next breakpoint, as under pdb. 3.12.15 and later raise `GeneratorExit` in it,
  and the step follows that to the frame that closed it.

## 25. Steps keep to the user's code (`justMyCode`)

`justMyCode` (default true) existed for the raised-exceptions breakpoint only. Stepping
now uses the same notion of user code (`_is_user`: not the standard library, not
`site-packages` or `dist-packages`). The agent decides, because only it sees each event.
The adapter sends the option with every step request: until now the agent learnt it only
from a breakpoint sync, which a session without breakpoints never sends.

- **A step never ends on a line of non-user code.** Such a line returns `DISABLE` from the
  line handler, so a library line costs one callback per step.
- **A step aimed at a frame that is not the user's becomes "the next place user code
  runs".** That covers the frame a step moves to when the stepped function returns into a
  library, a stop at a breakpoint set in a library file, and a pause inside a library.
  It is a step in that follows that frame: either a line of user code runs on the thread
  first, or the frame returns and the step moves to its caller by the same rule. The first
  user frame reached that way is stopped in on its calling line, like any return (§4c).
  One rule serves three situations:
  - a coroutine run by the event loop finishes (the next task's line, the waiting
    coroutine's next line, or the line that called `asyncio.run`);
  - a callback returns to a library that calls it in a loop (the next call of it);
  - a test function returns to its runner (the next fixture or test).
- Rejected: running until the nearest user frame on the stack resumes, ignoring user code
  called meanwhile. Under a test runner there is no such frame, and under an event loop it
  is the caller of `asyncio.run`, so stepping off the end of a test or a task would have
  run the rest of the program.
- A callback called by a *builtin* (`sorted(key=...)`) is unchanged: its Python caller is
  the user's own frame, so stepping out of it ends there when the builtin returns, not at
  the builtin's next call of it. That differs from the Python-library case; it is §4c's
  behaviour and what the frames say.
- **Breakpoints set in library files still stop.** A step from such a stop does not stay
  in the library (the rule above); to step through library code, set `justMyCode: false`.
- **Code compiled from a string by other code** (`<string>`: a dataclass's `__init__`, a
  namedtuple's `__new__`, `exec`) is treated like a library by steps, since there is no
  source to show. A `<string>` frame with only such frames below it is the program itself
  (`python -c`) and is stepped normally. This applies to stepping only; the exception
  filter's `_is_user` is unchanged.
- **Native code is not affected.** Step in still arms the user-function breakpoints (§7).
  Because library Python lines are no longer candidates, a native user function called
  through a library's Python code is now reached directly. When native code returns into a
  library's Python frame, the hand-over step follows the rule above.
- With `justMyCode: false` every Python file except Seam's own and the frozen modules is
  user code, and stepping is as before, apart from §24.

Cost: nothing new is enabled outside a step. The new callbacks are registered, but their
events are only switched on while a step is in progress.

## 26. Source paths: `sourceMap`, and what the editor is told a file is called

**The mapping is Seam's own, not LLDB's `target.source-map`.** LLDB's setting maps the
file of a frame's line entry only when the mapped file exists. It never maps the line
entry of an address (`SBAddress.GetLineEntry()`, which Seam uses to classify every
function of a module for step-in). It turns a breakpoint's path back into the debug
info's through the first matching entry only. (Measured on LLDB 20, except the frame
case, which was read from LLDB's source.) Seam needs one answer in all three places, so
it keeps the pairs itself (`adapter/sources.py`).

- *Debug info to this machine* (frames, source lines in the disassembly): entries are
  tried in the order given and the first file that exists wins. Without a matching entry
  the name itself is used, a relative one from the program's working directory, which is
  where gdb and lldb look. A file that is not found gets no path at all (§27).
- *This machine to debug info* (native line breakpoints): nothing says which name a
  library that loads later was built with, so the breakpoint is set under each candidate.
  The candidates are the name every matching entry maps here, the path as the editor gave
  it (a build made in place; it also matches relative debug-info names, which LLDB
  compares with the end of the full path), and that path with symbolic links resolved.
  They are one breakpoint to the client, with one id and one hit count
  (`native_bp_group`; `native_bps[path]` stays a flat list, so `stops.py` needed no
  change). With a single candidate, the usual case, this is exactly what was there
  before. Rejected: the first matching entry only, as LLDB and lldb-dap do. A mapping
  left in the configuration would then silently unbind breakpoints in a library that has
  since been built in place.
- *Glue decisions*: the fragments are tried on the debug info's name and on every mapped
  name, as plain strings with no file checks (a module can have 20,000 functions). A
  relative name is tested with a leading slash, so `nanobind/src/x.cpp` matches
  `/nanobind/src/`.
- *A missing file is explained once per session*, and only for a frame classified as user
  code. libc with `libc6-dbg` names files nobody has, and that is nobody's mistake.
- *An unbound breakpoint is explained* from `SBModule.FindCompileUnits(basename)` on the
  libraries that are loaded. LLDB broadcasts modules-loaded after it has resolved
  breakpoints in the new library, so "still nothing bound" is meaningful at that point.
  Only compiled files are looked at, not headers.

**What the editor is told a file is called.** Python frames used to be reported as
`realpath(co_filename)`. That was wrong in two ways:

- A module that is itself a symbolic link was shown as the file the link points to.
- A project opened through a linked directory was shown under the resolved directory.
  `sys.path[0]` is the *resolved* directory of the script, so every imported module has a
  resolved `co_filename` while `__main__` has the path as typed (asserted on 3.12, 3.13
  and 3.14). VS Code treats the two spellings as two files: the second opens without its
  breakpoints.

Now the path is reported as the program has it, tidied of `..` and doubled slashes, with
one addition. For every path the client names (`program`, `cwd`, breakpoint files) whose
real path differs, Seam keeps the pair (real directory, the client's name for it),
climbing while the parent directories still correspond. One breakpoint in a linked
project is enough for the whole tree. The rule is exact (the client's directory resolves
to the real one), so it never produces a path that is not the same file. A file that is a
link out of such a directory is reported by its place in the directory. Native frames go
through the same function. File-level agreement alone (frame equals breakpoint path)
would leave every file reached by stepping under the wrong name in a linked project.

## 27. Frames without source, disassembly, stepping by instruction

A native frame whose source cannot be opened here has no `source.path`. That covers no
debug info at all, and debug info naming a file that is not on this machine (libc with
`libc6-dbg` used to get the relative path `nptl/pthread_kill.c`). Such a frame is named
`library!function` (`library+0xoffset` when there is no name) and marked subtle; frames
with source keep their plain names. If the debug info names a file, the frame keeps its
line and a `source` with a name only, `presentationHint: deemphasize`, and the
debug-info path in `origin`. If the client asks for the text, the `source` request fails
with that explanation. How VS Code displays such a frame is from reading its source, not
observed.

**At a crash more is shown.**

1. Every frame above the user's own code or the newest Python frame, glue or not.
2. The interpreter's own frames at the top of the faulting thread, hidden at any other
   stop: the function that faulted when an extension hands the interpreter a bad pointer,
   or the one that called `raise`/`abort` (libc's frames above it do not end the run).
   They stop at the newest Python frame or the first frame of other code. The
   interpreter's call machinery between an extension and the eval loop stays hidden. If
   the fault is in the eval loop's own frame, that frame is shown above its Python frames.

**`disassemble`.** Forward from the address with `SBTarget.ReadInstructions`. x86 cannot
be decoded backwards, so each preceding function is decoded from its start
(`SBTarget.GetInstructions` on the bytes from the symbol's start), going back function by
function and skipping up to 32 bytes of alignment padding that belong to no symbol. A
stripped library still has function starts, because LLDB synthesises symbols from the
unwind tables. The protocol requires exactly `instructionCount` entries, and VS Code
finds rows by position and by binary search on the address. So what cannot be read is
filled with placeholders marked `invalid`, with addresses that keep the listing
ascending. LLDB's default syntax (AT&T) is used.

**Stepping by instruction.** `SBThread.StepInstruction`. A flag in `native_stepping`
keeps `_on_stop` from walking on out of glue or handing the step to Python: an
instruction step ends where it ends. At a Python stop (the thread is in Seam's trap) and
for `stepOut` the granularity is ignored. No Python is run for it.

## 28. Step-in for large modules: entry traps

Stepping in from Python arms a breakpoint on every user function of every extension
module (§7). LLDB inserts and removes sites one at a time, each a round trip to its debug
server: about 45 microseconds per function per direction. With a 15,000-function module
loaded every step-in took 1.3 to 1.8 s (0.02 s without), wherever it was going, and
modules over 20,000 functions were excluded: pydantic-core has 123,456 functions and
inlined instances.

For modules with 2,000 symbols or more Seam places the trap instructions itself
(`adapter/entrytraps.py`).

- The functions are found by a breakpoint in a second target that has no process, so
  resolving inserts nothing; its locations are the addresses LLDB's own breakpoint would
  use.
- Arming writes the trap byte to all of them through `/proc/<pid>/mem`, one read and one
  write per module; disarming puts the bytes back.
- The traps are in memory only while the process runs. At every stop, and when Seam
  interrupts the process itself, they come out before anything else happens, so LLDB
  never sees patched code and creates or removes no site of its own while they are in.
- Where memory does not hold the original byte (a breakpoint site of LLDB's), the address
  is left alone.
- A thread that runs into a trap stops with SIGTRAP one byte past it. The stepping thread
  is put back on the instruction and the step ends there. Any other thread is put back
  too, and that one address becomes an ordinary thread-specific LLDB breakpoint for the
  rest of the step.
- A child forked while traps are in would inherit them, and unlike LLDB's breakpoints
  nobody removes them; the helper restores the bytes in a `pthread_atfork` child handler
  from a list the adapter leaves in the process.
- Smaller modules keep LLDB's breakpoints, as does everything when `/proc/<pid>/mem`
  cannot be opened. `SEAM_ENTRY_TRAPS` overrides the threshold (`0`: traps everywhere,
  `off`: LLDB breakpoints everywhere).

Measured (python3.12, LLDB 20, -O0; before / after):

| | before | after |
|---|---|---|
| step in, Python to native, first time, 15,000 functions | 1.67 to 1.80 s | 0.39 to 0.42 s |
| the same, repeated | 1.27 to 1.43 s | 0.025 to 0.029 s |
| step in, Python to Python, large module loaded | 1.34 to 1.43 s | 0.020 to 0.024 s |
| the same steps with only the small test extension | 0.020 s | 0.018 to 0.025 s |
| repeated step-in, 60,000 functions | refused (over the limit) | 0.035 to 0.056 s |
| first step-in, pydantic-core (123,456 locations) | 14.6 s, and never reached Rust | 3.7 to 4.5 s, then 0.02 s |

Everything else is flat in the number of functions: launch, breakpoints, stack,
variables, native steps, and the no-breakpoint overhead (cpu 1.001, native 1.001 with the
large module loaded). Raw LLDB on the 15,043 sites takes 0.83 s to enable and 0.69 s to
disable them.

Rejected: bulk writes through LLDB (its debug server writes eight bytes per system call,
and LLDB's write path skips its own sites); narrowing the candidates through relocations
(binding layers call user functions directly from glue); page protection on the module
(other threads fault at once).

Three of the binding test modules are over the threshold (pybind11: 6 of 3,072 locations
are user code; nanobind: 217 of 2,385; PyO3: 749 of 8,980), so both paths run in the
ordinary suite. Known gaps: if LLDB dies while traps are in an attached process, the
process keeps them; a child made by a raw `clone` (no libc fork handlers) keeps them too.

## 29. Agent calls when other Python threads want the GIL

An agent call runs Python on one thread while all others are stopped. Two things could
make it wait for a thread that cannot move.

- A pending GIL drop request: the interpreter honours it inside the agent's code and
  waits for somebody to take the GIL. The adapter withdraws the request before the call
  with one memory write (3.12: an int in the interpreter state, checked by the layout
  unit test; 3.13+: bit 0 of the thread's `eval_breaker`); the thread that made it makes
  it again when it runs.
- The agent's code releasing the GIL itself (around a system call) while a stopped thread
  holds the GIL's mutex or is half-way through waking from its condition variable. That
  is common right after a resume, when every waiter's timeout has passed. Seam cannot see
  that state, so the call uses LLDB's own remedy: after one second alone, the other
  threads run until the call returns. They can do little while the agent's thread has the
  GIL, but if the agent releases it around a system call they take it for a moment.

Before: stepping out of native code with busy Python threads failed after 30 s
("Expression execution was interrupted"). After: 60 of 60 looped runs pass on 3.14, 4 of
them through the one-second path.

## 30. Frames of one function body; steps the user cannot see

LLDB gives a function that has code inlined into it the address where the inlined code
starts, so an inlined frame and its host share the PC only on the first instruction. They
always share the stack pointer, and a real caller's is always higher. Seam used to
compare both, and one instruction into inlined glue it took the user's function for a
caller; "run until return to it" then ran the program to its end (found stepping out of a
pydantic-core function with `map_err` inlined at the current line). Frames with one stack
pointer are now one function body: the step is taken from where the thread is, step out
leaves the whole body, and a return into a body waits at the PC of its innermost frame.
Stepping out of an inlined user function therefore leaves the function it was inlined
into as well.

A native step in or over that ends in glue inlined into the user's function, or back in
the function after passing through glue, with the user's function, line and stack
pointer unchanged, is taken again (at most 64 times): one line of optimised Rust can hold
seven such pieces, and none of them is a step to the user. A native step over on a
one-line loop with inlined glue in it may therefore run several iterations before it
stops.

## 31. Frames Cython adds to tracebacks

Cython gives the frame it adds to a traceback the .pyx path as it was at build time,
relative and nowhere to be found at run time. Such a name is not the user's code
(otherwise "Raised Python exceptions" never stops for an exception from a Cython library)
and the frame is shown without a source.

## 32. Inspection and diagnostics after the developer trial

The bootstrap executes in a fresh namespace. `PyRun_SimpleStringFlags` and the attach
mechanisms otherwise put helper imports in the user's `__main__`, making a missing
`import sys` work only under the debugger.

A condition which fails to evaluate still stops, as LLDB does, but now explains the
failure once. Python's explanation travels with the trap, including function
breakpoints and hit-count conditions. Native conditions are checked at their first
reported hit. Libraries without debug-info sections or external compile units are reported
once when loaded or when attaching, excluding the interpreter, helper, system libraries
and installed wheels. Embedded debug-info sections are checked before asking LLDB to
parse compile units, so a normal import does not need full DWARF parsing for this warning.

A local watchpoint carries the owning thread, frame CFA and function address. A hit
after that frame has disappeared deletes it and continues: a reused stack slot is not
the original variable. The function address distinguishes another function reusing the
same CFA. This cannot distinguish a later invocation of the very same function at the
same CFA if no watched access happens between them. Globals have no frame lifetime.

At `__cxa_throw`, the object is fully constructed and its RTTI and object arguments are
still in `rsi` and `rdi`. The adapter walks only Itanium single-inheritance RTTI, stopping
at other RTTI kinds, to prove that the object derives from `std::exception`. It identifies
the RTTI class through the vtable's own type-info pointer, not a symbol lookup at the
vtable address point: that lookup failed under LLDB 18 in the first extended CI run,
while LLDB 19 and 20 passed. Only then
does it evaluate `what()` with a two-second timeout. When LLDB cannot find the C++ type
declaration (stripped libstdc++), it calls the third entry in the Itanium virtual table:
the two destructor entries precede `what()`. This runs a native virtual method:
the standard implementations are safe to call here; a user's override may have side
effects, just like a native expression typed in the console. Python is never run. The
type stays in `exceptionId` and the message appears in the description. Unreadable or
unsupported RTTI and a failed evaluation fall back to the type alone.

Thread names are read through the agent only at safe stops and cached for native stops.
The cache cannot know a new or renamed Python thread until another safe thread request.
With `justMyCode`, Python library frames stay in the merged stack but are marked subtle
and their sources deemphasized. Native globals are filtered by declaration file, with
lexical-scope filtering disabled so file-level constants remain visible. Variables whose
debug info cannot describe their value say `<optimized out>` or show the LLDB error.
Python variable types use the short class name on both sides of the boundary.

The uncaught-exception target restores CPython's standard `sys.excepthook`; its `hooked`
mode still tests a replacement. Ubuntu's apport hook loads many native libraries on the
way out, costing about 24 seconds under local LLDB 20 and sometimes exceeding the
scenario's 30-second exit deadline. The same hook was slow on the pre-trial code. It
does finish when given longer; the regression fixture should exercise reporting the
exception rather than the distribution's crash reporter.

The native overhead workload now imports its extension before the work timer and reports
that import as `import_seconds` separately. The old timer included a fixed LLDB loader
pause: about 0.67 seconds on this machine on both `1139fdb` and the new code. The 12-million
native calls themselves took 1.119 seconds plain and 1.131 under the old adapter, 1.114
and 1.131 under the new one. Including that one-time pause produced an 84% reported
slowdown on a fast machine, despite unchanged steady throughput. The 10% assertion still
covers all CPU work and native calls; interpreter startup and initial library loading
are setup costs, with the latter now visible rather than folded into the work timer.

## 5. Toolchain for development

`uv` provides virtual environments (the system Python has no `ensurepip`) and stripped
standalone CPython 3.12/3.13/3.14 builds, which double as the "no debug info" test
targets. Commits are made from the Windows side, where `gh` is authenticated.

## 6. Licence

Apache-2.0, chosen by the project owner.
