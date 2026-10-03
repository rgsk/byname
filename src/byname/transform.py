"""Source transform: .pyn -> plain Python.

Everything is built on the `name=` shorthand from PEP 736 (rejected):

    fn(name=, age=)           call         -> fn(name=name, age=age)
    (name=, age=)             record       -> _rec_name__age(name=name, age=age)
    (name=, age=) = expr      destructure  -> _ds = expr; name = _ds.name; age = _ds.age
    (name=n) = expr           rename       -> _ds = expr; n = _ds.name

Left of `=` is always the field, right is always the local, in all three forms.

Records are generic NamedTuples, so pyright infers `fn() -> _rec_name__age[str, int]`
with no annotations. Valid Python is never changed: each form is a SyntaxError today.
The body keeps the input's line count; record classes go in a separate prelude.
"""

import ast
import io
import keyword
import tokenize
from dataclasses import dataclass, field

DS = "_ds"
SHORT, PAT, TYP = "__p", "__P", "__T"  # formatting stand-ins: `x=` -> `x=__p`, record `(` -> `__P(`, pattern -> `__P[...]`, record type -> `__T[...]`
REPR = "_byname_repr"
PRELUDE = (
    "from typing import NamedTuple as _NT\n"
    f"def {REPR}(self) -> str: return '(' + ', '.join(f'{{k}}={{v!r}}' for k, v in zip(self._fields, self)) + ')'\n"
)
STMT_START = {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}


@dataclass
class Mark:
    """A span of an Edit's text that stands for a span of the original source."""

    ts: int  # span in Edit.text
    te: int
    os: int  # span in the source; os == oe means "insert here" (e.g. a shorthand target)
    oe: int
    display: tuple[int, int] | None = None  # where to show diagnostics, if not (os, oe)


@dataclass
class Edit:
    start: int  # source offsets replaced by text
    end: int
    text: str
    display: tuple[int, int]  # source span to show diagnostics on generated text
    marks: list[Mark] = field(default_factory=list)
    kind: str = ""  # "shorthand": the value half of `x=`; display is the `x` the user wrote


@dataclass
class Result:
    prelude: str
    body: str
    edits: list[Edit] = field(default_factory=list)
    problems: list[tuple[int, int, str]] = field(default_factory=list)  # (start, end, message), tolerant mode
    standins: list[tuple[int, int, str]] = field(default_factory=list)  # (start, end, text): plain-Python stand-ins, for formatting
    fields: list[tuple[int, int]] = field(default_factory=list)  # source spans of record field names, for highlighting


def record_class(fields: tuple[str, ...]) -> str:
    return "_rec_" + "__".join(fields)


def record_def(fields: tuple[str, ...], portable: bool = False) -> str:
    """portable: plain `class R(_NT):` with `object` fields, which runs on Python 3.6+ (judges run PyPy 3.10).
    Otherwise a 3.12 generic class, so checkers infer each field's type."""
    params = ", ".join(f"T{i}" for i in range(len(fields)))
    body = "; ".join(f"{f}: {'object' if portable else f'T{i}'}" for i, f in enumerate(fields))
    if portable:  # record types annotate as R[int, str]; the plain class ignores the subscript
        body += "; __class_getitem__ = classmethod(lambda cls, _: cls)"
    head = record_class(fields) if portable else f"{record_class(fields)}[{params}]"
    # a real method, not `__repr__ = helper`: mypy rejects assignments in a NamedTuple body
    return (
        f"class {head}(_NT):\n"
        f"    {body}\n"
        f"    def __repr__(self) -> str: return {REPR}(self)\n"
    )


