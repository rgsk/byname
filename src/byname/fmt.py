"""Format .pyn with ruff: swap byname syntax for plain-Python stand-ins, format, swap back.

    fn(x=)            -> fn(x=__p)
    (x=, y=1)         -> __P(x=__p, y=1)
    (x=, y=t) = r     -> __P[x:__p, y:t] = r       (a subscript is a valid assignment target)
    -> (x: int)       -> -> __T[x: int]            (record type)

Stand-ins are a few characters wider than the real syntax, so a line right at the length limit
can wrap one step early.

Grids: if the first line inside a split bracket holds several items, later lines are packed up to
its width instead of one item per line:

    s = [
        1, 2, 3,
        4, 5, 6,
    ]
    bfs(
        n, m, k,
        start=(sr, sc),
        blocked=walls,
    )

The first line may also start right after the bracket (`s = [1, 2, 3,`). Like ruff's magic trailing
comma, it all needs a trailing comma after the last item.
`grid` tags these brackets with a comment before ruff runs, and `ungrid` repacks them after.
"""

import io
import re
import subprocess
import tokenize
from pathlib import Path

from .tools import resolve
from .transform import PAT, SHORT, TYP, transform


class FormatError(Exception):
    pass


def encode(src: str) -> str:
    if SHORT in src or PAT in src or TYP in src:
        raise FormatError(f"source already uses the stand-in names {SHORT}/{PAT}/{TYP}")
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
        elif t.string in (PAT, TYP) and toks[nxt(i)].string == "[":  # pattern / record type
            o, c = nxt(i), pair[nxt(i)]
            sep = "=" if t.string == PAT else ": "
            edits.append((off(t.start), off(toks[o].end), "("))
            edits.append((off(toks[c].start), off(toks[c].end), ")"))
            depth, j = 0, nxt(o)
            while j != c:  # top-level `name : x` -> `name=x` / `name: x`, eating ruff's slice spacing
                s = toks[j].string
                if j in pair:
                    j = pair[j]
                elif s == ":" and depth == 0:
                    prev_end = off(toks[sig[pos[j] - 1]].end)
                    after = nxt(j)
                    edits.append((prev_end, off(toks[after].start), sep))
                j = nxt(j)
    edits.sort()
    out = code
    for start, end, text in reversed(edits):
        out = out[:start] + text + out[end:]
    return out


GRID = "# __grid:"  # + how many items the first line holds
GRID_RE = re.compile(r"# __grid:(\d+)")


def _pairs(toks: list[tokenize.TokenInfo]) -> dict[int, int]:
    pair: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(toks):
        if t.type == tokenize.OP and t.string in "([{":
            stack.append(i)
        elif t.type == tokenize.OP and t.string in ")]}" and stack:
            pair[stack.pop()] = i
    return pair


def _items(toks: list[tokenize.TokenInfo], o: int, c: int, pair: dict[int, int]) -> list[tuple[int, int]] | None:
    """Top-level items between brackets o and c as (first token, its comma), or None if a comment
    sits inside or the last item has no trailing comma."""
    items: list[tuple[int, int]] = []
    first, j = None, o + 1
    while j < c:
        t = toks[j]
        if t.type == tokenize.COMMENT:
            return None
        if t.type not in (tokenize.NL, tokenize.NEWLINE):
            if first is None:
                first = j
            if j in pair:
                j = pair[j]
            elif t.string == ",":
                items.append((first, j))
                first = None
        j += 1
    return items if items and first is None else None


def grid(src: str) -> str:
    """Tag each grid-shaped bracket with a GRID comment (see the module docstring). Anything under
    `# fmt: off`, or in a statement marked `# fmt: skip`, is left alone."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError):
        return src  # broken code: ruff reports it
    pair = _pairs(toks)
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    skip: set[int] = set()
    off, stmt = False, 0
    for i, t in enumerate(toks):
        if t.type == tokenize.COMMENT:
            text = t.string.replace(" ", "")
            off = {"#fmt:off": True, "#fmt:on": False}.get(text, off)
            if text == "#fmt:skip":
                skip.update(range(stmt, i))
        if off:
            skip.add(i)
        if t.type == tokenize.NEWLINE:
            stmt = i + 1
    inserts: list[tuple[int, str]] = []
    for o, c in pair.items():
        if o in skip or toks[c].start[0] == toks[o].start[0]:
            continue
        items = _items(toks, o, c, pair)
        if not items:
            continue
        hug = toks[o + 1].type != tokenize.NL  # `[1, 2, 3,` : the first line starts right after the bracket
        row = toks[items[0][0]].start[0]
        k = sum(1 for _, b in items if toks[b].end[0] == row)
        if k >= 2 and toks[items[k - 1][1] + 1].type == tokenize.NL:  # k whole items, nothing after
            r, col = toks[o].end
            inserts.append((starts[r - 1] + col, f"  {GRID}{k}" + "\n" * hug))  # a hugged first line moves below the tag
    out = src
    for at, text in sorted(inserts, reverse=True):
        out = out[:at] + text + out[at:]
    return out


def ungrid(code: str) -> str:
    """Repack each GRID-tagged bracket that ruff split one item per line, and drop the tags."""
    toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    pair = _pairs(toks)
    lines = code.splitlines(keepends=True)
    edits: list[tuple[int, int, list[str]]] = []  # lines[a:b] = new
    for i, t in enumerate(toks):
        m = GRID_RE.fullmatch(t.string) if t.type == tokenize.COMMENT else None
        if not m:
            continue
        r = t.start[0] - 1
        lines[r] = lines[r][: t.start[1]].rstrip(" \t") + lines[r][t.end[1] :]
        o = i - 1
        if o not in pair:
            continue
        toks[i] = t._replace(type=tokenize.NL)  # the tag isn't a comment inside the bracket
        items = _items(toks, o, pair[o], pair) or []
        rows = [toks[a].start[0] for a, _ in items]
        if not items or len(set(rows)) != len(rows) or any(toks[b].end[0] != rr for (_, b), rr in zip(items, rows)):
            continue  # not one item per line (an item spans lines): leave it as ruff wrote it
        texts = [lines[rr - 1][toks[a].start[1] : toks[b].start[1]] for (a, b), rr in zip(items, rows)]
        indent = lines[rows[0] - 1][: toks[items[0][0]].start[1]]
        width = len(", ".join(texts[: int(m.group(1))]))
        packed: list[list[str]] = [[]]
        for s in texts:
            if packed[-1] and len(", ".join([*packed[-1], s])) > width:
                packed.append([])
            packed[-1].append(s)
        edits.append((rows[0] - 1, rows[-1], [indent + ", ".join(p) + ",\n" for p in packed]))
    for a, b, new in sorted(edits, reverse=True):
        lines[a:b] = new
    return "".join(lines)


def format_pyn(src: str, filename: str = "file.pyn", cwd: Path | None = None) -> str:
    """Formatted source. filename (as .py) lets ruff find the project's config."""
    code = encode(grid(src))
    p = subprocess.run(
        [resolve("ruff"), "format", "--stdin-filename", str(Path(filename).with_suffix(".py")), "-"],
        input=code, capture_output=True, text=True, cwd=cwd, check=False,
    )
    if p.returncode != 0:
        raise FormatError(p.stderr.strip() or "ruff format failed")
    return ungrid(decode(p.stdout))
