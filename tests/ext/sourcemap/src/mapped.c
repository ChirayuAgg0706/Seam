/* Extension for the source-path scenarios (tests/test_sourcemap.py). The tests build it in
 * another directory, or with remapped paths, so that the names in its debug info are not
 * the names of these files on disk. Stable ABI: one build serves every Python version. */
#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030C0000
#include <Python.h>
#include "mapped_math.h"

static long calls = 0;

__attribute__((noinline)) static long
scale(long value, long factor)
{
    long scaled = value * factor;
    return scaled; /* scale-return */
}

/* Called by vendor/shim.c, which stands for a binding layer. */
__attribute__((noinline)) long
scale_twice(long value)
{
    long twice = scale(value, 2); /* twice-call */
    return twice;
}

static PyObject *
mp_scale(PyObject *self, PyObject *args)
{
    long value, factor;
    if (!PyArg_ParseTuple(args, "ll", &value, &factor)) { /* scale-first */
        return NULL;
    }
    calls += 1; /* calls-bump */
    long result = scale(value, factor); /* scale-call */
    return PyLong_FromLong(halve(result));
}

static PyObject *
mp_calls(PyObject *self, PyObject *noargs)
{
    return PyLong_FromLong(calls);
}

static PyObject *
mp_crash(PyObject *self, PyObject *noargs)
{
    volatile int *nowhere = NULL;
    *nowhere = 1; /* crash-here */
    Py_RETURN_NONE;
}

PyObject *shim_call(PyObject *self, PyObject *arg);

static PyMethodDef methods[] = {
    {"scale", mp_scale, METH_VARARGS, NULL},
    {"calls", mp_calls, METH_NOARGS, NULL},
    {"via_shim", shim_call, METH_O, NULL},
    {"crash", mp_crash, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "seam_mapped", NULL, -1, methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC
PyInit_seam_mapped(void)
{
    return PyModule_Create(&moduledef);
}
