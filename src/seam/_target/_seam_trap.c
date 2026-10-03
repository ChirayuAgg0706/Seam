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
#include <stdlib.h>
#include <string.h>

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

static int
dispatch_buffer(const char *buf, long len)
{
    if (g_dispatch == NULL) {
        return -1;
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

static PyObject *
line_cb(PyObject *mod, PyObject *const *args, Py_ssize_t nargs)
{
    (void)mod;
    if (nargs != 2) {
        Py_RETURN_NONE;
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
    {"line_cb", (PyCFunction)(void (*)(void))line_cb, METH_FASTCALL, "LINE callback."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "_seam_trap", "Seam in-process helper.", -1, methods,
    NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC
PyInit__seam_trap(void)
{
    return PyModule_Create(&moduledef);
}
