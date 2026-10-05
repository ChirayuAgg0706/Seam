/*
 * Seam in-process helper (stable ABI, one binary for CPython 3.12+).
 *
 * - seam_trap(): the no-op function LLDB keeps a breakpoint on. Every Python-level
 *   stop is a native stop here, with the GIL held and the interpreter consistent.
 * - seam_dispatch(): called by LLDB (only at safe points) to run one JSON request
 *   through the Python agent. Request and reply travel through the buffers below.
 * - line_cb(): the hot sys.monitoring LINE callback.
 * - wrap(): turns a Python handler into a C callable that performs the trap itself,
 *   so no Seam Python frame is on the stack while stopped.
 */
#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030C0000
#include <Python.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SEAM_REQ_CAP (1 << 20)
#define EXPORT __attribute__((visibility("default"), used))

#define REASON_BREAKPOINT 1

EXPORT char seam_req_buf[SEAM_REQ_CAP];
EXPORT volatile long seam_req_len = 0;
EXPORT char *volatile seam_resp_ptr = NULL;
EXPORT volatile long seam_resp_len = 0;
EXPORT volatile long seam_req_cap = SEAM_REQ_CAP;
EXPORT char seam_pend_buf[SEAM_REQ_CAP];
EXPORT volatile long seam_pend_len = 0;
/* Bumped by the adapter (a plain memory write, legal at any stop) to cancel whatever
 * Python-level step is armed: the agent drops a step whose generation is stale. */
EXPORT volatile long seam_step_gen = 0;

static PyObject *g_dispatch = NULL; /* agent.dispatch(bytes) -> bytes */
static PyObject *g_bps = NULL;      /* dict: id(code) -> set of line numbers */
static PyObject *g_disable = NULL;  /* sys.monitoring.DISABLE */
static PyObject *g_py_line = NULL;  /* slow-path LINE handler */
static int g_slow = 0;              /* route every LINE event to g_py_line */

EXPORT __attribute__((noinline)) void
seam_trap(PyObject *code, long line, long reason)
{
    (void)code; (void)line; (void)reason;
    __asm__ volatile("" ::: "memory");
}

/* Call through a volatile pointer so the arguments are always materialised. */
static void (*volatile trap_ptr)(PyObject *, long, long) = seam_trap;

/*
 * Child processes. Nobody debugs a child the program forks: LLDB lets it go at the fork.
 * It starts as a copy of the parent, though, helper and all, with the parent's
 * breakpoints, and it must never trap or pay for them. The agent switches itself off in
 * a child through os.register_at_fork. A fork made by native code skips those hooks, so
 * the flag below (set by fork() itself) makes every entry point a no-op in a child and
 * has the agent switch itself off the first time one is reached.
 */
static volatile int g_forked = 0;
static PyObject *g_go_dormant = NULL; /* agent function that switches everything off */

static void
forked_child(void)
{
    g_forked = 1;
}

/* True in a forked child. The first call there also runs the agent's switch-off. */
static int
dormant(void)
{
    if (!g_forked) {
        return 0;
    }
    if (g_go_dormant != NULL) {
        PyObject *fn = g_go_dormant;
        g_go_dormant = NULL;
        PyObject *saved = PyErr_GetRaisedException();
        PyObject *res = PyObject_CallNoArgs(fn);
        if (res == NULL) {
            PyErr_WriteUnraisable(fn);
        }
        Py_XDECREF(res);
        Py_DECREF(fn);
        PyErr_SetRaisedException(saved);
    }
    return 1;
}

static PyObject *
trap_set_dormant(PyObject *mod, PyObject *fn)
{
    (void)mod;
    Py_INCREF(fn);
    Py_XDECREF(g_go_dormant);
    g_go_dormant = fn;
    Py_RETURN_NONE;
}

