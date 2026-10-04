// The running Python processes of this user, for the attach picker. Read straight from
// /proc: the extension only ever runs on Linux (on the WSL side of a Windows machine).
const fs = require("fs");
const path = require("path");

// python, python3, python3.13, python3.13t (free-threaded), python3.14d (debug build).
const PYTHON_NAME = /^python(\d+(\.\d+)?)?[dt]?$/;

function isPython(file) {
  return Boolean(file) && PYTHON_NAME.test(path.basename(file));
}

function link(file) {
  try {
    // The kernel appends this when the binary was replaced (a package upgrade).
    return fs.readlinkSync(file).replace(/ \(deleted\)$/, "");
  } catch (err) {
    return "";
  }
}

function describe(procRoot, pid, uid) {
  const dir = path.join(procRoot, String(pid));
  if (fs.statSync(dir).uid !== uid) {
    return undefined;
  }
  const argv = fs.readFileSync(path.join(dir, "cmdline"), "utf8").split("\0");
  if (argv[argv.length - 1] === "") {
    argv.pop();
  }
  const exe = link(path.join(dir, "exe"));
  // Kernel threads and zombies have no command line.
  if (!argv.length || !(isPython(exe) || isPython(argv[0]))) {
    return undefined;
  }
  // The fields after "pid (name)"; the name itself may contain spaces and brackets.
  const stat = fs.readFileSync(path.join(dir, "stat"), "utf8");
  const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
  const tracer = /^TracerPid:\s*(\d+)/m.exec(fs.readFileSync(path.join(dir, "status"), "utf8"));
  return {
    pid,
    argv,
    exe: exe || argv[0],
    cwd: link(path.join(dir, "cwd")),
    startTime: Number(fields[19]) || 0,   // in clock ticks since boot; only compared
    tracer: tracer ? Number(tracer[1]) : 0,
  };
}

// Options: procRoot and uid (for tests), and ignore(process) to leave some out.
function listPythonProcesses(options = {}) {
  const procRoot = options.procRoot || "/proc";
  const uid = options.uid !== undefined ? options.uid : process.getuid();
  const found = [];
  for (const name of fs.readdirSync(procRoot)) {
    if (!/^\d+$/.test(name) || Number(name) === process.pid) {
      continue;
    }
    let entry;
    try {
      entry = describe(procRoot, Number(name), uid);
    } catch (err) {
      continue;   // the process ended while it was being read
    }
    if (entry && !(options.ignore && options.ignore(entry))) {
      found.push(entry);
    }
  }
  // Newest first: the program just started is the likeliest one to be wanted.
  return found.sort((a, b) => b.startTime - a.startTime || b.pid - a.pid);
}

function quote(argument) {
  return /^[\w@%+=:,./-]+$/.test(argument) ? argument : `'${argument.replace(/'/g, "'\\''")}'`;
}

// What the quick pick shows for one process: what it runs first, since the interpreter's
// name says little; then the pid; the interpreter and working directory underneath.
function pickItem(entry) {
  const program = entry.argv.slice(1).map(quote).join(" ");
  const interpreter = path.basename(entry.exe);
  return {
    label: program || `${interpreter} (no arguments)`,
    description: `pid ${entry.pid}` + (entry.tracer ? ", already being debugged" : ""),
    detail: entry.cwd ? `${entry.exe}  in ${entry.cwd}` : entry.exe,
    pid: entry.pid,
  };
}

module.exports = { isPython, listPythonProcesses, pickItem };
