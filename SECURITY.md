# Security

Seam is a debugger: by design it controls another process, reads its memory and runs
code in it. That makes a few things worth stating plainly.

## What Seam does to the program it debugs

- It controls the process with `ptrace`, through LLDB. It needs the same permission any
  debugger needs, and no more: Seam never asks for root and has no setuid parts.
- It loads a small helper into the program (`_seam_trap` and `seam_agent`), which
  installs `sys.monitoring` callbacks and, while uncaught-exception stops are on, an audit
  hook and a wrapper around `threading.excepthook`.
- Expressions typed in the editor (the debug console, watch expressions, breakpoint
  conditions, log messages) are executed in the program with the program's privileges.
  Opening a project's `launch.json` and starting it is running that project's code.
- When attaching, Seam writes into the target's memory to make it load the helper
  (PEP 768 on Python 3.14+, a queued call on 3.12 and 3.13).

## What Seam does not do

- It opens no network ports. The adapter talks to the editor over its standard input
  and output; the only other channels are a pipe and Unix sockets in private temporary
  directories, between Seam's own processes on the same machine.
- It sends nothing anywhere. There is no telemetry.

## Attaching to processes

Attaching to a process that is not a child needs `ptrace` access to it. On most systems
that means lowering `kernel.yama.ptrace_scope`, which lets every process of a user debug
every other process of that user. Do that on development machines, not in production.
`seam doctor` reports the current setting.

## Log files

`SEAM_LOG` (or the extension's `seam.logFile` setting) makes the adapter write a protocol
log. It contains source paths, variable values and expression results from the debugged
program. Look through it before attaching it to a public bug report.

## Reporting a vulnerability

Please report security problems privately to the repository owner rather than in a public
issue, with enough detail to reproduce them.
