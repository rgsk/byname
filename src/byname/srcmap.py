"""Position mapping between a .pyn file and its plain-Python translation.

Three coordinate systems:
    source  the .pyn text
    body    source with edits applied (same line count)
    hidden  body with the record prelude inserted; what the type checker sees

LSP positions are (line, UTF-16 column), so LineIndex converts to and from offsets.
"""

import os
import re
import threading
from bisect import bisect_right
from functools import cached_property
from pathlib import Path

from .transform import (
    SHAPES,
    SPREAD,
    Edit,
    Known,
    Mark,
    class_fields,
    prelude_offset,
    shape_module,
    shape_source,
    transform,
)

Hit = tuple[Edit, Mark | None] | None  # what generated text a body offset landed on


class SourceMap:
    def __init__(self, edits: list[Edit]):
        self.edits = edits  # sorted, non-overlapping
        self.gstart: list[int] = []  # body offset where each edit's text starts
        delta = 0
        for e in edits:
            self.gstart.append(e.start + delta)
            delta += len(e.text) - (e.end - e.start)
        # marks that cover real source text (zero-width ones only map body -> source)
        self.marks = [(m, i) for i, e in enumerate(edits) for m in e.marks if m.oe > m.os]

    def to_body(self, o: int, end: bool = False, touch: bool = False) -> int:
        """touch: a cursor right after a marked word still counts as on it (completion at `na|`)."""
        for m, i in self.marks:
            if m.os <= o < m.oe or ((end or touch) and m.os < o <= m.oe):
                return self.gstart[i] + m.ts + min(o - m.os, m.te - m.ts)
        delta = 0
        for e, gs in zip(self.edits, self.gstart):
            if o < e.start or (end and o == e.start):
                break
            if o < e.end:  # inside replaced source: snap to the replacement
                return gs + (len(e.text) if end else 0)
            delta = gs + len(e.text) - e.end
        return o + delta

    def to_source(self, g: int, end: bool = False) -> tuple[int, Hit]:
        delta = 0
        for e, gs in zip(self.edits, self.gstart):
            ge = gs + len(e.text)
            if g < gs or (end and g == gs):
                break
            if g < ge or (end and g == ge):
                t = g - gs
                for m in e.marks:
                    if m.ts <= t < m.te or (end and m.ts < t <= m.te):
                        return m.os + min(t - m.ts, m.oe - m.os), (e, m)
                if end and g == ge:
                    return e.end, None
                return e.start, (e, None)
            delta = ge - e.end
        return g - delta, None


def generated(hit: Hit) -> tuple[int, int] | None:
    """Source span to display for a hit on generated text, or None if it hit real source."""
    if hit is None:
        return None
    e, m = hit
    if m is None:
        return e.display
    if m.os == m.oe:
        return m.display or e.display
    return None


class LineIndex:
    def __init__(self, text: str):
        self.text = text
        self.starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self.starts.append(i + 1)

    def offset(self, line: int, col16: int) -> int:
        if line >= len(self.starts):
            return len(self.text)
        start = self.starts[line]
        end = self.starts[line + 1] - 1 if line + 1 < len(self.starts) else len(self.text)
        units = 0
        for i in range(start, end):
            if units >= col16:
                return i
            units += 2 if ord(self.text[i]) > 0xFFFF else 1
        return end

    def position(self, off: int) -> dict:
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:  # last line start <= off
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        col = sum(2 if ord(c) > 0xFFFF else 1 for c in self.text[self.starts[lo] : off])
        return {"line": lo, "character": col}


def unknown_rest(name: str) -> str:
    """The hint on a `**rest` typed `(...)` (transform.rest_of), in the editor and `byname tool`."""
    return (
        f"{name} is (...), any record, unchecked: byname can't tell the fields of the record it takes apart,"
        " nor can the checker. Give that record a type: `row: User = rows[0]`, or `rows: list[User]`"
    )


Probed = dict[tuple[str, int], Known]
# the checker's answers for a file, by path: (the source they're for, what it said), set by `byname tool`
# before it translates (tools.probe), so every Translation of that text reads the same types
PROBED: dict[Path, tuple[str, Probed]] = {}

# a record type or literal written in the body, `_typ_sku__qty[` or `_rec_sku__qty___3(`: its fields' order
WRITTEN_SHAPE_RE = re.compile(r"\b_(?:rec|typ|opn)_(\w+?)(?=[\[(])")
# a field set -> an order some file of the project writes it in (the first translated): for a field set a file
# shows but doesn't write itself, a record another module returns (lsp.pretty, probed_from)
PROJECT_ORDERS: dict[frozenset[str], tuple[str, ...]] = {}
# `name = <literal>` on a line of its own: the name shows that literal's order (lsp.hover)
BOUND_RE = re.compile(r"^[ \t]*(\w+) = _rec_(\w+?)\(", re.M)

