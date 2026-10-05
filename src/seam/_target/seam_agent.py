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
from opcode import opmap

import _seam_trap as _t

mon = sys.monitoring
E = mon.events
TOOL = mon.DEBUGGER_ID
HERE = os.path.dirname(os.path.abspath(__file__))

R_BREAKPOINT = 1
R_STEP = 2
R_RETURN_NATIVE = 3
R_EXCEPTION = 6   # raised (5 is "attached")
R_UNCAUGHT = 7
LOG_FLAG = 0x100  # added to a reason (or alone): logpoint messages are waiting

_log_pending = []  # logpoint messages not yet collected by the adapter

_exc_filters = frozenset()  # of "raised", "uncaught", "user_unhandled"
_just_my_code = True
_exc_pending = None  # (exception, break mode) of the stop in progress, until fetched
_post_mortem = None  # frames of an uncaught exception, newest first: [(frame, line)]
_exc_fresh = False   # set at the trap; lets the stop's own first request keep the above
_thread_hook = None  # (ours, the one we replaced) while threading.excepthook is wrapped

_bps = {}         # canonical path -> {line: None (plain breakpoint) or _Spec}
_func_bps = {}    # bare function name -> [(name as given, None or _Spec)]
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
    __slots__ = ("mode", "ident", "frame", "glob", "native_return", "gen", "waiting")

    def __init__(self, mode, ident, frame, native_return):
        self.mode = mode
        self.ident = ident
        self.frame = frame
        self.glob = 0
        self.native_return = native_return
        self.gen = _t.step_gen()
        self.waiting = False  # a step in whose coroutine is suspended (see _on_yield)


_DEBUG_LOG = os.environ.get("SEAM_AGENT_LOG")


def _debug(*parts):
    if _DEBUG_LOG:
        with open(_DEBUG_LOG, "a") as fh:
            fh.write(" ".join(str(p) for p in parts) + "\n")


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
    want = ((E.PY_START if _bps or _func_bps else 0) | (_step.glob if _step else 0)
            | (E.RAISE if "raised" in _exc_filters else 0)
            | (E.PY_UNWIND if "user_unhandled" in _exc_filters else 0))
    if mon.get_events(TOOL) != want:
        mon.set_events(TOOL, want)


def _update_slow():
    _t.set_slow(_step is not None
                or any(spec is not None for table in _bps.values() for spec in table.values()))


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
    if _internal(code):
        return mon.DISABLE
    _compute(code)
    specs = _func_bps.get(code.co_name) if _func_bps and not _in_dispatch else None
    if not specs:
        return mon.DISABLE
    # A function breakpoint may name this function: stay enabled for it, and stop if the
    # name (bare, qualified, or with its module) and the breakpoint's conditions agree.
    frame = sys._getframe(1)
    module = frame.f_globals.get("__name__", "")
    for name, spec in specs:
        if name in (code.co_name, code.co_qualname, "%s.%s" % (module, code.co_qualname)):
            if _spec_verdict(spec, frame) == "stop":
                if _step is not None:
                    _finish_step()
                return (None, code, code.co_firstlineno,
                        R_BREAKPOINT | (LOG_FLAG if _log_pending else 0))
    return (None, code, code.co_firstlineno, LOG_FLAG) if _log_pending else None


class _Spec:
    """A breakpoint with a condition, a hit-count condition and/or a log message."""
    __slots__ = ("condition", "hit", "log", "hits", "complained")

    def __init__(self, condition, hit, log):
        self.condition = condition
        self.hit = tuple(hit) if hit else None  # (operator, number)
        self.log = log
        self.hits = 0
        self.complained = False  # the user has been told that the condition fails

    def same_as(self, other):
        return (other is not None and self.condition == other.condition
                and self.hit == other.hit and self.log == other.log)


_HIT_TESTS = {
    "==": lambda hits, n: hits == n,
    ">=": lambda hits, n: hits >= n,
    ">": lambda hits, n: hits > n,
    "<=": lambda hits, n: hits <= n,
    "<": lambda hits, n: hits < n,
    "%": lambda hits, n: hits % n == 0,
}


