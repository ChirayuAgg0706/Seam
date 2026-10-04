# Seam v1 status

Work after v1 is tracked in [ROADMAP.md](ROADMAP.md).

Legend: **done** = run and seen passing; **partial** = part of it runs and passes, the rest
is listed; **blocked** = needs something from the project owner.

Every "done" below refers to scenario tests that launch real programs under Seam through a
scripted DAP client (`tests/`). Nothing is marked done on the strength of reading code.

## Checklist

| # | Item | Status | Evidence / what is missing |
|---|---|---|---|
| 1 | Python 3.12, 3.13, 3.14, including builds with no debug info | **done** | With debug info: Ubuntu's 3.12 and the deadsnakes 3.13/3.14 with their `-dbg` packages, full suite. Without: uv's standalone builds of all three (symbol table, no DWARF), full suite; and system 3.12 with symbol lookup disabled (dynamic symbols only), smoke. |
| 2 | Breakpoints in Python files (incl. modules imported later) and native files, in one session, add/remove while running | **done** | `test_python.py`, `test_mixed.py::test_python_and_native_breakpoints_in_one_session`, `::test_breakpoints_added_and_removed_while_running`. Conditional breakpoints and moving a breakpoint off a line without code are also covered. |
| 3 | Merged call stack, Seam's frames hidden, nested Python → native → Python → native, multi-threaded | **done** | `test_mixed.py::test_native_breakpoint_shows_one_merged_stack` (four alternations), `::test_threads_each_have_a_correct_merged_stack`. Python frames are compared with `traceback.extract_stack()` / `sys._current_frames()` taken inside the target. |
| 4 | Variables: Python locals in Python frames, native locals in native frames | **done** | Same tests. At native stops Python locals are decoded from memory (simple built-in types show values, others type and address). |
| 5 | Evaluate Python at Python stops; clear refusal at native stops, process stays healthy | **done** | `test_python.py`, `test_mixed.py` (refusal, then the program runs to a normal exit). |
| 6 | Stepping: over/in/out in Python; Python → native; native → calling Python line; native → Python callback; callback → native caller | **done** | `test_python.py::test_stepping`, `test_stepping.py` (eight scenarios). Each was looped 15× with no failure (150 runs) on 3.12. |
| 7 | Binding layers: step in from Python lands in user code for C API, PyO3, pybind11, nanobind, Cython | **done at -O0; partial at -O2** | `test_bindings.py`, all five layers. See the matrix below for the two -O2 cells that cannot run and the Cython caveat. |
| 8 | Exceptions raised in native code don't break stepping | **done** | `test_stepping.py::test_native_exception_does_not_break_stepping`, `::test_step_over_a_python_line_whose_native_call_raises`. |
| 9 | Launch and attach; PEP 768 on 3.14; caveated injection on 3.12/3.13 | **done** | `test_attach.py` on all three versions; the test asserts which mechanism was used. Caveats in the README and `docs/decisions.md` §8. |
| 10 | DAP server tested with a scripted DAP client | **done** | Every scenario goes through `tests/dapclient.py` talking to `seam dap` over stdio. |
| 11 | VS Code extension as a `.vsix`; nvim-dap configuration | **partial** | `vscode/seam-debugger-0.1.0.vsix` builds with `vsce` and is on disk. **Not verified inside VS Code or Neovim**: I cannot drive either UI. Manual check below. |
| 12 | Overhead within 10% with no breakpoints | **done** for CPU-bound and native-call workloads | `test_overhead.py`: ratios 0.95–1.01 on 3.12/3.13/3.14. A thread-creation-heavy workload is about 2× slower under Seam (LLDB handles every thread start and exit); it is measured and printed, not held to 10%. |
| 13 | Docs: README (install, quick start, architecture, limitations) | **done** | `README.md`, `docs/decisions.md`, `docs/neovim.md`. |
| 14 | Clean-machine check with a PyO3 project | **done**, with one caveat | The `clean-machine` CI job starts from a bare `ubuntu:24.04` container, runs the README's install commands (`scripts/clean-machine-check.sh`) and then debugs `examples/pyo3-demo` with the installed `seam` (`tools/check_demo.py`, 12 checks). It passes. Caveat: the script installs from the checked-out repository instead of running the README's `git clone` line, because the repository is private. |

## CI

GitHub Actions, on every push (`.github/workflows/ci.yml`). Last run: all 8 jobs green.

- `full suite (3.12, -O0)`: all 40 tests on the runner's system Python (no debug info there).
- `smoke` × 5: 3.12 -O2, 3.13 -O0/-O2, 3.14 -O0/-O2, on uv's standalone interpreters.
- `clean machine`: see item 14.
- `VS Code extension package`: builds the `.vsix` and uploads it as an artifact.

A missing toolchain fails CI rather than skipping (`SEAM_TEST_STRICT=1`). One run costs
roughly 12 minutes of Actions time across the jobs.

## Test matrix

Full suite = 40 tests. Smoke = 27 tests (the mixed-mode, stepping, binding-layer and
attach scenarios plus two Python-only ones; not the overhead or unit checks).

**Local, LLDB 20.1.2** (last run; LLDB 20 replaced LLDB 18 on this machine when it was
installed, so local runs are LLDB 20 only from here on):

| Interpreter | Python debug info | -O0 | -O2 |
|---|---|---|---|
| 3.12.3 Ubuntu | yes (`python3-dbg`) | full: 40 pass | smoke: 25 pass, 2 skipped |
| 3.13.16 deadsnakes | yes (`python3.13-dbg`) | full: 40 pass | smoke: 25 pass, 2 skipped |
| 3.14.8 deadsnakes | yes (`python3.14-dbg`) | full: 40 pass | smoke: 25 pass, 2 skipped |
| 3.12.15 uv | symbol table, no DWARF | full: 40 pass | not run |
| 3.13.16 uv | symbol table, no DWARF | full: 40 pass | not run |
| 3.14.8 uv (tail-call interpreter) | symbol table, no DWARF | full: 40 pass | not run |

