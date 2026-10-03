"""Ruff's safe lint fixes for .pyn: what `source.fixAll` / `source.organizeImports` do on save in a .py.

Ruff checks the hidden translation, where `f(os=)` is `f(os=os)`, so it sees real uses (the formatter's
stand-ins would hide them: `f(os=__p)` makes `import os` look unused). Each fix is kept only if every
edit lands on text the user wrote, verbatim; fixes touching generated code or the record prelude are
dropped. Some fixes unlock others (`List[int]` -> `list[int]` leaves `from typing import List` unused),
so it repeats until nothing changes, like `ruff check --fix`.
"""

import json
import subprocess
from pathlib import Path

from .srcmap import Translation, generated
from .tools import resolve

ORGANIZE = ["--select", "I001"]  # what the Ruff extension's source.organizeImports applies


class FixError(Exception):
    pass


def char_offsets(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def source_edits(tr: Translation, fix: dict) -> list[tuple[int, int, str]] | None:
    """The fix's edits as source (start, end, text), or None if any edit isn't on user-written text."""
    starts = char_offsets(tr.hidden)
    out = []
    for ed in fix["edits"]:
        hs = starts[ed["location"]["row"] - 1] + ed["location"]["column"] - 1
        he = starts[ed["end_location"]["row"] - 1] + ed["end_location"]["column"] - 1
        a = tr._from_hidden(hs, False)
        b = tr._from_hidden(he, he > hs)
        if a is None or b is None or generated(a[1]) or (he > hs and generated(b[1])):
            return None
        s, e = a[0], b[0]
        if tr.source[s:e] != tr.hidden[hs:he]:  # a byname edit sits inside the range
            return None
        out.append((s, e, ed["content"]))
    return out


def fix_pyn(src: str, filename: str = "file.pyn", cwd: Path | None = None, select: list[str] | None = None) -> str:
    """src with Ruff's safe fixes applied. filename (as .py) lets ruff find the project's config;
    select: extra ruff arguments, e.g. ORGANIZE."""
    for _ in range(10):  # each round applies the fixes that don't overlap; later rounds pick up the rest
        tr = Translation(src)
        if tr.error is not None:
            raise FixError(f"can't translate: {tr.error}")
        p = subprocess.run(
            [resolve("ruff"), "check", *(select or []), "--output-format", "json", "--exit-zero", "--stdin-filename", str(Path(filename).with_suffix(".py")), "-"],
            input=tr.hidden,
            capture_output=True,
            text=True,
            cwd=cwd,
            check=False,
        )
        if p.returncode != 0:
            raise FixError(p.stderr.strip() or "ruff check failed")
        edits: list[tuple[int, int, str]] = []
        for d in json.loads(p.stdout):
            fix = d.get("fix")
            if not fix or fix.get("applicability") != "safe":
                continue
            mine = source_edits(tr, fix)
            # a fix is all-or-nothing, and skips this round if it overlaps one already taken
            if mine and not any(s < e2 and s2 < e or s == s2 for s, e, _ in mine for s2, e2, _ in edits):
                edits += mine
        if not edits:
            return src
        for s, e, text in sorted(edits, reverse=True):
            src = src[:s] + text + src[e:]
    return src