def _log_text(template, frame):
    """A logpoint's message with each {expression} replaced by its value."""
    out = []
    position = 0
    while True:
        start = template.find("{", position)
        end = template.find("}", start + 1) if start >= 0 else -1
        if end < 0:
            out.append(template[position:])
            return "".join(out)
        out.append(template[position:start])
        try:
            out.append(str(eval(template[start + 1:end], frame.f_globals, frame.f_locals)))
        except Exception as exc:
            out.append("{%s: %s}" % (type(exc).__name__, exc))
        position = end + 1


def _breakpoint_verdict(code, line, frame):
    """What a breakpoint on this line wants right now: "stop", "log" or nothing."""
    table = _bps.get(_canon(code.co_filename))
    return _spec_verdict(table.get(line) if table else None, frame)


def _spec_verdict(spec, frame):
    global _in_dispatch
    if spec is None:
        return "stop"
    _in_dispatch = True  # an exception inside a condition is not the program's
    try:
        if spec.condition:
            try:
                if not eval(spec.condition, frame.f_globals, frame.f_locals):
                    return None
            except Exception as exc:
                # A broken condition should be noticed, not silently skipped: the
                # breakpoint stops, and says why (once), or the user takes the stop
                # for a hit where the condition held.
                if not spec.complained:
                    spec.complained = True
                    _log_pending.append(
                        "Seam: the condition of this breakpoint could not be evaluated; "
                        "treating it as true: %s  (%s: %s)"
                        % (spec.condition, type(exc).__name__, exc))
        spec.hits += 1
        if spec.hit and not _HIT_TESTS[spec.hit[0]](spec.hits, spec.hit[1]):
            return None
        if spec.log is None:
            return "stop"
        _log_pending.append(_log_text(spec.log, frame))
        return "log"
    finally:
        _in_dispatch = False


# ----------------------------------------------------------------- exceptions

_library_prefixes = None
_user_code = {}  # co_filename -> bool


def _is_user(code):
    """True for code the user wrote: not the standard library, not installed packages."""
    global _library_prefixes
    name = code.co_filename
    known = _user_code.get(name)
    if known is None:
        if _internal(code):
            known = False
        elif not _just_my_code or name.startswith("<"):
            known = True
        else:
            if _library_prefixes is None:
                found = [os.path.dirname(os.path.realpath(os.__file__)) + os.sep]
                for entry in sys.path:
                    if entry.rstrip(os.sep).endswith(("site-packages", "dist-packages")):
                        found.append(os.path.realpath(entry) + os.sep)
                _library_prefixes = tuple(found)
            # A relative name that leads nowhere is not a file of the user's either:
            # Cython gives the frames it adds to a traceback the .pyx path as it was when
            # the module was built ("msgpack/_unpacker.pyx").
            path = _canon(name)
            known = (not path.startswith(_library_prefixes)
                     and (os.path.isabs(name) or os.path.exists(path)))
        _user_code[name] = known
    return known


def _stop_for_exception(exc, mode, frames):
    global _exc_pending, _post_mortem, _exc_fresh
    _exc_pending = (exc, mode)
    _post_mortem = frames
    _exc_fresh = True
    if _step is not None:
        _finish_step()


def _on_raise(code, offset, exc):
    if _in_dispatch or "raised" not in _exc_filters or not _is_user(code):
        return None
    # The event fires in every frame the exception passes through. Stop once: where it
    # is raised in the user's code, or where it first reaches it from a library.
    tb = exc.__traceback__
    tb = tb.tb_next if tb is not None else None
    while tb is not None:
        if _is_user(tb.tb_frame.f_code):
            return None
        tb = tb.tb_next
    _stop_for_exception(exc, "always", None)
    return (None, code, sys._getframe(1).f_lineno or 0, R_EXCEPTION)


def _on_uncaught(exc):
    """The interpreter (or threading) is about to report an exception nobody handled."""
    if (_in_dispatch or "uncaught" not in _exc_filters
            or not isinstance(exc, BaseException) or isinstance(exc, SystemExit)):
        return None
    # Its frames have unwound, but the traceback keeps them alive: show those.
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        if not _internal(tb.tb_frame.f_code):
            frames.append((tb.tb_frame, tb.tb_lineno))
        tb = tb.tb_next
    if not frames:
        return None
    frames.reverse()
    _stop_for_exception(exc, "unhandled", frames)
    return (None, None, 0, R_UNCAUGHT)


