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

    def _decode_frame(self, addr, raw):
        L = self.L
        code = struct.unpack_from("<Q", raw, L.frame_code)[0] & ~L.ref_tag_mask
        instr = struct.unpack_from("<Q", raw, L.frame_instr)[0]
        filename, qualname, firstlineno, table = self.code_info(code)
        # 3.12: prev_instr is the last instruction executed. 3.13+: instr_ptr is the
        # instruction being executed (a caller's CALL). Either way it is on the right line.
        offset = instr - (code + L.code_adaptive)
        line = linetable.line_at(table, firstlineno, max(offset, 0))
        return PyFrame(addr, code, qualname, filename, line)

    # ------------------------------------------------- values, without running code

    def type_name(self, obj):
        tp = self.u64(obj + self.L.ob_type)
        name = self.u64(tp + self.L.tp_name)
        raw = self.read(name, 64)
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    def _int(self, obj):
        L = self.L
        tag = self.u64(obj + L.long_tag)
        ndigits = tag >> 3
        sign = 1 - (tag & 3)
        if ndigits > 64:
            return None
        value = 0
        if ndigits:
            digits = struct.unpack("<%dI" % ndigits, self.read(obj + L.long_digit, 4 * ndigits))
            for i, digit in enumerate(digits):
                value |= digit << (30 * i)
        return sign * value

    def describe(self, obj, depth=1):
        """(repr-like text, type name) for the object at `obj`, read from memory only."""
        L = self.L
        try:
            tname = self.type_name(obj)
            if tname == "NoneType":
                return "None", tname
            if tname == "bool":
                return ("True" if self._int(obj) else "False"), tname
            if tname == "int":
                value = self._int(obj)
                return ("<int too large to decode>" if value is None else str(value)), tname
            if tname == "float":
                return repr(struct.unpack("<d", self.read(obj + L.float_value, 8))[0]), tname
            if tname == "str":
                return repr(self.read_str(obj)), tname
            if tname == "bytes":
                return repr(self.read_bytes(obj)[:200]), tname
            if tname in ("list", "tuple"):
                size = struct.unpack("<q", self.read(obj + L.var_size, 8))[0]
                if depth <= 0 or size > 1 << 24:
                    return "<%s, %d items>" % (tname, size), tname
                items = obj + L.tuple_item if tname == "tuple" else self.u64(obj + L.list_item)
                shown = min(size, 10)
                ptrs = struct.unpack("<%dQ" % shown, self.read(items, 8 * shown)) if shown else ()
                parts = [self.describe(p, depth - 1)[0] for p in ptrs]
                if size > shown:
                    parts.append("...")
                if tname == "tuple":
                    return "(%s%s)" % (", ".join(parts), "," if size == 1 else ""), tname
                return "[%s]" % ", ".join(parts), tname
            if tname == "dict":
                used = struct.unpack("<q", self.read(obj + L.dict_used, 8))[0]
                return "<dict, %d items>" % used, tname
            tname = tname.rsplit(".", 1)[-1]
            return "<%s object at %#x>" % (tname, obj), tname
        except (ValueError, struct.error, UnicodeError):
            return "<unreadable object at %#x>" % obj, "?"

    def frame_locals(self, frame):
        """[(name, text, type)] for a PyFrame, decoded from memory only."""
        L = self.L
        hdr = self.read(frame.code, L.code_adaptive)
        names_obj = struct.unpack_from("<Q", hdr, L.code_localsplusnames)[0]
        kinds = self.read_bytes(struct.unpack_from("<Q", hdr, L.code_localspluskinds)[0])
        count = struct.unpack("<q", self.read(names_obj + L.var_size, 8))[0]
        if count <= 0 or count > 4096:
            return []
        names = struct.unpack("<%dQ" % count, self.read(names_obj + L.tuple_item, 8 * count))
        slots = struct.unpack("<%dQ" % count,
                              self.read(frame.addr + L.frame_localsplus, 8 * count))
        out = []
        for i in range(count):
            ref = slots[i]
            if L.ref_tag_mask and ref & L.ref_tag_mask == L.ref_tag_mask:
                out.append((self.read_str(names[i]), str(ref >> 2), "int"))  # tagged int
                continue
            obj = ref & ~L.ref_tag_mask
            if not obj:
                continue  # unbound
            if i < len(kinds) and kinds[i] & 0xC0:  # CO_FAST_CELL | CO_FAST_FREE
                try:
                    if self.type_name(obj) == "cell":
                        obj = self.u64(obj + L.cell_ref)
                except ValueError:
                    pass
                if not obj:
                    continue
            text, tname = self.describe(obj)
            out.append((self.read_str(names[i]), text, tname))
        return out

    def thread_states(self):
        """Yield (native_thread_id, tstate, interp) for every thread of every interpreter."""
        L = self.L
        interp = self.u64(self.runtime + L.runtime_interp_head)
        guard = 0
        while interp and guard < 64:
            tstate = self.u64(interp + L.interp_threads_head)
            while tstate and guard < 100000:
                guard += 1
                yield self.u64(tstate + L.tstate_native_tid), tstate, interp
                tstate = self.u64(tstate + L.tstate_next)
            interp = self.u64(interp + L.interp_next)
            guard += 1

    def find_thread(self, tid):
        """(tstate, interp) of the Python thread with this OS thread id, or (None, None)."""
        for native_tid, tstate, interp in self.thread_states():
            if native_tid == tid:
                return tstate, interp
        return None, None

    def holds_gil(self, tid):
        """True if the OS thread `tid` is a Python thread that currently holds the GIL."""
        L = self.L
        try:
            tstate, interp = self.find_thread(tid)
            if tstate is None:
                return False
            if L.gil_ptr is not None:
                gil = self.u64(interp + L.gil_ptr)
                holder = self.u64(gil + L.gil_holder)
                locked = struct.unpack("<i", self.read(gil + L.gil_locked, 4))[0]
            else:
                holder = self.u64(interp + L.gil_holder)
                locked = struct.unpack("<i", self.read(interp + L.gil_locked, 4))[0]
            return locked == 1 and holder == tstate
        except (ValueError, struct.error):
            return False

    def gil_drop_request(self, tid):
        """Another thread's pending request that `tid` give up the GIL, if there is one.

        Returns (address, bytes) such that writing the bytes there withdraws the request,
        or None. The thread that asked asks again the next time its wait times out.
        """
        L = self.L
        try:
            tstate, interp = self.find_thread(tid)
            if tstate is None:
                return None
            if L.gil_drop_request is not None:
                address = interp + L.gil_drop_request
                if struct.unpack("<i", self.read(address, 4))[0]:
                    return address, struct.pack("<i", 0)
            elif L.tstate_eval_breaker is not None:
                address = tstate + L.tstate_eval_breaker
                breaker = self.u64(address)
                if breaker & 1:  # _PY_GIL_DROP_REQUEST_BIT
                    return address, struct.pack("<Q", breaker & ~1)
        except (ValueError, struct.error):
            pass
        return None

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
        while frame and guard < 20000:
            guard += 1
            raw = self.read(frame, L.frame_size)
            owner = raw[L.frame_owner]
            if owner == L.owner_entry:
                # An entry frame with no Python frames above it is not an eval loop
                # (3.15 ends every thread's chain with such a frame, in the thread state).
                if current:
                    groups.append((frame, current))
                current = []
            elif owner in L.owner_python:
                try:
                    current.append(self._decode_frame(frame, raw))
                except (ValueError, struct.error, UnicodeError):
                    current.append(PyFrame(frame, 0, "<unreadable frame>", "", 0))
            frame = struct.unpack_from("<Q", raw, L.frame_previous)[0]
        if current:
            groups.append((None, current))
        return groups