# the checker's mirrors (tools.mirror_dir adds each): every Translation writes the shape modules its prelude
# imports into each (write_shapes), so they're there before its text reaches a checker
SHAPE_ROOTS: set[Path] = set()
# told of each file write_shapes makes: the editor's checker hears of a new file only from such an event
NEW_SHAPES: list = []
_shapes_lock = threading.Lock()


def write_shapes(shapes: list[tuple[tuple[str, ...], bool]]) -> None:
    """The modules of `shapes` (transform.SHAPES) in every mirror that lacks them. A module never changes
    once written: its text is its shape's."""
    made: list[Path] = []
    with _shapes_lock:
        for root in list(SHAPE_ROOTS):
            pkg = root / SHAPES
            for f, o in shapes:
                path = pkg / f"{shape_module(f, o)}.py"
                if path.exists():
                    continue
                pkg.mkdir(parents=True, exist_ok=True)
                if not (init := pkg / "__init__.py").exists():
                    init.write_text("", encoding="utf-8")
                    made.append(init)
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(shape_source(f, o), encoding="utf-8")
                os.replace(tmp, path)  # whole: a checker reading it mid-write would see half a class
                made.append(path)
    for tell in NEW_SHAPES if made else ():
        tell(made)


# a probe's answer: `Type of "u" is "_typ_name__age___0[str, int]"`; an open record `_opn_...`
# a probe translation's last line, `reveal_type('_byname_probe')`: the checker answers it whatever else it
# can't (an unanalysed function's body), so a publish with its answer is of the probe (lsp.Proxy.probed)
PROBE_MARK = "_byname_probe"
# a spread of what another asked spread returns is answered a round later (tools.probe, lsp.Proxy.probed)
PROBE_ROUNDS = 3
# a comment that hides every diagnostic on its line, a probe's answer too: `# pyright: ignore` with no rules,
# `# type: ignore` with or without (the checker reads its rules as none). `# pyright: ignore[rule]` leaves it
IGNORE = re.compile(r"#\s*(?:pyright:\s*ignore(?![\w\[])|type:\s*ignore\b(?:\[[^\]]*\])?)")
REVEAL = re.compile(r'Type of "(.+?)" is "(_typ_|_opn_)(\w+)(.*)"$')


def one_type(args: str) -> bool:
    """`[str, list[int]]` after a class name is its own type arguments, nothing after: not `[str] | B[int]`."""
    if not args:
        return True
    depth = 0
    for k, c in enumerate(args):
        depth += {"[": 1, "]": -1}.get(c, 0)
        if depth == 0:
            return args[0] == "[" and k == len(args) - 1
    return False


def probed_from(found: list[tuple[int, int, str]], tr: "Translation", waiting: set | None = None) -> Probed:
    """What the checker said each unseen source of tr's probe translation is: its revealed record type's
    fields, by (name, source line), from (hidden line, character, message), 0-based. A spread of an
    expression is keyed by where it starts (transform's expr_key), found from where the answer stands. A type
    that isn't one record (a union, a dict, Any) or is itself built from unseen spreads says nothing; the
    latter may say more once the file it comes from is answered, so its key goes in `waiting`."""
    shift = tr.hidden.count("\n", tr.at, tr.at + tr.plen)
    first = tr.hidden.count("\n", 0, tr.at + tr.plen)  # the body's line after the prelude
    asked = set(tr.unseen)
    out: Probed = {}
    for line, character, message in found:
        m = REVEAL.match(message)
        if m is None or not one_type(m[4]):
            continue
        if m[1].isidentifier():  # a name, maybe byname's `_ds`: the translation keeps the source's lines
            key = (m[1], line + 1 - (shift if line >= first else 0))
        elif (hit := tr._from_hidden(tr.hid_lines.offset(line, character), False)) is not None:
            at = hit[0]
            key = (f"**{at - tr.source.rfind(chr(10), 0, at) - 1}", tr.source.count("\n", 0, at) + 1)
        else:
            continue
        if key in asked and SPREAD in m[3]:
            if waiting is not None:
                waiting.add(key)
        elif key in asked:
            # the checker names every order of a field set by one class (transform.canonical): the order written
            fields = class_fields(m[3])
            fields = tr.orders.get(frozenset(fields)) or PROJECT_ORDERS.get(frozenset(fields)) or fields
            got = Known(fields, is_open=m[2] == "_opn_")
            out[key] = got if out.get(key, got) == got else None  # two answers on a line that disagree
    return {k: v for k, v in out.items() if v is not None}