static int
dispatch_buffer(const char *buf, long len)
{
    if (g_dispatch == NULL || g_forked) {
        return -1; /* in a child: a request queued before the fork is not for it */
    }
    int rc = -2;
    PyObject *saved = PyErr_GetRaisedException();
    PyObject *res = PyObject_CallFunction(g_dispatch, "y#", buf, (Py_ssize_t)len);
    if (res != NULL) {
        char *p;
        Py_ssize_t n;
        if (PyBytes_AsStringAndSize(res, &p, &n) == 0) {
            char *copy = malloc((size_t)n + 1);
            if (copy != NULL) {
                memcpy(copy, p, (size_t)n);
                copy[n] = 0;
                char *old = seam_resp_ptr;
                seam_resp_ptr = copy;
                seam_resp_len = (long)n;
                free(old);
                rc = 0;
            }
        }
        Py_DECREF(res);
    }
    if (rc != 0) {
        PyErr_Clear();
    }
    if (g_forked) {
        /* The request (an expression typed into the debug console) forked, and this is
         * the child coming back from it. The caller is the debugger, which is not here:
         * there is nothing to return to. */
        _exit(0);
    }
    PyErr_SetRaisedException(saved);
    return rc;
}

EXPORT int
seam_dispatch(void)
{
    return dispatch_buffer(seam_req_buf, seam_req_len);
}

/*
 * Signature required by Py_AddPendingCall. The pending request has its own buffer so
 * that requests made at a later safe stop cannot be replayed by a late pending call;
 * the adapter cancels a pending request by zeroing seam_pend_len.
 */
EXPORT int
seam_pending(void *arg)
{
    (void)arg;
    long len = seam_pend_len;
    if (len > 0) {
        seam_pend_len = 0;
        dispatch_buffer(seam_pend_buf, len);
    }
    return 0;
}

/*
 * Run a Python handler. If it returns a 4-tuple (ret, code, line, reason) the trap
 * fires here, after the handler's frame is gone, and `ret` goes back to the interpreter.
 * A handler exception must never leak into the debugged program.
 */
static PyObject *
run_handler(PyObject *handler, PyObject *args)
{
    if (dormant()) {
        Py_RETURN_NONE;
    }
    PyObject *r = PyObject_CallObject(handler, args);
    if (r == NULL) {
        PyErr_WriteUnraisable(handler);
        Py_RETURN_NONE;
    }
    if (PyTuple_Check(r) && PyTuple_Size(r) == 4) {
        PyObject *ret = PyTuple_GetItem(r, 0);
        PyObject *code = PyTuple_GetItem(r, 1);
        long line = PyLong_AsLong(PyTuple_GetItem(r, 2));
        long reason = PyLong_AsLong(PyTuple_GetItem(r, 3));
        if (PyErr_Occurred()) {
            PyErr_Clear();
        }
        else {
            trap_ptr(code, line, reason);
        }
        Py_INCREF(ret);
        Py_DECREF(r);
        return ret;
    }
    return r;
}

static PyObject *
wrapped_call(PyObject *self, PyObject *args)
{
    return run_handler(self, args);
}

static PyMethodDef wrapped_def = {"seam_callback", wrapped_call, METH_VARARGS, NULL};

static PyObject *
trap_wrap(PyObject *mod, PyObject *handler)
{
    (void)mod;
    return PyCFunction_NewEx(&wrapped_def, handler, NULL);
}

/* chain(handler, next): run the handler (which may trap), then call `next` with the same
 * arguments and return its result. Used to sit in front of threading.excepthook without
 * leaving a Seam Python frame on the stack while the original hook runs. */
static PyObject *
chained_call(PyObject *self, PyObject *args)
{
    PyObject *r = run_handler(PyTuple_GetItem(self, 0), args);
    Py_XDECREF(r);
    return PyObject_CallObject(PyTuple_GetItem(self, 1), args);
}

static PyMethodDef chained_def = {"seam_chain", chained_call, METH_VARARGS, NULL};

