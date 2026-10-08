# Apple Silicon feasibility, stage 1

This is an opt-in experiment on `codex/macos-stage1`. Released Seam still supports
Linux x86-64, including WSL. No macOS package has been published.

## What passed

GitHub's `macos-15` ARM64 runners built and installed the wheel with Python 3.12.10,
3.13.15 and 3.14.7. The helper contains an ARM64 Mach-O bundle. Apple LLDB
`lldb-1700.0.9.502` launched the ARM64 interpreter and ran the injected helper.

[The first successful run](https://github.com/ChirayuAgg0706/Seam/actions/runs/37721537663)
passed five complete sessions per Python version. Each session checked a Python
source breakpoint, stack file and line, integer and string locals, expression
evaluation, stepping to the next line, the updated local, output, exit status and
termination. The client launched the installed wheel, not the source checkout.
Launch through the first breakpoint took 1.73 to 4.25 seconds in these small tests.
These timings do not establish performance on large projects.

The workflow also checks that ARM64 requires the experimental flag and that the
flag does not enter the debugged program's environment. Artifacts contain the
wheel, protocol logs, LLDB output and a JSON report for each Python version.

No extra signing, entitlement changes, `sudo` commands or system security changes
were needed on these runners. That establishes permissions for the tested runner
and Python distributions, not every user's Mac or every signed executable.

On Linux, 36 focused regression scenarios passed with LLDB 20 and Python 3.12.
They cover Python debugging, interpreter rejection, mixed Python/C debugging,
exceptions and native entry breakpoints.

## Changes needed

- Read ARM64 function arguments from `x0`, `x1` and `x2` rather than x86 registers.
- Read the ARM64 program counter from `pc`.
- Allow missing fork stop constants in Apple's LLDB Python bindings.
- Continue the macOS framework Python launcher's `exec` stop until `Py_RunMain`.
- Disable Seam's Linux `/proc` and x86 instruction-patching optimization on macOS.
  Ordinary LLDB breakpoints remain available.
- Require `SEAM_EXPERIMENTAL_MACOS=1` for the ARM64 experiment.

## Run it again

Push this branch to trigger `.github/workflows/macos-stage1.yml`. It uses actual
ARM64 runners and installs a freshly built wheel into a virtual environment.
The workflow does not publish packages or change the released extension.

On an Apple Silicon machine with Xcode command-line tools and CPython 3.12 or newer,
build and install the branch's wheel into a virtual environment, then run:

```sh
SEAM_LLDB=/usr/bin/lldb SEAM_EXPERIMENTAL_MACOS=1 \
  python scripts/macos-stage1-check.py --output stage1-results --repeat 5
```

## What this does not establish

Stage 1 proves the helper and basic adapter path work on Apple Silicon. Full
Python-to-Rust/C/C++ stepping, native stops and raw Python inspection there,
attach, threads, async programs, child processes, terminal interaction,
disassembly, large modules and other Python distributions still need Mac tests.
The current `seam doctor`, VS Code bundle and release workflows also need macOS
support before a public release. A manual VS Code install check on a user's Mac
remains useful after those automated tests pass.
