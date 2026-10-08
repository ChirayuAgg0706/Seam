# Seam in Neovim

Debug Python and native code in one nvim-dap session.
Use CPython 3.12, 3.13 or 3.14 and LLDB with Python scripting support.
On Linux, LLDB 19 or 20 avoids LLDB 18's threaded-child-process failure.
On macOS, use Apple's command-line tools and an ARM64 interpreter.

## Install

On Ubuntu 24.04:

```bash
sudo apt-get update
sudo apt-get install -y lldb-19 python3-venv
python3 -m venv ~/.venvs/seam
~/.venvs/seam/bin/pip install --only-binary=:all: seam-debugger==0.1.1
~/.venvs/seam/bin/seam doctor --python python3
```

On macOS, run `xcode-select --install`, then create the virtual environment with
an ARM64 CPython 3.12+ interpreter and run the pip and doctor commands above.
Skip the `apt-get` commands.

The wheel includes Seam's compiled helper. Install LLDB separately. You can also
install the wheel from the
[GitHub release](https://github.com/ChirayuAgg0706/Seam/releases/tag/v0.1.1).
Add `~/.venvs/seam/bin` to PATH, or use the full executable path in the configuration.
The program can use a different environment from Seam. Read the
[requirements](../README.md#requirements) and [limitations](../README.md#limitations).

## Configure nvim-dap

Install [nvim-dap](https://github.com/mfussenegger/nvim-dap), then add this Lua block
to your Neovim configuration. It starts the adapter with `seam dap` and offers
launch and attach configurations in Python and native buffers.

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

## Debug a program

Set a breakpoint with `:lua require("dap").toggle_breakpoint()`, then run
`:lua require("dap").continue()` and choose the launch configuration. Step Into
enters a native function called by Python. Step Out returns to the Python caller.

Breakpoints in `.py`, `.c`, `.cpp`, `.rs` and `.pyx` buffers share the same session.
Seam treats `.py` and `.pyw` as Python; other file extensions use native breakpoints.

The [README](../README.md#launch-options) lists every launch option. In Neovim:

- `console = "integratedTerminal"` runs the program in a Neovim terminal split, where it
  can read input. Without it the program's output goes to the nvim-dap REPL and its
  standard input is empty.
- nvim-dap enables uncaught Python exception stops by default. Use
  `:lua require("dap").set_exception_breakpoints({ "uncaught", "raised" })`
  to enable raised exceptions too. Other filters are `user_unhandled`, `cpp_throw`
  and `rust_panic`.

## Troubleshooting

Run `seam doctor --python /path/to/python` with your program's interpreter.
To record a debug session, set `SEAM_LOG=/tmp/seam.log` before starting Neovim.
Include the log and its `.lldb` companion in a bug report after checking them for
private paths and values.

CI tests the Lua block above in a real Neovim with nvim-dap. It debugs the PyO3 demo,
steps into Rust and back, and checks output in the REPL and a terminal.