def transform(src: str, path: str = "<pyn>", tolerant: bool = False, portable: bool = False) -> Result:
    """tolerant (editor only): a half-typed pattern item like `na` in `(name=, na) = r` becomes
    `_ds.na` instead of an error, so the checker can complete field names there."""
    toks = [
        t
        for t in tokenize.generate_tokens(io.StringIO(src).readline)
        if t.type not in (tokenize.COMMENT, tokenize.NL)
    ]

    # matching bracket for every opener
    pair: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(toks):
        if t.type == tokenize.OP and t.string in "([{":
            stack.append(i)
        elif t.type == tokenize.OP and t.string in ")]}":
            pair[stack.pop()] = i

    line_starts = [0]
    for line in src.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    def off(rc: tuple[int, int]) -> int:
        return line_starts[rc[0] - 1] + rc[1]

    def err(msg: str, t: tokenize.TokenInfo) -> SyntaxError:
        return SyntaxError(msg, (path, t.start[0], t.start[1] + 1, t.line))

    def is_name(t: tokenize.TokenInfo) -> bool:
        return t.type == tokenize.NAME and not keyword.iskeyword(t.string)

    def items(open_i: int) -> list[list[int]]:
        # top-level comma-separated items inside a bracket, as token index lists
        out, cur, j = [], [], open_i + 1
        while j < pair[open_i]:
            if toks[j].string == ",":
                out.append(cur)
                cur = []
            else:
                cur.append(j)
                if j in pair:  # skip over nested brackets
                    cur.extend(range(j + 1, pair[j] + 1))
                    j = pair[j]
            j += 1
        if cur:
            out.append(cur)
        return out

    def is_kw(item: list[int]) -> bool:
        return len(item) >= 2 and is_name(toks[item[0]]) and toks[item[1]].string == "="

    def is_short(item: list[int]) -> bool:
        return len(item) == 2 and is_kw(item)

    edits: list[Edit] = []
    records: dict[tuple[str, ...], None] = {}  # ordered set
    problems: list[tuple[int, int, str]] = []
    standins: list[tuple[int, int, str]] = []
    field_spans: list[tuple[int, int]] = []  # every record field name: in records, patterns and record types

    def span(a: int, b: int | None = None) -> tuple[int, int]:
        return off(toks[a].start), off(toks[a if b is None else b].end)

    def expand_shorthand(its: list[list[int]]) -> None:
        for it in its:
            if is_short(it):
                at = off(toks[it[1]].end)
                edits.append(Edit(at, at, toks[it[0]].string, span(it[0]), kind="shorthand"))
                standins.append((at, at, SHORT))

    for i, t in enumerate(toks):
        if not (t.type == tokenize.OP and t.string == "("):
            continue
        prev = toks[i - 1] if i else None
        prev2 = toks[i - 2] if i > 1 else None
        its = items(i)

        # def f(...) / class C(...): parameter lists, leave alone
        if prev2 is not None and prev2.string in ("def", "class"):
            continue

        is_call = prev is not None and (is_name(prev) or prev.string in (")", "]"))
        if is_call:
            expand_shorthand(its)
            continue

        if not its:
            continue
        close = pair[i]

        # record type, e.g. `-> (height: int, diameter: int)`: every item is `name: type`, never valid
        # Python. Becomes the generic class, R[int, int]; separate small edits, so nested types work
        if all(len(it) >= 3 and is_name(toks[it[0]]) and toks[it[1]].string == ":" for it in its):
            fields = tuple(toks[it[0]].string for it in its)
            for it in its:
                name = toks[it[0]]
                if name.string.startswith("_"):
                    raise err(f"record field {name.string!r} cannot start with '_'", name)
            if len(set(fields)) != len(fields):
                raise err(f"duplicate record field in {fields}", t)
            records[fields] = None
            group = span(i, close)
            edits.append(Edit(group[0], group[0] + 1, record_class(fields) + "[", group))
            edits.append(Edit(group[1] - 1, group[1], "]", group))
            standins.append((group[0], group[0] + 1, TYP + "["))
            standins.append((group[1] - 1, group[1], "]"))
            for it in its:
                edits.append(Edit(off(toks[it[0]].start), off(toks[it[2]].start), "", span(it[0])))
                field_spans.append(span(it[0]))
            continue
        at_stmt_start = prev is None or prev.type in STMT_START or prev.string == ";"
        is_pattern = at_stmt_start and toks[close + 1].string == "="
        kw = [is_kw(it) for it in its]
        bare = [len(it) == 1 and is_name(toks[it[0]]) for it in its]
        if not all(kw):
            # a pattern with some `field=` items and some bare names: half-typed, or a mistake
            if not (is_pattern and any(kw) and all(k or b for k, b in zip(kw, bare))):
                continue  # ordinary parenthesised expression / tuple / genexp
            name = toks[its[bare.index(True)][0]]
            if not tolerant:
                raise err(f"pattern item {name.string!r} needs '=': write {name.string}= or {name.string}=target", name)

        fields = tuple(toks[it[0]].string for it in its)
        if is_pattern:
            # (field=) binds local `field`; (field=target) binds `target`; bare `field` (tolerant) binds nothing
            targets = []
            for it, b in zip(its, bare):
                if b:
                    problems.append((*span(it[0]), f"pattern item {toks[it[0]].string!r} needs '='"))
                    targets.append(None)
                    continue
                if is_short(it):
                    targets.append(toks[it[0]].string)
                    continue
                text = src[off(toks[it[2]].start) : off(toks[it[-1]].end)]
                if not is_target(text):
                    raise err(f"cannot bind field {toks[it[0]].string!r} to {text!r}", toks[it[2]])
                targets.append(text)
            # end of statement: NEWLINE or ';' at depth 0
            j = close + 2
            while toks[j].type not in (tokenize.NEWLINE, tokenize.ENDMARKER) and toks[j].string != ";":
                j = pair.get(j, j) + 1
            group = span(i, close)
            binds, marks = "", []
            for it, f, tg in zip(its, fields, targets):
                binds += "; "
                if tg is None:  # half-typed item: just the attribute access, for completion
                    binds += f"{DS}."
                    marks.append(Mark(len(binds), len(binds) + len(f), *span(it[0])))
                    binds += f
                    continue
                ts = len(binds)
                binds += tg
                if is_short(it):  # target is implicit: insertion point right after `field=`
                    at = off(toks[it[1]].end)
                    marks.append(Mark(ts, len(binds), at, at, span(it[0])))
                else:
                    marks.append(Mark(ts, len(binds), *span(it[2], it[-1])))
                binds += f" = {DS}."
                marks.append(Mark(len(binds), len(binds) + len(f), *span(it[0])))
                binds += f
            stmt_end = off(toks[j - 1].end)
            # stand-in: __P[a:__p, b:target] = expr (a subscript is a valid assignment target)
            standins.append((group[0], group[0] + 1, PAT + "["))
            standins.append((group[1] - 1, group[1], "]"))
            for it in its:
                if len(it) >= 2 and toks[it[1]].string == "=":
                    standins.append((*span(it[1]), ":" + (SHORT if is_short(it) else "")))
            edits.append(Edit(*group, DS, group))
            edits.append(Edit(stmt_end, stmt_end, binds, group, marks))
            field_spans.extend(span(it[0]) for it, b in zip(its, bare) if not b)
            continue

        for it in its:
            name = toks[it[0]]
            if name.string.startswith("_"):
                raise err(f"record field {name.string!r} cannot start with '_'", name)
        if len(set(fields)) != len(fields):
            raise err(f"duplicate record field in {fields}", t)
        records[fields] = None
        field_spans.extend(span(it[0]) for it in its)
        at = off(t.start)
        edits.append(Edit(at, at, record_class(fields), (at, at + 1)))
        standins.append((at, at, PAT))
        expand_shorthand(its)

    # apply edits back-to-front
    edits.sort(key=lambda e: (e.start, e.end))
    body = src
    for e in reversed(edits):
        body = body[: e.start] + e.text + body[e.end :]

    prelude = ""
    if records:
        prelude = PRELUDE + "".join(record_def(f, portable) for f in records)
    standins.sort()
    return Result(prelude, body, edits, problems, standins, sorted(field_spans))


