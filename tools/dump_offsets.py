"""Print struct field offsets from an interpreter's debug info.

Run inside LLDB (needs the interpreter's debug symbols, e.g. Ubuntu's python3-dbg):

    lldb --batch -o "target create /usr/bin/python3.12" \
         -o "command script import tools/dump_offsets.py"

Used to produce and to re-check the bundled 3.12 table in src/seam/adapter/layouts.py.
"""
import json

FIELDS = {
    "pyruntimestate": ["interpreters"],
    "pyinterpreters": ["head"],
    "_is": ["next", "threads", "ceval"],
    "_ceval_state": ["gil", "gil_drop_request"],
    "pythreads": ["head"],
    "_ts": ["next", "cframe", "current_frame", "native_thread_id"],
    "_PyCFrame": ["current_frame"],
    "_PyInterpreterFrame": ["f_code", "f_executable", "previous", "prev_instr", "instr_ptr",
                            "owner", "localsplus", "f_locals", "frame_obj"],
    "PyCodeObject": ["co_firstlineno", "co_filename", "co_name", "co_qualname", "co_linetable",
                     "co_code_adaptive", "co_localsplusnames", "co_localspluskinds",
                     "co_nlocalsplus", "co_flags", "_co_firsttraceable"],
    "PyASCIIObject": ["length", "state"],
    "PyVarObject": ["ob_size"],
    "PyBytesObject": ["ob_sval"],
    "PyTupleObject": ["ob_item"],
    "PyListObject": ["ob_item"],
    "PyObject": ["ob_type"],
    "PyTypeObject": ["tp_name"],
    "PyLongObject": ["long_value"],
    "_PyLongValue": ["lv_tag", "ob_digit"],
    "PyFloatObject": ["ob_fval"],
    "PyCellObject": ["ob_ref"],
    "PyDictObject": ["ma_used"],
}


def _fields(sbtype, base=0, out=None):
    out = {} if out is None else out
    for i in range(sbtype.GetNumberOfFields()):
        member = sbtype.GetFieldAtIndex(i)
        name = member.GetName()
        offset = base + member.GetOffsetInBytes()
        if name:
            out.setdefault(name, offset)
        else:  # anonymous struct/union: flatten
            _fields(member.GetType(), offset, out)
    return out


def __lldb_init_module(debugger, internal_dict):
    target = debugger.GetSelectedTarget()
    result = {}
    for struct, wanted in FIELDS.items():
        sbtype = target.FindFirstType(struct)
        if not sbtype.IsValid():
            result[struct] = None
            continue
        sbtype = sbtype.GetCanonicalType()
        fields = _fields(sbtype)
        result[struct] = {"sizeof": sbtype.GetByteSize()}
        for name in wanted:
            if name in fields:
                result[struct][name] = fields[name]
    print("SEAM_OFFSETS " + json.dumps(result))
