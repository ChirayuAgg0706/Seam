-- Editor check for Neovim: the configuration in docs/neovim.md, taken from that file
-- verbatim, debugs examples/pyo3-demo through nvim-dap. Run from the repository root:
--
--   nvim --headless -u NONE --cmd "set rtp+=/path/to/nvim-dap" -l tests/editors/nvim_check.lua
--
-- SEAM_BIN names the seam executable (default: `seam` on PATH), SEAM_DEMO_DIR the built
-- demo (default: examples/pyo3-demo).
local TIMEOUT_MS = 90000

local function say(message)
  io.stdout:write("[seam nvim check] " .. message .. "\n")
  io.stdout:flush()
end

local function fail(message)
  say("FAILED: " .. message)
  vim.cmd("cquit 1")
end

local function check(condition, message)
  if not condition then
    fail(message)
  end
  say("ok: " .. message)
end

local function wait_for(what, predicate)
  if not vim.wait(TIMEOUT_MS, predicate, 50) then
    fail("timed out waiting for " .. what)
  end
end

local function line_of(path, marker)
  for number, text in ipairs(vim.fn.readfile(path)) do
    if text:find(marker, 1, true) then
      return number
    end
  end
  fail("marker " .. marker .. " not found in " .. path)
end

local root = vim.fn.getcwd()
local demo = vim.fn.fnamemodify(os.getenv("SEAM_DEMO_DIR") or "examples/pyo3-demo", ":p")
demo = demo:gsub("/$", "")

-- The documented configuration, exactly as a user would paste it.
local document = table.concat(vim.fn.readfile(root .. "/docs/neovim.md"), "\n")
local snippet = document:match("```lua\n(.-)```")
check(snippet ~= nil, "docs/neovim.md contains a Lua configuration")
assert(load(snippet, "=docs/neovim.md"))()
local dap = require("dap")
if os.getenv("SEAM_BIN") then
  dap.adapters.seam.command = os.getenv("SEAM_BIN") -- "or the full path", as documented
end

local stops, exits, output = {}, {}, {}
dap.listeners.after.event_stopped["seam-check"] = function(_, body)
  stops[#stops + 1] = body
end
dap.listeners.after.event_exited["seam-check"] = function(_, body)
  exits[#exits + 1] = body
end
dap.listeners.after.event_output["seam-check"] = function(_, body)
  output[#output + 1] = body.output
end

local function request(command, arguments)
  local done, reply, failure = false, nil, nil
  dap.session():request(command, arguments, function(err, result)
    done, reply, failure = true, result, err
  end)
  wait_for("the answer to " .. command, function()
    return done
  end)
  if failure then
    fail(command .. " failed: " .. vim.inspect(failure))
  end
  return reply
end

local function stack_at_stop(count)
  wait_for("stop number " .. count, function()
    return #stops >= count
  end)
  return request("stackTrace", { threadId = stops[count].threadId }).stackFrames
end

local function shows(path_suffix)
  -- nvim-dap jumps to the stopped frame's file on its own.
  wait_for("Neovim to show " .. path_suffix, function()
    return vim.api.nvim_buf_get_name(0):sub(-#path_suffix) == path_suffix
  end)
end

-- Session 1: breakpoint, step into Rust, step out, run to the end.
vim.cmd("cd " .. vim.fn.fnameescape(demo))
vim.cmd("edit demo.py")
local py_line = line_of(demo .. "/demo.py", "# step in here")
vim.api.nvim_win_set_cursor(0, { py_line, 0 })
dap.toggle_breakpoint()

local configuration = vim.deepcopy(dap.configurations.python[1])
check(configuration.name == "Seam: current file", "the documented launch configuration exists")
dap.run(configuration)

local frames = stack_at_stop(1)
check(frames[1].name == "report" and frames[1].line == py_line,
  "stopped at the Python breakpoint (" .. frames[1].name .. ":" .. frames[1].line .. ")")

dap.step_into()
frames = stack_at_stop(2)
check(frames[1].source.path == demo .. "/src/lib.rs" and frames[1].name:find("sum_squares", 1, true),
  "step into landed in Rust: " .. frames[1].name)
local names = {}
for _, frame in ipairs(frames) do
  names[#names + 1] = frame.name
end
check(vim.tbl_contains(names, "report") and names[#names] == "<module>",
  "merged stack: " .. table.concat(names, " < "))
shows("/src/lib.rs")
say("ok: Neovim opened src/lib.rs at the stop")

dap.step_out()
frames = stack_at_stop(3)
check(frames[1].name == "report" and frames[1].line == py_line, "step out returned to Python")
shows("/demo.py")

dap.clear_breakpoints()
dap.continue()
wait_for("the program to exit", function()
  return #exits >= 1
end)
check(exits[1].exitCode == 0, "the program exited normally")
check(table.concat(output):find("squares(5) = 30", 1, true) ~= nil,
  "the program's output reached nvim-dap")
wait_for("the session to end", function()
  return dap.session() == nil
end)

-- Session 2: the program in a Neovim terminal (nvim-dap answers runInTerminal).
configuration = vim.deepcopy(dap.configurations.python[1])
configuration.console = "integratedTerminal"
vim.cmd("edit " .. vim.fn.fnameescape(demo .. "/demo.py"))
dap.run(configuration)
wait_for("the program to exit in the terminal", function()
  return #exits >= 2
end)
check(exits[2].exitCode == 0, "the program ran in a Neovim terminal")
local terminals = 0
for _, buffer in ipairs(vim.api.nvim_list_bufs()) do
  if vim.bo[buffer].buftype == "terminal" then
    terminals = terminals + 1
  end
end
check(terminals >= 1, "nvim-dap opened a terminal buffer for it")

say("passed")
vim.cmd("qall!")