**LLDB 18.1.3**: the same suite passed locally before the switch (full at -O0 on system
3.12 and the three uv builds; smoke at -O2 on 3.12, 3.13 and 3.14; smoke on system 3.12
with symbol lookup disabled), and CI runs it on LLDB 18 on every push.

Flakiness hunting: on LLDB 18, every stepping scenario looped 15× (150 runs) and every
stepping and binding scenario 8× (144 runs) with no failure. On LLDB 20, the scenario
that exposed the transient-backtrace glitch looped 80× clean after the fix.

The two skips at -O2 are the same on every interpreter and are not Seam failures; the test
asserts what Seam reports before skipping. Which two depends on the LLDB version:

- **nanobind -O2, PyO3 -O2: breakpoint on the line that calls the Python callback.** The
  optimiser leaves that statement with no code of its own (it is one inlined library
  call). For nanobind LLDB moves the breakpoint to the next line, after the callback has
  run; Seam reports the new line. For PyO3 there is nowhere to put it; Seam reports the
  breakpoint as unverified and the program runs normally. So "step in from native to a
  Python callback" is **untested for nanobind and PyO3 at -O2**. It passes for the C API,
  pybind11 and Cython at -O2.
- **nanobind -O2 under LLDB 20** fails differently: the breakpoint is placed before the
  call and stepping into the callback works, but LLDB 20 cannot unwind through nanobind's
  optimised library code (its own `bt` stops there), so the native frames below the
  callback are missing and the callback cannot be stepped back out to its native caller.
  Seam says so in the debug console, still shows every Python frame, and a step out from
  such a place falls back to the Python caller instead of running away.

Binding-layer results in detail:

| Layer | Step in from Python lands in user code, step out returns | Native breakpoint → into Python callback → back out |
|---|---|---|
| C API | -O0 pass, -O2 pass | -O0 pass, -O2 pass |
| pybind11 | -O0 pass, -O2 pass | -O0 pass, -O2 pass (several presses) |
| nanobind | -O0 pass, -O2 pass | -O0 pass, -O2 not testable (above) |
| Cython | -O0 pass, -O2 pass | -O0 pass, -O2 pass (several presses) |
| PyO3 | -O0 pass, -O2 pass | -O0 pass, -O2 not testable (above) |

"Several presses": in Cython-generated code at any optimisation level, and in optimised
code generally, one source line is spread over many small address ranges. A single
"step in" on the line that calls a Python callback can stop elsewhere on the same line or
on the `def` line first; pressing it again gets there (the test allows up to 12, typically
2–5). At -O0 the other four layers need exactly one press, and the test enforces that.

## Known problems

- **One unexplained failure, not reproduced.** Once, in a full-suite run on system 3.12,
  a step-in in the Cython callback scenario reported an "exception" stop instead of a
  step. It has not recurred in more than 250 further runs of that scenario or in any of
  the full-suite passes since. The test now records the stop's description if it happens
  again. I do not know the cause and am not claiming it is fixed.
- **A second one-off failure, probable cause found later.** A nanobind callback scenario
  failed once on the uv 3.12 build under LLDB 18; I had filtered that run's output and
  lost the message, and it did not recur in 54 further runs. Under LLDB 20 an intermittent
  failure of the same shape was caught with full logs and traced to LLDB occasionally
  returning a two-frame backtrace; Seam now recovers from it (`docs/decisions.md` §12).
  That this was also the LLDB 18 failure is a guess.
- **Attach on 3.14 under LLDB 18 failed about 1 time in 13 on CI — fixed.** LLDB itself
  crashed or hung inside `SBFrame::EvaluateExpression` on the first request after an
  attach. It never happened locally. My first explanation (a stale process-state reading)
  was wrong: the failure came back. A CI soak then reproduced it 3 times in 40 attaches;
  after the fix in `docs/decisions.md` §8 the same soak passed 80 of 80.
- **Thread-heavy programs run about 2× slower** under Seam even with no breakpoints.
- **LLDB 19 is untested.** 18.1.3 (CI) and 20.1.2 (local) are. LLDB 20 needed three
  accommodations, all in `docs/decisions.md` §12: internal breakpoints by symbol address,
  recovery from a transient short backtrace, and safe handling of a backtrace LLDB cannot
  complete.
- Limits by design are listed in the README's Limitations section (no Python evaluation
  at native stops, no stdin for the debugged program, no embedded interpreters, modules
  over 20,000 functions excluded from step-in).

## Could not verify — how to check by hand

**VS Code extension.** In VS Code connected to WSL (Remote - WSL), with Seam installed in
WSL so that `seam dap` runs in a terminal:

1. `code --install-extension vscode/seam-debugger-0.1.0.vsix`
2. Open `examples/pyo3-demo` after building it (its README has the two commands).
3. If `seam` is not on the PATH VS Code sees, set `seam.adapterCommand` to
   `["/full/path/to/seam", "dap"]` in settings.
4. Open `demo.py`, put a breakpoint on the `result = ...` line, press F5 and pick
   "Seam: Python + native".
5. Expected: stops on that line; **Step Into** shows `src/lib.rs` inside `sum_squares`
   with `report` and `<module>` below it in the Call Stack; Variables shows `n` and
   `total`; **Step Out** returns to `demo.py`.

**Neovim.** `docs/neovim.md` has the configuration; the same five expectations apply.

## Blocked on the project owner

Nothing. (The deadsnakes interpreters and LLDB 20 were installed on 2026-10-04 and the
cells they unblocked are in the matrix above.)
