# Apple Silicon validation

Seam's Mac port uses Apple's LLDB and native ARM64 CPython. The release targets
macOS 14 and newer. Intel Macs and Python running through Rosetta are unsupported.
The initial experiment required `SEAM_EXPERIMENTAL_MACOS=1`; current builds work
without that flag.

## What the checks cover

The Apple Silicon workflow runs on actual macOS 14 and 15 ARM64 runners with
CPython 3.12, 3.13 and 3.14. It exercises the existing debugger behavior, including
Python and native stepping, C API, PyO3, pybind11, nanobind, Cython, mixed stacks,
variables, breakpoints, exceptions, attach, async code, threads, child processes,
terminal input, crash handling and disassembly. Optimized native builds run as well
as debug builds.

Separate jobs install freshly built wheels, repeat complete debug sessions, and
run the packaged extension in real VS Code. The Python extension interpreter
selection and Neovim's documented nvim-dap configuration are checked too.

[The final runtime matrix](https://github.com/ChirayuAgg0706/Seam/actions/runs/37770733074)
passed every native matrix cell, including the full Mac 15 CPython 3.12 suite with
298 passing tests and 16 expected skips. Mac 14 passed 242 debug-build and 240
optimized-build smoke checks. Installed wheels, architecture rejection, packaged
VS Code, Python-extension selection, Neovim, attach updates, fork/crash cases and
optimized regressions passed. The matrix's real-project job still used an outdated
C++ exception-description assertion; the corrected check passed in the final run below.

[The final real-project and scale run](https://github.com/ChirayuAgg0706/Seam/actions/runs/37773549934)
passed all 12 selected cases against regex, msgpack, contourpy, pydantic-core and
a generated 15,000-function module. The pydantic-core Python Step Into case remains
excluded because it exceeds the documented Mac function-count limit. Source/function
breakpoints, Python callbacks, native returns and exceptions in that library passed.
The final no-breakpoint timing ratios were 1.011 for Python work and 1.053 for
native work. These are measurements on a runner, not a universal overhead promise.

The final Linux suite passed 300 tests with 14 expected skips under LLDB 20.
Linux optimized smoke coverage passed 240 cases with three compiler-related skips;
its remaining help-text assertion passed after the CLI wording update. The final
Step Out changes also passed 39 focused Linux cases across debug and optimized builds.

An earlier Mac optimized run stalled during the debugger-exit test. The harness now
bounds process-status commands and closes benchmark sessions between samples.
Subsequent complete suites and 15 repeated exit cases passed. No Mac manual test
on the owner's laptop was needed; these jobs used actual ARM64 hardware.

## Port changes

- Read ARM64 arguments and program counters through their native registers.
- Follow framework Python's launcher exec to the interpreter entry point.
- Use libproc and sysctl for process discovery instead of `/proc`.
- Restore inherited software and hardware breakpoints in forked children.
- Keep optimized frameless and inline native functions in the mixed stack.
- Pass faults through macOS signal handlers before resuming the program.
- Package a thin ARM64 helper with a macOS 14 deployment target.

## Limits

The Linux entry-trap shortcut writes `/proc/<pid>/mem` and cannot run on macOS.
Mac Step Into excludes modules with more than 20,000 functions; source and function
breakpoints remain available. With 15,000 functions loaded, the measured first
Step Into took 5.3 seconds and repeated entries took 2.7-3.2 seconds. The Linux
subsecond performance bound does not apply to this Mac path. LLVM can omit an interrupted native frame under a
signal handler. Seam keeps Python callers and reports native unwind problems.

The CI checks needed no signing changes, `sudo`, or system security changes.
That proves permissions for the tested runners and interpreters. It does not give
Seam permission to attach to protected system processes or every signed executable.

## Run the checks

Dispatch `.github/workflows/macos-stage1.yml` from the Actions page. It tests the
selected commit without publishing packages.

After installing a wheel on an Apple Silicon machine:

```sh
SEAM_LLDB=/usr/bin/lldb python scripts/macos-stage1-check.py --output mac-results --repeat 5
```

The script drives the installed adapter rather than importing it from the checkout.