static PyObject *
trap_chain(PyObject *mod, PyObject *args)
{
    (void)mod;
    PyObject *handler, *next;
    if (!PyArg_ParseTuple(args, "OO", &handler, &next)) {
        return NULL;
    }
    PyObject *pair = PyTuple_Pack(2, handler, next);
    if (pair == NULL) {
        return NULL;
    }
    PyObject *fn = PyCFunction_NewEx(&chained_def, pair, NULL);
    Py_DECREF(pair);
    return fn;
}

/*
 * Uncaught exceptions. The interpreter raises the audit event "sys.excepthook" just
 * before it reports an exception nobody handled, whatever sys.excepthook has been
 * replaced with. A C audit hook costs a string comparison per audit event; a Python one
 * would run for every open(), import and ctypes call in the program.
 *
 * PySys_AddAuditHook is the one function used here that is outside the stable ABI. It
 * has been exported by every CPython since 3.8. A hook cannot be removed, so it is only
 * added the first time uncaught-exception stops are switched on, and does nothing while
 * g_uncaught is NULL.
 */
typedef int (*seam_audit_hook)(const char *, PyObject *, void *);
extern int PySys_AddAuditHook(seam_audit_hook, void *);

static PyObject *g_uncaught = NULL; /* handler(exception), or NULL when switched off */
static int g_audit_installed = 0;

static int
audit_hook(const char *event, PyObject *args, void *data)
{
    (void)data;
    if (g_uncaught == NULL || strcmp(event, "sys.excepthook") != 0) {
        return 0;
    }
    /* args is (hook, type, value, traceback). */
    PyObject *value = PyTuple_Check(args) && PyTuple_Size(args) == 4
        ? PyTuple_GetItem(args, 2) : NULL;
    if (value != NULL) {
        PyObject *call_args = PyTuple_Pack(1, value);
        if (call_args != NULL) {
            Py_XDECREF(run_handler(g_uncaught, call_args));
            Py_DECREF(call_args);
        }
    }
    if (PyErr_Occurred()) {
        PyErr_Clear(); /* never veto the audited operation */
    }
    return 0;
}

static PyObject *
trap_set_uncaught(PyObject *mod, PyObject *handler)
{
    (void)mod;
    if (handler == Py_None) {
        Py_CLEAR(g_uncaught);
        Py_RETURN_NONE;
    }
    if (!g_audit_installed) {
        if (PySys_AddAuditHook(audit_hook, NULL) != 0) {
            return NULL;
        }
        g_audit_installed = 1;
    }
    Py_INCREF(handler);
    Py_XDECREF(g_uncaught);
    g_uncaught = handler;
    Py_RETURN_NONE;
}

static PyObject *
line_cb(PyObject *mod, PyObject *const *args, Py_ssize_t nargs)
{
    (void)mod;
    if (nargs != 2) {
        Py_RETURN_NONE;
    }
    if (dormant()) {
        Py_INCREF(g_disable);
        return g_disable;
    }
    if (!g_slow) {
        PyObject *key = PyLong_FromVoidPtr(args[0]);
        if (key != NULL) {
            PyObject *lines = PyDict_GetItemWithError(g_bps, key);
            Py_DECREF(key);
            if (lines != NULL && PySet_Contains(lines, args[1]) == 1) {
                trap_ptr(args[0], PyLong_AsLong(args[1]), REASON_BREAKPOINT);
                Py_RETURN_NONE;
            }
        }
        if (PyErr_Occurred()) {
            PyErr_Clear();
        }
        Py_INCREF(g_disable);
        return g_disable;
    }
    PyObject *tup = PyTuple_Pack(2, args[0], args[1]);
    if (tup == NULL) {
        PyErr_Clear();
        Py_RETURN_NONE;
    }
    PyObject *r = run_handler(g_py_line, tup);
    Py_DECREF(tup);
    return r;
}

