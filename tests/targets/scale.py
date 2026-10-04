"""Target of the scale scenarios: python scale.py MODULE FUNCTION [OTHER_MODULE ...]

MODULE.FUNCTION(a, b) and MODULE.call_back(fn, arg) are called on marked lines; the other
modules are only imported. With `seamtest add` this is the baseline; with
`seam_large f07500` the same lines call into a module of 15,000 functions.
"""
import importlib
import sys


def cb(value):
    return value + 1  # cb-body


def helper(value):
    return value * 2  # helper-body


def main(name, function, others):
    for other in others:
        importlib.import_module(other)
    module = importlib.import_module(name)  # import-module
    call = getattr(module, function)
    total = 0  # after-import
    total += call(3, 4)  # first-call
    total += call(5, 6)  # second-call
    total += call(7, 8)  # third-call
    total += module.call_back(cb, 5)  # callback-call
    total += helper(1)  # python-call
    total += call(9, 10)  # last-call
    print("total", total)  # print-total


main(sys.argv[1], sys.argv[2], sys.argv[3:])  # module-main
