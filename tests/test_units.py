"""Checks of the pieces that decode CPython's memory, against CPython itself."""
import json
import os
import shutil
import subprocess
import sys
import types

import pytest

from conftest import ROOT

sys.path.insert(0, os.path.join(ROOT, "src"))

from seam.adapter import layouts, linetable  # noqa: E402


def all_code_objects(limit=4000):
    """Code objects of every loaded module, including nested functions and classes."""
    seen = []
    todo = []
    for module in list(sys.modules.values()):
        for value in list(vars(module).values()) if hasattr(module, "__dict__") else []:
            code = getattr(value, "__code__", None)
            if isinstance(code, types.CodeType):
                todo.append(code)
    while todo and len(seen) < limit:
        code = todo.pop()
        seen.append(code)
        todo.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
    return seen


def test_linetable_decoder_matches_co_lines():
    codes = all_code_objects()
    assert len(codes) > 500
    for code in codes:
        ours = [(s, e, l) for s, e, l in linetable.decode(code.co_linetable, code.co_firstlineno)]
        # co_lines() merges adjacent entries with the same line; do the same to ours.
        merged = []
        for start, end, line in ours:
            if merged and merged[-1][2] == line and merged[-1][1] == start:
                merged[-1] = (merged[-1][0], end, line)
            else:
                merged.append((start, end, line))
        assert merged == list(code.co_lines()), code


def test_line_at_agrees_with_co_lines_for_every_offset():
    for code in all_code_objects(300):
        for start, end, line in code.co_lines():
            if line is None:
                continue
            for offset in (start, end - 2):
                assert linetable.line_at(code.co_linetable, code.co_firstlineno, offset) == line


SYSTEM_PYTHON = "/usr/bin/python3.12"


@pytest.mark.skipif(not os.path.exists(SYSTEM_PYTHON) or not shutil.which("lldb"),
                    reason="needs the system CPython 3.12 and LLDB")
def test_bundled_312_layout_matches_debug_info():
    """3.12 has no _Py_DebugOffsets, so Seam bundles a table; re-derive it from debug info."""
    out = subprocess.run(
        ["lldb", "--batch", "-o", "target create " + SYSTEM_PYTHON,
         "-o", "command script import " + os.path.join(ROOT, "tools", "dump_offsets.py")],
        capture_output=True, text=True, timeout=300).stdout
    line = next((l for l in out.splitlines() if l.startswith("SEAM_OFFSETS ")), None)
    assert line, "dump_offsets produced nothing"
    info = json.loads(line[len("SEAM_OFFSETS "):])
    if not info.get("PyCodeObject"):
        pytest.skip("no debug info for the system interpreter (install python3-dbg to run this)")
    L = layouts.layout_for(None, 0, (3, 12))
    runtime, interp, tstate = info["pyruntimestate"], info["_is"], info["_ts"]
    frame, code = info["_PyInterpreterFrame"], info["PyCodeObject"]
    expected = {
        "runtime_interp_head": runtime["interpreters"] + info["pyinterpreters"]["head"],
        "interp_next": interp["next"],
        "interp_threads_head": interp["threads"] + info["pythreads"]["head"],
        "tstate_next": tstate["next"],
        "tstate_native_tid": tstate["native_thread_id"],
        "tstate_cframe": tstate["cframe"],
        "cframe_current": info["_PyCFrame"]["current_frame"],
        "frame_code": frame["f_code"],
        "frame_previous": frame["previous"],
        "frame_instr": frame["prev_instr"],
        "frame_owner": frame["owner"],
        "frame_localsplus": frame["localsplus"],
        "code_firstlineno": code["co_firstlineno"],
        "code_filename": code["co_filename"],
        "code_qualname": code["co_qualname"],
        "code_linetable": code["co_linetable"],
        "code_adaptive": code["co_code_adaptive"],
        "code_localsplusnames": code["co_localsplusnames"],
        "code_localspluskinds": code["co_localspluskinds"],
        "ob_type": info["PyObject"]["ob_type"],
        "tp_name": info["PyTypeObject"]["tp_name"],
        "str_length": info["PyASCIIObject"]["length"],
        "str_state": info["PyASCIIObject"]["state"],
        "str_ascii_data": info["PyASCIIObject"]["sizeof"],
        "var_size": info["PyVarObject"]["ob_size"],
        "bytes_data": info["PyBytesObject"]["ob_sval"],
        "tuple_item": info["PyTupleObject"]["ob_item"],
        "list_item": info["PyListObject"]["ob_item"],
        "long_tag": info["PyLongObject"]["long_value"] + info["_PyLongValue"]["lv_tag"],
        "long_digit": info["PyLongObject"]["long_value"] + info["_PyLongValue"]["ob_digit"],
        "float_value": info["PyFloatObject"]["ob_fval"],
        "cell_ref": info["PyCellObject"]["ob_ref"],
        "dict_used": info["PyDictObject"]["ma_used"],
    }
    actual = {name: getattr(L, name) for name in expected}
    assert actual == expected
