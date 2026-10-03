# cython: language_level=3
# Cython extension used by the binding-layer scenarios.


def add(int a, int b):
    cdef int total = a + b  # add-body
    return total


def call_back(fn, x):
    res = fn(x)  # callback-call
    return res  # callback-after
