"""Source paths: what the debug info calls a file, and what this machine and the editor do."""
import json
import os

import lldb

from .common import DapError, SYSTEM_LIB_PREFIXES

SOURCE_MAP_SHAPE = (
    'sourceMap must be a list of ["path in the debug info", "path on this machine"] pairs, '
    'or an object mapping the one to the other')
UNNAMED_SYMBOL = "___lldb_unnamed_symbol"  # LLDB's name for a function it found no name for


def _prefix(path):
    """A path without a leading "./" or trailing slashes; "" stands for "."."""
    while path.startswith("./"):
        path = path[2:].lstrip("/")
    if path == ".":
        return ""
    return path.rstrip("/") or ("/" if path.startswith("/") else "")


def _below(path, prefix):
    """What follows `prefix` in `path`, if `path` is that place or lies under it; else None.

    Whole path components only: "/build" covers "/build/a.c" but not "/builds/a.c". The
    prefix "" (written "." in the option) covers every relative path.
    """
    if prefix == "":
        return None if path.startswith("/") else path
    if path == prefix:
        return ""
    head = prefix.rstrip("/") + "/"
    return path[len(head):] if path.startswith(head) else None


class SourcesMixin:
    def _set_source_map(self, value):
        """Read the `sourceMap` option: (prefix in the debug info, prefix on this machine).

        Both forms in use are accepted: lldb-dap's list of two-element lists, and an
        object. An entry whose local side does not exist is kept (the directory may be
        mounted later) but pointed out, since it is most likely a typing mistake.
        """
        pairs = list(value.items()) if isinstance(value, dict) else value or []
        entries = []
        try:
            for prefix, local in pairs:
                if not isinstance(prefix, str) or not isinstance(local, str) or not local:
                    raise ValueError
                local = os.path.join(self.cwd, os.path.expanduser(local))
                entries.append((_prefix(prefix), local.rstrip("/") or "/"))
        except (TypeError, ValueError):
            raise DapError(SOURCE_MAP_SHAPE) from None
        self.source_map = entries
        for prefix, local in entries:
            if not os.path.exists(local):
                self.event("output", {"category": "console", "output":
                           "Seam: sourceMap maps %s to %s, which does not exist on this "
                           "machine.\n" % (prefix or ".", local)})

    def _mapped(self, name):
        """Where `sourceMap` puts a file named in the debug info, one path per entry."""
        out = []
        for prefix, local in self.source_map if name else ():
            rest = _below(name, prefix)
            if rest is not None:
                out.append(os.path.join(local, rest) if rest else local)
        return out

    def _is_glue_path(self, name):
        """True if a source file named in the debug info belongs to a binding layer.

        The fragments are tried on the name as the debug info has it and on each place
        `sourceMap` puts it: an extension built elsewhere is judged like one built here,
        and `frameworkPaths` can be written in either spelling.
        """
        known = self.glue_paths.get(name)
        if known is None:
            plain = _prefix(name)
            spellings = [plain if plain.startswith("/") else "/" + plain, *self._mapped(plain)]
            known = any(part in spelling for spelling in spellings
                        for part in self.framework_paths)
            self.glue_paths[name] = known
        return known

    def _local_source(self, name):
        """The file on this machine that the debug info calls `name`, or None.

        `sourceMap` is tried first, entry by entry, and the first file that exists wins.
        Otherwise the name itself is used; a relative one is looked for from the program's
        working directory, which is where gdb and lldb look as well.
        """
        found = self.local_sources.get(name)
        if found is None:
            plain = _prefix(name)
            candidates = self._mapped(plain)
            candidates.append(plain if plain.startswith("/") else os.path.join(self.cwd, plain))
            found = next((c for c in candidates if os.path.isfile(c)), "")
            self.local_sources[name] = found
        return self._editor_path(found) if found else None

    def _debug_spellings(self, path):
        """Every name the debug info may have for the source file the editor calls `path`.

        A native breakpoint is set under each of them, because nothing says which one a
        module that loads later was built with: the name `sourceMap` maps here, the path
        as the editor gave it (a build made in place), and that path with symbolic links
        resolved (compilers that record the physical directory, as rustc does). Relative
        names in the debug info need no spelling of their own: LLDB matches them against
        the end of the full path.
        """
        real = os.path.realpath(path)
        out = []
        for prefix, local in self.source_map:
            rest = _below(real, os.path.realpath(local)) if prefix else None
            if rest is not None:
                out.append(os.path.join(prefix, rest) if rest else prefix)
        return list(dict.fromkeys(out + [path, real]))

    def _note_client_path(self, path):
        """Remember how the client spells a path that leads through a symbolic link.

        The interpreter and the compilers often record the resolved path (`sys.path[0]`
        is the real directory of the script; rustc records the physical working
        directory), while the editor has the files open under the path the project was
        opened by. Showing a frame under the resolved name would open a second copy of
        the file, without its breakpoints. So for each path the client names, the pair
        (real directory, the client's name for it) is kept, climbing as long as the
        parent directories still correspond: one breakpoint in a linked project is
        enough to know the whole tree.
        """
        if not path or not os.path.isabs(path):
            return
        real = os.path.realpath(path)
        shown = os.path.normpath(path)
        if os.path.realpath(shown) != real:
            shown = path  # ".." after a link: the tidied path is another file
        if shown == real:
            return
        while True:
            up_real, up_shown = os.path.dirname(real), os.path.dirname(shown)
            if (os.path.basename(real) != os.path.basename(shown) or up_real == up_shown
                    or os.path.realpath(up_shown) != up_real):
                break
            real, shown = up_real, up_shown
        if self.aliases.get(real) != shown:
            self.aliases[real] = shown
            self.editor_paths.clear()

    def _editor_path(self, path):
        """The spelling of a file's path to give the editor.

        The client's own spelling if it has shown one for the file or a directory above
        it (see _note_client_path); otherwise the path as the program or the debug info
        has it, tidied of ".." and doubled slashes.
        """
        shown = self.editor_paths.get(path)
        if shown is None:
            real = os.path.realpath(path)
            shown = os.path.normpath(path)
            if os.path.realpath(shown) != real:
                shown = real
            # The file may itself be a link out of a directory the client knows: then
            # it is its place in that directory that the client has a name for.
            beside = os.path.join(os.path.realpath(os.path.dirname(shown)),
                                  os.path.basename(shown))
            for known in (real, beside) if self.aliases else ():
                head, tail = known, ""
                while head != os.path.dirname(head) and head not in self.aliases:
                    head, name = os.path.split(head)
                    tail = os.sep + name + tail
                if head in self.aliases:
                    shown = self.aliases[head] + tail
                    break
            self.editor_paths[path] = shown
        return shown

    def _native_record(self, frame, tid, index, sp):
        """A native frame's entry in the merged stack: its name, and the source it has.

        A frame whose source cannot be opened here (no debug info, or debug info naming
        a file that is not on this machine) gets no path. It is named with its shared
        object instead, so that the call stack still says which library it is in.
        """
        cls = self._classify_frame(frame)
        named, line = self._native_source_location(frame)
        path = self._local_source(named) if named else None
        record = {"kind": "native", "tid": tid, "index": index, "cls": cls,
                  "name": frame.GetFunctionName() or "%#x" % frame.GetPC(),
                  "path": path, "line": line if named else 0,
                  "at": (frame.GetPC(), sp)}
        if path is None:
            record["name"] = self._library_name(frame)
            if named:
                record["missing"] = named
                if cls == "user":
                    self._report_missing_source(named, frame.GetModule())
        return record

    @staticmethod
    def _library_name(frame):
        """`library!function` for a frame without source; `library+0x...` without a name."""
        library = frame.GetModule().GetFileSpec().GetFilename()
        name = frame.GetFunctionName() or ""
        if name.startswith(UNNAMED_SYMBOL):
            name = ""
        if not library:
            return name or "%#x" % frame.GetPC()
        if name:
            return "%s!%s" % (library, name)
        return "%s+%#x" % (library, frame.GetPCAddress().GetFileAddress())

    @staticmethod
    def _mapping_between(name, local):
        """The `sourceMap` entry that turns `name` into `local`: what is left of each
        once the trailing components they share are taken off."""
        ours, theirs = local.split("/"), name.split("/")
        while ours and theirs and ours[-1] == theirs[-1]:
            ours.pop()
            theirs.pop()
        prefix = "/".join(theirs) or ("/" if name.startswith("/") else ".")
        return json.dumps({prefix: "/".join(ours) or "/"})

    def _guess_source(self, name):
        """A file near the program's working directory that ends like `name`, or None."""
        parts = [part for part in name.split("/") if part not in ("", ".")]
        roots = [self.cwd, os.path.dirname(self.cwd), os.path.dirname(os.path.dirname(self.cwd))]
        for start in range(len(parts)):  # the longest matching tail first
            for root in dict.fromkeys(roots):
                candidate = os.path.join(root, *parts[start:])
                if os.path.isfile(candidate):
                    return candidate
        return None

    def _report_missing_source(self, name, module):
        """Say, once per session, why the user's native code is shown without source."""
        if self.missing_source_reported:
            return
        self.missing_source_reported = True
        library = module.GetFileSpec().GetFilename() or "the program"
        mapped = self._mapped(_prefix(name))
        if mapped:
            text = ("sourceMap puts %s (the name in the debug info of %s) at %s, which does "
                    "not exist. Check that entry of the launch configuration."
                    % (name, library, mapped[0]))
        else:
            text = ("the debug info of %s names its source as %s, which is not on this "
                    "machine: that code is shown without source, and breakpoints set in "
                    "your copy of the file do not bind. " % (library, name))
            guess = self._guess_source(name)
            if guess:
                text += ("%s looks like the same file. If it is, add this to the launch "
                         'configuration: "sourceMap": %s'
                         % (guess, self._mapping_between(name, guess)))
            else:
                prefix = os.path.dirname(name) if name.startswith("/") else "."
                text += ("If you have the sources, add this to the launch configuration, "
                         'with the directory they are in: "sourceMap": %s'
                         % json.dumps({prefix or "/": "/where/the/sources/are"}))
        self.event("output", {"category": "console", "output": "Seam: " + text + "\n"})

    def _unbound_reason(self, path, modules=None):
        """Why none of a file's native breakpoints has code, if a path is the likely reason.

        A loaded library was built from a file of the same name, under a path that no
        spelling of the breakpoint covers: most likely this file, built somewhere else.
        Returns the explanation with the `sourceMap` entry that would fit, and puts it in
        the debug console the first time. Only the files that were compiled are looked
        at, not the headers they include.
        """
        group = self.native_bps.get(path) or []
        if not group or any(self._native_bp_answer(bp)["verified"] for bp in group):
            return None
        spellings = self._debug_spellings(path)
        real = os.path.realpath(path)
        wanted = lldb.SBFileSpec(os.path.basename(path), False)
        for module in modules if modules is not None else self.target.module_iter():
            library = module.GetFileSpec().fullpath or ""
            if (not library.startswith("/") or library.startswith(SYSTEM_LIB_PREFIXES)
                    or library in (self.interp_module, self.helper_module)):
                continue
            units = module.FindCompileUnits(wanted)
            for i in range(units.GetSize()):
                named = units.GetContextAtIndex(i).GetCompileUnit().GetFileSpec().fullpath
                if not named or named in spellings:
                    continue
                if not named.startswith("/") and real.endswith("/" + _prefix(named)):
                    continue  # a relative name LLDB matches against the end of the path
                local = self._local_source(named)
                if local and os.path.realpath(local) == real:
                    continue  # this very file: it is the line that has no code
                text = ("the breakpoint in %s has no code to stop at: %s was built from %s. "
                        "If that is the same file, add this to the launch configuration: "
                        '"sourceMap": %s' % (path, os.path.basename(library), named,
                                             self._mapping_between(named, path)))
                if path not in self.unbound_explained:
                    self.unbound_explained.add(path)
                    self.event("output", {"category": "console",
                                          "output": "Seam: " + text + "\n"})
                return text
        return None

    def _on_modules_loaded(self, ev):
        """A library was loaded: a breakpoint still without code may have its reason now."""
        modules = [lldb.SBTarget.GetModuleAtIndexFromEvent(i, ev)
                   for i in range(lldb.SBTarget.GetNumModulesFromEvent(ev))]
        self._notice_no_debug_info(modules)
        for path, group in self.native_bps.items():
            reason = self._unbound_reason(path, modules)
            for bp in group if reason else ():
                if self.native_bp_group.get(bp.GetID(), [bp])[0].GetID() == bp.GetID():
                    self.event("breakpoint", {"reason": "changed", "breakpoint": {
                        "id": bp.GetID(), "verified": False, "message": reason,
                        "line": self.native_bp_lines.get(bp.GetID(), 0)}})

    def _notice_no_debug_info(self, modules):
        """Explain why stepping cannot enter a user library built without DWARF."""
        changed = False
        for module in modules:
            path = module.GetFileSpec().fullpath or ""
            name = os.path.basename(path)
            if (not module.IsValid() or not path.startswith("/")
                    or path.startswith(SYSTEM_LIB_PREFIXES)
                    or path in (self.interp_module, self.helper_module)
                    or ".so" not in name or name.startswith("_seam_trap.")
                    or any(part in path.split("/") for part in
                           ("site-packages", "dist-packages", "lib-dynload"))
                    or name in self.no_debug_info):
                continue
            # Counting compile units parses DWARF on first use. Do not make a normal
            # extension import pay for that: an embedded debug-info section suffices.
            # Without one, LLDB still gets a chance to find a separate debug file.
            if (module.FindSection(".debug_info").IsValid()
                    or module.FindSection(".zdebug_info").IsValid()
                    or module.GetNumCompileUnits()):
                continue
            self.no_debug_info.append(name)
            changed = True
            self.event("output", {"category": "console", "output":
                       "Seam: %s has no debug info: its functions cannot be stepped into "
                       "and breakpoints in its source will not bind; build it with -g.\n"
                       % name})
        if changed:
            self._refresh_native_bp_status()

    def req_source(self, args):
        """The editor asks for the text of a source it was given no path for.

        That is a frame whose debug info names a file that is not on this machine. Seam
        has no text to offer; the answer says what is missing.
        """
        source = args.get("source") or {}
        raise DapError(source.get("origin") or "Seam has no source text for %s"
                       % (source.get("name") or "this frame"))
