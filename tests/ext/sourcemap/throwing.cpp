// C++ extension for the source-path scenarios: throws a C++ exception and turns it into
// a Python one, as a binding layer would.
#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030C0000
#include <Python.h>
#include <stdexcept>

__attribute__((noinline)) static void
thrower(const char *reason)
{
    throw std::runtime_error(reason); // throw-here
}

static PyObject *
tw_fail(PyObject *self, PyObject *arg)
{
    try {
        thrower("thrown on purpose");
    } catch (const std::exception &exc) {
        PyErr_SetString(PyExc_RuntimeError, exc.what());
    }
    return NULL;
}

static PyMethodDef methods[] = {
    {"fail", tw_fail, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT, "seam_throwing", NULL, -1, methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC
PyInit_seam_throwing(void)
{
    return PyModule_Create(&moduledef);
}