def pattern_slot(src: str, at: int) -> tuple[int, int, int, list[str]] | None:
    """If offset `at` is a field position in a destructuring pattern `(...) = expr` (an empty item,
    or the field name being typed): (word_start, word_end, close_paren, fields_already_listed).
    Used for completion; also matches `(x|) = y`, which is plain Python but a pattern in the making."""
    try:
        toks = [
            t
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.COMMENT, tokenize.NL)
        ]
    except (tokenize.TokenError, SyntaxError):
        return None
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def off(rc: tuple[int, int]) -> int:
        return starts[rc[0] - 1] + rc[1]

    pair: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(toks):
        if t.type == tokenize.OP and t.string in "([{":
            stack.append(i)
        elif t.type == tokenize.OP and t.string in ")]}" and stack:
            pair[stack.pop()] = i

    for i, t in enumerate(toks):
        if t.string != "(" or i not in pair:
            continue
        prev = toks[i - 1] if i else None
        close = pair[i]
        if not (prev is None or prev.type in STMT_START or prev.string == ";"):
            continue
        if close + 1 >= len(toks) or toks[close + 1].string != "=":
            continue
        lo, hi = off(t.end), off(toks[close].start)
        if not lo <= at <= hi:
            continue
        items, start, cur, j = [], lo, [], i + 1  # (start, end, token indices) per top-level item
        while j < close:
            if toks[j].string == ",":
                items.append((start, off(toks[j].start), cur))
                start, cur = off(toks[j].end), []
            else:
                cur.append(j)
                if j in pair:
                    if off(toks[j].start) < at <= off(toks[pair[j]].start):
                        return None  # inside a nested bracket
                    cur.extend(range(j + 1, pair[j] + 1))
                    j = pair[j]
            j += 1
        items.append((start, hi, cur))
        listed = [toks[it[0]].string for _, _, it in items if len(it) >= 2 and toks[it[1]].string == "="]
        for s, e, it in items:
            if not s <= at <= e:
                continue
            if not it:
                return at, at, hi, listed
            name = toks[it[0]]
            on_name = name.type == tokenize.NAME and off(name.start) <= at <= off(name.end)
            if on_name and (len(it) == 1 or toks[it[1]].string == "="):
                return off(name.start), off(name.end), hi, [f for f in listed if f != name.string]
            return None
    return None


