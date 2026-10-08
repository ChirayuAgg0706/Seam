"""Starting and ending a session: launch, attach, the terminal, exit, detach."""
import json
import os
import select
import shutil
import signal
import socket
import struct
import sys
import tempfile
import termios
import threading
import time

import lldb

from . import pyread
from .common import (
    CONSOLES, DapError, EVAL_PLEASE_STOP_BIT, EXIT_PACKET, FAULT_SIGNALS, FORK_PACKET,
    FRAMEWORK_PATHS, HELPER_SYMBOLS, LLDB_SIGNALS, PRIVATE_ENV, TARGET_DIR, TERMINAL_HOLDER,
    TERMINAL_SIGNALS,
)


class SessionMixin:
    def _watch_macos_children(self):
        """Apple debugserver lacks fork packets; observe the native spawn calls instead."""
        if sys.platform != "darwin" or hasattr(self, "mac_child_entries"):
            return
        self.mac_child_entries, self.mac_child_returns = {}, {}
        for name in ("fork", "vfork", "posix_spawn"):
            bp = self._entry_breakpoint(name)
            self.mac_child_entries[bp.GetID()] = name

    def _macos_child_stop(self, thread):
        if (not hasattr(self, "mac_child_entries")
                or thread.GetStopReason() != lldb.eStopReasonBreakpoint):
            return False
        hit = thread.GetStopReasonDataAtIndex(0)
        kind = self.mac_child_entries.get(hit)
        frame = thread.GetFrameAtIndex(0)
        if kind is not None:
            pid_pointer = None
            if kind == "posix_spawn":
                error = lldb.SBError()
                path = self.process.ReadCStringFromMemory(
                    self._entry_argument(frame, 1), 4096, error)
                if not error.Success() or not os.path.basename(path).lower().startswith("python"):
                    return True
                pid_pointer = self._entry_argument(frame, 0)
            elif kind == "fork" and any("fork_exec" in (f.GetFunctionName() or "")
                                        for f in thread):
                kind = "fork_exec"
            address = frame.FindRegister("lr").GetValueAsUnsigned()
            bp = self.target.BreakpointCreateByAddress(address)
            bp.SetThreadID(thread.GetThreadID())
            self.mac_child_returns[bp.GetID()] = (kind, pid_pointer)
            return True
        returned = self.mac_child_returns.pop(hit, None)
        if returned is None:
            return False
        self.target.BreakpointDelete(hit)
        kind, pointer = returned
        value = self._entry_argument(frame, 0)
        pid = struct.unpack("<i", self._read(pointer, 4))[0] if pointer and value == 0 else value
        if pid <= 0 or pid >= 1 << 31:
            return True
        if kind in ("vfork", "fork_exec"):
            from seam._mac_processes import process_executable
            # fork_exec returns before the child execs. The child runs independently;
            # wait briefly for its executable to settle before identifying it.
            if kind == "fork_exec":
                time.sleep(0.05)
            executable = process_executable(pid)
            if not os.path.basename(executable).lower().startswith("python"):
                return True
        self._notice_child(pid)
        for bp_id in list(self.mac_child_entries) + list(self.mac_child_returns):
            self.target.BreakpointDelete(bp_id)
        self.mac_child_entries.clear()
        self.mac_child_returns.clear()
        return True

    def _notice_child(self, pid):
        if self.child_noticed:
            return
        self.child_noticed = True
        self.event("output", {"category": "console", "output":
                   "Seam: the program started a child process (pid %d). Seam debugs only the "
                   "program itself: child processes run freely, and breakpoints in them do "
                   "not stop.\n" % pid})

    def _sync_macos_fork_table(self):
        """Let forked children remove Apple's inherited ARM64 software breakpoints."""
        if sys.platform != "darwin" or not self.sym.get("seam_fork_table"):
            return
        addresses = set()
        for index in range(self.target.GetNumBreakpoints()):
            bp = self.target.GetBreakpointAtIndex(index)
            if not bp.IsEnabled():
                continue
            for location in bp:
                address = location.GetAddress().GetLoadAddress(self.target)
                if location.IsEnabled() and address != lldb.LLDB_INVALID_ADDRESS:
                    addresses.add(address)
        addresses = tuple(sorted(addresses))
        if addresses == getattr(self, "mac_fork_addresses", None):
            return
        # ReadMemory returns original code under LLDB's software breakpoints.
        data = b"".join(struct.pack("<QQ", address,
                                   struct.unpack("<I", self._read(address, 4))[0])
                        for address in addresses)
        self.log("macOS fork cleanup:", len(addresses), "sites; first records", data[:48].hex())
        capacity = getattr(self, "mac_fork_capacity", 0)
        if len(data) > capacity:
            error = lldb.SBError()
            capacity = max(32768, len(data))
            allocation = self.process.AllocateMemory(
                capacity, lldb.ePermissionsReadable | lldb.ePermissionsWritable, error)
            if not error.Success():
                raise DapError("cannot allocate the macOS fork cleanup table: %s"
                               % error.GetCString())
            old = getattr(self, "mac_fork_allocation", None)
            self.mac_fork_allocation, self.mac_fork_capacity = allocation, capacity
            self._write(self.sym["seam_fork_table"], struct.pack("<Q", allocation))
            if old is not None:
                self.process.DeallocateMemory(old)
        if data:
            self._write(self.mac_fork_allocation, data)
        self._write(self.sym["seam_fork_count"], struct.pack("<q", len(addresses)))
        self.mac_fork_addresses = addresses

    def _watch_exit_packets(self):
        """Learn whether the program exited or was killed by a signal.

        LLDB's API reports both as an exit status (SIGKILL and `sys.exit(9)` both read 9,
        with no description). The difference survives in one place only: the last packet
        of the debug-server protocol. So that channel is logged to a callback which keeps
        nothing but that packet, and the ones that report a child process.
        """
        def on_log(line):
            match = EXIT_PACKET.search(line)
            if match:
                self.exit_packet = (match.group(1), int(match.group(2), 16))
            elif "fork" in line and not self.child_noticed:
                self._on_fork_packet(line)

        self._on_log = on_log  # LLDB does not keep the callable alive
        self.dbg.SetLoggingCallback(on_log)
        self.dbg.HandleCommand("log enable gdb-remote packets")

    def _on_fork_packet(self, line):
        """Say once that the program has started a child process Seam does not debug.

        LLDB deals with a fork by itself (it takes its breakpoints out of the child and
        lets the child go) and reports it to nobody; the debug server's stop reply is the
        one place where it shows. Only a child that runs Python is worth a message: a
        fork, which is a copy of the program, or a new process whose executable is a
        Python interpreter. The latter is looked at when the debug server reports that
        the child has left the parent's memory ("vforkdone"), which is after its exec.
        Called on one of LLDB's threads.
        """
        match = FORK_PACKET.search(line)
        if not match:
            return
        thread, kind, child = match.groups()
        if kind == "vfork":
            self.vfork_children[thread] = int(child, 16)
            return
        if kind == "vforkdone":
            pid = self.vfork_children.pop(thread, None)
            try:
                name = os.path.basename(os.readlink("/proc/%d/exe" % pid))
            except (OSError, TypeError):
                return  # gone already, or a vfork that was never reported
            if not name.startswith("python"):
                return
        else:
            pid = int(child, 16)
        self._notice_child(pid)

    def _drain_output(self):
        for getter, category in ((self.process.GetSTDOUT, "stdout"),
                                 (self.process.GetSTDERR, "stderr")):
            while True:
                text = getter(4096)
                if not text:
                    break
                self.event("output", {"category": category, "output": text})

    def _pump_output(self, master):
        """Forward everything the target writes to its pty as DAP output events."""
        try:
            while True:
                select.select([master], [], [])
                # Reading and forwarding are one step for _flush_output.
                with self.output_lock:
                    data = os.read(master, 65536)
                    if not data:
                        break
                    self.event("output", {"category": "stdout",
                                          "output": data.decode("utf-8", "replace")})
        except OSError:
            pass  # EIO: every writer has closed the pty
        finally:
            with self.output_lock:
                self.output_master = None
                os.close(master)

    def _flush_output(self, timeout=2):
        """Wait until what the program wrote before it ended has been forwarded.

        The wait is for the pty to be empty, not for its end: a child process that
        outlives the program keeps the pty open for as long as it likes (and what it
        writes later is still forwarded, for as long as the session lasts).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.output_lock:
                master = self.output_master
                if master is None or not select.select([master], [], [], 0)[0]:
                    return
            time.sleep(0.005)

    def _on_exit(self):
        if self.exited:
            return
        self.exited = True
        self.running = False
        self._flush_output()
        self._drain_output()
        self._release_terminal()
        code = self.process.GetExitStatus()
        if self.exit_packet == ("X", code):
            self.event("output", {"category": "console", "output":
                       "Seam: the program was terminated by signal %s.\n"
                       % self._signal_name(code)})
            code += 128  # what a shell would report
        elif self.exit_packet is None and code == -1:
            # No exit was reported: LLDB lost the program. The one known cause is
            # LLDB 18 meeting child processes started by several threads at the same
            # moment (llvm-project #81564, fixed in LLDB 19).
            self.event("output", {"category": "console", "output":
                       "Seam: LLDB lost contact with the program, so the session is over; "
                       "this is a failure inside LLDB (%s). LLDB 18 does this when several "
                       "threads start child processes at the same moment; LLDB 19 and "
                       "newer do not.\n" % self.dbg.GetVersionString().split("\n")[0]})
        self.event("exited", {"exitCode": code})
        self.event("terminated")

    @staticmethod
    def _signal_name(number):
        try:
            return signal.Signals(number).name
        except ValueError:
            return str(number)

    def _kill(self):
        """End the session: kill a program Seam launched, detach from one it attached to."""
        if self.process is not None and self.process.IsValid() and not self.exited:
            if self.attached:
                self._detach()
            else:
                self.process.Kill()
                self._signal_children(signal.SIGKILL)
                self.exited = True
        for path in self.temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.temp_files = []
        self.traps.close()
        self._release_terminal()

    def _signal_children(self, number):
        """Send a signal to the child processes of a program Seam launched.

        LLDB starts the program as the leader of a process group of its own, so that
        group is the program plus those of its descendants that have not left it. Stopping
        the session ends them with the program, as Ctrl-C or closing the terminal would
        when it runs by hand. A child that moved to a session or group of its own (a
        daemon) is not touched; nor is anything when the program ends by itself.
        """
        if self.program_group is not None:
            try:
                os.killpg(self.program_group, number)
            except OSError:
                pass  # nobody is left in the group

    def _release_terminal(self):
        """Hang up on the terminal holder: the program is gone, the terminal is free."""
        if self.terminal is not None:
            try:
                self.terminal.shutdown(socket.SHUT_RDWR)
                self.terminal.close()
            except OSError:
                pass
            self.terminal = None

    def _resolve_python(self, name):
        path = name if os.sep in name else shutil.which(name)
        if not path or not os.path.exists(path):
            raise DapError("Python interpreter not found: %s" % name)
        return os.path.abspath(path)

    def _apply_settings(self, args):
        if sys.platform == "darwin":
            # Let the kernel translate faults to BSD signals. Resuming a raw Mach
            # exception otherwise retries the fault forever instead of delivering it.
            result = lldb.SBCommandReturnObject()
            self.dbg.GetCommandInterpreter().HandleCommand(
                "settings set platform.plugin.darwin.ignored-exceptions "
                "EXC_BAD_ACCESS|EXC_BAD_INSTRUCTION|EXC_ARITHMETIC", result)
            if not result.Succeeded():
                raise DapError("cannot configure macOS fault delivery: %s" % result.GetError())
        unknown = [str(s) for s in args.get("stopOnSignals") or ()
                   if str(s).upper() not in signal.Signals.__members__]
        if unknown:
            raise DapError("stopOnSignals: unknown signal %s" % ", ".join(unknown))
        if not args.get("debugInfoLookup", True):
            self.dbg.HandleCommand("settings set symbols.enable-external-lookup false")
        # A native step that leaves user code must stop as soon as it is back in the
        # interpreter, so Seam can hand the step over to the Python side.
        self.dbg.HandleCommand(
            "settings set target.process.thread.step-out-avoid-nodebug false")
        self.framework_paths = FRAMEWORK_PATHS + tuple(args.get("frameworkPaths") or ())
        self.show_glue_frames = bool(args.get("showGlueFrames"))
        self.just_my_code = bool(args.get("justMyCode", True))
        self._set_source_map(args.get("sourceMap"))
        for named in (args.get("cwd"), args.get("program")):
            self._note_client_path(named)  # how the editor spells the project's directory

    def _apply_signal_policy(self, args):
        """Stop on the signals that mean a crash; hand every other signal to the program.

        LLDB's defaults suit C programs: it stops on SIGUSR1, SIGTERM, SIGPIPE and friends
        and swallows SIGINT. Python programs use those routinely (handlers, timers,
        KeyboardInterrupt), so by default only fault signals stop the debugger.
        """
        wanted = args.get("stopOnSignals")
        wanted = FAULT_SIGNALS if wanted is None else tuple(str(s).upper() for s in wanted)
        signals = self.process.GetUnixSignals()
        known = {}
        for i in range(signals.GetNumSignals()):
            number = signals.GetSignalAtIndex(i)
            known[signals.GetSignalAsCString(number)] = number
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise DapError("stopOnSignals: unknown signal %s" % ", ".join(unknown))
        for name, number in known.items():
            if name in LLDB_SIGNALS:
                continue
            signals.SetShouldStop(number, name in wanted)
            signals.SetShouldNotify(number, name in wanted)
            signals.SetShouldSuppress(number, False)

    def _require_no_session(self):
        if self.target is not None:
            raise DapError("this session is already debugging a program")

    def _require_x86_64(self):
        triple = self.target.GetTriple() or ""
        # Stage 1 is an explicitly opted-in feasibility experiment, not released support.
        if (sys.platform == "darwin" and triple.startswith(("arm64", "aarch64"))
                and os.environ.get("SEAM_EXPERIMENTAL_MACOS") == "1"):
            self.log("experimental Apple Silicon target:", triple)
            return
        if triple and not triple.startswith("x86_64"):
            raise DapError("Seam supports x86-64 Linux programs only; this one is %s" % triple)

    def req_launch(self, args):
        self._require_no_session()
        python = self._resolve_python(args.get("python") or "python3")
        self.cwd = args.get("cwd") or os.getcwd()
        if not os.path.isdir(self.cwd):
            raise DapError("working directory does not exist: %s" % self.cwd)
        argv = list(args.get("pythonArgs") or [])
        if args.get("module"):
            argv += ["-m", args["module"]]
        elif args.get("program"):
            argv.append(args["program"])
        else:
            raise DapError("launch needs either 'program' or 'module'")
        argv += [str(a) for a in args.get("args") or []]

        self._apply_settings(args)
        console = args.get("console") or "internalConsole"
        if console not in CONSOLES:
            raise DapError("console must be one of %s" % ", ".join(CONSOLES))
        terminal_tty = None
        if console != "internalConsole":
            terminal_tty = self._open_terminal(console)
        err = lldb.SBError()
        self.log("launch: create target", python)
        phase = time.monotonic()
        self.target = self.dbg.CreateTarget(python, None, None, False, err)
        self.log("launch: target created in %.3f s" % (time.monotonic() - phase))
        if not self.target or not self.target.IsValid():
            raise DapError("cannot create a target for %s: %s" % (python, err.GetCString()))
        self._require_x86_64()
        bp_main = self._entry_breakpoint("Py_RunMain")
        self.log("launch: entry breakpoint", bp_main.GetID(), "locations",
                 bp_main.GetNumLocations())

        info = lldb.SBLaunchInfo(argv)
        info.SetWorkingDirectory(self.cwd)
        # The program inherits the environment `seam dap` was started in, plus launch "env".
        env = {k: v for k, v in os.environ.items() if k not in PRIVATE_ENV}
        env.update({str(k): str(v) for k, v in (args.get("env") or {}).items()})
        info.SetEnvironmentEntries(["%s=%s" % kv for kv in env.items()], False)
        info.SetListener(self.listener)
        master = slave = None
        if terminal_tty is not None:
            # The client's terminal: the program reads the keyboard and writes there.
            info.AddOpenFileAction(0, terminal_tty, True, False)
            info.AddOpenFileAction(1, terminal_tty, False, True)
            info.AddOpenFileAction(2, terminal_tty, False, True)
        else:
            # The debug console. Output goes to a pty owned by the adapter (LLDB's driver
            # would otherwise swallow it, and a pipe would make the program buffer it).
            # Nobody can type into the debug console, so input is empty rather than a
            # terminal that never answers.
            master, slave = os.openpty()
            attrs = termios.tcgetattr(slave)
            attrs[1] &= ~termios.ONLCR
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            tty = os.ttyname(slave)
            info.AddOpenFileAction(0, os.devnull, True, False)
            info.AddOpenFileAction(1, tty, False, True)
            info.AddOpenFileAction(2, tty, False, True)
        self.log("launch: starting process")
        phase = time.monotonic()
        self.process = self.target.Launch(info, err)
        self.log("launch: process launch returned in %.3f s" % (time.monotonic() - phase))
        if slave is not None:
            os.close(slave)
        if not err.Success() or not self.process or not self.process.IsValid():
            if master is not None:
                os.close(master)
            raise DapError("launch failed: %s" % err.GetCString())
        pid = self.process.GetProcessID()
        self._note("launched %d" % pid)
        try:
            if os.getpgid(pid) == pid:
                self.program_group = pid
        except OSError:
            pass
        if master is not None:
            self.output_master = master
            self.output_thread = threading.Thread(target=self._pump_output, args=(master,),
                                                  daemon=True)
            self.output_thread.start()
        else:
            threading.Thread(target=self._serve_terminal, args=(self.terminal, pid),
                             daemon=True).start()
        state = self._wait_stop()
        # Framework Python on macOS starts through a launcher which execs the real
        # interpreter. Apple LLDB reports that exec before our pending entry breakpoint.
        # Keep waiting for Py_RunMain, with a deadline even for a broken exec loop.
        bootstrap_deadline = time.monotonic() + 30
        while (sys.platform == "darwin" and state == lldb.eStateStopped
               and self.process.GetSelectedThread().GetStopReason() == lldb.eStopReasonExec):
            self.log("launch: framework interpreter exec; waiting for Py_RunMain")
            self._require_x86_64()
            if time.monotonic() >= bootstrap_deadline:
                raise DapError("timed out waiting for the macOS interpreter launcher")
            resumed = self.process.Continue()
            if not resumed.Success():
                raise DapError("cannot continue the interpreter launcher: %s"
                               % resumed.GetCString())
            state = self._wait_stop(timeout=max(1, bootstrap_deadline - time.monotonic()))
        if state != lldb.eStateStopped:
            self._on_exit()
            raise DapError("the process exited before reaching Py_RunMain; is %s a "
                           "CPython 3.12+ interpreter?" % python)
        thread = self.process.GetSelectedThread()
        if (thread.GetStopReason() != lldb.eStopReasonBreakpoint
                or thread.GetStopReasonDataAtIndex(0) != bp_main.GetID()):
            raise DapError("unexpected stop before Py_RunMain: %s" % thread.GetStopDescription(200))
        self.target.BreakpointDelete(bp_main.GetID())
        self.log("launch: at entry, deleted bootstrap breakpoint; pc",
                 "%#x" % thread.GetFrameAtIndex(0).GetPC(), "stop-id", self.process.GetStopID())
        self._apply_signal_policy(args)
        self._inject(thread)
        self.safe_tid = thread.GetThreadID()
        self._sync_native_bps()
        if args.get("stopOnEntry"):
            self.agent("step", mode="any", just_my_code=self.just_my_code)
            self.py_step_armed = True
        return None, lambda: self.event("initialized")

    def _open_terminal(self, console):
        """Get a terminal from the client; returns its device path, or None to fall back.

        The client runs Seam's small holder program (seam/terminal.py) in a terminal of
        its own. The holder reports which terminal it is on and then stays out of the
        way; the program is launched with that terminal as its input and output.
        """
        if not self.client.get("supportsRunInTerminalRequest"):
            self.event("output", {"category": "console", "output":
                       "Seam: this client cannot run the program in a terminal; its output "
                       "goes to the debug console and it cannot read input.\n"})
            return None
        directory = tempfile.mkdtemp(prefix="seam-terminal-")
        path = os.path.join(directory, "channel")
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(path)
            server.listen(1)
            server.settimeout(30)
            holder = [os.environ.get("SEAM_PYTHON") or shutil.which("python3") or "python3",
                      TERMINAL_HOLDER, path]
            self._reverse_request("runInTerminal", {
                "kind": "external" if console == "externalTerminal" else "integrated",
                "title": "Seam", "cwd": self.cwd, "args": holder})
            connection, _ = server.accept()
            connection.settimeout(10)
            line = b""
            while not line.endswith(b"\n"):
                data = connection.recv(256)
                if not data:
                    break
                line += data
            kind, _, tty = line.decode().strip().partition(" ")
            if kind != "tty" or not tty.startswith("/dev/"):
                connection.close()
                raise DapError("the terminal holder did not report a terminal")
            connection.settimeout(None)
            self.terminal = connection
            return tty
        except OSError as exc:
            raise DapError("could not get a terminal from the client: %s" % exc) from None
        finally:
            server.close()
            shutil.rmtree(directory, ignore_errors=True)

    def _serve_terminal(self, connection, pid):
        """Pass on what the terminal holder reports: Ctrl-C, Ctrl-\\, the terminal closing."""
        pending = b""
        try:
            while True:
                data = connection.recv(256)
                if not data:
                    break
                pending += data
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    kind, _, value = line.decode("ascii", "replace").partition(" ")
                    if (kind == "signal" and value.isdigit() and not self.exited
                            and int(value) in TERMINAL_SIGNALS):
                        self.log("terminal: signal", value)
                        if self.program_group is not None:
                            # As the terminal itself would: to the program and to the
                            # children it may be waiting for.
                            os.killpg(self.program_group, int(value))
                        else:
                            os.kill(pid, int(value))
        except OSError:
            pass

    def _watch_breakpoints(self):
        """Receive LLDB's breakpoint events (locations resolving when a module loads), and
        the loading itself (a breakpoint that did not resolve may be explained by it)."""
        self.target.GetBroadcaster().AddListener(
            self.listener, lldb.SBTarget.eBroadcastBitBreakpointChanged
            | lldb.SBTarget.eBroadcastBitModulesLoaded)

    def _entry_breakpoint(self, name):
        """Breakpoint on a function's first instruction, located through the symbol table.

        Seam's own breakpoints are not set by function name. LLDB 20 resolves a name
        breakpoint through the debug info and skips the prologue; with the interpreter's
        debug info in a separate file it lands at a wrong address (`Py_RunMain` ended up
        inside a data table), so the breakpoint was never hit. The symbol table is right.
        """
        for ctx in self.target.FindSymbols(name):
            address = ctx.GetSymbol().GetStartAddress()
            if address.IsValid():
                return self.target.BreakpointCreateBySBAddress(address)
        # Not in any module yet (an interpreter linked against libpython): by name.
        return self.target.BreakpointCreateByName(name)

    def _find_python(self):
        """Locate the interpreter in the process and set up the raw-memory reader."""
        self._watch_breakpoints()
        run, module = self._symbol("PyRun_SimpleStringFlags")
        runtime, _ = self._symbol("_PyRuntime")
        version_addr, _ = self._symbol("Py_Version")
        if not run or not runtime or not version_addr:
            raise DapError("this does not look like a CPython 3.12+ process: the "
                           "interpreter's symbols are missing")
        self.interp_module = module.GetFileSpec().fullpath
        hexversion = struct.unpack("<I", self._read(version_addr, 4))[0]
        version = (hexversion >> 24, (hexversion >> 16) & 0xFF)
        if version < (3, 12):
            raise DapError("Seam needs CPython 3.12 or newer; this is %d.%d" % version)
        self.sym["PyRun_SimpleStringFlags"] = run
        self.sym["PyRun_SimpleString"] = self._symbol("PyRun_SimpleString")[0]
        self.sym["Py_AddPendingCall"] = self._symbol("Py_AddPendingCall")[0]
        self.sym["getpid"] = self._symbol("getpid")[0]
        try:
            self.py = pyread.PyReader(self._read, runtime, version)
        except (ValueError, NotImplementedError) as exc:
            raise DapError("unsupported interpreter: %s" % exc) from None

    def _load_helper(self):
        """Resolve the helper's symbols once the agent has been imported."""
        module = None
        for name in HELPER_SYMBOLS:
            addr, module = self._symbol(name)
            if not addr:
                raise DapError("Seam helper symbol %s not found after injection" % name)
            self.sym[name] = addr
        self.helper_module = module.GetFileSpec().fullpath
        self.sym["cap"] = struct.unpack("<q", self._read(self.sym["seam_req_cap"], 8))[0]
        if self.bp_trap is None:
            # Launch: set by address (see _entry_breakpoint). On attach the breakpoint
            # was set by name before the helper loaded and the thread is stopped at it
            # right now, so it is left alone.
            self.bp_trap = self.target.BreakpointCreateByAddress(self.sym["seam_trap"])
        self._watch_macos_children()

    def _inject(self, thread):
        """Load the agent. The caller guarantees `thread` is at a safe point."""
        self.log("bootstrap: locating interpreter symbols")
        phase = time.monotonic()
        self._find_python()
        self.log("bootstrap: symbols located in %.3f s" % (time.monotonic() - phase))
        code = ("import sys; sys.path.insert(0, %r)\n"
                "try:\n    import seam_agent\n"
                "finally:\n    sys.path.remove(%r)\n" % (TARGET_DIR, TARGET_DIR))
        # PyRun_SimpleString runs in `__main__`. Run there directly, the two imports
        # became globals of the user's script: a program that forgot `import sys` worked
        # under the debugger and failed without it. So the code gets a namespace of its
        # own.
        code = "exec(%r, {'__name__': 'seam_bootstrap'})" % code
        self.log("bootstrap: injecting agent")
        phase = time.monotonic()
        rc = self._call(thread, "((int(*)(const char*, void*))%d)(%s, (void*)0)"
                        % (self.sym["PyRun_SimpleStringFlags"], json.dumps(code)))
        self.log("bootstrap: injection completed in %.3f s" % (time.monotonic() - phase))
        self._drain_output()
        if rc != 0:
            raise DapError("could not load the Seam agent into the process (see its output)")
        self._load_helper()

    def req_attach(self, args):
        self._require_no_session()
        pid = args.get("pid") or 0
        try:
            # A number when written by hand; text when an editor's process picker chose it.
            pid = int(pid)
        except (TypeError, ValueError):
            raise DapError("attach needs a process id as 'pid'; %r is not one" % (pid,)) from None
        if pid <= 0:
            raise DapError("attach needs a 'pid'")
        self._apply_settings(args)
        err = lldb.SBError()
        self.target = self.dbg.CreateTarget("")
        self.process = self.target.AttachToProcessWithID(self.listener, pid, err)
        if not err.Success() or not self.process or not self.process.IsValid():
            self.process = None
            if sys.platform == "darwin":
                raise DapError("cannot attach to pid %d: %s. macOS requires debugging "
                               "permission and a target that permits debugger attachment; "
                               "protected system executables cannot be attached to."
                               % (pid, err.GetCString()))
            raise DapError("cannot attach to pid %d: %s (is ptrace allowed? see "
                           "/proc/sys/kernel/yama/ptrace_scope)" % (pid, err.GetCString()))
        self.attached = True
        try:
            self._wait_attached(pid)
            self._require_x86_64()
            self._apply_signal_policy(args)
            if sys.platform == "darwin":
                from seam._mac_processes import process_cwd
                self.cwd = process_cwd(pid) or self.cwd
            try:
                self.cwd = os.readlink("/proc/%d/cwd" % pid)
            except OSError:
                pass
            self._find_python()
            # The helper is not loaded yet, so this breakpoint can only be set by name.
            # (The helper carries its own debug info, so the LLDB 20 problem with name
            # breakpoints and separate debug files does not apply to it.)
            self.bp_trap = self.target.BreakpointCreateByName("seam_trap")
            # Evaluate one harmless call now. On 3.14 the PEP 768 path would otherwise
            # make its first expression at the helper's trap, right after the helper
            # library was loaded, and LLDB 18 crashed or hung there in about 1 attach
            # in 13 on CI. The 3.12/3.13 path already evaluates an expression here.
            try:
                self._call(self.process.GetSelectedThread(),
                           "((int(*)(void))%d)()" % self.sym["getpid"], timeout_s=5)
            except DapError as exc:
                self.log("warm-up call failed:", exc)
            method, cancel = self._request_agent_load()
            try:
                thread = self._wait_for_attach_trap(float(args.get("timeout") or 15))
            except DapError:
                self._cancel_agent_load(cancel)
                raise
        except DapError:
            self._abandon()
            raise
        self._load_helper()
        self.safe_tid = thread.GetThreadID()
        self.stop_is_trap = True
        self.event("output", {"category": "console", "output":
                   "Seam: attached to pid %d (helper loaded via %s).\n" % (pid, method)})
        self._notice_no_debug_info(self.target.module_iter())
        return None, lambda: self.event("initialized")

    def _wait_attached(self, pid, timeout=30):
        """Wait for the stop that completes an attach.

        LLDB does not always deliver a stop event for it, so the public state is polled
        as well. That is safe only here: the process has never been resumed by Seam, so
        "stopped" cannot be a stale reading from before a resume.
        """
        ev = lldb.SBEvent()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.listener.WaitForEvent(1, ev) and lldb.SBProcess.EventIsProcessEvent(ev):
                state = lldb.SBProcess.GetStateFromEvent(ev)
                if state in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateCrashed):
                    raise DapError("pid %d exited while attaching" % pid)
                if state == lldb.eStateStopped:
                    return
            if self.process.GetState() == lldb.eStateStopped:
                return
        raise DapError("timed out attaching to pid %d" % pid)

    def _request_agent_load(self):
        """Ask the stopped process to import the agent at its main thread's next safe point.

        Returns how it was asked, and a function that takes the request back (to be
        called with the process stopped).
        """
        code = (
            "import sys\n"
            "try:\n"
            "    sys.path.insert(0, %r)\n"
            "    try:\n"
            "        import seam_agent\n"
            "    finally:\n"
            "        sys.path.remove(%r)\n"
            "    seam_agent.attached()\n"
            "except BaseException:\n"
            "    import traceback\n"
            "    traceback.print_exc()\n" % (TARGET_DIR, TARGET_DIR))
        # In a namespace of its own, not in `__main__` (see _inject).
        code = "exec(%r, {'__name__': 'seam_bootstrap'})\n" % code
        remote = self.py.L.remote
        if remote is not None:
            # PEP 768 (3.14+): three memory writes, no code run by the debugger.
            interp = self.py.u64(self.py.runtime + self.py.L.runtime_interp_head)
            enabled = struct.unpack("<i", self._read(interp + remote["enabled"], 4))[0]
            tstate = self.py.u64(interp + remote["threads_main"])
            if enabled and tstate:
                fd, script = tempfile.mkstemp(prefix="seam-attach-", suffix=".py")
                with os.fdopen(fd, "w") as fh:
                    fh.write(code)
                self.temp_files.append(script)
                path = script.encode() + b"\0"
                if len(path) <= remote["path_size"]:
                    support = tstate + remote["support"]
                    self._write(support + remote["path"], path)
                    self._write(support + remote["pending"], struct.pack("<i", 1))
                    breaker = self.py.u64(tstate + remote["eval_breaker"])
                    self._write(tstate + remote["eval_breaker"],
                                struct.pack("<Q", breaker | EVAL_PLEASE_STOP_BIT))
                    return "PEP 768 remote exec", lambda: self._write(
                        support + remote["pending"], struct.pack("<i", 0))
        # 3.12/3.13 (or remote debugging disabled): queue a pending call. This runs
        # Py_AddPendingCall in the stopped process, which is not a safe point; the call
        # only takes a short internal lock, and it is abandoned if it does not return.
        if not self.sym.get("PyRun_SimpleString") or not self.sym.get("Py_AddPendingCall"):
            raise DapError("this interpreter does not export the functions attach needs")
        err = lldb.SBError()
        data = code.encode() + b"\0"
        addr = self.process.AllocateMemory(
            len(data), lldb.ePermissionsReadable | lldb.ePermissionsWritable, err)
        if not err.Success():
            raise DapError("cannot allocate memory in the process: %s" % err.GetCString())
        self._write(addr, data)
        self._call(self.process.GetSelectedThread(),
                   "((int(*)(int(*)(void*), void*))%d)((int(*)(void*))%d, (void*)%d)"
                   % (self.sym["Py_AddPendingCall"], self.sym["PyRun_SimpleString"], addr),
                   timeout_s=3)
        # A queued call cannot be taken out of the interpreter's queue, but the program
        # text it will run can be emptied.
        return "a pending call", lambda: self._write(addr, b"\0")

    def _cancel_agent_load(self, cancel):
        """Take back the request to load the helper: the attach is being given up.

        Otherwise the process would act on it once its main thread runs Python again,
        long after the debugger has gone: on 3.14 the interpreter then complains that
        the script is missing, on 3.12/3.13 it loads a helper nobody is listening to.
        """
        if self.process.GetState() != lldb.eStateStopped:
            return
        try:
            cancel()
        except DapError as exc:
            self.log("could not take back the helper load:", exc)

    def _wait_for_attach_trap(self, timeout):
        """Run until the agent reports in from `seam_agent.attached()`."""
        deadline = time.monotonic() + timeout
        while True:
            err = self.process.Continue()
            if not err.Success():
                raise DapError("could not resume the process: %s" % err.GetCString())
            remaining = int(max(1, deadline - time.monotonic()))
            try:
                state = self._wait_stop(remaining)
            except DapError:
                self.process.SendAsyncInterrupt()
                self._wait_stop(10)
                raise DapError(
                    "the process did not load the Seam helper within %d s. Its main "
                    "thread never reached a safe point; it is probably blocked in a "
                    "system call or a long native call." % timeout) from None
            if state != lldb.eStateStopped:
                raise DapError("the process exited while attaching")
            thread = self._trap_thread()
            if thread is not None:
                return thread
            if time.monotonic() > deadline:
                raise DapError("the process did not load the Seam helper in time")

    def _abandon(self):
        """Give up on a process we attached to, leaving it running."""
        if self.process is not None and self.process.IsValid():
            self.target.DeleteAllBreakpoints()
            self.process.Detach()
        self.exited = True

    def _detach(self):
        """Remove everything Seam armed and let the process carry on."""
        if self.exited or self.process is None:
            return
        try:
            if self.running:
                self._interrupt()
            self._finish_steps(self.process.GetSelectedThread())
            if self.helper_module:
                self.py_bps = {}
                if self.safe_tid is not None:
                    self.agent("shutdown")
                else:
                    self._agent_pending("shutdown")
        except (DapError, ValueError) as exc:
            self.log("detach clean-up failed:", exc)
        self.target.DeleteAllBreakpoints()
        self.process.Detach()
        self.exited = True

    def req_configurationDone(self, args):
        if self.process is None:
            raise DapError("nothing has been launched")
        self._new_stop()
        self._continue()
        return None

    def req_disconnect(self, args):
        self.done = True
        return None

    def req_terminate(self, args):
        self._kill()
        self.event("terminated")
        return None
