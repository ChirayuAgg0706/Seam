# Handoff: fixing what the trial user found (2026-10-05)

`main` is green and complete up to commit `1139fdb` (see STATUS.md, ROADMAP.md).
This branch, `wip/tester-fixes`, is **unfinished and untested**: nothing on it has been
run. Do not merge it until the suite passes (CONTRIBUTING.md says how to run it; the
suite needs three batches, it no longer fits one 600 s call).

A subagent played a Python developer with a pybind11 + ctypes project and no docs. Its
verdict: usable tomorrow for launch-mode work, with the problems below.

## Started on this branch (code written, NOT run, no tests yet)

1. **Seam leaked `sys` and `seam_agent` into the script's `__main__`.**
   `adapter/session.py` `_inject` and `_request_agent_load` now run the bootstrap through
   `exec(code, {...})`. Target for a test exists: `tests/targets/clean_main.py` (expect
   output `clean []`). Still needed: a test for launch, and one assertion in
   `tests/test_attach.py` that `'seam_agent' not in __main__.__dict__` (both attach paths:
   pending call on 3.12/3.13, PEP 768 on 3.14).
2. **A breakpoint condition that cannot be evaluated stopped at every hit, silently.**
   - Python side: `_target/seam_agent.py` `_spec_verdict` queues a console message once
     (`_Spec.complained`) and `_on_line` returns `R_BREAKPOINT | LOG_FLAG`.
   - Native side: `adapter/breakpoints.py` `_check_condition` (called from
     `_native_breakpoint_wants_a_stop`) evaluates the condition once at the first stop,
     prints the error to the console and sends a `breakpoint` event with a message.
     Check that `breakpoints.py` imports `os`. State: `conditions_checked` in server.py.
   - Tests needed: Python `--if no_such_name`, C++ `name == "right"` on a std::string
     (see `tests/test_breakpoints.py` for the pattern).
3. **A library built without `-g`: grey breakpoints with a wrong reason, Step Into
   silently steps over.** Half done: `_native_bp_answer` has the new messages and reads
   `self.no_debug_info`, and the breakpoint state now includes the message. **Not written
   yet**: the code that fills `no_debug_info`. Plan: in `adapter/sources.py`
   `_on_modules_loaded` (remove its early return), for each new module that is not a
   system library, not the interpreter or helper, not under `site-packages`,
   `dist-packages` or `lib-dynload`, and has `GetNumCompileUnits() == 0`: append its name,
   print once "X.so has no debug info: its functions cannot be stepped into and
   breakpoints in its source will not bind; build it with -g", then call
   `_refresh_native_bp_status()`. Do the same scan over all modules at the end of
   `req_attach`. `tests/test_nosource.py` has a stripped extension to test with; check
   no existing test compares console output exactly.

## Not started

4. **Data breakpoint on a C++ local keeps firing after its function returned.** Put the
   frame's CFA and thread id into the `dataId` when the variable is a local or argument
   (`req_dataBreakpointInfo`); in `_watchpoint_stop`, if no frame of that thread has that
   CFA any more, delete the watchpoint, say so in the console and carry on.
5. **C++ throw stop shows the type twice and never `what()`.** `adapter/stops.py`
   `_report_native_exception`: the thrown object is in `rdi` at `__cxa_throw`. Walk the
   `type_info` single-inheritance chain (`+8` name, `+16` base) to see whether it derives
   from `St9exception`; if so evaluate `((const std::exception*)ptr)->what()` with
   `self._evaluate(frame, expr, 2)`. Put the message in the description; `exceptionId`
   keeps the type.
6. **Thread names** (`worker-0` shows as `python (413)`): `req_threads` in
   `adapter/stack.py`; at a safe stop ask the agent (`self.agent("threads")` returns
   tid and name), cache in a dict, use the cache at native stops.
7. **Library Python frames are not greyed** (a pytest stop shows 33 frames): in
   `req_stackTrace`, when `just_my_code`, give Python frames under `site-packages`,
   `dist-packages` or the standard library `presentationHint: "subtle"` and a source with
   `presentationHint: "deemphasize"` and an `origin`, which VS Code collapses.
8. **`seam --help` says nothing about LLDB, editors or docs** (`src/seam/cli.py`).
9. A Python expression typed at a native frame is compiled as C and fails with
   "undeclared identifier": add a hint to the error in `req_evaluate` when Python frames
   are below.
10. Optimised-away native locals show an empty value instead of saying so (`_sb_var`).
11. Type shown as `Mixer` at Python stops and `pkg.mod.Mixer` at native stops.
12. A native frame's Globals scope lists dozens of template statics from headers and
    missed the user's own file-level constant.

## Larger, by design so far (the tester's top wishes)

- Python is read-only text at native stops: lists, dicts and objects cannot be opened
  there. Reading them from memory (`adapter/pyread.py`) is possible; running Python there
  is not safe.
- Objects of extension types (a pybind11 instance, a numpy array) cannot be opened.
- No terminal front end (`seam run script.py`). The stand-in editor written for the
  trial is a starting point: `scratchpad\trial\dbg` (not in the repository).
- Logpoints cost about 8 ms per hit; a never-true C++ condition about 1.8 ms.

## Not Seam's

The tester's "attach takes 22 s" was the stand-in editor waiting for a first stop: the
adapter's own log shows the attach done in 2 s.

## When the fixes are in

Run the three batches on 3.12 and 3.14 and the -O2 smoke selection, `uvx ruff check src
tests tools`, then push to `main` with `[ci full]` in the commit message (LLDB 18 exists
only on CI). Update STATUS.md, ROADMAP.md and CHANGELOG.md, and delete this file.
