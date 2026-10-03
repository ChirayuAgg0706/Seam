# Seam

A mixed-mode debugger for Python programs with native (C, C++, Rust) extensions, on
Linux x86-64. One debugger, one call stack, breakpoints on both sides of the boundary.

**Work in progress.** See [STATUS.md](STATUS.md) for what works today and
[docs/decisions.md](docs/decisions.md) for why it is built the way it is.

## Running the tests

```bash
scripts/test.sh -q
```

Requires LLDB 18+, gcc, CPython 3.12 headers and [uv](https://docs.astral.sh/uv/).
