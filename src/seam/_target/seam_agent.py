"""Seam agent: runs inside the debugged Python process.

It owns the sys.monitoring configuration (breakpoints and Python-level stepping) and
answers JSON requests from the adapter. Requests only ever arrive at safe points: the
adapter calls ``seam_dispatch()`` while the process is stopped in ``seam_trap``, at
``Py_RunMain``, or from a pending call.

Handlers that want the debugger to stop return ``(ret, code, line, reason)``; the C
wrapper fires the trap after the handler's frame is gone, so no agent frame is ever on
the stack while stopped.
"""
import bisect
import json
import os
import sys
import threading
import traceback
from _thread import get_ident

import _seam_trap as _t

mon = sys.monitoring
E = mon.events
TOOL = mon.DEBUGGER_ID
HERE = os.path.dirname(os.path.abspath(__file__))

R_BREAKPOINT = 1
R_STEP = 2
R_RETURN_NATIVE = 3

_bps = {}         # canonical path -> {line: condition or None}
_lines = {}       # id(code) -> set of breakpoint lines (shared with the C callback)
_codes = {}       # id(code) -> code, for every code object carrying local events
_paths = {}       # co_filename -> canonical path
_valid_cache = {}  # canonical path -> (mtime, sorted list of lines that have code)
_step = None
_step_local = {}  # id(code) -> extra local events needed by the current step
_refs = {}        # variable reference -> object, valid for one stop
_next_ref = 1
_epoch = None
_in_dispatch = False


class _Step:
    __slots__ = ("mode", "ident", "frame", "glob", "native_return")

    def __init__(self, mode, ident, frame, native_return):
        self.mode = mode
        self.ident = ident
        self.frame = frame
        self.glob = 0
        self.native_return = native_return


def _canon(filename):
    p = _paths.get(filename)
    if p is None:
        p = filename if filename.startswith("<") else os.path.realpath(filename)
        _paths[filename] = p
    return p


def _internal(code):
    name = code.co_filename
    return name.startswith(HERE) or name.startswith("<frozen ")


# ---------------------------------------------------------------- breakpoints

def _valid_lines(path):
    """Sorted line numbers in `path` that carry bytecode, or None if unknown."""
    try:
        mtime = os.stat(path).st_mtime
        cached = _valid_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(path, "rb") as fh:
            top = compile(fh.read(), path, "exec", dont_inherit=True)
    except (OSError, SyntaxError, ValueError):
        return None
    lines = set()
    todo = [top]
    while todo:
        code = todo.pop()
        lines.update(l for _, _, l in code.co_lines() if l is not None)
        todo.extend(c for c in code.co_consts if hasattr(c, "co_lines"))
    out = sorted(lines)
    _valid_cache[path] = (mtime, out)
    return out


def _apply_local(code):
    cid = id(code)
    want = (E.LINE if cid in _lines else 0) | _step_local.get(cid, 0)
    if mon.get_local_events(TOOL, code) != want:
        mon.set_local_events(TOOL, code, want)
    if want:
        _codes[cid] = code
    else:
        _codes.pop(cid, None)


def _compute(code):
    table = _bps.get(_canon(code.co_filename))
    hits = None
    if table:
        hits = {l for _, _, l in code.co_lines() if l in table}
    if hits:
        _lines[id(code)] = hits
    else:
        _lines.pop(id(code), None)
    _apply_local(code)


def _update_global():
    want = (E.PY_START if _bps else 0) | (_step.glob if _step else 0)
    if mon.get_events(TOOL) != want:
        mon.set_events(TOOL, want)


def _update_slow():
    _t.set_slow(_step is not None or any(c for t in _bps.values() for c in t.values()))


def _reinstrument():
    for code in list(_codes.values()):
        _compute(code)
    for frame in sys._current_frames().values():
        while frame is not None:
            if not _internal(frame.f_code):
                _compute(frame.f_code)
            frame = frame.f_back
    _update_slow()
    _update_global()
    mon.restart_events()


def _on_start(code, offset):
    if not _internal(code):
        _compute(code)
    return mon.DISABLE


def _condition_ok(code, line, frame):
    table = _bps.get(_canon(code.co_filename))
    cond = table.get(line) if table else None
    if not cond:
        return True
    try:
        return bool(eval(cond, frame.f_globals, frame.f_locals))
    except Exception:
        return True  # a broken condition should be noticed, not silently skipped


# ------------------------------------------------------------------- stepping

def _finish_step():
    global _step
    _step = None
    codes = [_codes[cid] for cid in _step_local if cid in _codes]
    _step_local.clear()
    for code in codes:
        _apply_local(code)
    _update_slow()
    _update_global()


def _add_step_local(code, events):
    _step_local[id(code)] = _step_local.get(id(code), 0) | events
    _codes[id(code)] = code
    _apply_local(code)


