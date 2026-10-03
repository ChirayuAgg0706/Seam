"""Target for the binding-layer scenarios: python binding.py <module name>."""
import importlib
import sys

mod = importlib.import_module(sys.argv[1])


def cb(v):
    return v + 1  # cb-body


def main():
    total = mod.add(20, 22)  # bind-add
    print("sum", total)  # bind-after
    back = mod.call_back(cb, 5)  # bind-callback
    print("back", back)  # bind-done


main()  # module-main