def _on_thread_exception(args):
    return _on_uncaught(args.exc_value)


def _user_unhandled(code, offset, exc):
    """A frame is unwinding: is an exception leaving the user's code for a library?

    That is the moment a frame of user code hands an exception to library code that
    called it: a failing assert going back to the test runner, an error in a callback.
    Whether user code further out catches it later is not asked. Exceptions that are not
    errors are left alone: the ones outside `Exception` (exits, cancellations, a test
    runner's "skip") and the ones that end an iteration.
    """
    if (_in_dispatch or not isinstance(exc, Exception)
            or isinstance(exc, (StopIteration, StopAsyncIteration)) or not _is_user(code)):
        return None
    frame = sys._getframe(1)
    back = frame.f_back
    while back is not None and _internal(back.f_code):
        back = back.f_back  # the import system and runpy are nobody's caller
    if back is None or _is_user(back.f_code):
        # Still in user code; or nobody is left to catch it, which is "uncaught".
        return None
    # The unwinding frame is still on the stack, at the line the exception passed. The
    # frames it came through have gone; the traceback has them, as for an uncaught one.
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame is not frame and not _internal(tb.tb_frame.f_code):
            frames.append((tb.tb_frame, tb.tb_lineno))
        tb = tb.tb_next
    frames.reverse()
    _stop_for_exception(exc, "userUnhandled", frames)
    return (None, code, frame.f_lineno or 0, R_EXCEPTION)


_unwind_hooks = None     # (stepping's PY_UNWIND callback, the same with the filter in front)
_unwind_checked = False  # the second of them is the one registered


def _watch_unwinding(wanted):
    """Put "user_unhandled" in front of stepping's PY_UNWIND callback, or take it out."""
    global _unwind_hooks, _unwind_checked
    if wanted == _unwind_checked:
        return
    if _unwind_hooks is None:
        # The event otherwise belongs to stepping alone. Whatever stepping registered is
        # chained to in C, because its handler finds its frame by counting from itself.
        # Both callables are kept for good: the interpreter holds no reference to a
        # callback while it runs, and a stop may be inside the one being replaced.
        stepping = mon.register_callback(TOOL, E.PY_UNWIND, None)
        checked = (_t.chain(_user_unhandled, stepping) if stepping is not None
                   else _t.wrap(_user_unhandled))
        _unwind_hooks = (stepping, checked)
    _unwind_checked = wanted
    mon.register_callback(TOOL, E.PY_UNWIND, _unwind_hooks[wanted])


def _set_exception_filters(filters, just_my_code):
    global _exc_filters, _just_my_code, _thread_hook
    if just_my_code != _just_my_code:
        _just_my_code = just_my_code
        _user_code.clear()
    _exc_filters = frozenset(filters)
    _watch_unwinding("user_unhandled" in _exc_filters)
    if "uncaught" in _exc_filters:
        _t.set_uncaught(_on_uncaught)
        if _thread_hook is None:
            ours = _t.chain(_on_thread_exception, threading.excepthook)
            _thread_hook = (ours, threading.excepthook)
            threading.excepthook = ours
    else:
        _t.set_uncaught(None)
        if _thread_hook is not None:
            if threading.excepthook is _thread_hook[0]:
                threading.excepthook = _thread_hook[1]
            _thread_hook = None
    _update_global()


# ------------------------------------------------------------ child processes

_dormant = False  # True in a forked child, where the helper has switched itself off


def _go_dormant():
    """Switch the helper off for good. Runs in a child process, right after the fork.

    Seam does not follow children: LLDB lets a forked child go, and nobody would ever
    answer a trap there. But the child starts as a copy of the parent, with the parent's
    breakpoints, exception filters and perhaps a step armed in its copy of the helper.
    Leave no trace of them: no monitoring events, no hooks, and the debugger's slot in
    sys.monitoring free for whoever wants it in the child.
    """
    global _dormant, _in_dispatch
    if _dormant:
        return
    _dormant = True
    _in_dispatch = False  # the fork may have come from an expression Seam was evaluating
    _cmd_shutdown(None)
    del _log_pending[:]
    _refs.clear()
    for event in vars(E).values():
        try:
            mon.register_callback(TOOL, event, None)
        except ValueError:
            pass  # NO_EVENTS, or a name that stands for several events
    mon.free_tool_id(TOOL)


