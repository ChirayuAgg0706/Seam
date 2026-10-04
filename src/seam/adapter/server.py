"""Seam's debug adapter. Runs inside LLDB's embedded Python interpreter (stdlib only).

One controller owns the process: LLDB. Python-level stops arrive as native stops in
`seam_trap`; only there (and at other known safe points) does the adapter execute code
in the target. At every other stop it only reads memory.

The adapter is one class, `Adapter`, assembled from a mixin per concern:

    protocol.py     the DAP connection, request dispatch, access to the target and agent
    session.py      launch, attach, the terminal, exit, detach
    stops.py        process events: what a stop is, reporting it or carrying on
    stepping.py     stepping across the boundary; what counts as user code
    breakpoints.py  source-line, function, data and exception breakpoints
    stack.py        the merged call stack, variables, expressions
    sources.py      source paths: the debug info's, this machine's, the editor's
    disassembly.py  the listing for frames without source, stepping by instruction
    common.py       constants and small helpers

`Adapter.__init__` below is the one place that lists the session's state.
"""
import collections
import os
import socket
import threading
import traceback

import lldb

from .common import FRAMEWORK_PATHS
from .breakpoints import BreakpointsMixin
from .disassembly import DisassemblyMixin
from .protocol import ProtocolMixin
from .session import SessionMixin
from .sources import SourcesMixin
from .stack import StackMixin
from .stepping import SteppingMixin
from .stops import StopsMixin


class Adapter(ProtocolMixin, SessionMixin, StopsMixin, SteppingMixin, BreakpointsMixin, StackMixin,
              SourcesMixin, DisassemblyMixin):
    """One debug session. The behaviour lives in the mixins; the state is all here."""

    def __init__(self, debugger, sock, log=None):
        self.dbg = debugger
        self.dbg.SetAsync(True)
        self.sock = sock
        self.rfile = sock.makefile("rb")
        self.wlock = threading.Lock()
        self.logfile = log
        self.listener = lldb.SBListener("seam")
        self.wake = lldb.SBBroadcaster("seam.wake")
        self.listener.StartListeningForEvents(self.wake, 1)
        self.requests = collections.deque()
        self.seq = 0
        self.done = False

        self.target = None
        self.process = None
        self.running = False
        self.exited = False
        self.cwd = os.getcwd()
        self.sym = {}
        self.py = None
        self.interp_module = None
        self.helper_module = None
        self.bp_trap = None

        self.epoch = 0
        self.safe_tid = None          # thread stopped at a safe point, if any
        self.stop_is_trap = False
        self.frames = {}              # frame id -> record
        self.stacks = {}              # tid -> merged stack for the current stop
        self.refs = {}                # variablesReference -> record
        self.next_id = 1
        self.py_bps = {}              # path -> [{line, condition}]
        self.native_bps = {}          # path -> [SBBreakpoint]
        self.function_bps = []
        self.py_function_bps = []     # [{name, condition, hit}] for the agent
        self.watchpoints = {}         # watchpoint id -> {name, address, size, hit, hits}
        self.pending_sync = False
        self.pause_requested = False
        self.output_thread = None
        self.framework_paths = FRAMEWORK_PATHS
        self.show_glue_frames = False
        self.user_bps = {}            # module path -> breakpoint on all its user functions
        self.user_bps_on = False
        self.unwind_warnings = set()  # functions LLDB failed to unwind (warned once each)
        self.last_native_stop = {}    # tid -> (line key, pc) of the last reported stop
        self.native_bp_specs = {}     # breakpoint id -> {"hit", "log", "hits"} if it has any
        self.native_bp_lines = {}     # breakpoint id -> line the user asked for
        self.native_bp_state = {}     # breakpoint id -> (verified, line) last reported
        self.attached = False         # attached to an existing process (detach, don't kill)
        self.temp_files = []
        self.native_stepping = None   # {"tid", "hops"} while an LLDB step plan is running
        self.stepout = None           # {"bp", "sp"}: Seam's own run-until-return
        self.py_step_armed = False    # the agent has a Python-level step armed
        self.exception_info = {}      # tid -> exceptionInfo body for the current stop
        self.fault_stop = False       # stopped at a fault signal: run nothing in the process
        self.exit_packet = None       # ("W" | "X", number) from the final stop reply
        self.note_fd = None           # side channel to `seam dap` (see cli.py)
        self.exc_filters = []         # exception breakpoint filters in force
        self.native_exc_bps = {}      # filter -> SBBreakpoint (cpp_throw, rust_panic)
        self.just_my_code = True
        self.client = {}              # what the client said about itself in `initialize`
        self.terminal = None          # connection to the terminal holder (console option)
        self.post_mortem = {}         # tid -> frames of the uncaught exception shown there
        self.throw_stop = False       # stopped at a C++ throw or Rust panic
        self.leftover_stops = 0       # stops ignored as leftovers (see _is_leftover)
        self.source_map = []          # [(prefix in the debug info, prefix on this machine)]
        self.aliases = {}             # real path -> the client's spelling (symbolic links)
        self.editor_paths = {}        # path -> the spelling given to the editor (a cache)
        self.local_sources = {}       # name in the debug info -> file here, "" if none
        self.glue_paths = {}          # name in the debug info -> is binding-layer glue
        self.missing_source_reported = False
        self.native_bp_group = {}     # breakpoint id -> [SBBreakpoint], one per spelling
        self._watch_exit_packets()


def serve(debugger):
    """Run one debug session on the socket `seam dap` handed over (called by lldb_entry)."""
    fd = int(os.environ["SEAM_DAP_FD"])
    os.set_inheritable(fd, False)  # neither channel is the debugged program's business
    sock = socket.socket(fileno=fd)
    log_path = os.environ.get("SEAM_LOG")
    log = open(log_path, "a") if log_path else None
    # Seam does not use the debugger object of the `lldb` program it runs in. That one
    # has an event-handler thread of its own, which receives every process event as well
    # and handles it (updating LLDB's public state, running stop actions, restarting the
    # process) concurrently with Seam. A debugger created here has no such thread: Seam's
    # listener is the only consumer of its process's events. See docs/decisions.md §18.
    debugger = lldb.SBDebugger.Create(False)
    adapter = Adapter(debugger, sock, log)
    note_fd = os.environ.get("SEAM_NOTE_FD")
    if note_fd:
        os.set_inheritable(int(note_fd), False)
        adapter.note_fd = int(note_fd)
        adapter._note("ready")
    try:
        adapter.run()
    except Exception:
        adapter.log(traceback.format_exc())
        raise
    finally:
        adapter._kill()
        try:
            sock.close()
        except OSError:
            pass
        lldb.SBDebugger.Destroy(debugger)
