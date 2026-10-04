"""CPython struct layouts (x86-64) used by the raw-memory reader.

3.12 has no `_Py_DebugOffsets`, so its offsets are bundled; they were taken from the
debug info of CPython 3.12.3 with tools/dump_offsets.py and are re-checked against debug
info by tests/test_layouts.py. 3.13 and 3.14 publish their offsets at the start of
`_PyRuntime`; the struct holding them is itself versioned, hence one field list per minor.
"""
import struct


class Layout:
    version = (3, 12)
    # PyRuntime / interpreter / thread state
    runtime_interp_head = 0
    interp_next = 0
    interp_threads_head = 0
    tstate_next = 0
    tstate_native_tid = 0
    tstate_cframe = None      # 3.12 only: tstate->cframe->current_frame
    cframe_current = 0
    tstate_frame = 0          # 3.13+: tstate->current_frame
    # _PyInterpreterFrame
    frame_code = 0
    frame_previous = 8
    frame_instr = 0
    frame_owner = 0
    frame_localsplus = 0
    frame_size = 80
    ref_tag_mask = 0          # 3.14: frame slots are tagged _PyStackRef values
    owner_entry = 3           # FRAME_OWNED_BY_CSTACK
    owner_python = (0, 1, 2)  # thread, generator, frame object
    # PyCodeObject
    code_firstlineno = 0
    code_filename = 0
    code_qualname = 0
    code_linetable = 0
    code_adaptive = 0
    code_localsplusnames = 0
    code_localspluskinds = 0
    # objects
    ob_type = 8
    tp_name = 24
    str_length = 16
    str_state = 32
    str_kind_shift = 2
    str_ascii_data = 40
    str_compact_data = 56
    var_size = 16
    bytes_data = 32
    tuple_item = 24
    list_item = 24
    long_tag = 16
    long_digit = 24
    float_value = 16
    cell_ref = 16
    dict_used = 16
    # GIL: 3.12 reaches it through interp->ceval.gil; 3.13+ publish offsets within interp
    gil_ptr = None
    gil_holder = 8
    gil_locked = 16
    remote = None             # 3.14+: PEP 768 remote-exec offsets


def _layout_312():
    L = Layout()
    L.gil_ptr = 384           # offsetof(_is, ceval) + offsetof(_ceval_state, gil)
    L.runtime_interp_head = 40
    L.interp_next = 0
    L.interp_threads_head = 72
    L.tstate_next = 8
    L.tstate_native_tid = 144
    L.tstate_cframe = 56
    L.cframe_current = 0
    L.frame_code = 0
    L.frame_previous = 8
    L.frame_instr = 56
    L.frame_owner = 70
    L.frame_localsplus = 72
    L.code_firstlineno = 68
    L.code_filename = 112
    L.code_qualname = 128
    L.code_linetable = 136
    L.code_adaptive = 192
    L.code_localsplusnames = 96
    L.code_localspluskinds = 104
    return L


_COMMON_TAIL = [
    ("pyobject", ["size", "ob_type"]),
    ("type_object", ["size", "tp_name", "tp_repr", "tp_flags"]),
    ("tuple_object", ["size", "ob_item", "ob_size"]),
    ("list_object", ["size", "ob_item", "ob_size"]),
]