# os.fork() runs the first; a fork made by native code only sets a flag in the C helper,
# which then calls the function the first time the child reaches any of Seam's callbacks.
os.register_at_fork(after_in_child=_go_dormant)
_t.set_dormant(_go_dormant)


# ------------------------------------------------------------------- stepping

# What each kind of step listens to: (events of every code object, events of the code of
# the frame it follows). PY_UNWIND and PY_THROW cannot be asked for per code object.
_STEP_EVENTS = {
    "in": (E.LINE | E.PY_UNWIND | E.PY_THROW, E.PY_RETURN | E.PY_YIELD | E.PY_RESUME),
    "over": (E.PY_UNWIND, E.LINE | E.PY_RETURN),
    "out": (E.PY_UNWIND, E.PY_RETURN),
    "caller": (E.PY_UNWIND, E.INSTRUCTION | E.PY_RETURN),
}
_CLEANUP_THROW = opmap.get("CLEANUP_THROW")
_CO_GENERATOR = 0x20
_CO_ITERABLE_COROUTINE = 0x100  # a generator used as a coroutine (types.coroutine)
_CO_SUSPENDS = 0x3A0  # generator, coroutine, iterable coroutine or async generator


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


def _generated(frame):
    """True for code that other code compiled from a string: a dataclass's __init__, a
    namedtuple's __new__, whatever goes through exec().

    There is no source to show for it, so a step treats it as it treats a library. Code
    given on the command line (python -c) has nothing below it: that is the program.
    """
    if not _just_my_code or frame.f_code.co_filename != "<string>":
        return False
    back = frame.f_back
    while back is not None and (back.f_code.co_filename == "<string>"
                                or _internal(back.f_code)):
        back = back.f_back
    return back is not None


def _aim(st, mode, frame):
    """Make the step follow `frame`: "in", "over", "out", or "caller" (stop as soon as it
    runs again).

    A step never stops in code that is not the user's (justMyCode). If that is what
    `frame` is (the event loop that ran a coroutine, a library that called a callback, a
    breakpoint set in a library), whatever was asked for becomes "the next line of user
    code this thread runs": a step in, which follows the frame down to its own caller
    when it returns.
    """
    if not _is_user(frame.f_code) or _generated(frame):
        mode = "in"
    st.mode = mode
    st.frame = frame
    st.waiting = False
    st.glob, local = _STEP_EVENTS[mode]
    _add_step_local(frame.f_code, local)


def _on_line(code, line):
    if _in_dispatch:
        return None  # code run on behalf of the debugger never stops the debugger
    frame = sys._getframe(1)
    if code.co_flags & _CO_SUSPENDS and code.co_code[frame.f_lasti] == _CLEANUP_THROW:
        # From 3.13 on, an exception thrown into a suspended `await` reports the await's
        # line once more, for the hidden instruction that passes the exception on. The
        # program is not about to run that line again; the handler's line comes next.
        return None
    lines = _lines.get(id(code))
    log = 0
    if lines and line in lines:
        verdict = _breakpoint_verdict(code, line, frame)
        if verdict == "stop":
            if _step is not None:
                _finish_step()
            # With LOG_FLAG if the verdict left something to say (a condition that failed).
            return (None, code, line, R_BREAKPOINT | (LOG_FLAG if _log_pending else 0))
        if _log_pending:
            log = LOG_FLAG  # deliver the message; a step in progress carries on
    st = _current()
    if st is None:
        if log:
            return (None, code, line, log)
        return mon.DISABLE if not lines or line not in lines else None
    if not _is_user(code):
        # No step ends here, whichever thread it is on: the line need not report again.
        if log:
            return (None, code, line, log)
        return mon.DISABLE if not lines or line not in lines else None
    if st.ident == get_ident() and (
            (st.mode == "in" and not st.waiting)
            or (frame is st.frame and st.mode in ("in", "over"))) and not _generated(frame):
        _finish_step()
        return (None, code, line, R_STEP | log)
    return (None, code, line, log) if log else None


