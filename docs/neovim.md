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

Launch options are the same as in the VS Code extension: `program` or `module`, `args`,
`python`, `pythonArgs`, `cwd`, `env`, `stopOnEntry`, `debugInfoLookup`, `frameworkPaths`.

This configuration has not been exercised inside Neovim by the Seam test suite; the suite
drives the same adapter over the same protocol with a scripted client. If something
misbehaves, set the environment variable `SEAM_LOG=/tmp/seam.log` before starting Neovim
and attach the log to a bug report.
