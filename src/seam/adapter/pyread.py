"""Read CPython's interpreter state from raw process memory.

Nothing here needs Python's debug info: it works from `_PyRuntime` and a table of
struct offsets (bundled for 3.12, taken from `_Py_DebugOffsets` on 3.13+).
The reader takes a `read(addr, size) -> bytes` callable, so it can be tested without LLDB.
"""
import struct

from . import layouts, linetable


class PyFrame:
    __slots__ = ("addr", "code", "name", "filename", "line")

    def __init__(self, addr, code, name, filename, line):
        self.addr = addr
        self.code = code
        self.name = name
        self.filename = filename
        self.line = line

    def __repr__(self):
        return "<PyFrame %s %s:%s>" % (self.name, self.filename, self.line)


class PyReader:
    def __init__(self, read, runtime_addr, version):
        self.read = read
        self.runtime = runtime_addr
        self.version = version
        self.L = layouts.layout_for(read, runtime_addr, version)
        self._codes = {}

    def new_stop(self):
        """Drop caches; call whenever the process has run."""
        self._codes.clear()

    def u64(self, addr):
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def read_str(self, addr):
        L = self.L
        hdr = self.read(addr, 64)
        length = struct.unpack_from("<q", hdr, L.str_length)[0]
        state = struct.unpack_from("<I", hdr, L.str_state)[0]
        kind = (state >> L.str_kind_shift) & 7
        compact = (state >> (L.str_kind_shift + 3)) & 1
        ascii_ = (state >> (L.str_kind_shift + 4)) & 1
        if length < 0 or length > 1 << 20 or kind not in (1, 2, 4):
            raise ValueError("not a str object at %#x" % addr)
        if compact:
            data = addr + (L.str_ascii_data if ascii_ else L.str_compact_data)
        else:
            data = struct.unpack_from("<Q", hdr, L.str_compact_data)[0]
        raw = self.read(data, length * kind) if length else b""
        return raw.decode({1: "latin-1", 2: "utf-16-le", 4: "utf-32-le"}[kind], "replace")

    def read_bytes(self, addr):
        size = struct.unpack("<q", self.read(addr + self.L.var_size, 8))[0]
        if size < 0 or size > 1 << 24:
            raise ValueError("not a bytes object at %#x" % addr)
        return self.read(addr + self.L.bytes_data, size) if size else b""

    def code_info(self, addr):
        info = self._codes.get(addr)
        if info is None:
            L = self.L
            hdr = self.read(addr, L.code_adaptive)
            firstlineno = struct.unpack_from("<i", hdr, L.code_firstlineno)[0]
            filename = self.read_str(struct.unpack_from("<Q", hdr, L.code_filename)[0])
            qualname = self.read_str(struct.unpack_from("<Q", hdr, L.code_qualname)[0])
            table = self.read_bytes(struct.unpack_from("<Q", hdr, L.code_linetable)[0])
            info = (filename, qualname, firstlineno, table)
            self._codes[addr] = info
        return info

    def _decode_frame(self, addr, raw, is_top):
        L = self.L
        code = struct.unpack_from("<Q", raw, L.frame_code)[0] & ~L.code_tag_mask
        instr = struct.unpack_from("<Q", raw, L.frame_instr)[0]
        filename, qualname, firstlineno, table = self.code_info(code)
        offset = instr - (code + L.code_adaptive)
        if not L.instr_is_prev and not is_top:
            # instr_ptr of a caller points just after its CALL; step back into it.
            offset -= 2
        line = linetable.line_at(table, firstlineno, max(offset, 0))
        return PyFrame(addr, code, qualname, filename, line)

    def thread_states(self):
        """Yield (native_thread_id, tstate_addr) for every thread of every interpreter."""
        L = self.L
        interp = self.u64(self.runtime + L.runtime_interp_head)
        guard = 0
        while interp and guard < 64:
            tstate = self.u64(interp + L.interp_threads_head)
            while tstate and guard < 100000:
                guard += 1
                yield self.u64(tstate + L.tstate_native_tid), tstate
                tstate = self.u64(tstate + L.tstate_next)
            interp = self.u64(interp + L.interp_next)
            guard += 1

    def current_frame(self, tstate):
        L = self.L
        if L.tstate_cframe is not None:
            cframe = self.u64(tstate + L.tstate_cframe)
            return self.u64(cframe + L.cframe_current) if cframe else 0
        return self.u64(tstate + L.tstate_frame)

    def thread_groups(self, tstate):
        """Python frames of one thread, newest first, grouped by eval-loop invocation.

        Returns [(entry_frame_addr, [PyFrame, ...]), ...]. Each group is the run of Python
        frames executed by one `_PyEval_EvalFrameDefault` C frame; `entry_frame_addr` is
        the address of that invocation's entry frame, which lives on the C stack.
        """
        L = self.L
        groups = []
        current = []
        frame = self.current_frame(tstate)
        guard = 0
        first = True
        while frame and guard < 20000:
            guard += 1
            raw = self.read(frame, L.frame_size)
            owner = raw[L.frame_owner]
            if owner == L.owner_entry:
                groups.append((frame, current))
                current = []
            elif owner in L.owner_python:
                try:
                    current.append(self._decode_frame(frame, raw, first))
                except (ValueError, struct.error, UnicodeError):
                    current.append(PyFrame(frame, 0, "<unreadable frame>", "", 0))
                first = False
            frame = struct.unpack_from("<Q", raw, L.frame_previous)[0]
        if current:
            groups.append((None, current))
        return groups
