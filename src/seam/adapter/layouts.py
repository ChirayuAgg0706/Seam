"""CPython struct layouts (x86-64) used by the raw-memory reader.

3.12 has no `_Py_DebugOffsets`, so its offsets are bundled; they were taken from the
debug info of CPython 3.12.3 and are checked by tests/test_layouts.py.
"""


class Layout:
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
    frame_previous = 0
    frame_instr = 0
    frame_owner = 0
    frame_size = 80
    code_tag_mask = 0         # 3.14: f_executable is a tagged _PyStackRef
    instr_is_prev = True      # 3.12: prev_instr; 3.13+: instr_ptr
    owner_entry = 3           # FRAME_OWNED_BY_CSTACK
    owner_python = (0, 1, 2)  # thread, generator, frame object
    # PyCodeObject
    code_firstlineno = 0
    code_filename = 0
    code_qualname = 0
    code_linetable = 0
    code_adaptive = 0
    # objects
    str_length = 16
    str_state = 32
    str_kind_shift = 2
    str_ascii_data = 40
    str_compact_data = 56
    var_size = 16
    bytes_data = 32


def _layout_312():
    L = Layout()
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
    L.code_firstlineno = 68
    L.code_filename = 112
    L.code_qualname = 128
    L.code_linetable = 136
    L.code_adaptive = 192
    return L


def layout_for(read, runtime_addr, version):
    """`version` is (major, minor)."""
    if version == (3, 12):
        return _layout_312()
    raise NotImplementedError("Seam does not support CPython %d.%d yet" % version)
