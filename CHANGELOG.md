# Changelog

Seam has not been released yet. Version numbers follow semantic versioning once it is;
until 1.0 any release may change behaviour.

## Unreleased

Work towards a tool people can rely on; see [ROADMAP.md](ROADMAP.md).

### Added

- Crashes: a segfault or abort in an extension stops with the merged stack; death by
  signal is reported as such, with the conventional exit code (128 + signal).
- Signals a program handles itself no longer stop the debugger; `stopOnSignals` chooses
  which do.
- Exception breakpoints: uncaught and raised Python exceptions (with `justMyCode`), C++
  `throw`, Rust panic; exception details in the editor.
- `console: integratedTerminal` / `externalTerminal`: the program runs in the editor's
  terminal and can read input; Ctrl-C there interrupts it.
- Hit-count conditions and logpoints, on Python and native breakpoints.
- Set variable (Python locals, globals and members; native variables); long lists in
  pages; expressions for nested values ("Add to Watch").
- Function breakpoints on Python and native functions, with conditions and hit counts.
- Data breakpoints on native variables.
- Completion in the debug console: names in the frame and attributes of the name before
  the cursor.
- `seam doctor`: checks the installation and runs a real debug session.
- Python 3.15 (tested with 3.15.0rc3).
- LLDB 19 and 20, besides 18.

### Changed

- In the debug console (`internalConsole`) the program's standard input is empty instead
  of a terminal nobody can type into.
- The VS Code extension runs the program in the integrated terminal by default.

### Fixed

- A program left running, with nobody attached, when LLDB died.
- An internal error instead of an error message for a request with a missing argument.
- Rare extra or missing stops on a busy machine. Seam shared its debugger object with
  the `lldb` program it runs in, whose own event thread handled the same events; it now
  has a debugger of its own.
- An attach that was refused because the program's main thread was blocked left its
  request in the program, which later printed an error (Python 3.14) or loaded Seam's
  helper with no debugger attached (3.12, 3.13).

## 0.1.0

The first complete version: breakpoints, one merged call stack, variables, evaluation
and stepping across the Python/native boundary for C API, pybind11, nanobind, Cython and
PyO3 extensions; launch and attach; CPython 3.12 to 3.14; a VS Code extension and an
nvim-dap configuration. See [STATUS.md](STATUS.md).