def _current():
    """The armed step, unless the adapter cancelled it by bumping the generation."""
    st = _step
    if st is not None and st.gen != _t.step_gen():
        _finish_step()
        return None
    return st


def _leave_frame(code, unwinding):
    """The frame being stepped is finishing: retarget the step at its caller."""
    st = _current()
    _debug("leave", code.co_qualname, "unwinding" if unwinding else "return", st and st.mode)
    if st is None or _in_dispatch or st.ident != get_ident():
        return None
    frame = sys._getframe(2)
    if frame is not st.frame:
        return None
    if st.native_return:
        line = frame.f_lineno or 0
        _finish_step()
        return (None, code, line, R_RETURN_NATIVE)
    back = frame.f_back
    while back is not None and _internal(back.f_code):
        back = back.f_back
    if back is None:
        _finish_step()
        return None
    st.native_return = False
    # With an exception on its way there: stop at the next line the caller runs (its
    # except/finally block), or keep following the exception upwards. Otherwise stop in
    # the caller as soon as it resumes, still on the calling line.
    _aim(st, "over" if unwinding else "caller", back)
    _update_global()
    return None


def _on_return(code, offset, retval):
    return _leave_frame(code, False)


def _on_unwind(code, offset, exc):
    return _leave_frame(code, True)


def _on_yield(code, offset, value):
    """A generator or coroutine suspends. That is not a return: the step stays with it.

    Only a step in listens to this. Stepping over or out, nothing but the frame's own
    events is being listened to, so whatever runs until the frame is resumed goes by.
    """
    st = _current()
    if st is None or _in_dispatch or st.mode != "in" or sys._getframe(1) is not st.frame:
        return None
    if (code.co_flags & (_CO_GENERATOR | _CO_ITERABLE_COROUTINE) == _CO_GENERATOR
            or type(value).__name__ == "async_generator_wrapped_value"):
        # A `yield`: the value goes to the consumer, which runs next. Stepping in
        # follows it there.
        return None
    # An `await` that suspends hands the thread to the event loop. The tasks that run
    # meanwhile are not something this line called: stop listening to every line until
    # the frame is resumed.
    st.waiting = True
    st.glob &= ~E.LINE
    _update_global()
    return None


def _on_resume(code, offset, exc=None):
    """The suspended coroutine a step in is waiting for runs again (resumed or thrown into)."""
    st = _current()
    if st is None or _in_dispatch or not st.waiting or sys._getframe(1) is not st.frame:
        return None
    st.waiting = False
    st.glob |= E.LINE
    _update_global()
    return None


def _on_instruction(code, offset):
    st = _current()
    if st is None or _in_dispatch or st.mode != "caller" or st.ident != get_ident():
        return None
    frame = sys._getframe(1)
    if frame is not st.frame:
        return None
    line = frame.f_lineno
    if line is None:
        # Artificial instructions (e.g. the entry of an exception handler) have no
        # line; keep going until the frame is on a real one.
        return None
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
    position = req.get("pm")
    if position is not None:
        # A frame of the uncaught exception being shown; it is no longer on any stack.
        if _post_mortem is None or position >= len(_post_mortem):
            raise LookupError("that frame is no longer available")
        return _post_mortem[position][0]
    ident = _ident_for(req.get("tid"))
    if ident is None:
        raise LookupError("thread %s is not known to the threading module" % req.get("tid"))
    frames = _frames(ident)
    index = req.get("index", 0)
    if index >= len(frames):
        raise LookupError("no Python frame %d on thread %s" % (index, req.get("tid")))
    return frames[index]


def _new_ref(obj, expr):
    """Remember `obj` (and the expression that reaches it, if known) for this stop."""
    global _next_ref
    ref = _next_ref
    _next_ref += 1
    _refs[ref] = (obj, expr)
    return ref


def _safe_repr(value, limit=2000):
    try:
        text = repr(value)
    except BaseException as exc:  # repr runs arbitrary user code
        text = "<repr failed: %s>" % type(exc).__name__
    return text if len(text) <= limit else text[:limit] + "..."


_SIMPLE_KEYS = (str, int, float, bool, bytes, type(None))


