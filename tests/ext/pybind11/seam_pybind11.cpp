// pybind11 extension used by the binding-layer scenarios.
#include <pybind11/pybind11.h>

#include <stdexcept>
#include <string>

namespace py = pybind11;

static int add(int a, int b) {
    int sum = a + b;  // add-body
    return sum;
}

static py::object call_back(py::function fn, int x) {
    py::object res = fn(x);  // callback-call
    return res;  // callback-after
}

static void fail(const std::string &reason) {
    throw std::runtime_error(reason);  // throw-here
}

PYBIND11_MODULE(seam_pybind11, m) {
    m.def("add", &add);
    m.def("call_back", &call_back);
    m.def("fail", &fail);
}
