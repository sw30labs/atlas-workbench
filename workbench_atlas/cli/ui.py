"""Small terminal helpers: consistent, colour-aware, dependency-free."""

from __future__ import annotations

import os
import sys
from typing import Iterable, Sequence

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _paint(code: str, text: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return _paint("1", text)


def dim(text: str) -> str:
    return _paint("2", text)


def green(text: str) -> str:
    return _paint("32", text)


def yellow(text: str) -> str:
    return _paint("33", text)


def red(text: str) -> str:
    return _paint("31", text)


def cyan(text: str) -> str:
    return _paint("36", text)


def header(title: str) -> None:
    print(f"\n{bold(title)}")


def ok(message: str) -> None:
    print(f"  {green('OK')}    {message}")


def warn(message: str) -> None:
    print(f"  {yellow('WARN')}  {message}")


def fail(message: str) -> None:
    print(f"  {red('FAIL')}  {message}")


def info(message: str) -> None:
    print(f"  {dim('·')} {message}")


def kv(pairs: Iterable[tuple[str, object]]) -> None:
    rows = [(str(k), str(v)) for k, v in pairs]
    if not rows:
        return
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key.ljust(width)}  {value}")


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  " + "  ".join(bold(h.ljust(widths[i])) for i, h in enumerate(headers)))
    print("  " + "  ".join(dim("─" * widths[i]) for i in range(len(headers))))
    for row in cells:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
