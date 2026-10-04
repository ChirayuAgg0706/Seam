/* Extension for the scenarios about code without source (tests/test_nosource.py). The
 * tests build it optimised, without debug info, and strip it, as a wheel from PyPI is.
 * The functions the tests name are exported, so they keep their names. Stable ABI. */
#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030C0000
#include <Python.h>

static volatile int *volatile nowhere = NULL;

__attribute__((noinline)) long
nosource_leaf(long value)
{
    return value * 3 + 1;
}

__attribute__((noinline)) long
nosource_work(long value)
{
    long tripled = nosource_leaf(value);
    return tripled + nosource_leaf(tripled);
}

__attribute__((noinline)) void
nosource_crash(volatile int *where)
{
    *where = 1;
}

static PyObject *
ns_work(PyObject *self, PyObject *arg)
{
    long value = PyLong_AsLong(arg);
    if (value == -1 && PyErr_Occurred()) {
        return NULL;
    }
    return PyLong_FromLong(nosource_work(value));
}

static PyObject *
ns_crash(PyObject *self, PyObject *noargs)
{
    nosource_crash(nowhere);
    Py_RETURN_NONE;
}

/* Hands the interpreter a pointer that is not an object: the crash is inside the
 * interpreter itself, as it is after a use-after-free in an extension. */
PyObject *
nosource_bad_object(PyObject *self, PyObject *noargs)
{
    PyObject *text = PyObject_Repr((PyObject *)16);
    Py_XDECREF(text);
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"work", ns_work, METH_O, NULL},
    {"crash", ns_crash, METH_NOARGS, NULL},
    {"bad_object", nosource_bad_object, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "seam_nosource", NULL, -1, methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC
PyInit_seam_nosource(void)
{
    return PyModule_Create(&moduledef);
}
