/* Stands for a binding layer's own source: the function Python calls lives here and
 * hands over to the user's code in src/mapped.c. The tests mark this directory as glue
 * with `frameworkPaths`. */
#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030C0000
#include <Python.h>

long scale_twice(long value);

PyObject *
shim_call(PyObject *self, PyObject *arg)
{
    long value = PyLong_AsLong(arg); /* shim-first */
    if (value == -1 && PyErr_Occurred()) {
        return NULL;
    }
    return PyLong_FromLong(scale_twice(value)); /* shim-call */
}