def _on_line(code, line):
    if _in_dispatch:
        return None  # code run on behalf of the debugger never stops the debugger
    frame = sys._getframe(1)
    lines = _lines.get(id(code))
    if lines and line in lines and _condition_ok(code, line, frame):
        if _step is not None:
            _finish_step()
        return (None, code, line, R_BREAKPOINT)
    st = _step
    if st is None:
        return mon.DISABLE if not lines or line not in lines else None
    if st.ident != get_ident() or _internal(code):
        return None
    if st.mode == "in" or (st.mode == "over" and frame is st.frame):
        _finish_step()
        return (None, code, line, R_STEP)
    return None


def _leave_frame(code, unwinding):
    """The frame being stepped is finishing: retarget the step at its caller."""
    st = _step
    if st is None or _in_dispatch or st.ident != get_ident():
        return None
    frame = sys._getframe(2)
    if frame is not st.frame:
        return None
    if st.native_return:
        line = frame.f_lineno
        _finish_step()
        return (None, code, line, R_RETURN_NATIVE)
    back = frame.f_back
    while back is not None and _internal(back.f_code):
        back = back.f_back
    if back is None:
        _finish_step()
        return None
    st.frame = back
    st.native_return = False
    if unwinding:
        # Stop at the next line the caller runs (its except/finally block), or keep
        # following the exception upwards.
        st.mode = "over"
        st.glob = E.PY_UNWIND
        _add_step_local(back.f_code, E.LINE | E.PY_RETURN | E.PY_YIELD)
    else:
        # Stop in the caller as soon as it resumes, still on the calling line.
        st.mode = "caller"
        st.glob = E.PY_UNWIND
        _add_step_local(back.f_code, E.INSTRUCTION | E.PY_RETURN | E.PY_YIELD)
    _update_global()
    return None


def _on_return(code, offset, retval):
    return _leave_frame(code, False)


def _on_unwind(code, offset, exc):
    return _leave_frame(code, True)


def _on_instruction(code, offset):
    st = _step
    if st is None or _in_dispatch or st.mode != "caller" or st.ident != get_ident():
        return None
    frame = sys._getframe(1)
    if frame is not st.frame:
        return None
    line = frame.f_lineno
    _finish_step()
    return (None, code, line, R_STEP)


# ------------------------------------------------------------------- requests

def _ident_for(native_tid):
    if native_tid is not None:
        for t in threading.enumerate():
            if t.native_id == native_tid:
                return t.ident
    return None


def _frames(ident):
    frame = sys._current_frames().get(ident)
    out = []
    while frame is not None:
        if not _internal(frame.f_code):
            out.append(frame)
        frame = frame.f_back
    return out


def _frame_for(req):
    ident = _ident_for(req.get("tid"))
    if ident is None:
        raise LookupError("thread %s is not known to the threading module" % req.get("tid"))
    frames = _frames(ident)
    index = req.get("index", 0)
    if index >= len(frames):
        raise LookupError("no Python frame %d on thread %s" % (index, req.get("tid")))
    return frames[index]


def _new_ref(obj):
    global _next_ref
    ref = _next_ref
    _next_ref += 1
    _refs[ref] = obj
    return ref


def _safe_repr(value, limit=2000):
    try:
        text = repr(value)
    except BaseException as exc:  # repr runs arbitrary user code
        text = "<repr failed: %s>" % type(exc).__name__
    return text if len(text) <= limit else text[:limit] + "..."


def _has_children(value):
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return len(value) > 0
    if isinstance(value, (int, float, complex, str, bytes, bytearray, type(None))):
        return False
    try:
        return bool(getattr(value, "__dict__", None))
    except BaseException:
        return False


def _describe(name, value):
    return {
        "name": str(name),
        "value": _safe_repr(value),
        "type": type(value).__qualname__,
        "ref": _new_ref(value) if _has_children(value) else 0,
    }


def _children(obj, limit=500):
    if isinstance(obj, dict):
        pairs = ((_safe_repr(k, 200), v) for k, v in list(obj.items())[:limit])
    elif isinstance(obj, (list, tuple)):
        pairs = (("[%d]" % i, v) for i, v in enumerate(obj[:limit]))
    elif isinstance(obj, (set, frozenset)):
        pairs = (("[%d]" % i, v) for i, v in enumerate(list(obj)[:limit]))
    else:
        pairs = list(getattr(obj, "__dict__", {}).items())[:limit]
    return [_describe(k, v) for k, v in pairs]


