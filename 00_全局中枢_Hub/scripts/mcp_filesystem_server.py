#!/usr/bin/env python3
"""Pure-stdlib Python 3 MCP filesystem server for Claude Desktop in isolated VMs.

Compatible with @modelcontextprotocol/server-filesystem tool names and schemas,
requiring zero Node.js/npm dependencies. Protects frozen archive directories
('冻结存档-20261117') from any write, edit, create, or move operation.
"""

import datetime
import difflib
import fnmatch
import json
import os
import shutil
import stat
import sys


PROTECTED_DIR_NAMES = ("benchmark", "冻结存档-20261117", "冻结件", "99_历史归档_Archive", "00_全局中枢_Hub")
PROTECTED_PREFIXES = ("冻结存档-",)
APPEND_ONLY_SUFFIX = os.path.join("_control", "02_DECISION_LEDGER.md")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"


def _err_log(msg):
    sys.stderr.write("[mcp-filesystem] {}\n".format(msg))
    sys.stderr.flush()


class PathGuard:
    """Validates filesystem paths against allowed roots and frozen-archive rules."""

    def __init__(self, allowed_dirs):
        self.raw_roots = []
        self.real_roots = []
        for entry in allowed_dirs:
            expanded = os.path.expanduser(entry)
            abs_p = os.path.abspath(expanded)
            real_p = os.path.realpath(abs_p)
            self.raw_roots.append(abs_p)
            self.real_roots.append(real_p)
        if not self.real_roots:
            cwd = os.getcwd()
            self.raw_roots = [os.path.abspath(cwd)]
            self.real_roots = [os.path.realpath(cwd)]

    def _is_within_root(self, candidate, root):
        try:
            return os.path.commonpath([candidate, root]) == root
        except ValueError:
            return False

    def validate(self, requested_path):
        if not isinstance(requested_path, str) or not requested_path.strip():
            raise ValueError("Path must be a non-empty string")
        expanded = os.path.expanduser(requested_path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.raw_roots[0], expanded)
        abs_target = os.path.abspath(expanded)
        real_target = os.path.realpath(abs_target)

        for root in self.real_roots:
            if self._is_within_root(real_target, root):
                return real_target

        raise PermissionError(
            "Access denied - path outside allowed directories: {}".format(
                requested_path
            )
        )

    def _is_protected_name(self, name):
        if name in PROTECTED_DIR_NAMES:
            return True
        return any(name.startswith(pref) for pref in PROTECTED_PREFIXES)

    def ensure_writable(self, resolved_path):
        parts = resolved_path.split(os.sep)
        for part in parts:
            if self._is_protected_name(part):
                raise PermissionError(
                    "Operation denied - protected directory '{}' is strictly read-only for VM: {}".format(
                        part, resolved_path
                    )
                )

    def ensure_no_protected_descendants(self, resolved_dir):
        if not os.path.isdir(resolved_dir):
            return
        for cur_root, dirs, _ in os.walk(resolved_dir, followlinks=False):
            for dname in dirs:
                if self._is_protected_name(dname):
                    raise PermissionError(
                        "Operation denied - source directory contains protected read-only subtree '{}': {}".format(
                            dname, os.path.join(cur_root, dname)
                        )
                    )

    def ensure_append_only_ledger(self, resolved_path, new_text):
        if not resolved_path.endswith(APPEND_ONLY_SUFFIX) or not os.path.isfile(resolved_path):
            return
        with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
            old_text = f.read()
        import re as _re
        old_ids = set(_re.findall(r"\bD-\d+\b", old_text))
        new_ids = set(_re.findall(r"\bD-\d+\b", new_text))
        missing = sorted(old_ids - new_ids)
        if missing:
            raise PermissionError(
                "Operation denied - 02_DECISION_LEDGER.md is append-only; missing locked decision IDs: {}".format(
                    ", ".join(missing)
                )
            )


