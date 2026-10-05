/* A user function with glue inlined into it, even at -O0.
 *
 * CPython's Py_INCREF is `static inline` and always inlined, and its code belongs to a
 * header under include/python3.x, which Seam treats as glue. Stepping into the marked
 * line therefore leaves the thread in a glue frame that shares its PC and stack pointer
 * with the user's function: the shape optimised Rust and C++ have everywhere.
 *
 * Not built against the stable ABI (there Py_INCREF is an ordinary call), so this one is
 * built per interpreter. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *
si_keep(PyObject *self, PyObject *arg)
{
    long count = 1; /* keep-first */
    Py_INCREF(arg); /* keep-incref */
    count += 1; /* keep-after */
    (void)count;
    return arg; /* keep-return */
}

/* Py_DECREF is inlined too, and here it calls out: the object dies in it and its Python
 * `__del__` runs. The return address of that call is in the inlined glue, not in a frame
 * of si_drop's own. */
static PyObject *
si_drop(PyObject *self, PyObject *factory)
{
    PyObject *made = PyObject_CallNoArgs(factory); /* drop-make */
    if (made == NULL) {
        return NULL;
    }
    Py_DECREF(made); /* drop-decref */
    Py_RETURN_NONE; /* drop-return */
}

static PyMethodDef methods[] = {
    {"keep", si_keep, METH_O, NULL},
    {"drop", si_drop, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "seam_inline", NULL, -1, methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC
PyInit_seam_inline(void)
{
    return PyModule_Create(&moduledef);
}
