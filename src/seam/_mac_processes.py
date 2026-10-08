"""macOS process metadata for the existing attach picker and adapter.

The libproc layout and flags follow Apple's bsd/sys/proc_info.h. Arguments come
from KERN_PROCARGS2 so spaces and quotes are preserved rather than parsed from ps.
This module also runs under the Python shipped with Xcode command-line tools.
"""
import ctypes
import json
import os
import re
import struct
import subprocess


class BsdInfo(ctypes.Structure):
    _fields_ = ([(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid",
        "svuid", "svgid", "reserved")]
        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        + [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
        + [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64),
           ("start_usec", ctypes.c_uint64)])


def process_cwd(pid):
    try:
        done = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(int(pid)), "-d", "cwd",
                               "-F0n"], capture_output=True, timeout=3)
        for field in done.stdout.split(b"\0"):
            field = field.lstrip(b"\n")
            if field.startswith(b"n/"):
                return os.fsdecode(field[1:])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def process_executable(pid):
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    path = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(int(pid), path, len(path)) > 0:
        return os.fsdecode(path.value)
    return ""


def list_python_processes():
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libc = ctypes.CDLL(None, use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                 ctypes.c_int]
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                           ctypes.c_void_p, ctypes.c_size_t]
    capacity = lib.proc_listpids(1, 0, None, 0) + 4096
    if capacity <= 0:
        raise OSError(ctypes.get_errno(), "cannot list macOS processes")
    pids = (ctypes.c_int * (capacity // 4))()
    count = lib.proc_listpids(1, 0, pids, ctypes.sizeof(pids)) // 4
    python_name = re.compile(r"^python(\d+(\.\d+)?)?[dt]?$", re.IGNORECASE)
    found = []
    for pid in pids[:count]:
        if pid <= 0 or pid == os.getpid():
            continue
        info = BsdInfo()
        if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            continue
        if info.uid != os.getuid() or info.status == 5:
            continue
        # First filter by the executable's short name; fetching argv is costlier.
        if not python_name.match(os.fsdecode(info.comm)):
            continue
        data = ctypes.create_string_buffer(1 << 20)
        size = ctypes.c_size_t(len(data))
        mib = (ctypes.c_int * 3)(1, 49, pid)
        if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0) != 0:
            continue  # the process ended or its arguments cannot be read
        raw = data.raw[:size.value]
        if len(raw) < 5:
            continue
        argc = struct.unpack_from("=i", raw)[0]
        executable, _, rest = raw[4:].partition(b"\0")
        argv = [os.fsdecode(a) for a in rest.lstrip(b"\0").split(b"\0")[:argc]]
        if argc <= 0 or len(argv) != argc:
            continue
        found.append({"pid": pid, "argv": argv, "exe": os.fsdecode(executable),
                      "cwd": process_cwd(pid),
                      "startTime": info.start_sec * 1000000 + info.start_usec,
                      "tracer": 1 if info.flags & 2 else 0})
    return found


if __name__ == "__main__":
    print(json.dumps(list_python_processes()))
