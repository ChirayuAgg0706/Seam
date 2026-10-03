// nanobind extension used by the binding-layer scenarios.
#include <nanobind/nanobind.h>

namespace nb = nanobind;

static int add(int a, int b) {
    int sum = a + b;  // add-body
    return sum;
}

static nb::object call_back(nb::callable fn, int x) {
    nb::object res = fn(x);  // callback-call
    return res;  // callback-after
}

NB_MODULE(seam_nanobind, m) {
    m.def("add", &add);
    m.def("call_back", &call_back);
}
