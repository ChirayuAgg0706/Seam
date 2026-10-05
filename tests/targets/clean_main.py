"""Uses a module it never imported. Only a debugger that leaves names behind in `__main__`
lets the first line work."""
try:
    print("leaked", sys.version_info[:2])  # noqa: F821
except NameError:
    print("clean", sorted(name for name in globals() if not name.startswith("__")))
