# Using Seam from Neovim (nvim-dap)

Seam is an ordinary DAP adapter started with `seam dap`, so
[nvim-dap](https://github.com/mfussenegger/nvim-dap) needs only an adapter entry and one
or more configurations. Put this in your Neovim configuration:

```lua
local dap = require("dap")

dap.adapters.seam = {
  type = "executable",
  command = "seam",            -- or the full path, e.g. vim.fn.expand("~/.venvs/seam/bin/seam")
  args = { "dap" },
}

local seam_launch = {
  {
    type = "seam",
    request = "launch",
    name = "Seam: current file",
    program = "${file}",
    python = "python3",        -- any CPython 3.12+; use your virtualenv's interpreter
    cwd = "${workspaceFolder}",
  },
  {
    type = "seam",
    request = "attach",
    name = "Seam: attach to pid",
    pid = function()
      return tonumber(vim.fn.input("pid: "))
    end,
  },
}

-- Offer the same configurations from Python and native buffers, so a session can be
-- started from whichever side of the boundary you are looking at.
for _, filetype in ipairs({ "python", "c", "cpp", "rust", "cython" }) do
  dap.configurations[filetype] = seam_launch
end
```

Breakpoints set with `:lua require("dap").toggle_breakpoint()` in `.py`, `.c`, `.cpp`,
`.rs` and `.pyx` buffers all go to the same session. Seam routes them by file extension:
`.py`/`.pyw` are Python breakpoints, everything else is a native breakpoint.

Launch options are the same as in the VS Code extension; the README lists them. Two are
worth knowing here:

- `console = "integratedTerminal"` runs the program in a Neovim terminal split, where it
  can read input. Without it the program's output goes to the nvim-dap REPL and its
  standard input is empty.
- Exception stops: nvim-dap enables the adapter's default (uncaught Python exceptions).
  `:lua require("dap").set_exception_breakpoints({ "uncaught", "raised" })` chooses others
  (`cpp_throw` and `rust_panic` are the native ones).

CI runs this exact configuration: `tests/editors/nvim_check.lua` reads the Lua block above
out of this file, loads it into a headless Neovim with nvim-dap, and debugs
`examples/pyo3-demo` with it (breakpoint, step into Rust, step out, run to the end, and
once more in a terminal). If something misbehaves for you, set the environment variable
`SEAM_LOG=/tmp/seam.log` before starting Neovim and attach the log to a bug report.