_DEBUG_OFFSETS = {
    (3, 13): [
        ("runtime_state", ["size", "finalizing", "interpreters_head"]),
        ("interpreter_state", ["size", "id", "next", "threads_head", "gc", "imports_modules",
                               "sysdict", "builtins", "ceval_gil", "gil_runtime_state",
                               "gil_runtime_state_enabled", "gil_runtime_state_locked",
                               "gil_runtime_state_holder"]),
        ("thread_state", ["size", "prev", "next", "interp", "current_frame", "thread_id",
                          "native_thread_id", "datastack_chunk", "status"]),
        ("interpreter_frame", ["size", "previous", "executable", "instr_ptr", "localsplus",
                               "owner"]),
        ("code_object", ["size", "filename", "name", "qualname", "linetable", "firstlineno",
                         "argcount", "localsplusnames", "localspluskinds", "co_code_adaptive"]),
    ] + _COMMON_TAIL + [
        ("dict_object", ["size", "ma_keys", "ma_values"]),
        ("float_object", ["size", "ob_fval"]),
        ("long_object", ["size", "lv_tag", "ob_digit"]),
        ("bytes_object", ["size", "ob_size", "ob_sval"]),
        ("unicode_object", ["size", "state", "length", "asciiobject_size"]),
        ("gc", ["size", "collecting"]),
    ],
    (3, 14): [
        ("runtime_state", ["size", "finalizing", "interpreters_head"]),
        ("interpreter_state", ["size", "id", "next", "threads_head", "threads_main", "gc",
                               "imports_modules", "sysdict", "builtins", "ceval_gil",
                               "gil_runtime_state", "gil_runtime_state_enabled",
                               "gil_runtime_state_locked", "gil_runtime_state_holder",
                               "code_object_generation", "tlbc_generation"]),
        ("thread_state", ["size", "prev", "next", "interp", "current_frame", "thread_id",
                          "native_thread_id", "datastack_chunk", "status"]),
        ("interpreter_frame", ["size", "previous", "executable", "instr_ptr", "localsplus",
                               "owner", "stackpointer", "tlbc_index"]),
        ("code_object", ["size", "filename", "name", "qualname", "linetable", "firstlineno",
                         "argcount", "localsplusnames", "localspluskinds", "co_code_adaptive",
                         "co_tlbc"]),
    ] + _COMMON_TAIL + [
        ("set_object", ["size", "used", "table", "mask"]),
        ("dict_object", ["size", "ma_keys", "ma_values"]),
        ("float_object", ["size", "ob_fval"]),
        ("long_object", ["size", "lv_tag", "ob_digit"]),
        ("bytes_object", ["size", "ob_size", "ob_sval"]),
        ("unicode_object", ["size", "state", "length", "asciiobject_size"]),
        ("gc", ["size", "collecting"]),
        ("gen_object", ["size", "gi_name", "gi_iframe", "gi_frame_state"]),
        ("llist_node", ["next", "prev"]),
        ("debugger_support", ["eval_breaker", "remote_debugger_support",
                              "remote_debugging_enabled", "debugger_pending_call",
                              "debugger_script_path", "debugger_script_path_size"]),
    ],
    # Taken from Include/internal/pycore_debug_offsets.h of 3.15.0rc3.
    (3, 15): [
        ("runtime_state", ["size", "finalizing", "interpreters_head"]),
        ("interpreter_state", ["size", "id", "next", "threads_head", "threads_main", "gc",
                               "imports_modules", "sysdict", "builtins", "ceval_gil",
                               "gil_runtime_state", "gil_runtime_state_enabled",
                               "gil_runtime_state_locked", "gil_runtime_state_holder",
                               "code_object_generation", "tlbc_generation"]),
        ("thread_state", ["size", "prev", "next", "interp", "current_frame", "base_frame",
                          "last_profiled_frame", "last_profiled_frame_seq", "thread_id",
                          "native_thread_id", "datastack_chunk", "status", "holds_gil",
                          "gil_requested", "current_exception", "exc_state"]),
        ("err_stackitem", ["exc_value"]),
        ("interpreter_frame", ["size", "previous", "executable", "instr_ptr", "localsplus",
                               "owner", "stackpointer", "tlbc_index"]),
        ("code_object", ["size", "filename", "name", "qualname", "linetable", "firstlineno",
                         "argcount", "localsplusnames", "localspluskinds", "co_code_adaptive",
                         "co_tlbc"]),
        ("pyobject", ["size", "ob_type"]),
        ("type_object", ["size", "tp_name", "tp_repr", "tp_flags", "tp_basicsize",
                         "tp_dictoffset"]),
        ("heap_type_object", ["size", "ht_cached_keys"]),
        ("tuple_object", ["size", "ob_item", "ob_size"]),
        ("list_object", ["size", "ob_item", "ob_size"]),
        ("set_object", ["size", "used", "table", "mask"]),
        ("dict_object", ["size", "ma_keys", "ma_values"]),
        ("float_object", ["size", "ob_fval"]),
        ("long_object", ["size", "lv_tag", "ob_digit"]),
        ("bytes_object", ["size", "ob_size", "ob_sval"]),
        ("unicode_object", ["size", "state", "length", "asciiobject_size",
                            "compactunicodeobject_size"]),
        ("gc", ["size", "collecting", "frame", "generation_stats_size", "generation_stats"]),
        ("gen_object", ["size", "gi_name", "gi_iframe", "gi_frame_state"]),
        ("llist_node", ["next", "prev"]),
        ("debugger_support", ["eval_breaker", "remote_debugger_support",
                              "remote_debugging_enabled", "debugger_pending_call",
                              "debugger_script_path", "debugger_script_path_size"]),
    ],
}


