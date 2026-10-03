"""Decoder for CPython's co_linetable (the 3.11+ location table). Pure Python, no LLDB."""


def _varint(table, i):
    b = table[i]
    i += 1
    value = b & 63
    shift = 0
    while b & 64:
        b = table[i]
        i += 1
        shift += 6
        value |= (b & 63) << shift
    return value, i


def _svarint(table, i):
    value, i = _varint(table, i)
    return (-(value >> 1) if value & 1 else value >> 1), i


def decode(table, firstlineno):
    """Yield (start, end, line) byte ranges; line is None where there is no location."""
    line = firstlineno
    addr = 0
    i = 0
    n = len(table)
    while i < n:
        first = table[i]
        i += 1
        code = (first >> 3) & 15
        length = ((first & 7) + 1) * 2
        if code == 15:
            current = None
        elif code == 14:
            delta, i = _svarint(table, i)
            line += delta
            for _ in range(3):
                _, i = _varint(table, i)
            current = line
        elif code == 13:
            delta, i = _svarint(table, i)
            line += delta
            current = line
        elif code >= 10:
            line += code - 10
            i += 2
            current = line
        else:
            i += 1
            current = line
        yield addr, addr + length, current
        addr += length


def line_at(table, firstlineno, offset):
    """Line number for the instruction at byte `offset`, or the nearest earlier one."""
    last = firstlineno
    for start, end, line in decode(table, firstlineno):
        if line is not None:
            last = line
        if start <= offset < end:
            return last
    return last