class Translation:
    """A .pyn source and its hidden Python, with LSP position mapping both ways."""

    def __init__(
        self,
        source: str,
        path: Path | None = None,
        probe: bool = False,
        probed: Probed | None = None,
        sites: bool = False,
    ):
        """path: the file, whose answers `byname tool` keeps (PROBED).
        probe: wrap each unseen spread source and rest in `reveal_type(...)` (transform's probe); probed: the
        checker's answers (probed_from), which make them known. Without, those `byname tool` has for this
        text (PROBED). sites: each record literal and record type has its own class (transform's), for a rename."""
        if probed is None and path is not None and (got := PROBED.get(path)) is not None and got[0] == source:
            probed = got[1]
        self.source = source
        self.probe = probe
        self.probed: Probed = probed or {}  # the answers it was translated with
        self.unseen: list[tuple[str, int]] = []  # see Result.unseen
        self.error: Exception | None = None
        self.problems: list[dict] = []  # byname diagnostics from tolerant translation
        self.fields: list[tuple[int, int]] = []  # source spans of record field names
        self.shorthands: list[tuple[int, int]] = []  # every bare `x=` (see Result.shorthands)
        self.type_fields: list[tuple[str, str, int, int]] = []  # see Result.type_fields
        self.reads: list[tuple[int, int, str]] = []  # see Result.declared_reads
        self.unknown_rests: list[tuple[int, int]] = []  # see Result.unknown_rests
        self.labels: set[int] = set()  # where a walrus labelling a returned tuple starts (see Result.labels)
        # a field set -> the order first written in the file; a name bound from a literal -> its order (display_orders)
        self.orders: dict[frozenset[str], tuple[str, ...]] = {}
        self.bound: dict[str, tuple[str, ...]] = {}
        try:
            r = transform(source, tolerant=True, checker=True, probed=probed, probe=probe, sites=sites)
            body, prelude, edits = r.body, r.prelude, r.edits
            self.fields = r.fields
            self.shorthands = r.shorthands
            self.type_fields = r.type_fields
            self.reads = r.declared_reads
            self.unknown_rests = r.unknown_rests
            self.unseen = r.unseen
            self.labels = {s for s, _ in r.labels}
            write_shapes(r.shapes)
            for m in WRITTEN_SHAPE_RE.finditer(body):  # in the source's order: the body keeps it
                self.orders.setdefault(frozenset(f := class_fields(m[1])), f)
            for k, v in self.orders.items():
                PROJECT_ORDERS.setdefault(k, v)
            self.bound = {m[1]: class_fields(m[2]) for m in BOUND_RE.finditer(body)}
        except Exception as e:  # mid-edit code: send it raw; the checker reports the syntax error
            self.error = e
            body, prelude, edits = source, "", []
        self.map = SourceMap(edits)
        self.at = prelude_offset(body) if prelude else 0
        self.plen = len(prelude)
        self.hidden = body[: self.at] + prelude + body[self.at :]
        if probe and self.unseen:
            self.hidden += ("" if self.hidden.endswith("\n") else "\n") + f"reveal_type({PROBE_MARK!r})\n"
        self.src_lines = LineIndex(source)
        self.hid_lines = LineIndex(self.hidden)
        # a probe's own `reveal_type(...)`s, by where the checker puts its answer (the argument): not the
        # user's, which are source text, and whose notes are shown
        self.reveals: set[tuple[int, int]] = set()
        for m in re.finditer(r"\breveal_type\(", self.hidden if probe else ""):
            hit = self._from_hidden(m.start(), False)
            if hit is not None and self.source.startswith("reveal_type(", hit[0]):
                continue
            pos = self.hid_lines.position(m.end())
            self.reveals.add((pos["line"], pos["character"]))
        # a probe's line whose comment would hide its answer: the comment blanked (same columns), and what
        # the checker says on that line not shown (lsp.Proxy.diagnostics), as the comment would have it
        self.unignored: set[int] = set()
        if self.reveals:
            lines = self.hidden.split("\n")
            for line in {n for n, _ in self.reveals}:
                if m := IGNORE.search(lines[line]):
                    lines[line] = lines[line][: m.start()] + " " * (m.end() - m.start()) + lines[line][m.end() :]
                    self.unignored.add(line)
            self.hidden = "\n".join(lines)
        if self.error is None:
            for s, e, msg in r.problems:
                rng = {"start": self.src_lines.position(s), "end": self.src_lines.position(e)}
                self.problems.append({"range": rng, "severity": 1, "source": "byname", "message": msg})

    @cached_property
    def hints(self) -> list[dict]:
        """`p["msg"]` where p's record type declares msg: a dot read is the checked one (lsp.quick_fix). The
        reads come from the translation's token pass (transform.declared_reads). And a `**rest` typed `(...)`,
        whose record byname can't tell the fields of (unknown_rest)."""
        out: list[dict] = []
        for a, b in self.unknown_rests:
            rng = {"start": self.src_lines.position(a), "end": self.src_lines.position(b)}
            message = unknown_rest(self.source[a:b].removeprefix("**").strip())
            out.append({"range": rng, "severity": 3, "source": "byname", "message": message})
        for a, b, field in self.reads:
            name = re.search(r"\w*$", self.source[:a])[0]  # `p`: the range starts right after it
            rng = {"start": self.src_lines.position(a), "end": self.src_lines.position(b)}
            message = f'"{field}" is declared on {name}\'s type: read it checked, {name}.{field}'
            out.append(
                {"range": rng, "severity": 3, "source": "byname", "message": message, "data": {"bynameChecked": field}}
            )
        return out

    def quiet(self, start: int, message: str) -> bool:
        """The checker (or Ruff's F841) calls a walrus that labels a returned tuple unused: it's bound to
        name a position. And `**rest` of a value that isn't a record: the checker says no overload of
        `_byname_omit_...` fits, and again which argument didn't (explain_fields words that one)."""
        if 'No overloads for "_byname_omit_' in message:
            return True
        return start in self.labels and ("is not accessed" in message or "is assigned to but never used" in message)

    def _to_hidden(self, off: int, end: bool, touch: bool = False) -> int:
        g = self.map.to_body(off, end, touch)
        return g + self.plen if g >= self.at and self.plen else g

    def _from_hidden(self, h: int, end: bool) -> tuple[int, Hit] | None:
        if self.plen and self.at <= h < self.at + self.plen and not (end and h == self.at):
            return None  # inside the prelude: no source counterpart
        g = h - self.plen if h >= self.at + self.plen and self.plen else h
        return self.map.to_source(g, end)

    def position_to_hidden(self, pos: dict) -> dict:
        off = self.src_lines.offset(pos["line"], pos["character"])
        return self.hid_lines.position(self._to_hidden(off, False, touch=True))

    def range_to_hidden(self, r: dict) -> dict:
        s = self.src_lines.offset(r["start"]["line"], r["start"]["character"])
        e = self.src_lines.offset(r["end"]["line"], r["end"]["character"])
        return {
            "start": self.hid_lines.position(self._to_hidden(s, False)),
            "end": self.hid_lines.position(self._to_hidden(e, e > s)),
        }

    def value_position(self, pos: dict) -> dict | None:
        """On the `x` of a shorthand `x=`: the hidden position of its implicit value `x`, else None.
        Go-to-definition uses it to jump to the local variable instead of the parameter/field."""
        off = self.src_lines.offset(pos["line"], pos["character"])
        for e, gs in zip(self.map.edits, self.map.gstart):
            if e.kind == "shorthand" and e.display[0] <= off <= e.display[1]:
                g = gs + min(off - e.display[0], len(e.text))
                return self.hid_lines.position(g + self.plen if g >= self.at and self.plen else g)
        return None

    def shorthand_from_hidden(self, r: dict) -> dict | None:
        """If the range is exactly the implicit local of a shorthand, `x` in `fn(x=x)` from `fn(x=)` or in
        `x = _ds.x` from `(x=) = r`, the range of the `x` written. Semantic tokens colour it as that local."""
        hs = self.hid_lines.offset(r["start"]["line"], r["start"]["character"])
        he = self.hid_lines.offset(r["end"]["line"], r["end"]["character"])
        if self.plen and self.at <= hs < self.at + self.plen:
            return None  # inside the prelude
        if self.plen and hs >= self.at + self.plen:
            hs, he = hs - self.plen, he - self.plen
        # the edits whose text could hold the range: several may start at one offset (empty ones)
        for k in range(bisect_right(self.map.gstart, hs) - 1, -1, -1):
            e, gs = self.map.edits[k], self.map.gstart[k]
            if gs + len(e.text) < he:
                break
            span = e.display if e.kind == "shorthand" and (hs, he) == (gs, gs + len(e.text)) else None
            for m in e.marks:
                if m.os == m.oe and m.display and (hs, he) == (gs + m.ts, gs + m.te):
                    span = m.display
            if span is not None:
                return {"start": self.src_lines.position(span[0]), "end": self.src_lines.position(span[1])}
        return None

    def split_shorthands(self, edits: list[dict]) -> list[dict]:
        """TextEdits already mapped to the source: keep each shorthand `x=` naming both of its symbols.

        `x=` is one text for two symbols, the label (parameter, field, attribute) and the implicit local.
        A rename of the local arrives as an insertion after `=` (see range_from_hidden), giving `x=new`.
        A rename of the label replaces the `x` written, which would silently rename the local too: `fn(x=)`
        -> `fn(new=)` passes whatever local is called `new`. So the old local is inserted after `=`,
        giving `new=x`. When one rename hits both halves (one symbol, `fn(n=)` inside `fn`), the insertion
        is dropped and the shorthand stays: `m=`, not `m=m`."""
        if not self.shorthands or not edits:
            return edits

        def span(e: dict) -> tuple[int, int]:
            r = e["range"]
            return (
                self.src_lines.offset(r["start"]["line"], r["start"]["character"]),
                self.src_lines.offset(r["end"]["line"], r["end"]["character"]),
            )

        spans = [span(e) if isinstance(e, dict) and "range" in e and "newText" in e else None for e in edits]
        out, drop = list(edits), set()
        for fs, fe in self.shorthands:
            eq = self.source.find("=", fe)
            if eq < 0:
                continue
            value_at = eq + 1
            label = next((k for k, s in enumerate(spans) if s == (fs, fe)), None)
            value = next((k for k, s in enumerate(spans) if s == (value_at, value_at)), None)
            if label is not None and value is not None:
                drop.add(value)
            elif label is not None:
                p = self.src_lines.position(value_at)
                out.append({"range": {"start": p, "end": p}, "newText": self.source[fs:fe]})
        return [e for k, e in enumerate(out) if k not in drop]

    def position_from_hidden(self, pos: dict) -> dict | None:
        hit = self._from_hidden(self.hid_lines.offset(pos["line"], pos["character"]), False)
        return None if hit is None else self.src_lines.position(hit[0])

    def range_from_hidden_exact(self, r: dict) -> dict | None:
        """Only if the range lies wholly on text the user wrote (semantic tokens); else None."""
        hs = self.hid_lines.offset(r["start"]["line"], r["start"]["character"])
        he = self.hid_lines.offset(r["end"]["line"], r["end"]["character"])
        if he <= hs:
            return None
        a = self._from_hidden(hs, False)
        b = self._from_hidden(he - 1, False)
        if a is None or b is None or generated(a[1]) or generated(b[1]):
            return None
        return {"start": self.src_lines.position(a[0]), "end": self.src_lines.position(b[0] + 1)}

    def only_generated(self, r: dict) -> bool:
        """The range is on text byname wrote that isn't a copy of what the user wrote there: `.name` in the
        reads a spread `(**u, x=1)` becomes (shown on `u`). A copy is the user's: a field written next to a
        spread, generated over its own name, and a shorthand's value half `x` (an insertion after `x=`)."""
        hs = self.hid_lines.offset(r["start"]["line"], r["start"]["character"])
        he = self.hid_lines.offset(r["end"]["line"], r["end"]["character"])
        a = self._from_hidden(hs, False)
        if a is None or (span := generated(a[1])) is None or (a[1] is not None and a[1][0].kind == "shorthand"):
            return False
        return self.source[span[0] : span[1]] != self.hidden[hs:he]

    def range_from_hidden(self, r: dict, display: bool = False) -> dict | None:
        """display=True (diagnostics): a range on generated text widens to the source it came from.
        display=False (edits, locations): it collapses to the insertion point, so a rename of `x`
        landing on the expanded half of `f(x=)` becomes `f(x=new)`."""
        hs = self.hid_lines.offset(r["start"]["line"], r["start"]["character"])
        he = self.hid_lines.offset(r["end"]["line"], r["end"]["character"])
        a = self._from_hidden(hs, False)
        if display:  # map the last character, so the end lands in the same piece as the text it ends
            b = self._from_hidden(max(he - 1, hs), False)
        else:
            b = self._from_hidden(he, he > hs)
        if a is None or b is None:
            return None
        (s, hit_s), (e, hit_e) = a, b
        if display:
            s = span[0] if (span := generated(hit_s)) else s
            e = span[1] if (span := generated(hit_e)) else e + (he > hs)
        e = max(e, s)
        return {"start": self.src_lines.position(s), "end": self.src_lines.position(e)}
