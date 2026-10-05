# cython: language_level=3
"""An exception raised inside a Cython module (tests/test_cython_exceptions.py).

Cython adds a frame of its own to the traceback, named after this file as it was called
when the module was built: a relative path that exists nowhere at run time."""


def fail(value):
    raise ValueError("bad value %r" % (value,))  # fail-raise
