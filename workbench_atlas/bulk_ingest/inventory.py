"""Inventory pod: walk a folder, hash, sniff formats, detect duplicates.

Never follows symlinks, never reads answer keys, and gives a reason for every
file it does not accept.
"""

from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .models import FileEntry

EXCLUDED_NAMES = frozenset({"answer_key.json"})
SYSTEM_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini", "ehthumbs.db", "icon\r"})
SYSTEM_DIRS = frozenset({"__macosx", ".git", ".svn", "__pycache__", ".trash", ".trashes"})
TEXT_EXTENSIONS = {
    ".csv": "csv", ".tsv": "tsv", ".tab": "tsv", ".md": "md", ".markdown": "md",
    ".txt": "txt", ".text": "txt", ".json": "json", ".eml": "eml", ".html": "html", ".htm": "html",
}
OOXML = {"xlsx": "xl/workbook.xml", "docx": "word/document.xml", "pptx": "ppt/presentation.xml"}
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_IMAGES = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"II*\x00", b"MM\x00*", b"RIFF")


@dataclass(frozen=True)
class Limits:
    max_depth: int = 8
    max_files: int = 2000
    max_file_bytes: int = 25 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024


@dataclass
class Manifest:
    root: Path
    entries: list[FileEntry] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)

    @property
    def accepted(self) -> list[FileEntry]:
        return [e for e in self.entries if e.status == "accepted"]

    def to_dict(self) -> dict:
        from dataclasses import asdict

        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return {"root_name": self.root.name, "limits": asdict(self.limits), "counts": counts,
                "total_bytes": sum(e.size for e in self.entries if e.status in ("accepted", "duplicate")),
                "entries": [asdict(e) for e in self.entries]}


class InventoryError(ValueError):
    """The folder itself cannot be inventoried."""


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def sniff_format(head: bytes, name: str, path: Path) -> tuple[str, str, str]:
    """Return (format, disposition, reason); disposition accepted|skipped|quarantined."""
    ext = os.path.splitext(name)[1].lower()
    claimed = ext.lstrip(".")
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            return claimed or "zip", "quarantined", f"corrupt: damaged zip container ({type(exc).__name__})"
        for fmt, marker in OOXML.items():
            if marker in names:
                return fmt, "accepted", ""  # content wins over a wrong extension
        if claimed in OOXML:
            return claimed, "quarantined", f"corrupt: .{claimed} file is a zip without an Office {claimed} part"
        return "zip", "skipped", "unsupported: archives are not unpacked"
    if head.startswith(_OLE):
        if claimed in OOXML:
            return claimed, "quarantined", "encrypted: password-protected Office file (OLE container)"
        return "ole", "skipped", "unsupported: legacy binary Office or Outlook format"
    if head.startswith(b"%PDF-"):
        return "pdf", "accepted", ""
    if claimed in OOXML or claimed == "pdf":
        return claimed, "quarantined", f"corrupt: content does not match the .{claimed} format"
    if head.startswith(_IMAGES):
        return "image", "skipped", "unsupported: images need an OCR lane that is not available"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        text_kind = TEXT_EXTENSIONS.get(ext, "txt")
        return text_kind, "accepted", ""
    if b"\x00" in head:
        return "binary", "skipped", "unsupported: binary content"
    if ext in TEXT_EXTENSIONS:
        return TEXT_EXTENSIONS[ext], "accepted", ""
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    lowered = stripped[:200].lower()
    if stripped[:1] in (b"{", b"["):
        return "json", "accepted", ""
    if lowered.startswith((b"<!doctype html", b"<html")):
        return "html", "accepted", ""
    if any(lowered.startswith(h) for h in (b"from:", b"received:", b"subject:", b"return-path:",
                                            b"message-id:", b"mime-version:")):
        return "eml", "accepted", ""
    if ext in ("", ".log", ".rst", ".text"):
        return "txt", "accepted", ""
    return "unknown", "skipped", f"unsupported: extension {ext or '(none)'}"