def _attributes(obj):
    """An object's own data attributes: its __dict__, then its classes' __slots__."""
    out = {}
    try:
        out.update(getattr(obj, "__dict__", None) or {})
        for klass in type(obj).__mro__:
            slots = klass.__dict__.get("__slots__", ())
            for slot in (slots,) if isinstance(slots, str) else slots:
                if slot not in ("__dict__", "__weakref__") and slot not in out:
                    try:
                        out[slot] = getattr(obj, slot)
                    except AttributeError:
                        pass  # a slot that has not been assigned yet
    except BaseException:  # attribute access runs arbitrary user code
        pass
    return out


def _has_children(value):
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return len(value) > 0
    if isinstance(value, (int, float, complex, str, bytes, bytearray, type(None))):
        return False
    return bool(_attributes(value))


def _describe(name, value, expr=None):
    """One variable for the adapter. `expr` is source text that evaluates to it."""
    out = {
        "name": str(name),
        "value": _safe_repr(value),
        "type": type(value).__name__,
        "ref": _new_ref(value, expr) if _has_children(value) else 0,
    }
    if expr:
        out["expr"] = expr
    if isinstance(value, (list, tuple)) and value:
        out["indexed"] = len(value)  # lets the client fetch long sequences in pages
    return out


def _children(obj, expr, start=0, count=None, limit=500):
    """The children of a container or object, as (name, value, expression) triples."""
    if isinstance(obj, (list, tuple)):
        stop = len(obj) if count is None else min(len(obj), start + count)
        stop = min(stop, start + max(limit, count or 0))
        return [("[%d]" % i, obj[i], expr and "%s[%d]" % (expr, i))
                for i in range(start, stop)]
    if isinstance(obj, dict):
        return [(_safe_repr(k, 200), v,
                 expr and isinstance(k, _SIMPLE_KEYS) and "%s[%r]" % (expr, k) or None)
                for k, v in list(obj.items())[:limit]]
    if isinstance(obj, (set, frozenset)):
        return [("[%d]" % i, v, None) for i, v in enumerate(list(obj)[:limit])]
    return [(k, v, expr and "%s.%s" % (expr, k))
            for k, v in list(_attributes(obj).items())[:limit]]


def _write_back(frame):
    """Make assignments to a function frame's locals take effect (Python 3.12).

    From 3.13 on `frame.f_locals` writes through. In 3.12 it is a snapshot dict, and the
    interpreter only copies it back for old-style trace functions; do what it does there.
    """
    if sys.version_info < (3, 13):
        import ctypes

        ctypes.pythonapi.PyFrame_LocalsToFast(ctypes.py_object(frame), ctypes.c_int(0))


def _assign_child(container, name, value):
    """Store `value` where the child shown as `name` lives in `container`."""
    if isinstance(container, list):
        container[int(name.strip("[]"))] = value
    elif isinstance(container, dict):
        for key in container:
            if _safe_repr(key, 200) == name:
                container[key] = value
                return
        raise LookupError("no key shown as %s" % name)
    elif isinstance(container, (tuple, set, frozenset, str, bytes)):
        raise TypeError("a %s cannot be changed in place" % type(container).__name__)
    else:
        setattr(container, name, value)


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
                spec = None
                if item.get("condition") or item.get("hit") or item.get("log") is not None:
                    spec = _Spec(item.get("condition") or None, item.get("hit"),
                                 item.get("log"))
                    # An unchanged breakpoint keeps its hit count across edits elsewhere.
                    old = (_bps.get(cpath) or {}).get(line)
                    if spec.same_as(old):
                        spec.hits = old.hits
                table[line] = spec
            answers.append({"line": line, "verified": verified})
        if not table:
            del new[cpath]
        result[path] = answers
    _bps.clear()
    _bps.update(new)
    functions = {}
    for item in req.get("functions") or ():
        spec = None
        if item.get("condition") or item.get("hit"):
            spec = _Spec(item.get("condition") or None, item.get("hit"), None)
            for name, old in _func_bps.get(item["name"].rpartition(".")[2], ()):
                if name == item["name"] and spec.same_as(old):
                    spec.hits = old.hits
        functions.setdefault(item["name"].rpartition(".")[2], []).append((item["name"], spec))
    _func_bps.clear()
    _func_bps.update(functions)
    exceptions = req.get("exceptions")
    if exceptions is not None:
        _set_exception_filters(exceptions["filters"], bool(exceptions["just_my_code"]))
    _reinstrument()
    return result


