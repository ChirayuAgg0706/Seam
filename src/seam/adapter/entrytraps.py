"""Entry traps: how "step in" from Python reaches the functions of a large module.

Stepping in from a Python line arms a breakpoint on every user function of every
extension module (docs/decisions.md §7). LLDB inserts and removes breakpoint sites one at
a time, each a round trip to its debug server: about 45 microseconds per function, per
step, in each direction. For a module of a hundred functions that is nothing; with 15,000
functions loaded every step-in took 1.3 seconds, wherever it was going.

For large modules Seam therefore places the trap instructions itself:

- The functions are found by a breakpoint in a second target that has no process, so
  resolving them inserts nothing. Its locations are the addresses LLDB's own breakpoint
  would have used (after the prologue, inlined instances included).
- Arming writes the trap byte to all of them through /proc/<pid>/mem, one read and one
  write per module, and disarming puts the bytes back the same way.
- The traps are in memory only while the process runs. At every stop they are taken out
  before anything else happens, so LLDB never sees patched code and no breakpoint site of
  LLDB's is created or removed while they are in. Where LLDB has a site of its own on one
  of the addresses, the byte in memory is not the original one and Seam leaves it alone.
- A thread that runs into a trap stops with SIGTRAP one byte past it. The stepping thread
  is put back on the instruction and that is where the step ends. Any other thread is put
  back too, and that one address becomes an ordinary thread-specific LLDB breakpoint for
  the rest of the step, so LLDB steps the other thread over it.
- A child forked while the traps are in would inherit them. The helper keeps the list of
  patched addresses and restores them in the child (see _seam_trap.c).
"""
import array
import os
import signal
import struct
import time

import lldb

TRAP = 0xCC  # int3
PAGE = 4096
# Modules with at least this many symbols get entry traps; smaller ones keep LLDB's own
# breakpoints. SEAM_ENTRY_TRAPS overrides it: a number (0: every module) or "off".
MIN_SYMBOLS = 2000
MAX_REGION = 1 << 30


class _Region:
    """One module's sites: where they are and what belongs there."""

    def __init__(self, anchor, low, sites, original):
        self.anchor = anchor        # load address of the module's header when prepared
        self.low = low              # page-aligned start of the stretch of memory patched
        self.sites = sites          # sorted load addresses
        self.site_set = frozenset(sites)
        self.original = original    # the stretch as it is with no trap in it
        template = bytearray(original)
        for site in sites:
            template[site - low] = TRAP
        self.template = bytes(template)
        self.armed = None           # offsets patched right now, or None


