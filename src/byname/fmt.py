"""Format .pyn with ruff: swap byname syntax for plain-Python stand-ins, format, swap back.

    fn(x=)            -> fn(x=__p)
    (x=, y=1)         -> __P(x=__p, y=1)
    (x=, y=t) = r     -> __P[x:__p, y:t] = r       (a subscript is a valid assignment target)

Stand-ins are a few characters wider than the real syntax, so a line right at the length limit
can wrap one step early.
"""

import io
import subprocess
import tokenize
from pathlib import Path

from .tools import resolve
from .transform import PAT, SHORT, transform


class FormatError(Exception):
    pass


def encode(src: str) -> str:
    if SHORT in src or PAT in src:
        raise FormatError(f"source already uses the stand-in names {SHORT}/{PAT}")
    r = transform(src)
    out = src
    for start, end, text in reversed(r.standins):
        out = out[:start] + text + out[end:]
    return out


def decode(code: str) -> str:
    toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    starts = [0]
    for line in code.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def off(rc: tuple[int, int]) -> int:
        return starts[rc[0] - 1] + rc[1]

    sig = [i for i, t in enumerate(toks) if t.type not in (tokenize.COMMENT, tokenize.NL)]
    pos = {i: k for k, i in enumerate(sig)}  # token index -> index in sig
    edits: list[tuple[int, int, str]] = []
    pair: dict[int, int] = {}
    stack: list[int] = []
    for i in sig:
        if toks[i].string in "([{":
            stack.append(i)
        elif toks[i].string in ")]}":
            pair[stack.pop()] = i

    def nxt(i: int) -> int:
        return sig[pos[i] + 1]

    for i in sig:
        t = toks[i]
        # x=__p / x:__p -> drop the value (colon handled with its pattern); __P( -> ( for records
        if t.string == SHORT or (t.string == PAT and toks[nxt(i)].string == "("):
            edits.append((off(t.start), off(t.end), ""))
        elif t.string == PAT and toks[nxt(i)].string == "[":  # pattern
            o, c = nxt(i), pair[nxt(i)]
            edits.append((off(t.start), off(toks[o].end), "("))
            edits.append((off(toks[c].start), off(toks[c].end), ")"))
            depth, j = 0, nxt(o)
            while j != c:  # top-level `name : target` -> `name=target`, eating ruff's slice spacing
                s = toks[j].string
                if j in pair:
                    j = pair[j]
                elif s == ":" and depth == 0:
                    prev_end = off(toks[sig[pos[j] - 1]].end)
                    after = nxt(j)
                    edits.append((prev_end, off(toks[after].start), "="))
                j = nxt(j)
    edits.sort()
    out = code
    for start, end, text in reversed(edits):
        out = out[:start] + text + out[end:]
    return out


def format_pyn(src: str, filename: str = "file.pyn", cwd: Path | None = None) -> str:
    """Formatted source. filename (as .py) lets ruff find the project's config."""
    code = encode(src)
    p = subprocess.run(
        [resolve("ruff"), "format", "--stdin-filename", str(Path(filename).with_suffix(".py")), "-"],
        input=code, capture_output=True, text=True, cwd=cwd, check=False,
    )
    if p.returncode != 0:
        raise FormatError(p.stderr.strip() or "ruff format failed")
    return decode(p.stdout)