def _cmd_logs(req):
    """Hand over (and forget) the logpoint messages produced since the last call."""
    messages = list(_log_pending)
    del _log_pending[:]
    return messages


def _cmd_exception(req):
    """Describe the exception this stop is about, and where its frames are."""
    global _exc_pending
    if _exc_pending is None:
        raise LookupError("not stopped at an exception")
    exc, mode = _exc_pending
    _exc_pending = None  # the object is not kept alive beyond this request
    kind = type(exc)
    module = getattr(kind, "__module__", None)
    full = kind.__qualname__
    if module and module not in ("builtins", "__main__"):
        full = "%s.%s" % (module, full)
    try:
        message = str(exc)
    except BaseException as failure:  # str() runs arbitrary user code
        message = "<str() failed: %s>" % type(failure).__name__
    try:
        trace = "".join(traceback.format_exception(kind, exc, exc.__traceback__))
    except BaseException:
        trace = ""
    out = {"type": kind.__qualname__, "full_type": full, "message": message,
           "trace": trace, "mode": mode}
    if _post_mortem is not None:
        out["frames"] = [
            {"filename": frame.f_code.co_filename, "name": frame.f_code.co_qualname,
             "line": line}
            for frame, line in _post_mortem]
    return out


def _cmd_step(req):
    global _step, _just_my_code
    if _step is not None:
        _finish_step()
    # The adapter says with every step what counts as the user's code: a session that
    # never set a breakpoint has not told the agent yet.
    just_my_code = bool(req.get("just_my_code", _just_my_code))
    if just_my_code != _just_my_code:
        _just_my_code = just_my_code
        _user_code.clear()
    ident = _ident_for(req.get("tid")) or get_ident()
    frames = _frames(ident)
    mode = req["mode"]
    if mode == "any":
        # Stop on the next line of the user's Python this thread runs, in whatever frame:
        # used for stop-on-entry and for stepping from native code into a Python callback.
        _step = st = _Step("in", ident, None, False)
        st.glob = E.LINE
    elif mode not in _STEP_EVENTS:
        raise ValueError("unknown step mode %r" % mode)
    elif not frames:
        raise LookupError("no Python frame to step in")
    else:
        # "caller": native code has just returned into the interpreter: stop as soon as
        # the calling Python frame resumes (or, if the call raised, where it is handled).
        _step = st = _Step(mode, ident, None, bool(req.get("native_return")))
        _aim(st, mode, frames[req.get("index", 0)])
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
        obj, expr = _refs[req["ref"]]
        return [_describe(name, value, child_expr) for name, value, child_expr
                in _children(obj, expr, req.get("start") or 0, req.get("count"))]
    frame = _frame_for(req)
    scope = frame.f_locals if kind == "locals" else frame.f_globals
    return [_describe(k, v, k if isinstance(k, str) and k.isidentifier() else None)
            for k, v in list(scope.items())]


def _cmd_set_variable(req):
    """Assign the value of an expression to a local, a global, or a child of an object."""
    frame = _frame_for(req)
    kind = req["kind"]
    name = req["name"]
    local_names = frame.f_locals  # in 3.12, a snapshot that _write_back copies back
    value = eval(req["value"], frame.f_globals, local_names)
    if kind == "locals":
        local_names[name] = value
        _write_back(frame)
        return _describe(name, value, name)
    if kind == "globals":
        frame.f_globals[name] = value
        return _describe(name, value, name)
    container, expr = _refs[req["ref"]]
    _assign_child(container, name, value)
    for child, _, child_expr in _children(container, expr):
        if child == name:
            return _describe(name, value, child_expr)
    return _describe(name, value)


def _cmd_evaluate(req):
    frame = _frame_for(req)
    expr = req["expr"]
    local_names = frame.f_locals
    try:
        code = compile(expr, "<seam-eval>", "eval")
    except SyntaxError:
        # A statement, typically an assignment typed into the debug console.
        exec(compile(expr, "<seam-eval>", "exec"), frame.f_globals, local_names)
        _write_back(frame)
        return {"name": "", "value": "", "type": "", "ref": 0}
    return _describe("", eval(code, frame.f_globals, local_names), "(%s)" % expr)


