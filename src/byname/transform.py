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


@dataclass
class Result:
    prelude: str
    body: str
    edits: list[Edit] = field(default_factory=list)


def record_class(fields: tuple[str, ...]) -> str:
    return "_rec_" + "__".join(fields)


def record_def(fields: tuple[str, ...]) -> str:
    params = ", ".join(f"T{i}" for i in range(len(fields)))
    body = "; ".join(f"{f}: T{i}" for i, f in enumerate(fields))
    return f"class {record_class(fields)}[{params}](_NT): {body}\n"


def transform(src: str, path: str = "<pyn>") -> Result:
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

    def span(a: int, b: int | None = None) -> tuple[int, int]:
        return off(toks[a].start), off(toks[a if b is None else b].end)

    def expand_shorthand(its: list[list[int]]) -> None:
        for it in its:
            if is_short(it):
                at = off(toks[it[1]].end)
                edits.append(Edit(at, at, toks[it[0]].string, span(it[0])))

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

        if not its or not all(is_kw(it) for it in its):
            continue  # ordinary parenthesised expression / tuple / genexp

        fields = tuple(toks[it[0]].string for it in its)
        close = pair[i]
        at_stmt_start = prev is None or prev.type in STMT_START or prev.string == ";"
        nxt = toks[close + 1]
        if at_stmt_start and nxt.string == "=":
            # (field=) binds local `field`; (field=target) binds `target`
            targets = []
            for it in its:
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
            edits.append(Edit(*group, DS, group))
            edits.append(Edit(stmt_end, stmt_end, binds, group, marks))
            continue

        for it in its:
            name = toks[it[0]]
            if name.string.startswith("_"):
                raise err(f"record field {name.string!r} cannot start with '_'", name)
        if len(set(fields)) != len(fields):
            raise err(f"duplicate record field in {fields}", t)
        records[fields] = None
        at = off(t.start)
        edits.append(Edit(at, at, record_class(fields), (at, at + 1)))
        expand_shorthand(its)

    # apply edits back-to-front
    edits.sort(key=lambda e: (e.start, e.end))
    body = src
    for e in reversed(edits):
        body = body[: e.start] + e.text + body[e.end :]

    prelude = ""
    if records:
        prelude = "from typing import NamedTuple as _NT\n" + "".join(record_def(f) for f in records)
    return Result(prelude, body, edits)


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


def to_python(src: str, path: str = "<pyn>") -> str:
    """Single plain-Python file (prelude inlined after docstring/__future__). For reading and type-checking."""
    r = transform(src, path)
    at = prelude_offset(r.body)
    return r.body[:at] + r.prelude + r.body[at:]
