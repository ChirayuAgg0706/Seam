# Security

Seam controls the program you debug, reads its memory and evaluates code inside it.
Only debug programs and configurations you trust.

## What Seam does to the program it debugs

- It controls the process with `ptrace`, through LLDB. It needs the same permission any
  debugger needs. Seam does not require root and has no setuid components.
- It loads `_seam_trap` and `seam_agent` into the program. The helper installs
  `sys.monitoring` callbacks. Uncaught-exception stops also use an audit hook
  and a wrapper around `threading.excepthook`.
- Expressions typed in the editor (the debug console, watch expressions, breakpoint
  conditions, log messages) are executed in the program with the program's privileges.
  Opening a project's `launch.json` and starting it is running that project's code.
- When attaching, Seam writes into the target's memory to make it load the helper
  (PEP 768 on Python 3.14+, a queued call on 3.12 and 3.13).

## Connections and telemetry

The adapter communicates with the editor over standard input and output. Seam's
other processes use a pipe and Unix sockets in private temporary directories on
the same machine. Seam opens no network ports and sends no telemetry.

## Attaching to processes

Attaching to a process that is not a child needs `ptrace` access to it. On most systems
that means lowering `kernel.yama.ptrace_scope`, which lets every process of a user debug
every other process of that user. Do that on development machines, not in production.
`seam doctor` reports the current setting.

## Log files

Set `SEAM_LOG` or the extension's `seam.logFile` setting to record a protocol log.
The log can contain source paths, variable values and expression results.
Check it and its `.lldb` companion for private information before sharing them.

## Reporting a vulnerability

Please report security problems privately to the repository owner rather than in a public
issue, with enough detail to reproduce them.
