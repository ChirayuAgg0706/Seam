"""Machine code: the listing for the editor's disassembly view, and stepping through it."""
import os

import lldb

from .common import DapError

MAX_INSTRUCTIONS = 20000     # per request; the editor asks for a few hundred at a time
MAX_FUNCTION_BYTES = 1 << 20  # a "function" larger than this is not decoded to look back
PADDING = 32                 # how far back to look for a function across alignment padding


class DisassemblyMixin:
    def req_disassemble(self, args):
        """Instructions around an address, for frames that have no source to show.

        Exactly `instructionCount` entries are returned, as the protocol requires: the
        editor finds its rows by position, with the instruction at the address itself at
        position -instructionOffset. Where there is nothing to decode (unmapped memory,
        or code before the address with no function start to decode from), the entries
        are placeholders marked invalid.
        """
        self._require_stopped()
        try:
            base = int(str(args["memoryReference"]), 0) + int(args.get("offset") or 0)
            first = int(args.get("instructionOffset") or 0)
            count = int(args["instructionCount"])
        except (TypeError, ValueError):
            raise DapError("disassemble needs an address as memoryReference (0x...) and "
                           "whole numbers for the offsets and the count") from None
        if not 0 <= count <= MAX_INSTRUCTIONS or abs(first) > MAX_INSTRUCTIONS:
            raise DapError("disassemble: at most %d instructions at a time" % MAX_INSTRUCTIONS)
        if not 0 <= base < 1 << 64:
            raise DapError("disassemble: %#x is not an address" % base)
        stride = 4 if (self.target.GetTriple() or "").startswith(("arm64", "aarch64")) else 1
        before = self._instructions_before(base, -first) if first < 0 else []
        after = []
        if first + count > 0:
            found = self.target.ReadInstructions(self.target.ResolveLoadAddress(base),
                                                 first + count)
            after = [found.GetInstructionAtIndex(i) for i in range(found.GetSize())]
        symbols = args.get("resolveSymbols", True)
        low = self._span(before[0])[0] if before else base
        high = self._span(after[-1])[1] if after else base
        out = []
        for position in range(first, first + count):
            if position >= len(after):
                out.append(self._no_instruction(high + (position - len(after)) * stride))
            elif position >= 0:
                out.append(self._instruction(after[position], symbols))
            elif len(before) + position >= 0:
                out.append(self._instruction(before[len(before) + position], symbols))
            else:
                out.append(self._no_instruction(low + (len(before) + position) * stride))
        return {"instructions": out}

    def _span(self, instruction):
        """(first address, address after the last byte) of an instruction."""
        start = instruction.GetAddress().GetLoadAddress(self.target)
        return start, start + instruction.GetByteSize()

    def _instructions_before(self, address, wanted):
        """Up to `wanted` instructions that end where `address` begins, oldest first.

        x86 code cannot be decoded backwards: where an instruction starts is only known
        by decoding from a known start. The start of the function before `address` is
        one, so each function is decoded from its beginning, going back function by
        function until there are enough. A stripped library still has function starts:
        LLDB takes them from the unwind tables.
        """
        if (self.target.GetTriple() or "").startswith(("arm64", "aarch64")):
            # AArch64 instructions are four bytes. Stripped Mach-O symbol ranges do
            # not reliably identify an aligned function start, but no such start is
            # needed to decode this architecture backwards.
            out = []
            for back in range(1, wanted + 1):
                if address < back * 4:
                    break
                where = self.target.ResolveLoadAddress(address - back * 4)
                section = where.GetSection()
                if not section.IsValid() or not section.GetPermissions() & lldb.ePermissionsExecutable:
                    break
                found = self.target.ReadInstructions(where, 1)
                if found.GetSize() != 1 or found.GetInstructionAtIndex(0).GetByteSize() != 4:
                    break
                out.append(found.GetInstructionAtIndex(0))
            return list(reversed(out))
        out = []
        while len(out) < wanted:
            start = lldb.LLDB_INVALID_ADDRESS
            # The bytes just before a function are usually padding that belongs to no
            # symbol; the function before it is a little further back.
            for back in range(1, min(PADDING, address) + 1):
                where = self.target.ResolveLoadAddress(address - back)
                for scope in (where.GetSymbol(), where.GetFunction()):
                    if scope.IsValid():
                        start = scope.GetStartAddress().GetLoadAddress(self.target)
                        break
                if start != lldb.LLDB_INVALID_ADDRESS:
                    break
            if start >= address or address - start > MAX_FUNCTION_BYTES:
                break
            try:
                code = self._read(start, address - start)
            except ValueError:
                break
            found = self.target.GetInstructions(self.target.ResolveLoadAddress(start), code)
            chunk = [found.GetInstructionAtIndex(i) for i in range(found.GetSize())]
            chunk = [inst for inst in chunk if self._span(inst)[1] <= address]
            if not chunk:
                break
            out[:0] = chunk
            address = start
        return out[-wanted:] if wanted > 0 else []

    def _instruction(self, instruction, symbols):
        target = self.target
        address = instruction.GetAddress().GetLoadAddress(target)
        data = instruction.GetData(target)
        raw = data.ReadRawData(lldb.SBError(), 0, data.GetByteSize()) or b""
        text = " ".join(part for part in (instruction.GetMnemonic(target),
                                          instruction.GetOperands(target)) if part)
        if instruction.GetComment(target):
            text += " ; " + instruction.GetComment(target)
        item = {"address": "%#x" % address, "instructionBytes": raw.hex(" "),
                "instruction": text or "??"}
        if symbols:
            where = target.ResolveLoadAddress(address)
            name = where.GetSymbol().GetName()
            if name:
                item["symbol"] = name
            entry = where.GetLineEntry()
            spec = entry.GetFileSpec()
            path = (self._local_source(spec.fullpath)
                    if entry.IsValid() and spec.IsValid() and entry.GetLine() else None)
            if path:
                item["location"] = {"name": os.path.basename(path), "path": path}
                item["line"] = entry.GetLine()
        return item

    @staticmethod
    def _no_instruction(address):
        """Stands for an instruction that cannot be read. The addresses keep the listing
        in ascending order, which the editor relies on to find its place in it."""
        return {"address": "%#x" % max(address, 0), "instruction": "??",
                "presentationHint": "invalid"}

    def _step_instruction(self, thread, over):
        """Run one machine instruction; `over` runs a called function to its return.

        Unlike a step by line this ends wherever it ends, be it glue, a library without
        debug info or the interpreter: the disassembly view shows any of them.
        """
        self.process.SetSelectedThread(thread)
        self.native_stepping = {"tid": thread.GetThreadID(), "hops": 0, "instruction": True}
        self._new_stop()
        error = lldb.SBError()
        thread.StepInstruction(over, error)
        if not error.Success():
            self.native_stepping = None
            raise DapError("could not step: %s" % error.GetCString())
        self.running = True