def read_debug_offsets(read, runtime_addr, version):
    """Parse `_Py_DebugOffsets` into {"struct.field": offset}."""
    spec = _DEBUG_OFFSETS[version]
    count = 2 + sum(len(fields) for _, fields in spec)
    raw = read(runtime_addr, 8 + 8 * count)
    if raw[:8] != b"xdebugpy":
        raise ValueError("_PyRuntime does not start with the debug-offsets cookie")
    values = struct.unpack_from("<%dQ" % count, raw, 8)
    table = {"version": values[0], "free_threaded": values[1]}
    index = 2
    for name, fields in spec:
        for field in fields:
            table["%s.%s" % (name, field)] = values[index]
            index += 1
    return table


def _layout_from_debug_offsets(read, runtime_addr, version):
    t = read_debug_offsets(read, runtime_addr, version)
    if (t["version"] >> 24, (t["version"] >> 16) & 0xFF) != version:
        raise ValueError("debug offsets are for a different Python version")
    if t["free_threaded"]:
        raise NotImplementedError("free-threaded CPython builds are not supported")
    sizes = (t["interpreter_frame.size"], t["code_object.size"], t["thread_state.size"])
    if not all(16 <= s < 1 << 20 for s in sizes):
        raise ValueError("debug offsets look wrong for this build: %r" % (sizes,))
    L = Layout()
    L.version = version
    L.runtime_interp_head = t["runtime_state.interpreters_head"]
    L.interp_next = t["interpreter_state.next"]
    L.interp_threads_head = t["interpreter_state.threads_head"]
    L.tstate_next = t["thread_state.next"]
    L.tstate_native_tid = t["thread_state.native_thread_id"]
    L.tstate_frame = t["thread_state.current_frame"]
    L.frame_code = t["interpreter_frame.executable"]
    L.frame_previous = t["interpreter_frame.previous"]
    L.frame_instr = t["interpreter_frame.instr_ptr"]
    L.frame_owner = t["interpreter_frame.owner"]
    L.frame_localsplus = t["interpreter_frame.localsplus"]
    L.frame_size = t["interpreter_frame.localsplus"]
    L.code_firstlineno = t["code_object.firstlineno"]
    L.code_filename = t["code_object.filename"]
    L.code_qualname = t["code_object.qualname"]
    L.code_linetable = t["code_object.linetable"]
    L.code_adaptive = t["code_object.co_code_adaptive"]
    L.code_localsplusnames = t["code_object.localsplusnames"]
    L.code_localspluskinds = t["code_object.localspluskinds"]
    L.ob_type = t["pyobject.ob_type"]
    L.tp_name = t["type_object.tp_name"]
    L.str_length = t["unicode_object.length"]
    L.str_state = t["unicode_object.state"]
    L.str_ascii_data = t["unicode_object.asciiobject_size"]
    L.str_compact_data = t.get("unicode_object.compactunicodeobject_size",
                               t["unicode_object.asciiobject_size"] + 16)
    L.var_size = t["bytes_object.ob_size"]
    L.bytes_data = t["bytes_object.ob_sval"]
    L.tuple_item = t["tuple_object.ob_item"]
    L.list_item = t["list_object.ob_item"]
    L.long_tag = t["long_object.lv_tag"]
    L.long_digit = t["long_object.ob_digit"]
    L.float_value = t["float_object.ob_fval"]
    L.gil_holder = t["interpreter_state.gil_runtime_state_holder"]
    L.gil_locked = t["interpreter_state.gil_runtime_state_locked"]
    if version >= (3, 14):
        L.ref_tag_mask = 3
        # PEP 768: fields a debugger writes to ask the interpreter to run a script.
        L.remote = {
            "threads_main": t["interpreter_state.threads_main"],
            "enabled": t["debugger_support.remote_debugging_enabled"],   # in the interp
            "eval_breaker": t["debugger_support.eval_breaker"],          # in the tstate
            "support": t["debugger_support.remote_debugger_support"],    # in the tstate
            "pending": t["debugger_support.debugger_pending_call"],      # in the support
            "path": t["debugger_support.debugger_script_path"],          # in the support
            "path_size": t["debugger_support.debugger_script_path_size"],
        }
        # 3.14 renumbered the owners: the per-eval-loop entry frame is
        # FRAME_OWNED_BY_INTERPRETER (3); FRAME_OWNED_BY_CSTACK (4) is not Python code.
        L.owner_entry = 3
    return L


def layout_for(read, runtime_addr, version):
    """`version` is (major, minor)."""
    if version == (3, 12):
        return _layout_312()
    if version in _DEBUG_OFFSETS:
        return _layout_from_debug_offsets(read, runtime_addr, version)
    raise NotImplementedError("Seam does not support CPython %d.%d" % version)