static PyObject *
trap_configure(PyObject *mod, PyObject *args)
{
    (void)mod;
    PyObject *bps, *disable, *py_line, *dispatch;
    if (!PyArg_ParseTuple(args, "O!OOO", &PyDict_Type, &bps, &disable, &py_line, &dispatch)) {
        return NULL;
    }
    Py_INCREF(bps); Py_XDECREF(g_bps); g_bps = bps;
    Py_INCREF(disable); Py_XDECREF(g_disable); g_disable = disable;
    Py_INCREF(py_line); Py_XDECREF(g_py_line); g_py_line = py_line;
    Py_INCREF(dispatch); Py_XDECREF(g_dispatch); g_dispatch = dispatch;
    Py_RETURN_NONE;
}

static PyObject *
trap_set_slow(PyObject *mod, PyObject *arg)
{
    (void)mod;
    int v = PyObject_IsTrue(arg);
    if (v < 0) {
        return NULL;
    }
    g_slow = v;
    Py_RETURN_NONE;
}

static PyObject *
trap_step_gen(PyObject *mod, PyObject *noargs)
{
    (void)mod; (void)noargs;
    return PyLong_FromLong(seam_step_gen);
}

static PyMethodDef methods[] = {
    {"step_gen", trap_step_gen, METH_NOARGS, "Current step generation."},
    {"configure", trap_configure, METH_VARARGS, "configure(bps, DISABLE, py_line, dispatch)"},
    {"set_slow", trap_set_slow, METH_O, "Route every LINE event through the Python handler."},
    {"wrap", trap_wrap, METH_O, "Wrap a Python handler so the trap fires from C."},
    {"chain", trap_chain, METH_VARARGS, "chain(handler, next): handler (may trap), then next."},
    {"set_uncaught", trap_set_uncaught, METH_O, "Handler for uncaught exceptions, or None."},
    {"set_dormant", trap_set_dormant, METH_O, "Function that switches the agent off in a child."},
    {"line_cb", (PyCFunction)(void (*)(void))line_cb, METH_FASTCALL, "LINE callback."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "_seam_trap", "Seam in-process helper.", -1, methods,
    NULL, NULL, NULL, NULL,
};

/*
 * Entry traps (src/seam/adapter/entrytraps.py). While a step-in from Python is in flight
 * the adapter may have written trap instructions over the first instruction of native
 * functions, straight into this process's memory. LLDB does not know about them, so
 * unlike its own breakpoints nobody takes them out of a child the program forks in that
 * window, and the child would die at its first call of such a function. The adapter
 * leaves the list of patched addresses here; a forked child puts the original bytes back
 * before it runs anything else. seam_fork_count is non-zero only while traps are in.
 */
struct seam_patch {
    uint64_t address;
    uint64_t original;
};
EXPORT struct seam_patch *volatile seam_fork_table = NULL;
EXPORT volatile long seam_fork_count = 0;

static void
fork_child(void)
{
    long count = seam_fork_count;
    struct seam_patch *table = seam_fork_table;
    seam_fork_count = 0;
    if (count <= 0 || table == NULL) {
        return;
    }
    /* The code is mapped read-only; a process may still write to it through this file. */
    int fd = open("/proc/self/mem", O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        return;
    }
    for (long i = 0; i < count; i++) {
        unsigned char byte = (unsigned char)table[i].original;
        if (pwrite(fd, &byte, 1, (off_t)table[i].address) != 1) {
            break;
        }
    }
    close(fd);
}

PyMODINIT_FUNC
PyInit__seam_trap(void)
{
    static int registered = 0;
    if (!registered) {
        /* In a forked child: the entry traps come out, then the helper goes dormant. */
        pthread_atfork(NULL, NULL, fork_child);
        pthread_atfork(NULL, NULL, forked_child);
        registered = 1;
    }
    return PyModule_Create(&moduledef);
}
