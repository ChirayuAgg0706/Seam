# PyO3 demo

A minimal Rust extension and a Python script that calls it, for trying Seam.

```bash
cd examples/pyo3-demo
cargo build                                   # debug build: has debug info
cp target/debug/libseam_demo.so seam_demo.so  # the name Python imports
```

Then debug `demo.py` with Seam (see the main README). Put a breakpoint on the
`result = ...` line and use Step Into: the debugger stops inside `sum_squares` in
`src/lib.rs`, with `report` and `<module>` from `demo.py` below it in the same stack.