def _cmd_complete(req):
    """Names that could continue the text typed into the debug console.

    Completes the dotted name the text ends with: `stem` is the part of its last word
    already typed, `names` the candidates. Everything before the last dot is evaluated,
    as hovering over it would.
    """
    import builtins
    import keyword
    import re
    frame = _frame_for(req)
    text = req["text"]
    match = re.search(r"((?:[A-Za-z_]\w*\.)*)([A-Za-z_]\w*)?$", text)
    path, stem = match.group(1), match.group(2) or ""
    if path:
        try:
            names = dir(eval(path[:-1], frame.f_globals, frame.f_locals))
        except BaseException:  # not something that exists here: nothing to offer
            return {"stem": stem, "names": []}
    elif text[:match.start()].endswith("."):
        # After a call, a subscript or a literal ("...".up): not evaluated for a guess.
        return {"stem": stem, "names": []}
    else:
        names = [*frame.f_locals, *frame.f_globals, *dir(builtins), *keyword.kwlist]
    hidden = not stem.startswith("_")
    found = sorted({n for n in names if isinstance(n, str) and n.startswith(stem)
                    and not (hidden and n.startswith("_"))})
    return {"stem": stem, "names": found[:300]}


def _cmd_shutdown(req):
    """The debugger is detaching: leave no trace in the running program."""
    global _post_mortem, _exc_pending
    if _step is not None:
        _finish_step()
    _bps.clear()
    _func_bps.clear()
    _set_exception_filters((), True)
    _post_mortem = _exc_pending = None
    _reinstrument()
    for code in list(_codes.values()):
        mon.set_local_events(TOOL, code, 0)
    _codes.clear()
    _lines.clear()
    mon.set_events(TOOL, 0)
    _t.set_slow(False)
    return True


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
        "exception_filters": sorted(_exc_filters),
        "function_breakpoints": sorted(name for specs in _func_bps.values()
                                       for name, _ in specs),
    }


_COMMANDS = {
    "sync_breakpoints": _cmd_sync_breakpoints,
    "step": _cmd_step,
    "clear_step": _cmd_clear_step,
    "threads": _cmd_threads,
    "variables": _cmd_variables,
    "evaluate": _cmd_evaluate,
    "complete": _cmd_complete,
    "set_variable": _cmd_set_variable,
    "exception": _cmd_exception,
    "logs": _cmd_logs,
    "status": _cmd_status,
    "shutdown": _cmd_shutdown,
}


def dispatch(raw):
    global _epoch, _in_dispatch
    _in_dispatch = True
    try:
        return _dispatch(raw)
    finally:
        _in_dispatch = False


def _dispatch(raw):
    global _epoch, _post_mortem, _exc_pending, _exc_fresh
    try:
        req = json.loads(raw)
        if req.get("epoch") != _epoch:
            _epoch = req.get("epoch")
            _refs.clear()
            # Whatever an earlier stop held on to is released; what the stop now in
            # progress has just recorded is kept.
            if _exc_fresh:
                _exc_fresh = False
            else:
                _post_mortem = _exc_pending = None
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
    mon.register_callback(TOOL, E.PY_START, _t.wrap(_on_start))
    mon.register_callback(TOOL, E.PY_RETURN, _t.wrap(_on_return))
    mon.register_callback(TOOL, E.PY_YIELD, _t.wrap(_on_yield))
    mon.register_callback(TOOL, E.PY_RESUME, _t.wrap(_on_resume))
    mon.register_callback(TOOL, E.PY_THROW, _t.wrap(_on_resume))
    mon.register_callback(TOOL, E.PY_UNWIND, _t.wrap(_on_unwind))
    mon.register_callback(TOOL, E.INSTRUCTION, _t.wrap(_on_instruction))
    mon.register_callback(TOOL, E.RAISE, _t.wrap(_on_raise))


_install()

R_ATTACHED = 5
_attached_trap = _t.wrap(lambda: (None, None, 0, R_ATTACHED))


def attached():
    """Called by the attach bootstrap: stop in the debugger now that the agent is loaded."""
    _attached_trap()