class EntryTraps:
    def __init__(self, adapter):
        self.a = adapter
        setting = os.environ.get("SEAM_ENTRY_TRAPS", "").strip().lower()
        self.min_symbols = None if setting == "off" else (
            int(setting) if setting.isdigit() else MIN_SYMBOLS)
        self.fd = None              # /proc/<pid>/mem of the debugged process
        self.unavailable = None     # why this session cannot have entry traps, once known
        self.resolver = None        # a target without a process, to resolve functions in
        self.regions = {}           # module path -> _Region, or None (no user function)
        self.declined = set()       # module paths left to LLDB's breakpoints
        self.tid = None             # the thread stepping in, while a step-in is in flight
        self.armed = False
        self.landed = None          # thread that reached a trap at this stop (the step's end)
        self.promote = set()        # sites another thread ran into: LLDB breakpoints next
        self.promoted = []          # those breakpoints, for the stepping thread only
        self.fork_count = None      # address of the helper's seam_fork_count
        self.fork_table = None      # address of its seam_fork_table
        self.table_for = None       # the regions the helper's table was written for
        self.table_entries = 0

    # ------------------------------------------------------------ preparation

    def _open(self):
        """Access to the process's memory. False if this session cannot have entry traps."""
        if self.fd is not None:
            return True
        if self.unavailable is None:
            a = self.a
            self.fork_count = a._symbol("seam_fork_count")[0]
            self.fork_table = a._symbol("seam_fork_table")[0]
            try:
                if not self.fork_count or not self.fork_table:
                    raise OSError("the helper in the process has no fork table")
                self.fd = os.open("/proc/%d/mem" % a.process.GetProcessID(), os.O_RDWR)
                return True
            except OSError as exc:
                self.unavailable = str(exc)
                a.log("no entry traps in this session:", exc)
        return False

    def covers(self, path, module):
        """True if entry traps take care of this module (else: an LLDB breakpoint)."""
        if path in self.regions:
            return True
        if (path in self.declined or self.min_symbols is None
                or module.GetNumSymbols() < self.min_symbols or not self._open()):
            return False
        try:
            self.regions[path] = self._prepare(path, module)
        except (OSError, ValueError) as exc:
            self.a.log("no entry traps for", path, "-", exc)
            self.declined.add(path)
            return False
        return True

    def _prepare(self, path, module):
        a = self.a
        if self.resolver is None:
            # A target of its own, with no process: a breakpoint resolves there without a
            # single site being inserted. Creating a target selects it; command-line
            # commands must keep going to the real one.
            self.resolver = a.dbg.CreateTarget("")
            a.dbg.SetSelectedTarget(a.target)
        self.resolver.AddModule(module)
        modules = lldb.SBFileSpecList()
        modules.Append(module.GetFileSpec())
        started = time.monotonic()
        bp = self.resolver.BreakpointCreateByRegex(
            ".", lldb.eLanguageTypeUnknown, modules, lldb.SBFileSpecList())
        count = bp.GetNumLocations()
        resolved = time.monotonic()
        sites = set()
        for i in range(count):
            address = bp.GetLocationAtIndex(i).GetAddress()
            if a._classify_address(address) == "user":
                load = address.GetLoadAddress(a.target)
                if load != lldb.LLDB_INVALID_ADDRESS:
                    sites.add(load)
        self.resolver.BreakpointDelete(bp.GetID())
        a.log("entry traps for", path, len(sites), "of", count, "(resolved in %.2f s, "
              "classified in %.2f s)" % (resolved - started, time.monotonic() - resolved))
        if not sites:
            return None
        sites = sorted(sites)
        low = sites[0] & ~(PAGE - 1)
        size = (sites[-1] | (PAGE - 1)) + 1 - low
        if size > MAX_REGION:
            raise ValueError("its functions are spread over %d bytes" % size)
        original = bytearray(os.pread(self.fd, size, low))
        if len(original) != size:
            raise ValueError("its code could not be read")
        for site in sites:
            # A trap byte here is a breakpoint site of LLDB's; LLDB knows what it replaced.
            if original[site - low] == TRAP:
                original[site - low] = a._read(site, 1)[0]
        anchor = module.GetObjectFileHeaderAddress().GetLoadAddress(a.target)
        return _Region(anchor, low, sites, bytes(original))

    def _write_fork_table(self, regions):
        """Leave the list of patched addresses where the helper finds it after a fork."""
        key = tuple(id(region) for region in regions)
        if self.table_for == key:
            return
        a = self.a
        table = array.array("Q")
        for region in regions:
            for site in region.sites:
                table.append(site)
                table.append(region.original[site - region.low])
        data = table.tobytes()
        error = lldb.SBError()
        address = a.process.AllocateMemory(
            len(data), lldb.ePermissionsReadable | lldb.ePermissionsWritable, error)
        if not error.Success():
            raise OSError("cannot allocate the fork table: %s" % error.GetCString())
        os.pwrite(self.fd, data, address)
        os.pwrite(self.fd, struct.pack("<Q", address), self.fork_table)
        self.table_for = key
        self.table_entries = len(table) // 2

    # ---------------------------------------------------------------- a step

    def begin(self, tid):
        """A step-in from Python is starting on this thread."""
        self.tid = tid
        self.landed = None

    @property
    def pending(self):
        return self.tid is not None

    def _loaded(self):
        """The regions whose module is still where it was when they were prepared."""
        a = self.a
        current = {path: module for path, module in a._user_modules()}
        out = []
        for path, region in list(self.regions.items()):
            if region is None:
                continue
            module = current.get(path)
            if module is None or (module.GetObjectFileHeaderAddress().GetLoadAddress(a.target)
                                  != region.anchor):
                del self.regions[path]  # unloaded or moved: prepared again when next seen
                continue
            out.append(region)
        return out

    def arm(self):
        """Put the traps in. The process is stopped and is about to be resumed."""
        if self.tid is None or self.armed:
            return
        a = self.a
        for site in sorted(self.promote):
            bp = a.target.BreakpointCreateByAddress(site)
            bp.SetThreadID(self.tid)
            self.promoted.append(bp)
        self.promote.clear()
        try:
            regions = self._loaded()
            if not regions:
                return
            self._write_fork_table(regions)
            for region in regions:
                current = os.pread(self.fd, len(region.original), region.low)
                if current == region.original:
                    os.pwrite(self.fd, region.template, region.low)
                    region.armed = [site - region.low for site in region.sites]
                    continue
                # Something in this stretch differs from when it was prepared (a
                # breakpoint site of LLDB's set or removed since). A site whose byte is
                # not the original one is someone else's and is left alone.
                patched = bytearray(current)
                region.armed = []
                for site in region.sites:
                    offset = site - region.low
                    if current[offset] == region.original[offset]:
                        patched[offset] = TRAP
                        region.armed.append(offset)
                os.pwrite(self.fd, patched, region.low)
            os.pwrite(self.fd, struct.pack("<q", self.table_entries), self.fork_count)
            self.armed = True
        except OSError as exc:
            a.log("could not arm the entry traps:", exc)
            self.disarm()

    def disarm(self):
        """Take every trap out again."""
        try:
            os.pwrite(self.fd, struct.pack("<q", 0), self.fork_count)
        except OSError:
            pass
        for path, region in list(self.regions.items()):
            if region is None or region.armed is None:
                continue
            try:
                current = os.pread(self.fd, len(region.original), region.low)
                if current == region.template:
                    os.pwrite(self.fd, region.original, region.low)
                else:
                    restored = bytearray(current)
                    for offset in region.armed:
                        if current[offset] == TRAP:
                            restored[offset] = region.original[offset]
                    os.pwrite(self.fd, restored, region.low)
            except OSError as exc:
                self.a.log("could not take the entry traps out of", path, "-", exc)
                del self.regions[path]
            region.armed = None
        self.armed = False

    def stopped(self):
        """The process has stopped: take the traps out and put right every thread that ran
        into one. Returns how many did. Runs before anything else looks at the stop."""
        if not self.armed:
            return 0
        a = self.a
        if any(t.GetStopReason() == lldb.eStopReasonExec for t in a.process):
            # A new program: nothing of what was patched is there any more.
            self.forget()
            return 0
        self.disarm()
        hits = 0
        for thread in a.process:
            reason = thread.GetStopReason()
            if reason in (lldb.eStopReasonNone, lldb.eStopReasonInvalid):
                continue
            site = a._pc(thread) - 1
            if not self.is_site(site):
                continue
            if not self._is_trap_stop(thread):
                a.log("thread", thread.GetThreadID(), "is one byte past an entry trap with "
                      "stop reason", reason, thread.GetStopDescription(80))
                continue
            tid = thread.GetThreadID()
            thread.GetFrameAtIndex(0).SetPC(site)
            hits += 1
            if tid == self.tid:
                self.landed = tid
            else:
                self.promote.add(site)
            a.log("entry trap at %#x reached by thread" % site, tid,
                  "(the step ends here)" if tid == self.tid else "(not the stepping thread)")
        return hits

    @staticmethod
    def _is_trap_stop(thread):
        """A trap instruction LLDB did not put there is reported as the signal SIGTRAP."""
        reason = thread.GetStopReason()
        return reason == lldb.eStopReasonException or (
            reason == lldb.eStopReasonSignal
            and thread.GetStopReasonDataAtIndex(0) == signal.SIGTRAP)

    def is_site(self, address):
        return any(region is not None and address in region.site_set
                   for region in self.regions.values())

    def handled(self, thread):
        """True if this thread's stop reason is an entry trap that has been dealt with:
        it sits on the instruction again and has no reason of its own to be stopped."""
        return (bool(self.regions) and self._is_trap_stop(thread)
                and self.is_site(self.a._pc(thread)))

    def breakpoint_ids(self):
        return [bp.GetID() for bp in self.promoted]

    def finish(self):
        """The step is over (or cancelled): nothing stays behind."""
        if self.armed:
            self.disarm()
        for bp in self.promoted:
            self.a.target.BreakpointDelete(bp.GetID())
        self.promoted = []
        self.promote.clear()
        self.tid = None
        self.landed = None

    def forget(self):
        """The process image is gone (exec, exit): drop everything without writing."""
        self.regions.clear()
        self.armed = False
        self.table_for = None
        self.promote.clear()
        self.promoted = []
        self.tid = None
        self.landed = None
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.unavailable = None
        if self.resolver is not None:
            self.a.dbg.DeleteTarget(self.resolver)
            self.resolver = None