def _heal_from_virtiofs_sidecar(resolved_path):
    """If the host wrote a 0644 content-addressed sidecar in .virtiofs_cache/<name>.<sha256>.cache,
    verify whether the guest's local VFS view of resolved_path is stale (e.g. contains NUL padding or old hash)
    and self-heal the guest's UBC cache by writing through the guest VFS fd.
    """
    import hashlib as _hashlib
    parent = os.path.dirname(resolved_path)
    base = os.path.basename(resolved_path)
    cache_dir = os.path.join(parent, ".virtiofs_cache")
    if not os.path.isdir(cache_dir):
        return
    try:
        prefix = base + "."
        cands = [
            os.path.join(cache_dir, n)
            for n in os.listdir(cache_dir)
            if n.startswith(prefix) and n.endswith(".cache")
        ]
        if not cands:
            return
        latest = max(cands, key=lambda p: os.stat(p).st_mtime_ns)
        expected_sha = os.path.basename(latest)[len(prefix) : -len(".cache")]
        with open(resolved_path, "rb") as rf:
            cur_bytes = rf.read()
        if b"\x00" not in cur_bytes and _hashlib.sha256(cur_bytes).hexdigest() == expected_sha:
            return
        with open(latest, "rb") as sf:
            fresh_bytes = sf.read()
        if _hashlib.sha256(fresh_bytes).hexdigest() == expected_sha:
            with open(resolved_path, "wb") as wf:
                wf.write(fresh_bytes)
                wf.flush()
                os.fsync(wf.fileno())
    except OSError:
        pass


def _open_nocache_text(resolved_path):
    """Open file with macOS XNU msync(MS_INVALIDATE) + F_NOCACHE (48) + .virtiofs_cache sidecar self-healing."""
    import ctypes as _ctypes
    import ctypes.util as _cutil
    import fcntl as _fcntl
    _heal_from_virtiofs_sidecar(resolved_path)
    F_NOCACHE = getattr(_fcntl, "F_NOCACHE", 48)
    fd = os.open(resolved_path, os.O_RDONLY)
    try:
        _fcntl.fcntl(fd, F_NOCACHE, 1)
    except OSError:
        pass
    try:
        sz = os.fstat(fd).st_size
        if sz > 0:
            libc = _ctypes.CDLL(_cutil.find_library("c"), use_errno=True)
            libc.mmap.argtypes = [_ctypes.c_void_p, _ctypes.c_size_t, _ctypes.c_int, _ctypes.c_int, _ctypes.c_int, _ctypes.c_longlong]
            libc.mmap.restype = _ctypes.c_void_p
            libc.msync.argtypes = [_ctypes.c_void_p, _ctypes.c_size_t, _ctypes.c_int]
            libc.msync.restype = _ctypes.c_int
            libc.munmap.argtypes = [_ctypes.c_void_p, _ctypes.c_size_t]
            libc.munmap.restype = _ctypes.c_int
            addr = libc.mmap(None, sz, 0x01, 0x0001, fd, 0)
            if addr and addr != _ctypes.c_void_p(-1).value:
                libc.msync(_ctypes.c_void_p(addr), sz, 0x0002)
                libc.munmap(_ctypes.c_void_p(addr), sz)
    except Exception:
        pass
    return os.fdopen(fd, "r", encoding="utf-8", errors="replace")


def _read_text_slices(resolved_path, head=None, tail=None):
    if head is not None and tail is not None:
        raise ValueError("Cannot specify both head and tail parameters simultaneously")
    proj = state_project(resolved_path)
    if proj is not None and head is None and tail is None:
        try:
            import state_client
            return state_client.read(proj)["content"]
        except Exception:
            pass
    with _open_nocache_text(resolved_path) as f:
        if head is not None:
            head_n = int(head)
            if head_n < 0:
                raise ValueError("head must be non-negative")
            lines = []
            for idx, line in enumerate(f):
                if idx >= head_n:
                    break
                lines.append(line)
            return "".join(lines)
        if tail is not None:
            tail_n = int(tail)
            if tail_n < 0:
                raise ValueError("tail must be non-negative")
            if tail_n == 0:
                return ""
            all_lines = f.readlines()
            return "".join(all_lines[-tail_n:])
        return f.read()


