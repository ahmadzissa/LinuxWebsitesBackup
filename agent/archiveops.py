#!/usr/bin/env python3
"""Archive indexing and selective restores; uses only the Python standard library."""
import argparse
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path


def safe_path(value):
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ValueError("Invalid archive path")
    if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Unsupported characters in archive path")
    parts = value.rstrip("/").split("/")
    while parts and parts[0] == ".":
        parts.pop(0)
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ValueError("Unsafe archive path: %s" % value)
    return "/".join(parts)


def archive_name(root):
    """Name panel website archives by domain; retain path names for other roots."""
    root = root.rstrip("/")
    match = re.fullmatch(r"/home/[^/]+/(?:web/([^/]+)/public_html|htdocs/([^/]+))", root)
    domain = (match.group(1) or match.group(2)) if match else root.rsplit("/", 1)[-1]
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z0-9-]+", domain):
        return domain.lower()
    return root.lstrip("/").replace("/", "_")


def member_entry(member):
    return {"path": safe_path(member.name),
            "type": "directory" if member.isdir() else "file" if member.isfile() else "link" if member.issym() or member.islnk() else "special",
            "size": member.size if member.isfile() else 0,
            "modified": member.mtime}


def complete_index(entries):
    """Add implicit directories and enforce one archive root with no link parents."""
    nodes = {}
    for entry in entries:
        path = safe_path(entry["path"])
        if path in nodes:
            raise ValueError("Duplicate archive member: %s" % path)
        nodes[path] = dict(entry, path=path)
    for path in list(nodes):
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent in nodes and nodes[parent]["type"] != "directory":
                raise ValueError("Archive path crosses a non-directory: %s" % parent)
            nodes.setdefault(parent, {"path": parent, "type": "directory", "size": 0, "modified": 0})
    roots = {p.split("/")[0] for p in nodes}
    if len(roots) != 1:
        raise ValueError("Expected one website root in the archive")
    root = roots.pop()
    if nodes[root]["type"] != "directory":
        raise ValueError("Website root must be a directory")
    return {"version": 1, "root": root, "entries": list(nodes.values())}


def read_index(fileobj):
    with tarfile.open(fileobj=fileobj, mode="r|gz") as archive:
        return complete_index([member_entry(m) for m in archive])


def selected_entries(index, selection):
    if not isinstance(selection, list) or not selection or len(selection) > 5000:
        raise ValueError("Select between 1 and 5000 files or folders")
    paths = {safe_path(p) for p in selection}
    nodes = {e["path"]: e for e in index["entries"]}
    for path in paths:
        if path not in nodes or nodes[path]["type"] not in ("file", "directory"):
            raise ValueError("File or folder cannot be restored: %s" % path)
    # A selected folder includes its regular files and subfolders. Links/special
    # files are visible in the browser but are deliberately not restored.
    return [e for e in nodes.values() if e["type"] in ("file", "directory")
            and is_selected(e["path"], paths)]


def is_selected(path, requested):
    parts = path.split("/")
    return any("/".join(parts[:i]) in requested for i in range(1, len(parts) + 1))


def check_destination(parent, path, kind):
    destination = parent.joinpath(*path.split("/"))
    current = Path(parent.anchor)
    for part in destination.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("Destination is a symbolic link: %s" % current)
        if current != destination and not stat.S_ISDIR(mode):
            raise ValueError("Destination parent is not a directory: %s" % current)
        if current == destination and not (stat.S_ISDIR(mode) if kind == "directory" else stat.S_ISREG(mode)):
            raise ValueError("Destination has a different file type: %s" % current)
    return destination


def apply_metadata(path, member):
    # Never restore setuid/setgid/sticky flags from a backup.
    if hasattr(os, "chown") and os.geteuid() == 0:
        os.chown(path, member.uid, member.gid)
    os.chmod(path, member.mode & 0o777)
    os.utime(path, (member.mtime, member.mtime))


@contextmanager
def directory_handle(path, create=False):
    """Walk Linux directories with O_NOFOLLOW, keeping operations anchored to fds."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def replace_file(destination, staged, member):
    if os.name == "posix":
        # Directory descriptors also protect against a live website swapping a
        # parent directory for a symlink between validation and replacement.
        with directory_handle(destination.parent) as directory:
            temporary = ".webbackup-" + secrets.token_hex(16)
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as output, staged.open("rb") as source:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    apply_metadata(output.fileno(), member)
                os.replace(temporary, destination.name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    else:  # Allows disposable restore fixtures to run on Windows workstations.
        fd, temporary = tempfile.mkstemp(prefix=".webbackup-", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "wb") as output, staged.open("rb") as source:
                shutil.copyfileobj(source, output)
            apply_metadata(temporary, member)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def restore(archive_path, parent_path, selection):
    parent = Path(parent_path)
    if not parent.is_absolute() or ".." in parent.parts or parent == Path(parent.anchor):
        raise ValueError("Invalid restore parent")
    requested = {safe_path(p) for p in selection} if isinstance(selection, list) else set()
    if not requested or len(selection) > 5000:
        raise ValueError("Select between 1 and 5000 files or folders")
    with tempfile.TemporaryDirectory(prefix="wb-selected-") as staging:
        entries, members, staged = [], {}, {}
        # Scan and stage in one pass, so gzip archives are not decompressed once
        # per selected file. Validate the entire selection before live changes.
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for member in archive:
                entry = member_entry(member)
                entries.append(entry)
                path = entry["path"]
                members[path] = member
                if member.isfile() and is_selected(path, requested):
                    staged_path = Path(staging) / str(len(staged))
                    with archive.extractfile(member) as source, staged_path.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    staged[path] = staged_path
        index = complete_index(entries)
        chosen = selected_entries(index, selection)
        for entry in chosen:
            check_destination(parent, entry["path"], entry["type"])
        # Include ancestors of individually selected files, without changing
        # permissions or timestamps of directories that already exist.
        directories = {entry["path"] for entry in chosen if entry["type"] == "directory"}
        for entry in chosen:
            parts = entry["path"].split("/")
            directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
        created = []
        for path in sorted(directories, key=lambda p: (p.count("/"), p)):
            destination = check_destination(parent, path, "directory")
            if not destination.exists():
                if os.name == "posix":
                    with directory_handle(destination, create=True):
                        pass
                else:
                    destination.mkdir(parents=True)
                created.append((destination, members.get(path)))
        count = 0
        for entry in chosen:
            if entry["type"] != "file":
                continue
            path = entry["path"]
            destination = check_destination(parent, path, "file")
            replace_file(destination, staged[path], members[path])
            count += 1
            print("Restored: %s" % destination, flush=True)
        for destination, member in reversed(created):
            if member:
                if os.name == "posix":
                    with directory_handle(destination) as fd:
                        apply_metadata(fd, member)
                else:
                    apply_metadata(destination, member)
        print("Restored %d file(s) from %d selection(s)." % (count, len(requested)), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("name", "index", "restore"))
    parser.add_argument("path")
    parser.add_argument("parent", nargs="?")
    parser.add_argument("selection", nargs="?")
    args = parser.parse_args()
    if args.command == "name":
        print(archive_name(args.path))
    elif args.command == "index":
        with open(args.path, "rb") as source:
            json.dump(read_index(source), sys.stdout, separators=(",", ":"))
    else:
        with open(args.selection, encoding="utf-8") as source:
            restore(args.path, args.parent, json.load(source))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, tarfile.TarError) as exc:
        raise SystemExit("Restore/archive error: %s" % exc)
