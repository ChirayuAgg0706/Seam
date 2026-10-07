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
- Stepping follows a coroutine or generator across suspensions: step over an `await`
  ends on the next line of the same coroutine instead of inside asyncio.
- Stepping honours `justMyCode`: steps never end in the standard library or in installed
  packages, but do end in your code that they call.
- Debugging a pytest run (`"module": "pytest"`), and a fifth exception filter,
  "User-unhandled Python exceptions", which stops where a failing test's exception leaves
  your code.
- Child processes (`os.fork`, `subprocess`, `multiprocessing`, pytest-xdist) run
  undisturbed; the debug console says once that they are not debugged.
- `sourceMap`: native code built in a container, in CI or with remapped paths can be
  debugged against the sources on this machine. A breakpoint that cannot bind, or a stop
  whose source is missing, says which path the library was built from and what to add.
- Step Into no longer slows down with the size of the loaded extension modules (it took
  1.3 s per step with a 15,000-function module loaded, and never reached native code in
  modules over 20,000 functions such as pydantic-core).
- Frames without source are named `library!function`; the `disassemble` request and
  stepping by instruction make VS Code's disassembly view work for them. A crash inside
  the interpreter shows the interpreter function that faulted.
- `seam doctor`: checks the installation and runs a real debug session.
- VS Code: the extension contains the debug adapter and its compiled helper (package
  target `linux-x64`); installing Seam separately is no longer needed.
  `seam.adapterCommand` is empty by default and still selects an installation of your own.
- VS Code: without `"python"` in the configuration, the project's interpreter is used
  (the Python extension's selection, `python.defaultInterpreterPath`, `.venv`/`venv`,
  `python3`).
- VS Code: `"pid": "${command:seam.pickProcess}"` picks the process to attach to.
- VS Code: the command "Seam: Check This Machine" runs `seam doctor` out of the
  extension.
- Python 3.15 (tested with 3.15.0rc3).
- LLDB 19 and 20, besides 18.

### Changed

- Prefer installed `lldb-20` and `lldb-19` over the distribution's plain `lldb`, which
  may still be 18 and suffer from the threaded-child-process session failure. Explicit
  `SEAM_LLDB` overrides remain authoritative; installation guidance now recommends 19.
- Python library frames are deemphasized with `justMyCode`; native stops retain cached
  Python thread names, and Python variable types have the same short name at either stop.
- Native Globals show declarations from the current file, including file-level constants.
- `seam --help` names the requirements, editors and documentation.
- The native-call throughput benchmark reports initial library loading separately from
  steady work; its 10% bound is unchanged. The exception fixture uses CPython's standard
  hook, with a separate replacement-hook scenario, to exclude Ubuntu's crash reporter.
- The terminal child Ctrl-C fixture announces readiness after setting its signal
  disposition, removing a shell-fork race that also occurred without the debugger.

- In the debug console (`internalConsole`) the program's standard input is empty instead
  of a terminal nobody can type into.
- The VS Code extension runs the program in the integrated terminal by default, and
  keeps that terminal in view when a session starts.
- VS Code: a missing LLDB or Python is reported in the adapter's own words instead of
  "terminated unexpectedly".
- Attach: a `pid` given as text is accepted; one that is not a number is refused clearly.
- Stopping a session ends the program's whole process group, not only the program, and
  Ctrl-C in the program's terminal reaches its children too.
- Python frames are reported under the path the editor uses for the file (a project
  opened through a symbolic link), not under the resolved path.

### Fixed

- Stepping out after stepping over a suspended `await` now stops in the awaiting
  caller instead of letting the program exit. Step Over and Step Into at the same
  return instruction also follow the caller.
- Python Globals and module-level Locals remain visible at native stops, decoded
  from memory without running Python or calling object representations.

- Native stops without source remain the selected stack frame in VS Code, making
  disassembly available without manually selecting a dimmed frame above the Python caller.
- Inherited debuginfod servers no longer stall launch/attach or interrupt helper
  injection with a misleading deleted-breakpoint error. Local separate debug symbols
  remain enabled; automatic network downloads are disabled inside the debugger.
- Startup logs identify target creation, process launch, symbol lookup and helper
  injection phases, with elapsed time and stop details when an injected call fails.
- Helpers built with newer Python headers can dispatch debugger requests on Python
  3.12, as promised by the stable-ABI wheel tag.
- Plain breakpoints on `await` no longer stop again on hidden cancellation or timeout
  cleanup instructions on Python 3.13+, when continuing without a step armed.

- Launch and attach no longer add `sys` or `seam_agent` to the program's globals.
- Unevaluable breakpoint conditions report their error once instead of silently stopping.
- User libraries built without debug info explain their unbound breakpoints and skipped
  Step Into calls, including libraries already loaded when attaching.
- Local native data breakpoints expire when their stack slot is reused after return.
- C++ throw stops show `std::exception::what()` where RTTI identifies the base, keeping
  the type in the exception identifier.
- Failed expressions in a native frame explain how to reach a Python frame; unavailable
  native locals show a reason instead of an empty value.

- A program left running, with nobody attached, when LLDB died.
- An internal error instead of an error message for a request with a missing argument.
- Rare extra or missing stops on a busy machine. Seam shared its debugger object with
  the `lldb` program it runs in, whose own event thread handled the same events; it now
  has a debugger of its own.
- Stepping from a line where binding-layer code is inlined into your function (optimised
  Rust and C++) could run the program to its end; it also took one press of Step Into
  per inlined piece.
- Stepping out of native code failed after 30 seconds in programs with busy Python
  threads.
- "Raised Python exceptions" never stopped for an exception raised by a Cython library.
- An expression that starts a process (`subprocess.run(...)` typed into the debug
  console) ended the session.
- Ctrl-C in the program's terminal did nothing while the program sat in `os.system()`.
- The exit of a program whose child process was still running was reported two seconds
  late.
- An attach that was refused because the program's main thread was blocked left its
  request in the program, which later printed an error (Python 3.14) or loaded Seam's
  helper with no debugger attached (3.12, 3.13).

## 0.1.0

The first complete version: breakpoints, one merged call stack, variables, evaluation
and stepping across the Python/native boundary for C API, pybind11, nanobind, Cython and
PyO3 extensions; launch and attach; CPython 3.12 to 3.14; a VS Code extension and an
nvim-dap configuration. See [STATUS.md](STATUS.md).