def is_target(text: str) -> bool:
    """A plain assignment target: name, attribute or subscript (no nested patterns)."""
    try:
        stmt = ast.parse(f"{text} = 0").body[0]
    except SyntaxError:
        return False
    return isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], (ast.Name, ast.Attribute, ast.Subscript))


def prelude_index(tree: ast.Module) -> int:
    """Index of the first statement after the docstring and __future__ imports."""
    i = 0
    body = tree.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        i = 1
    while i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__":
        i += 1
    return i


def to_code(src: str, path: str):
    """Compile .pyn source. Body line numbers match the .pyn file exactly."""
    r = transform(src, path)
    tree = ast.parse(r.body, path)
    if r.prelude:
        k = prelude_index(tree)
        tree.body[k:k] = ast.parse(r.prelude).body
    return compile(tree, path, "exec", dont_inherit=True)


def prelude_offset(body: str) -> int:
    """Offset in body where the prelude goes: the line after the docstring/__future__ imports."""
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return 0  # mid-edit; top of file is good enough
    k = prelude_index(tree)
    if not k:
        return 0
    lines = body.splitlines(keepends=True)
    return sum(len(line) for line in lines[: tree.body[k - 1].end_lineno])


def to_python(src: str, path: str = "<pyn>", divider: str = "", portable: bool = False) -> str:
    """Single plain-Python file (prelude inlined after docstring/__future__). For reading and type-checking.
    divider: a line put between the generated prelude and the user's code (always, even with no prelude,
    so every output file has the same shape). portable: record classes that run on Python 3.6+ (see record_def)."""
    r = transform(src, path, portable=portable)
    at = prelude_offset(r.body)
    gap = divider + "\n" if divider else ""
    return r.body[:at] + r.prelude + gap + r.body[at:]
