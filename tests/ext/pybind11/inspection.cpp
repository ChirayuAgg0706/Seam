// Inspection regressions: conditions, constants, stack slots and unavailable locals.
#include <pybind11/pybind11.h>
#include <string>
#include <stdexcept>

namespace py = pybind11;
static const int LIMIT = 17;
static int calls = 0;

struct Mixer {};
struct TrialError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

static void fail(const std::string &reason) {
    if (reason == "integer") {
        throw 42;
    }
    throw TrialError(reason);
}

__attribute__((noinline)) static int inspect(int input) {
    int unused = input * 100;
    std::string name = "wrong";
    calls += input;  // inspection-stop
    return calls + LIMIT;  // inspection-return
}

// Identical stack layouts deliberately reuse the watched local's address.
__attribute__((noinline)) static int watched(int input) {
    volatile int slot = input;
    slot = input + 1;  // watch-local
    slot = input + 2;
    return slot;
}

__attribute__((noinline)) static int reuse(int input) {
    volatile int slot = input;
    slot = input + 3;
    slot = input + 4;
    return slot;
}

PYBIND11_MODULE(seam_inspection, m) {
    py::class_<Mixer>(m, "Mixer").def(py::init<>());
    m.def("inspect", &inspect);
    m.def("watched", &watched);
    m.def("reuse", &reuse);
    m.def("fail", &fail);
}
