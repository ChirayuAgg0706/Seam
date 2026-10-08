/* Plain C-API extension used by the mixed-mode scenarios. Built against the stable ABI
 * so one source serves every Python version under test. Line markers are trailing
 * comments; tests look them up by name. */
#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030C0000
#include <Python.h>
#include <stdlib.h>
#include <unistd.h>
#include <errno.h>

__attribute__((noinline)) static long
add_impl(long a, long b)
{
    long sum = a + b;
    return sum; /* add-impl-return */
}

static PyObject *
st_add(PyObject *self, PyObject *args)
{
    long a, b;
    if (!PyArg_ParseTuple(args, "ll", &a, &b)) { /* add-first */
        return NULL;
    }
    long result = add_impl(a, b); /* add-call */
    return PyLong_FromLong(result); /* add-return */
}

static PyObject *
st_call_back(PyObject *self, PyObject *args)
{
    PyObject *fn, *arg;
    if (!PyArg_ParseTuple(args, "OO", &fn, &arg)) { /* callback-first */
        return NULL;
    }
    int depth = 1;
    PyObject *res = PyObject_CallFunctionObjArgs(fn, arg, NULL); /* callback-call */
    depth += 1; /* callback-after */
    if (res == NULL) {
        return NULL;
    }
    (void)depth;
    return res; /* callback-return */
}

static PyObject *
st_fail(PyObject *self, PyObject *noargs)
{
    PyErr_SetString(PyExc_ValueError, "native failure"); /* fail-first */
    return NULL;
}

static PyObject *
st_sleep_nogil(PyObject *self, PyObject *arg)
{
    long ms = PyLong_AsLong(arg);
    if (ms == -1 && PyErr_Occurred()) {
        return NULL;
    }
    Py_BEGIN_ALLOW_THREADS
    usleep((useconds_t)ms * 1000); /* sleep-body */
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;
}

static PyObject *
st_crash(PyObject *self, PyObject *noargs)
{
    volatile int *nowhere = NULL;
    int before = 7;
    *nowhere = before; /* crash-here */
    Py_RETURN_NONE;
}

static PyObject *
st_do_abort(PyObject *self, PyObject *noargs)
{
    abort(); /* abort-here */
    Py_RETURN_NONE;
}

static long bump_count = 0;

static PyObject *
st_bump(PyObject *self, PyObject *noargs)
{
    bump_count += 1; /* bump-here */
    return PyLong_FromLong(bump_count);
}

static PyObject *
st_fork_child(PyObject *self, PyObject *noargs)
{
    pid_t pid = fork(); /* native-fork-call */
    long result = (long)pid; /* native-fork-after */
    if (pid == 0) {
        _exit(7);
    }
    return PyLong_FromLong(result);
}

static PyObject *
st_blocked_read(PyObject *self, PyObject *noargs)
{
    char byte = 0;
    ssize_t received;
    Py_BEGIN_ALLOW_THREADS
    do {
        do {
            received = read(STDIN_FILENO, &byte, 1);
        } while (received < 0 && errno == EINTR);
    } while (received > 0 && byte != '\n');
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"blocked_read", st_blocked_read, METH_NOARGS, NULL},
    {"fork_child", st_fork_child, METH_NOARGS, NULL},
    {"bump", st_bump, METH_NOARGS, NULL},
    {"add", st_add, METH_VARARGS, NULL},
    {"call_back", st_call_back, METH_VARARGS, NULL},
    {"fail", st_fail, METH_NOARGS, NULL},
    {"sleep_nogil", st_sleep_nogil, METH_O, NULL},
    {"crash", st_crash, METH_NOARGS, NULL},
    {"do_abort", st_do_abort, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "seamtest", NULL, -1, methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC
PyInit_seamtest(void)
{
    return PyModule_Create(&moduledef);
}
