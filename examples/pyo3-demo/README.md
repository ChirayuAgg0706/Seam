# Debug Python and Rust with Seam

This example calls Rust's `sum_squares` from Python. Seam lets you step into
the Rust function, inspect its locals, and return to the Python caller.

## Build

Use Linux x86-64, including WSL, with a Rust toolchain and a supported CPython
interpreter. Install [Seam and LLDB](../../README.md#install) first.
From the repository root:

```bash
cd examples/pyo3-demo
cargo build
cp target/debug/libseam_demo.so seam_demo.so
python3 demo.py
```

The debug build includes source information. Renaming the library to `seam_demo.so`
lets Python import it. Running the script prints `squares(5) = 30`.

## Debug in VS Code

1. Open `examples/pyo3-demo` in a Linux or WSL window.
2. Open `demo.py` and set a breakpoint on `result = ...`.
3. Press F5 and choose **Seam: Python + native** if prompted.
4. Use Step Into. VS Code opens `src/lib.rs` inside `sum_squares`.
5. Inspect the Rust locals. The call stack also shows Python's `report` and `<module>`.
6. Use Step Out to return to the Python call line, then Step Over to complete the
   assignment. The Python local `result` is `30`.
7. Continue to print the result and finish.

For Neovim, use the [nvim-dap configuration](../../docs/neovim.md).