def _hash_file(path: Path, max_bytes: int) -> tuple[str, bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest, head, size = hashlib.sha256(), b"", 0
    with os.fdopen(fd, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if not head:
                head = chunk[:8192]
            size += len(chunk)
            if size > max_bytes:
                raise OverflowError
            digest.update(chunk)
    return digest.hexdigest(), head, size


def build_inventory(folder: str | Path, limits: Limits | None = None) -> Manifest:
    limits = limits or Limits()
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise InventoryError("The ingest folder does not exist or is not a directory.")
    root = root.resolve()
    manifest = Manifest(root=root, limits=limits)
    seen: dict[str, str] = {}
    state = {"files": 0, "bytes": 0}
    _walk(root, root, 0, manifest, seen, state)
    manifest.entries.sort(key=lambda e: e.path)
    return manifest


def _walk(root: Path, directory: Path, depth: int, manifest: Manifest,
          seen: dict[str, str], state: dict[str, int]) -> None:
    limits = manifest.limits
    try:
        children = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError as exc:
        manifest.entries.append(FileEntry(_rel(root, Path(directory)), 0, "skipped",
                                          f"unreadable directory: {type(exc).__name__}"))
        return
    for child in children:
        rel = _rel(root, Path(child.path))
        lowered = child.name.casefold()
        if child.is_symlink():
            target = os.path.realpath(child.path)
            if not _within(target, str(root)):
                manifest.entries.append(FileEntry(rel, 0, "quarantined", "symlink escapes the ingest folder"))
            else:
                manifest.entries.append(FileEntry(rel, 0, "skipped", "symlink not followed"))
            continue
        if child.name.startswith("~$"):
            manifest.entries.append(FileEntry(rel, 0, "skipped", "office lock file, not a document"))
            continue
        if child.name.startswith(".") or lowered in SYSTEM_NAMES or child.name.startswith("._") \
                or lowered in SYSTEM_DIRS:
            if not child.name.startswith("._") and lowered not in SYSTEM_NAMES:
                manifest.entries.append(FileEntry(rel, 0, "skipped", "hidden or system entry"))
            else:
                manifest.entries.append(FileEntry(rel, 0, "skipped", "system file"))
            continue
        try:
            info = child.stat(follow_symlinks=False)
        except OSError as exc:
            manifest.entries.append(FileEntry(rel, 0, "skipped", f"cannot stat: {type(exc).__name__}"))
            continue
        if stat.S_ISDIR(info.st_mode):
            if depth + 1 > limits.max_depth:
                manifest.entries.append(FileEntry(rel + "/", 0, "skipped", "directory deeper than the depth limit"))
                continue
            _walk(root, Path(child.path), depth + 1, manifest, seen, state)
            continue
        if not stat.S_ISREG(info.st_mode):
            manifest.entries.append(FileEntry(rel, 0, "skipped", "not a regular file"))
            continue
        if lowered in EXCLUDED_NAMES:
            manifest.entries.append(FileEntry(rel, info.st_size, "skipped",
                                              "excluded: scoring answer keys are never read"))
            continue
        if state["files"] >= limits.max_files:
            manifest.entries.append(FileEntry(rel, info.st_size, "skipped", "file count limit reached"))
            continue
        if info.st_size > limits.max_file_bytes:
            manifest.entries.append(FileEntry(rel, info.st_size, "skipped",
                                              f"exceeds the {limits.max_file_bytes // (1024 * 1024)} MB file limit"))
            continue
        if state["bytes"] + info.st_size > limits.max_total_bytes:
            manifest.entries.append(FileEntry(rel, info.st_size, "skipped", "total batch size limit reached"))
            continue
        if info.st_size == 0:
            manifest.entries.append(FileEntry(rel, 0, "skipped", "empty file"))
            continue
        try:
            digest, head, size = _hash_file(Path(child.path), limits.max_file_bytes)
        except OverflowError:
            manifest.entries.append(FileEntry(rel, info.st_size, "skipped", "file grew beyond the size limit"))
            continue
        except OSError as exc:
            manifest.entries.append(FileEntry(rel, info.st_size, "quarantined",
                                              f"unreadable: {type(exc).__name__}"))
            continue
        state["files"] += 1
        state["bytes"] += size
        fmt, disposition, reason = sniff_format(head, child.name, Path(child.path))
        entry = FileEntry(rel, size, disposition, reason, fmt, digest)  # type: ignore[arg-type]
        if disposition == "accepted":
            if digest in seen:
                entry.status, entry.duplicate_of = "duplicate", seen[digest]
                entry.reason = f"byte-identical duplicate of {seen[digest]}"
            else:
                seen[digest] = rel
        manifest.entries.append(entry)


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() if path != root else "."
