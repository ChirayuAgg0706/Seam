# Seam

A mixed-mode debugger for Python programs with native extensions (C, C++, Rust), on
Linux x86-64.

Python debuggers cannot see into native code, and native debuggers show CPython's
internals instead of Python lines. Seam is one debugger that understands both sides:

- breakpoints in `.py` files and in `.c`/`.cpp`/`.rs`/`.pyx` files, in one session;
- one call stack with Python and native frames interleaved in their true order;
- stepping from a Python line into the native function it calls, and back out;
- Python and native variables, each in their own frames;
- the Debug Adapter Protocol, so it works in VS Code and Neovim.

See [STATUS.md](STATUS.md) for exactly what is tested and what is not, and
[Limitations](#limitations) before relying on it.

## Requirements

- Linux x86-64.
- CPython 3.12, 3.13 or 3.14 as the program being debugged. Interpreters without debug
  info (uv-managed Pythons, `-slim` container images) are supported.
- LLDB 18 or newer, with its Python scripting support (the normal distro package). Tested with 18.1.3 and 20.1.2.
- A C compiler and the CPython headers, to build Seam's small in-process helper at
  install time.
- Permission to `ptrace` the program (the default when Seam launches it).

## Install

On Ubuntu 24.04:

```bash
sudo apt-get install -y lldb gcc python3-dev python3-venv git
git clone https://github.com/ChirayuAgg0706/Seam.git
python3 -m venv ~/.venvs/seam
~/.venvs/seam/bin/pip install ./Seam
~/.venvs/seam/bin/seam --version
```

Seam itself can live in any Python 3.12+ environment; it does not have to be the
environment of the program you debug. Put `~/.venvs/seam/bin` on `PATH`, or use the full
path to `seam` in the editor configuration below.

## Quick start

Suppose `demo.py` calls a function from your extension module:

```python
import mymodule

def main():
    total = mymodule.add(20, 22)
    print(total)

main()
```

Build the extension with debug info (`-g`; for Rust, a debug build or
`[profile.release] debug = true`).

**VS Code.** Build and install the extension, then press F5 on a Python file:

```bash
scripts/build-vsix.sh           # produces vscode/seam-debugger-0.1.0.vsix
code --install-extension vscode/seam-debugger-0.1.0.vsix
```

or add a `launch.json` entry:

```json
{
  "type": "seam",
  "request": "launch",
  "name": "Seam: demo",
  "program": "${workspaceFolder}/demo.py",
  "python": "${workspaceFolder}/.venv/bin/python"
}
```

Set a breakpoint on the `total = ...` line, start the session, and use **Step Into**: the
debugger stops inside your native `add`, with `main` and `<module>` below it in the same
call stack. **Step Out** returns to the Python line.

**Neovim.** See [docs/neovim.md](docs/neovim.md).

**Any DAP client.** The adapter is `seam dap`, speaking DAP on stdin/stdout.

### Launch options

| Option | Meaning |
|---|---|
| `program` / `module` | Script to run, or module to run with `-m`. |
| `args` | Arguments for the program. |
| `python` | Interpreter to run (default `python3`). Use your virtualenv's. |
| `pythonArgs` | Arguments for the interpreter itself. |
| `cwd`, `env` | Working directory and extra environment variables. |
| `stopOnEntry` | Stop on the first line of Python. |
| `stopOnSignals` | Signals that stop the debugger (default `SIGSEGV`, `SIGBUS`, `SIGILL`, `SIGFPE`, `SIGABRT`). Every other signal goes straight to the program. Also valid for attach. |
| `justMyCode` | For "Raised Python exceptions": ignore exceptions that stay inside libraries (default true). |
| `debugInfoLookup` | Let LLDB find separate debug-info files (default true). |
| `frameworkPaths` | Extra path fragments marking native source as glue to step through. |
| `showGlueFrames` | Show binding-layer trampoline frames in the call stack (default false). |

### Attach

```json
{ "type": "seam", "request": "attach", "name": "Seam: attach", "pid": 12345 }
```

Attaching needs ptrace permission for a non-child process
(`/proc/sys/kernel/yama/ptrace_scope` must be 0, or the program must allow it). See
[Limitations](#limitations) for what attach can and cannot do on each Python version.

### Exceptions

The editor's exception-breakpoint list offers four choices:

| Filter | Stops when |
|---|---|
| Uncaught Python exceptions (on by default) | an exception nobody handled is about to end the program or a thread. The call stack shows the frames it passed through, from the `raise` outwards; their variables can be inspected and expressions evaluated in them. |
| Raised Python exceptions | an exception is raised in your code, or first reaches your code from a library or from native code, even if it is handled afterwards. One stop per exception. |
| C++ throw | native code executes a `throw`. |
| Rust panic | Rust code panics. |

"Your code" means anything outside the standard library and `site-packages`; set
`"justMyCode": false` to treat library code the same as yours. `SystemExit` is not an
uncaught exception.

### Crashes and signals

If the program crashes in native code (a segfault, an `abort()`), Seam stops at the
faulting line with the usual merged call stack: the native
frames, then the Python frames that led there, with their locals. Continuing lets the
signal take its course, and the debug console says which signal ended the program.

Signals a Python program handles itself (`SIGINT`, `SIGTERM`, `SIGUSR1`, timers) do not
stop the debugger; they are delivered as if it were not there. Use `stopOnSignals` to
change which ones stop.

## How it works

The usual workaround for mixed debugging attaches two debuggers that each believe they
control the process. Seam has a single controller.

1. **LLDB owns the process.** It launches it, sets native breakpoints, steps native code
   and reads memory. Seam's adapter is a script running inside LLDB.
2. **A small helper runs inside the Python process.** It uses `sys.monitoring` (PEP 669)
   to watch Python-level events. When a Python breakpoint or step fires, it calls an empty
   C function, `seam_trap()`, on which LLDB keeps a breakpoint. Every Python stop is
   therefore a native stop at a known safe point: the GIL is held and the interpreter is
   consistent.
3. **Python is only executed in the target at safe points.** Evaluating expressions and
   reading variables with `repr()` happens at Python stops. At a native stop Seam reads
   memory and nothing else, with two narrow exceptions for its own bookkeeping, described
   in [docs/decisions.md](docs/decisions.md).
4. **The merged stack is built from raw memory**, without Python's debug info. Seam walks
   `_PyRuntime` → interpreter → thread state → frames, decodes code objects and line
   tables, and splices each run of Python frames into the native stack at the C frame
   whose stack area contains that run's entry frame.
5. **Stepping across the boundary** combines both sides. Stepping in from Python arms a
   Python step *and* a one-shot breakpoint on every user function of the extension
   modules, so the step lands in the first user code entered, Python or native, whatever
   binding layer sits in between. A native step that returns into the interpreter is
   handed to the helper, which stops on the calling Python line.

With no breakpoints set and no step in progress, the helper has no monitoring events
enabled, so the program runs at full speed. Stopping on uncaught exceptions costs nothing
either: it hangs off the hook the interpreter calls when it reports one.

## Limitations

Out of scope for this version: macOS, Windows, architectures other than x86-64,
free-threaded (no-GIL) builds, the experimental JIT, PyPy, sub-interpreters, and remote or
container debugging (Seam must run on the same machine and in the same container as the
program).

Known limits of what is in scope:

- **Python expressions cannot be evaluated at native stops.** If the program is stopped
  in C, C++ or Rust code, Seam refuses to run Python and says so. Python locals of the
  frames below are still shown, decoded from memory: `int`, `float`, `str`, `bytes`,
  `bool`, `None` and shallow `list`/`tuple` show their values; other objects show their
  type and address. Step or continue to a Python line for full inspection.
- **Optimised native code (`-O2`, Rust release builds).** Stepping in from Python needs
  the user function to exist as a function or as an inlined instance in the debug info;
  if the compiler removed it entirely the step behaves like step-over. A statement that
  is a single inlined library call can end up with no code of its own: a breakpoint on it
  is then reported as unverified, or moved to the next line that has code (Seam tells the
  client which). Debug builds do not have these problems.
- **Stepping into a Python callback from Cython or from optimised code** can take several
  presses of Step Into: the generated or optimised code spreads one source line over many
  small ranges, and each press advances to the next one. A breakpoint in the callback is
  the reliable alternative.
- **Exception stops.** At an uncaught exception, a C++ throw or a Rust panic there is
  nothing to step through (the frames have unwound, or control is about to leave by
  unwinding), so a step simply continues. Uncaught exceptions in threads are seen through
  `threading.excepthook`; a program that replaces that hook after start-up hides them
  from Seam. A thread started with the low-level `_thread` module is not covered.
- **Extension modules with more than 20,000 functions** are excluded from step-in from
  Python (a message says so); breakpoints in them work normally.
- **Changing Python breakpoints while the program runs** is applied by the main thread at
  its next bytecode boundary. If the main thread is blocked in a long native call, the
  change takes effect when that call returns.
- **Attach** loads the helper at the main thread's next safe point. A main thread blocked
  indefinitely in a system call will not get there, and the attach times out.
- **Program input.** The program runs on a pseudo-terminal owned by the adapter; its
  stdout and stderr arrive as one stream, and typing input into it is not supported yet.
- **Thread-heavy programs** run about twice as slowly under Seam even with no breakpoints,
  because LLDB handles every thread start and exit. CPU-bound work is unaffected.
- **Embedded interpreters.** Launch expects a normal `python` executable (it injects the
  helper at `Py_RunMain`). Programs that embed Python are not supported.
- **LLDB quirks.** Seam works around LLDB showing stale or cut-short frame lists (see
  [docs/decisions.md](docs/decisions.md) §4d and §12); the workaround calls `getpid()` in
  the target. Where LLDB genuinely cannot unwind a function (LLDB 20 through nanobind's
  optimised library code), native frames below it are missing; Seam says so in the debug
  console and still shows every Python frame.

## Development

```bash
scripts/test.sh -q              # full suite against /usr/bin/python3.12
SEAM_TEST_PYTHON=/path/to/python3.14 scripts/test.sh -q
SEAM_TEST_OPT=O2 scripts/test.sh -q
SEAM_TEST_REPEAT=20 scripts/test.sh -q -k stepping   # hunt for flakiness
```

The suite launches real programs under Seam through a scripted DAP client and needs LLDB,
gcc/g++, [uv](https://docs.astral.sh/uv/) and, for the PyO3 scenarios, a Rust toolchain.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