def _format_size(num_bytes):
    if num_bytes < 1024:
        return "{} B".format(num_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        num_bytes /= 1024.0
        if num_bytes < 1024.0:
            return "{:.1f} {}".format(num_bytes, unit)
    return "{:.1f} PB".format(num_bytes)


def _normalize_line_endings(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _apply_edits(original_text, edits):
    had_crlf = "\r\n" in original_text
    working = _normalize_line_endings(original_text)

    for idx, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError("Edit #{} must be an object".format(idx))
        old_text = edit.get("oldText")
        new_text = edit.get("newText")
        if old_text is None or new_text is None:
            raise ValueError("Edit #{} must contain oldText and newText".format(idx))
        norm_old = _normalize_line_endings(str(old_text))
        norm_new = _normalize_line_endings(str(new_text))

        if norm_old in working:
            working = working.replace(norm_old, norm_new, 1)
            continue

        # Whitespace-trimmed line-by-line fallback matching upstream behavior
        content_lines = working.split("\n")
        old_lines = norm_old.split("\n")
        matched = False
        if old_lines:
            span = len(old_lines)
            for i in range(len(content_lines) - span + 1):
                window = content_lines[i : i + span]
                if all(
                    window[j].strip() == old_lines[j].strip() for j in range(span)
                ):
                    indent = ""
                    if window[0]:
                        indent = window[0][: len(window[0]) - len(window[0].lstrip())]
                    replacement_lines = []
                    for k, rline in enumerate(norm_new.split("\n")):
                        if k == 0 and indent and rline and not rline[0].isspace():
                            replacement_lines.append(indent + rline)
                        else:
                            replacement_lines.append(rline)
                    content_lines[i : i + span] = replacement_lines
                    working = "\n".join(content_lines)
                    matched = True
                    break

        if not matched:
            raise ValueError(
                "Could not find exact match for edit:\n{}".format(old_text)
            )

    diff_lines = list(
        difflib.unified_diff(
            _normalize_line_endings(original_text).splitlines(True),
            working.splitlines(True),
            fromfile="original",
            tofile="modified",
        )
    )
    diff_text = "".join(diff_lines) if diff_lines else "(no changes)"
    final_text = working.replace("\n", "\r\n") if had_crlf else working
    return final_text, diff_text


def _matches_exclude(rel_path, name, exclude_patterns):
    if not exclude_patterns:
        return False
    norm_rel = rel_path.replace(os.sep, "/")
    for pat in exclude_patterns:
        if not pat:
            continue
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(norm_rel, pat):
            return True
        if "/" in pat and fnmatch.fnmatch(norm_rel, pat.lstrip("/")):
            return True
    return False


def _build_tree(dir_path, root_path, exclude_patterns, max_entries=2000):
    if any(n in ('benchmark','冻结件') or n.startswith('冻结存档-') for n in os.path.realpath(dir_path).split(os.sep)):
        return []
    entries = []
    try:
        items = sorted(os.listdir(dir_path))
    except OSError:
        return entries

    for name in items:
        full = os.path.join(dir_path, name)
        rel = os.path.relpath(full, root_path)
        if _matches_exclude(rel, name, exclude_patterns):
            continue
        if os.path.isdir(full):
            node = {
                "name": name,
                "type": "directory",
                "children": [],
            }
            if not os.path.islink(full):
                node["children"] = _build_tree(
                    full, root_path, exclude_patterns, max_entries
                )
            entries.append(node)
        else:
            entries.append({"name": name, "type": "file"})
        if len(entries) >= max_entries:
            break
    return entries


def state_project(path):
    from pathlib import Path
    candidate = Path(path)
    if (candidate.name == '01_PROJECT_STATE.md' and candidate.parent.name == '_control'
            and candidate.parents[2].name == '01_在研课题_Projects'):
        return candidate.parents[1]
    return None


class MCPFilesystemServer:
    def __init__(self, guard):
        self.guard = guard

    def list_tools(self):
        state_tools = []
        for tool, fields in [('read_project_state', ['path']), ('submit_project_state', ['path', 'base_sha256', 'content'])]:
            state_tools.append({'name': tool, 'description': 'Read the state including its hash, or queue a conditional update for the host.',
                                'inputSchema': {'type': 'object', 'properties': {k: {'type':'string'} for k in fields}, 'required': fields}})
        return state_tools + [
            {
                "name": "read_file",
                "description": (
                    "Read the complete contents of a file as UTF-8 text. "
                    "Supports optional 'head' or 'tail' line counts."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "head": {"type": "number"},
                        "tail": {"type": "number"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "read_text_file",
                "description": (
                    "Read the complete contents of a text file with optional head/tail lines."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "head": {"type": "number"},
                        "tail": {"type": "number"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "read_multiple_files",
                "description": "Read the contents of multiple files simultaneously.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["paths"],
                },
            },
            {
                "name": "write_file",
                "description": "Create a new file or overwrite an existing file with UTF-8 content.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
            {
                "name": "edit_file",
                "description": "Make line-based edits to a text file and return a unified diff.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "oldText": {"type": "string"},
                                    "newText": {"type": "string"},
                                },
                                "required": ["oldText", "newText"],
                            },
                        },
                        "dryRun": {"type": "boolean", "default": False},
                    },
                    "required": ["path", "edits"],
                },
            },
            {
                "name": "create_directory",
                "description": "Create a new directory or ensure a directory exists.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "list_directory",
                "description": "Get a detailed listing of all files and directories in a path.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "withSizes": {"type": "boolean", "default": False},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "directory_tree",
                "description": "Get a recursive JSON tree view of files and directories.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "excludePatterns": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "move_file",
                "description": "Move or rename files and directories.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "destination": {"type": "string"},
                    },
                    "required": ["source", "destination"],
                },
            },
            {
                "name": "search_files",
                "description": "Recursively search for files and directories matching a pattern.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "pattern": {"type": "string"},
                        "excludePatterns": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["path", "pattern"],
                },
            },
            {
                "name": "get_file_info",
                "description": "Retrieve detailed metadata about a file or directory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "list_allowed_directories",
                "description": "Returns the list of directories that this server is allowed to access.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def call_tool(self, name, args):
        args = args or {}
        if name in ('read_project_state', 'submit_project_state'):
            import importlib
            import state_client
            try:
                state_client = importlib.reload(state_client)
            except Exception:
                pass
            resolved = self.guard.validate(args.get('path'))
            project = state_project(resolved)
            if project is None:
                raise ValueError('请选择项目状态原件')
            if name == 'read_project_state':
                result = state_client.read(project)
            else:
                self.guard.ensure_writable(resolved)
                result = state_client.submit(project, args.get('base_sha256'), args.get('content'))
            return json.dumps(result, ensure_ascii=False)
        if name in ("read_file", "read_text_file"):
            resolved = self.guard.validate(args.get("path"))
            text = _read_text_slices(
                resolved, head=args.get("head"), tail=args.get("tail")
            )
            return text

        if name == "read_multiple_files":
            paths = args.get("paths")
            if not isinstance(paths, list):
                raise ValueError("'paths' must be an array of strings")
            chunks = []
            for p in paths:
                try:
                    resolved = self.guard.validate(p)
                    content = _read_text_slices(resolved)
                    chunks.append("{}:\n{}\n".format(p, content))
                except Exception as exc:
                    chunks.append("{}: Error - {}".format(p, exc))
            return "\n---\n".join(chunks)

        if name == "write_file":
            resolved = self.guard.validate(args.get("path"))
            self.guard.ensure_writable(resolved)
            if state_project(resolved):
                raise ValueError('请用 read_project_state / submit_project_state')
            content = args.get("content")
            if content is None:
                raise ValueError("'content' is required")
            self.guard.ensure_append_only_ledger(resolved, str(content))
            parent = os.path.dirname(resolved)
            if parent:
                self.guard.ensure_writable(parent)
                os.makedirs(parent, exist_ok=True)
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(str(content))
            return "Successfully wrote to {}".format(resolved)

        if name == "edit_file":
            resolved = self.guard.validate(args.get("path"))
            self.guard.ensure_writable(resolved)
            if state_project(resolved) and not args.get('dryRun', False):
                raise ValueError('请用 read_project_state / submit_project_state')
            edits = args.get("edits")
            if not isinstance(edits, list):
                raise ValueError("'edits' must be a list")
            dry_run = bool(args.get("dryRun", False))
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                original = f.read()
            updated, diff_text = _apply_edits(original, edits)
            self.guard.ensure_append_only_ledger(resolved, updated)
            if not dry_run:
                with open(resolved, "w", encoding="utf-8") as f:
                    f.write(updated)
            return diff_text

        if name == "create_directory":
            resolved = self.guard.validate(args.get("path"))
            self.guard.ensure_writable(resolved)
            os.makedirs(resolved, exist_ok=True)
            return "Successfully created directory {}".format(resolved)

        if name == "list_directory":
            resolved = self.guard.validate(args.get("path"))
            if not os.path.isdir(resolved):
                raise ValueError("Not a directory: {}".format(resolved))
            with_sizes = bool(args.get("withSizes", False))
            lines = []
            for entry in sorted(os.listdir(resolved)):
                full = os.path.join(resolved, entry)
                is_dir = os.path.isdir(full)
                if is_dir:
                    lines.append("[DIR] {}".format(entry))
                elif with_sizes:
                    try:
                        sz = os.path.getsize(full)
                    except OSError:
                        sz = 0
                    lines.append("[FILE] {} ({})".format(entry, _format_size(sz)))
                else:
                    lines.append("[FILE] {}".format(entry))
            return "\n".join(lines)

        if name == "directory_tree":
            resolved = self.guard.validate(args.get("path"))
            if not os.path.isdir(resolved):
                raise ValueError("Not a directory: {}".format(resolved))
            excludes = args.get("excludePatterns") or []
            tree = _build_tree(resolved, resolved, excludes)
            return json.dumps(tree, ensure_ascii=False, indent=2)

        if name == "move_file":
            src = self.guard.validate(args.get("source"))
            dst = self.guard.validate(args.get("destination"))
            actual_dst = os.path.join(dst, os.path.basename(src)) if os.path.isdir(dst) else dst
            if any(state_project(p) or (os.path.isdir(p) and any(os.path.isfile(os.path.join(p, rel))
                   for rel in ('_control/01_PROJECT_STATE.md', '01_PROJECT_STATE.md'))) for p in (src, dst, actual_dst)):
                raise ValueError('状态文件及所属目录需保留原路径')
            self.guard.ensure_writable(src)
            self.guard.ensure_no_protected_descendants(src)
            self.guard.ensure_writable(dst)
            dst_parent = os.path.dirname(dst)
            if dst_parent:
                self.guard.ensure_writable(dst_parent)
                os.makedirs(dst_parent, exist_ok=True)
            shutil.move(src, dst)
            return "Successfully moved {} to {}".format(src, dst)

        if name == "search_files":
            resolved = self.guard.validate(args.get("path"))
            if not os.path.isdir(resolved):
                raise ValueError("Not a directory: {}".format(resolved))
            pattern = str(args.get("pattern", "")).strip()
            if not pattern:
                raise ValueError("'pattern' must be non-empty")
            excludes = args.get("excludePatterns") or []
            has_wildcard = any(c in pattern for c in "*?[")
            pat_lower = pattern.lower()
            matches = []

            if any(n in ("benchmark","冻结件") or n.startswith("冻结存档-") for n in resolved.split(os.sep)):
                raise PermissionError("Protected tree cannot be recursively searched")
            for cur_root, dirs, files in os.walk(resolved, followlinks=False):
                # Prune excluded subdirectories in-place.
                kept_dirs = []
                for dname in sorted(dirs):
                    dfull = os.path.join(cur_root, dname)
                    drel = os.path.relpath(dfull, resolved)
                    if dname not in ("benchmark","冻结件") and not dname.startswith("冻结存档-") and not _matches_exclude(drel, dname, excludes):
                        kept_dirs.append(dname)
                dirs[:] = kept_dirs

                for entry in sorted(dirs) + sorted(files):
                    full = os.path.join(cur_root, entry)
                    rel = os.path.relpath(full, resolved).replace(os.sep, "/")
                    if _matches_exclude(rel, entry, excludes):
                        continue
                    name_lower = entry.lower()
                    rel_lower = rel.lower()
                    if has_wildcard:
                        hit = fnmatch.fnmatch(name_lower, pat_lower) or fnmatch.fnmatch(
                            rel_lower, pat_lower
                        )
                    else:
                        hit = (pat_lower in name_lower) or (pat_lower in rel_lower)
                    if hit:
                        matches.append(full)

            return "\n".join(matches) if matches else "No matches found"

        if name == "get_file_info":
            resolved = self.guard.validate(args.get("path"))
            st = os.stat(resolved)
            info = [
                "size: {}".format(st.st_size),
                "created: {}".format(
                    datetime.datetime.fromtimestamp(
                        st.st_ctime, tz=datetime.timezone.utc
                    ).isoformat()
                ),
                "modified: {}".format(
                    datetime.datetime.fromtimestamp(
                        st.st_mtime, tz=datetime.timezone.utc
                    ).isoformat()
                ),
                "accessed: {}".format(
                    datetime.datetime.fromtimestamp(
                        st.st_atime, tz=datetime.timezone.utc
                    ).isoformat()
                ),
                "isDirectory: {}".format(str(stat.S_ISDIR(st.st_mode)).lower()),
                "isFile: {}".format(str(stat.S_ISREG(st.st_mode)).lower()),
                "permissions: {:03o}".format(stat.S_IMODE(st.st_mode)),
            ]
            return "\n".join(info)

        if name == "list_allowed_directories":
            return "Allowed directories:\n" + "\n".join(self.guard.real_roots)

        raise ValueError("Unknown tool: {}".format(name))

    def handle_message(self, msg):
        method = msg.get("method")
        has_id = "id" in msg and msg["id"] is not None
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if not has_id:
            # JSON-RPC 2.0 notifications (e.g. notifications/initialized) expect no response.
            return None

        if method == "initialize":
            client_proto = params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": client_proto,
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "hub-python-filesystem",
                        "version": "1.0.0",
                    },
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self.list_tools()},
            }

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments") or {}
            try:
                text = self.call_tool(tool_name, tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": str(text)}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "Error: {}".format(exc)}],
                        "isError": True,
                    },
                }

        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

        if method == "resources/templates/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"resourceTemplates": []},
            }

        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": "Method not found: {}".format(method),
            },
        }