def _cmd_sync_breakpoints(req):
    result = {}
    new = {}
    for path, items in req["files"].items():
        cpath = _canon(path)
        valid = _valid_lines(cpath)
        table = new.setdefault(cpath, {})
        answers = []
        for item in items:
            line = item["line"]
            verified = True
            if valid is not None:
                pos = bisect.bisect_left(valid, line)
                if pos < len(valid):
                    line = valid[pos]
                else:
                    verified = False
            if verified:
                table[line] = item.get("condition") or None
            answers.append({"line": line, "verified": verified})
        if not table:
            del new[cpath]
        result[path] = answers
    _bps.clear()
    _bps.update(new)
    _reinstrument()
    return result


def _cmd_step(req):
    global _step
    if _step is not None:
        _finish_step()
    ident = _ident_for(req.get("tid")) or get_ident()
    frames = _frames(ident)
    mode = req["mode"]
    if mode == "entry":
        # Stop on the first line of user code; no Python frame exists yet.
        _step = st = _Step("in", ident, None, False)
        st.glob = E.LINE
        _update_slow()
        _update_global()
        mon.restart_events()
        return True
    if not frames:
        raise LookupError("no Python frame to step in")
    frame = frames[req.get("index", 0)]
    _step = st = _Step(mode, ident, frame, bool(req.get("native_return")))
    if mode == "in":
        st.glob = E.LINE | E.PY_UNWIND
        _add_step_local(frame.f_code, E.PY_RETURN | E.PY_YIELD)
    elif mode == "over":
        st.glob = E.PY_UNWIND
        _add_step_local(frame.f_code, E.LINE | E.PY_RETURN | E.PY_YIELD)
    elif mode == "out":
        st.glob = E.PY_UNWIND
        _add_step_local(frame.f_code, E.PY_RETURN | E.PY_YIELD)
    else:
        _step = None
        raise ValueError("unknown step mode %r" % mode)
    _update_slow()
    _update_global()
    mon.restart_events()
    return True


def _cmd_clear_step(req):
    if _step is not None:
        _finish_step()
    return True


def _cmd_threads(req):
    return [{"tid": t.native_id, "name": t.name} for t in threading.enumerate()]


def _cmd_variables(req):
    kind = req["kind"]
    if kind == "ref":
        return _children(_refs[req["ref"]])
    frame = _frame_for(req)
    scope = frame.f_locals if kind == "locals" else frame.f_globals
    return [_describe(k, v) for k, v in list(scope.items())]


def _cmd_evaluate(req):
    frame = _frame_for(req)
    expr = req["expr"]
    try:
        code = compile(expr, "<seam-eval>", "eval")
    except SyntaxError:
        exec(compile(expr, "<seam-eval>", "exec"), frame.f_globals, frame.f_locals)
        return {"name": "", "value": "", "type": "", "ref": 0}
    return _describe("", eval(code, frame.f_globals, frame.f_locals))


def _cmd_status(req):
    return {
        "pid": os.getpid(),
        "version": list(sys.version_info[:3]),
        "global_events": mon.get_events(TOOL),
        "local_events": {
            "%s:%s" % (c.co_filename, c.co_qualname): mon.get_local_events(TOOL, c)
            for c in _codes.values()
        },
        "stepping": _step.mode if _step else None,
        "breakpoints": {p: sorted(t) for p, t in _bps.items()},
    }


_COMMANDS = {
    "sync_breakpoints": _cmd_sync_breakpoints,
    "step": _cmd_step,
    "clear_step": _cmd_clear_step,
    "threads": _cmd_threads,
    "variables": _cmd_variables,
    "evaluate": _cmd_evaluate,
    "status": _cmd_status,
}


def dispatch(raw):
    global _epoch, _in_dispatch
    _in_dispatch = True
    try:
        return _dispatch(raw)
    finally:
        _in_dispatch = False


def _dispatch(raw):
    global _epoch
    try:
        req = json.loads(raw)
        if req.get("epoch") != _epoch:
            _epoch = req.get("epoch")
            _refs.clear()
        out = {"ok": True, "result": _COMMANDS[req["cmd"]](req)}
    except BaseException as exc:
        out = {
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "trace": traceback.format_exc(),
        }
    return json.dumps(out, default=repr).encode()


def _install():
    mon.use_tool_id(TOOL, "seam")
    _t.configure(_lines, mon.DISABLE, _on_line, dispatch)
    mon.register_callback(TOOL, E.LINE, _t.line_cb)
    mon.register_callback(TOOL, E.PY_START, _on_start)
    mon.register_callback(TOOL, E.PY_RETURN, _t.wrap(_on_return))
    mon.register_callback(TOOL, E.PY_YIELD, _t.wrap(_on_return))
    mon.register_callback(TOOL, E.PY_UNWIND, _t.wrap(_on_unwind))
    mon.register_callback(TOOL, E.INSTRUCTION, _t.wrap(_on_instruction))


_install()
