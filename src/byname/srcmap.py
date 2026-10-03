"""Position mapping between a .pyn file and its plain-Python translation.

Three coordinate systems:
    source  the .pyn text
    body    source with edits applied (same line count)
    hidden  body with the record prelude inserted; what the type checker sees

LSP positions are (line, UTF-16 column), so LineIndex converts to and from offsets.
"""

from .transform import Edit, Mark, prelude_offset, transform

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


class Translation:
    """A .pyn source and its hidden Python, with LSP position mapping both ways."""

    def __init__(self, source: str):
        self.source = source
        self.error: Exception | None = None
        self.problems: list[dict] = []  # byname diagnostics from tolerant translation
        self.fields: list[tuple[int, int]] = []  # source spans of record field names
        try:
            r = transform(source, tolerant=True)
            body, prelude, edits = r.body, r.prelude, r.edits
            self.fields = r.fields
        except Exception as e:  # mid-edit code: send it raw; the checker reports the syntax error
            self.error = e
            body, prelude, edits = source, "", []
        self.map = SourceMap(edits)
        self.at = prelude_offset(body) if prelude else 0
        self.plen = len(prelude)
        self.hidden = body[: self.at] + prelude + body[self.at :]
        self.src_lines = LineIndex(source)
        self.hid_lines = LineIndex(self.hidden)
        if self.error is None:
            for s, e, msg in r.problems:
                rng = {"start": self.src_lines.position(s), "end": self.src_lines.position(e)}
                self.problems.append({"range": rng, "severity": 1, "source": "byname", "message": msg})

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