def _read_next_message(stdin_buf):
    """Read one JSON-RPC message (supports both newline JSON and Content-Length framing)."""
    while True:
        raw_line = stdin_buf.readline()
        if not raw_line:
            return None, None
        stripped = raw_line.strip()
        if not stripped:
            continue

        lower = stripped.lower()
        if lower.startswith(b"content-length:"):
            length = int(stripped.split(b":", 1)[1].strip())
            while True:
                hdr = stdin_buf.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
            payload = stdin_buf.read(length)
            if not payload:
                return None, None
            return json.loads(payload.decode("utf-8")), "lsp"

        return json.loads(stripped.decode("utf-8")), "ndjson"


def _write_message(stdout_buf, obj, framing):
    encoded = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if framing == "lsp":
        header = "Content-Length: {}\r\n\r\n".format(len(encoded)).encode("ascii")
        stdout_buf.write(header + encoded)
    else:
        stdout_buf.write(encoded + b"\n")
    stdout_buf.flush()


def main():
    allowed = sys.argv[1:] if len(sys.argv) > 1 else [os.getcwd()]
    guard = PathGuard(allowed)
    server = MCPFilesystemServer(guard)

    stdin_buf = sys.stdin.buffer
    stdout_buf = sys.stdout.buffer

    while True:
        try:
            msg, framing = _read_next_message(stdin_buf)
        except Exception as exc:
            _err_log("Malformed input frame: {}".format(exc))
            continue

        if msg is None:
            break

        try:
            response = server.handle_message(msg)
        except Exception as exc:
            _err_log("Internal handler error: {}".format(exc))
            req_id = msg.get("id") if isinstance(msg, dict) else None
            if req_id is not None:
                response = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": str(exc)},
                }
            else:
                response = None

        if response is not None:
            _write_message(stdout_buf, response, framing or "ndjson")


if __name__ == "__main__":
    main()
