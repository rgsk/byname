"""Source transform: .pyn -> plain Python.

Everything is built on the `name=` shorthand from PEP 736 (rejected):

    fn(name=, age=)           call         -> fn(name=name, age=age)
    (name=, age=)             record       -> _rec_name__age(name=name, age=age)
    (name=, age=) = expr      destructure  -> _ds = expr; name = _ds.name; age = _ds.age
    (name=n) = expr           rename       -> _ds = expr; n = _ds.name
    for (name=, age=) in xs:  loop         -> for (name, age) in ((_ds.name, _ds.age) for _ds in xs):
    (user=(name=)) = expr     nested       -> _ds = expr; name = _ds.user.name

Left of `=` is always the field, right is always the local, in all three forms.

Records are read by name only: field order never matters. At runtime a record is a NamedTuple that
stores its fields sorted by name (so equality and hashing ignore the written order) and shows them in the
order written; checkers see a plain generic class with no tuple face, so pyright infers
`fn() -> _rec_name__age[str, int]` with no annotations and rejects `a, b = rec`, `rec[0]` and `*rec`.
At runtime those raise too, as does ordering (`sorted(recs)`); `"name" in rec` asks for a field name.
Valid Python is never changed: each form is a SyntaxError today.
The body keeps the input's line count; record classes go in a separate prelude.
"""

import ast
import bisect
import io
import keyword
import tokenize
from dataclasses import dataclass, field

DS = "_ds"
SHORT, PAT, TYP = "__p", "__P", "__T"  # formatting stand-ins: `x=` -> `x=__p`, record `(` -> `__P(`, pattern -> `__P[...]`, record type -> `__T[...]`
PARAM = "__D"  # formatting stand-in for a parameter pattern: `(a=): T` -> `__D: (__P[a:__p], T)`
PARAM_NAME = "_byname_p"  # + index: the parameter a parameter pattern stands for
REPR = "_byname_repr"
ORDER = "_byname_order"  # a record class's fields in the order written; `_fields` is the sorted storage order
PRELUDE = (
    "from typing import NamedTuple as _NT\n"
    f"def {REPR}(self) -> str: return '(' + ', '.join(f'{{k}}={{getattr(self, k)!r}}' for k in self.{ORDER}) + ')'\n"
    # records are mappings by field name at runtime, so `f(**rec)` and `{**rec}` work (see RESERVED)
    f"def _byname_keys(self): return self.{ORDER}\n"
    "def _byname_item(self, k):\n"
    "    if not isinstance(k, str): _byname_positional(self)\n"
    f"    if k in self.{ORDER}: return getattr(self, k)\n"
    "    raise KeyError(k)\n"
    f"def _byname_has(self, k): return k in self.{ORDER}\n"
    # Records are read by name only, at runtime too: the tuple underneath stores the fields sorted by name, so
    # iterating, indexing, ordering or concatenating would expose an order nobody wrote (`a, b = (name=, age=)`
    # gave age, name). pickle, copy and _replace iterate in NamedTuple's own versions, hence their own here.
    "def _byname_positional(self, *_):\n"
    "    raise TypeError(f'{type(self).__name__} is read by name only: use .field, keys() or _asdict()')\n"
    "def _byname_unordered(self, o): return NotImplemented\n"
    "def _byname_args(self): return tuple.__getitem__(self, slice(None))\n"
    "def _byname_replace(self, **kw):\n"
    "    r = self._make([kw.pop(f, getattr(self, f)) for f in self._fields])\n"
    "    if kw: raise ValueError(f'Got unexpected field names: {list(kw)!r}')\n"
    "    return r\n"
    f"def _byname_asdict(self): return {{k: getattr(self, k) for k in self.{ORDER}}}\n"
    # Equal: the same field set with the same values, whatever order each was written in. Storage is sorted
    # by name, so that is tuple equality between two records with the same `_fields`. Never equal to a plain
    # tuple (a record is a tuple subclass, so its reflected __eq__ runs first for `(1, 2) == rec` too). Other
    # modules have their own record classes, hence `_fields`, not the class. Hashing stays tuple's.
    "def _byname_eq(self, o):\n"
    "    if type(o) is type(self): return tuple.__eq__(self, o)\n"
    f"    if isinstance(o, tuple): return hasattr(o, {ORDER!r}) and self._fields == o._fields and tuple.__eq__(self, o)  # pyright: ignore\n"
    "    return NotImplemented\n"
    "def _byname_ne(self, o):\n"
    "    r = _byname_eq(self, o)\n"
    "    return r if r is NotImplemented else not r\n"
    # set after the class is made: NamedTuple refuses `_asdict` in a class body, and a class body defining
    # __eq__ loses tuple's (C) __hash__
    "def _byname_setup(c, order):\n"
    f"    c.{ORDER}, c.__repr__, c.keys, c.__getitem__ = order, {REPR}, _byname_keys, _byname_item\n"
    "    c.__eq__, c.__ne__, c._asdict = _byname_eq, _byname_ne, _byname_asdict\n"
    "    c.__contains__ = _byname_has\n"
    "    c.__iter__ = c.__add__ = c.__mul__ = c.__rmul__ = _byname_positional\n"
    "    c.__lt__ = c.__le__ = c.__gt__ = c.__ge__ = _byname_unordered\n"
    "    c.__getnewargs__, c._replace = _byname_args, _byname_replace\n"
)
# basedpyright infers a mixed list like [(age="90"), (age=23)] as list[Unknown], which switches off every
# check on what comes out of it. Strict inference gives list[A | B]. A comment, not a config setting: the
# checker runs in the user's project, and these aren't language-server settings.
CHECKER_DIRECTIVE = "# pyright: strictListInference=true, strictDictionaryInference=true, strictSetInference=true\n"
TYPED_PRELUDE = (  # checker-facing typing: typed methods, record types as Protocols
    "import typing as _t\n"
    "from typing import TypedDict as _TD, Protocol as _PR, Literal as _L, Self as _S, Final as _Fi, Any as _Ay\n"
)
# Spreads: `**rec` in a call or dict display, and records built from spreads, `(**u, **r, age=27)`.
# At runtime records are mappings by field name (`keys()` and `rec["name"]`), so Python's own `**` works
# on them and valid Python is never rewritten. For the checker, `**x` goes through _byname_kw (a record
# becomes its typed `_asdict()` TypedDict, a dict passes through), checker translation only.
# A spread record is built from a dict display, so a later field wins and keeps the first one's position,
# like `{**a, **b}`. The checker sees `_byname_ctx(lambda t: _byname_check(lambda: t)({**u, 'age': 27}))`:
# the lambda's parameter gets the type expected where the record stands (`fn(x)`, `fn(a=x)`, `p: T = x`,
# `return x` under `-> T`, ...), and the dict is checked against that type's `_asdict()` TypedDict. Not a
# generic `def f[T](d: Dict[T]) -> T`: pyright retries a call that fails with its expected type without it,
# so the error would vanish; one inside a lambda's body doesn't fail the call. With no type expected (or an
# overloaded callee) it's unchecked. Inline `(name: str)(**u)` checks against the type it names (`cast(T, …)`
# makes the checker read T as a type).
RESERVED = {"keys"}  # record methods that make `**rec` work
HASDICT = (
    "from typing import overload as _ov, Any as _A, Protocol as _PR\n"
    "class _byname_HasDict[D](_PR):\n"
    "    def _asdict(self) -> D: ...\n"
)
KW_PRELUDE = (  # checker only
    "@_ov\ndef _byname_kw[D](x: _byname_HasDict[D], /) -> D: ...  # pyright: ignore\n"
    "@_ov\ndef _byname_kw[M](x: M, /) -> M: ...  # pyright: ignore\n"
    "def _byname_kw(x: _A, /) -> _A: return x._asdict() if hasattr(x, '_asdict') else x  # pyright: ignore\n"
)
BUILD_PRELUDE = (
    "from typing import cast as _cast\n"
    "from collections.abc import Callable as _Cl, Mapping as _Mp\n"
    "from collections import namedtuple as _ntf\n"
    "@_ov\ndef _byname_check[D](t: _Cl[[], _byname_HasDict[D]], /) -> _Cl[[D], D]: ...  # pyright: ignore\n"
    "@_ov\ndef _byname_check(t: _Cl[[], object], /) -> _Cl[[_A], _A]: ...  # pyright: ignore\n"
    "def _byname_check(t: _A, /) -> _A: return _byname_same  # pyright: ignore\n"
    "def _byname_same(d: _A, /) -> _A: return d\n"
    "_byname_anyv: _A = None\n"
)
CTX_PRELUDE = (  # checker only: T is the type expected where the record is built, see the comment above
    # the checker reads typing_extensions from its bundled stubs; the package needn't be installed
    "from typing_extensions import TypeVar as _TV  # pyright: ignore[reportMissingModuleSource]\n"
    "def _byname_ctx[T](f: _Cl[[T], object], /) -> T: ...  # pyright: ignore\n"
    "def _byname_pick[V](x: object, v: V, /) -> V: ...  # pyright: ignore\n"  # x: the user's name, v: its narrowed copy
    # a call argument: a record type (or None) expected, or else _byname_AnyRec, which a non-record
    # overload `f(x: int)` rejects, so the checker moves on to the overload that takes a record
    "class _byname_AnyRec:\n"
    "    def _asdict(self) -> dict[str, _A]: ...  # pyright: ignore\n"
    "    def keys(self) -> tuple[str, ...]: ...  # pyright: ignore\n"
    "    def __len__(self) -> int: ...  # pyright: ignore\n"
    "    def __getattr__(self, name: str, /) -> _A: ...\n"
    "_byname_R = _TV('_byname_R', bound=_byname_HasDict[_A] | None, default=_byname_AnyRec)\n"
    "def _byname_arg(f: _Cl[[_byname_R], object], /) -> _byname_R: ...  # pyright: ignore\n"
)
BUILD_PORTABLE = (  # what runs in output files: no typing
    "from typing import cast as _cast\n"
    "from collections import namedtuple as _ntf\n"
    "def _byname_same(d): return d\n"
    "def _byname_check(t): return _byname_same\n"
    "_byname_anyv = None\n"
)
BUILD_REC = (  # build a record from a dict: one class per field tuple, made on first use
    "_byname_cls = {}\n"
    "{sig}\n"
    "    k = tuple(d)\n"
    "    c = _byname_cls.get(k)\n"
    "    if c is None:\n"
    "        c = _byname_cls[k] = _ntf('_rec_' + '__'.join(k), sorted(k))\n"
    "        _byname_setup(c, k)\n"
    "    return c(**d)\n"
)
FIELDSET = "_byname_fieldset"  # a record's field names, sorted: what an exact record type matches on
STMT_START = {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}


class BynameError(SyntaxError):
    """A byname rule broken (e.g. shorthand in a parameter's default), as opposed to Python that doesn't
    tokenize. The editor shows only this then: the checker, sent the raw .pyn, would echo it as noise."""


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
    inserted: list[int] = field(default_factory=list)  # source lines that got a generated line before them (see to_code)
    labels: list[tuple[int, int]] = field(default_factory=list)  # source spans of walrus names labelling a returned tuple


def field_error(name: str) -> str:
    if name in RESERVED:
        return f"record field {name!r} is reserved: records have a {name}() method, which lets `**record` work"
    return f"record field {name!r} cannot start with '_'"


def record_class(fields: tuple[str, ...]) -> str:
    return "_rec_" + "__".join(fields)


def dict_class(fields: tuple[str, ...]) -> str:
    """The TypedDict a record's `_asdict()` returns, as the checker sees it."""
    return "_dct_" + "__".join(fields)


def type_class(fields: tuple[str, ...], is_open: bool) -> str:
    """An explicit record type: `(name: str, age: int)` is _typ_..., `(..., name: str, age: int)` is _opn_...,
    `(...)` is _opn_ (no fields)."""
    return ("_opn_" if is_open else "_typ_") + "__".join(fields)


def key_alias(fields: tuple[str, ...]) -> str:
    """The key type of `rec["name"]`: str, aliased per field set so that the checker's complaint about
    `rec[0]` names the record's fields (see POSITIONAL_RE in lsp.py)."""
    return "_key_" + "__".join(fields)


def tuple_class(labels: tuple[str, ...]) -> str:
    """A tuple returned by name, checker only: `return x, y.to(d)` is _tup_x__ (see label_returns)."""
    return "_tup_" + "__".join(labels)


def tuple_maker(labels: tuple[str, ...]) -> str:
    return "_byname" + tuple_class(labels)


def tuple_def(labels: tuple[str, ...]) -> str:
    """A tuple subclass, so unpacking, indexing and assigning to `tuple[...]` work as on the tuple it is at
    runtime, with no attributes: the labels are only the class's name, which hover shows."""
    params = ", ".join(f"T{i}" for i in range(len(labels)))
    args = ", ".join(f"a{i}: T{i}" for i in range(len(labels)))
    return (
        f"class {tuple_class(labels)}[{params}](tuple[{params}]): ...\n"
        f"def {tuple_maker(labels)}[{params}]({args}, /) -> {tuple_class(labels)}[{params}]: ...  # pyright: ignore[reportReturnType]\n"
    )


def fieldset(fields: tuple[str, ...]) -> str:
    return ",".join(sorted(fields))


def dict_def(fields: tuple[str, ...]) -> str:
    params = ", ".join(f"T{i}" for i in range(len(fields)))
    body = "; ".join(f"{f}: T{i}" for i, f in enumerate(fields))
    return f"class {dict_class(fields)}[{params}](_TD):\n    {body}\n"


def type_def(fields: tuple[str, ...], is_open: bool, portable: bool = False) -> str:
    """An explicit record type as a Protocol, so field order doesn't matter: inferred records keep their
    written order, explicit types match any order. Exact types also require the same field set (FIELDSET),
    so an extra field is an error; open types (`...`) accept records with at least these fields.
    `rec["name"]` is the unchecked read: any string key, typed Any; `rec.name` is the checked one."""
    name = type_class(fields, is_open)
    if portable:  # annotations are evaluated on old Pythons: a subscriptable stand-in is all they need
        return f"class {name}: __class_getitem__ = classmethod(lambda cls, _: cls)\n"
    params = ", ".join(f"T{i}" for i in range(len(fields)))
    out = f"class {name}[{params}](_PR):\n" if fields else f"class {name}(_PR):\n"
    # read-only fields, so records (immutable) match; Final rather than @property, so `p.age` is coloured
    # like a record field (see remap_tokens). Checkers object to a type variable in Final: silenced.
    for i, f in enumerate(fields):
        out += f"    {f}: _Fi[T{i}]  # pyright: ignore\n"
    out += f"    def __getitem__(self, k: {key_alias(fields)}, /) -> _Ay: ...\n"  # a str key, never a position
    out += "    def __contains__(self, k: object, /) -> bool: ...\n"  # a field name
    out += "    def keys(self) -> tuple[str, ...]: ...\n"  # what `**rec` reads
    if is_open:
        return out
    kw = ", ".join(f"{f}: T{i} = ..." for i, f in enumerate(fields))
    return out + (  # no __iter__, and __getitem__ takes no position: a record is read by name (see record_def)
        f"    @property\n    def {FIELDSET}(self) -> _L[{fieldset(fields)!r}]: ...\n"
        f"    def __len__(self) -> int: ...\n"
        "    @property\n    def _fields(self) -> tuple[str, ...]: ...\n"
        f"    def _replace(self, *, {kw}) -> _S: ...\n"
        f"    def _asdict(self) -> {dict_class(fields)}[{params}]: ...\n"
    )


def record_def(fields: tuple[str, ...], portable: bool = False) -> str:
    """portable: plain `class R(_NT):` with `object` fields, which runs on Python 3.6+ (judges run PyPy 3.10).
    Otherwise a 3.12 generic class, so checkers infer each field's type.

    The runtime class stores the fields sorted by name, so records with the same fields are equal and hash
    alike in any written order (see _byname_eq); _byname_setup gives it the written order for repr, keys()
    and _asdict(). Positional access and ordering raise at runtime (they would read the sorted order, which
    nobody wrote); the checkers' version rejects them."""
    params = ", ".join(f"T{i}" for i in range(len(fields)))
    typevar = {f: f"T{i}" for i, f in enumerate(fields)}
    body = "; ".join(f"{f}: {'object' if portable else typevar[f]}" for f in sorted(fields))
    if portable:  # record types annotate as R[int, str]; the plain class ignores the subscript
        body += "; __class_getitem__ = classmethod(lambda cls, _: cls)"
    head = record_class(fields) if portable else f"{record_class(fields)}[{params}]"
    out = f"class {head}(_NT):\n    {body}\n_byname_setup({record_class(fields)}, {fields!r})\n"
    if portable:
        return out
    # Checkers see their own version (`if _t.TYPE_CHECKING`, a form pyright recognises; an
    # aliased `TYPE_CHECKING` isn't): a function returning the record's exact type, the Protocol a written
    # `(name: str, age: int)` is (see type_def). Protocols match by structure, so a record written in another
    # order is the same type (`xs.append((age=1, name="a"))` on a list of `(name=, age=)` records), and they
    # have no tuple face: unpacking, indexing, iterating, `*rec` and ordering are errors. (A NamedTuple won't
    # do: pyright reads its fields by position whatever its __iter__ says.)
    init = ", ".join(f"{f}: T{i}" for i, f in enumerate(fields))
    checker = f"def {head}(*, {init}) -> {type_class(fields, False)}[{params}]: ...\n"

    def indent(text: str) -> str:
        return "".join("    " + line + "\n" for line in text.splitlines())

    return "if _t.TYPE_CHECKING:\n" + indent(checker) + "else:\n" + indent(out)


def parse_around_errors(body: str, tries: int = 10) -> ast.Module | None:
    """`ast.parse`, with each line that doesn't parse turned into `pass` (at its indent) and parsing tried
    again: in the editor one half-typed line (`print(b2.)`) would otherwise leave every name in the file
    unknown, so `b2.` had no type to complete from."""
    lines = body.splitlines(keepends=True)
    for _ in range(tries):
        try:
            return ast.parse("".join(lines))
        except SyntaxError as e:
            if not e.lineno or e.lineno > len(lines):
                return None
            line = lines[e.lineno - 1]
            stub = line[: len(line) - len(line.lstrip())] + "pass\n"
            if line == stub:
                return None
            lines[e.lineno - 1] = stub
    return None


@dataclass(frozen=True)
class Known:
    """A name's record fields, read off the file (known_fields). is_dict: it holds a record's `_asdict()`,
    so a spread reads it as `d["name"]`, not `d.name`."""

    fields: tuple[str, ...]
    is_dict: bool = False


def known_fields(body: str) -> dict[str, Known]:
    """Names whose record fields can be read off the translation: bound exactly once in the whole file,
    by a record literal, a spread record of such names, an annotation with a record type (inline, a
    `type` alias, or a parameter's), or `x._asdict()` of such a name (a TypedDict: its keys can't be
    removed, so its fields stay). Any other binding of the name anywhere makes it unknown: a wrong
    field set would be worse than the generic spread path. Checker translation only (see known_build)."""
    tree = parse_around_errors(body)
    if tree is None:
        return {}
    count: dict[str, int] = {}
    source: dict[str, tuple[str, ast.expr]] = {}  # name -> ("value" | "type", node)

    def bind(name: str, n: int = 1) -> None:
        count[name] = count.get(name, 0) + n

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bind(node.id)
        elif isinstance(node, ast.arg):
            bind(node.arg)
            if node.annotation is not None:
                source[node.arg] = ("type", node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bind((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                bind(name, 2)  # rebinds a name across scopes: never known
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            bind(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(node.rest)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            source[node.targets[0].id] = ("value", node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            source[node.target.id] = ("type", node.annotation)
        elif isinstance(node, ast.TypeAlias):
            source[node.name.id] = ("type", node.value)

    memo: dict[str, Known | None] = {}

    def resolve(name: str) -> Known | None:
        if name in memo:
            return memo[name]
        memo[name] = None  # a cycle stays unknown
        if count.get(name) == 1 and name in source:
            kind, node = source[name]
            memo[name] = of_value(node) if kind == "value" else of_type(node)
        return memo[name]

    def of_value(e: ast.expr) -> Known | None:
        if isinstance(e, ast.Name):
            return resolve(e.id)
        if (  # `rec._asdict()`: a dict with the record's fields
            isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "_asdict"
            and isinstance(e.func.value, ast.Name) and not e.args and not e.keywords
        ):
            got = resolve(e.func.value.id)
            return Known(got.fields, is_dict=True) if got is not None and not got.is_dict else None
        if not isinstance(e, ast.Call) or not isinstance(e.func, ast.Name):
            return None
        if e.func.id.startswith("_rec_") and all(k.arg for k in e.keywords) and not e.args:
            return Known(tuple(k.arg for k in e.keywords if k.arg))  # a record literal
        if e.func.id in ("_byname_ctx", "_byname_arg"):  # a spread record: its dict display, in order
            d = next((n for n in ast.walk(e) if isinstance(n, ast.Dict)), None)
            if d is None:
                return None
            out: dict[str, None] = {}
            for k, v in zip(d.keys, d.values):
                if k is None:
                    inner = v.args[0] if isinstance(v, ast.Call) and v.args else v
                    got = resolve(inner.id) if isinstance(inner, ast.Name) else None
                    if got is None:
                        return None
                    out.update(dict.fromkeys(got.fields))
                elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                    out[k.value] = None
                else:
                    return None
            return Known(tuple(out))
        return None

    def of_type(t: ast.expr) -> Known | None:
        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id.startswith("_typ_"):
            return Known(tuple(t.value.id.removeprefix("_typ_").split("__")))  # exact types only, not `_opn_`
        if isinstance(t, ast.Name):
            got = resolve(t.id)
            return got if got is None or not got.is_dict else None  # a dict isn't a type
        return None

    return {name: f for name in source if (f := resolve(name))}


def transform(
    src: str,
    path: str = "<pyn>",
    tolerant: bool = False,
    portable: bool = False,
    checker: bool = False,
    known: dict[str, Known] | None = None,
) -> Result:
    """tolerant (editor only): a half-typed pattern item like `na` in `(name=, na) = r` becomes
    `_ds.na` instead of an error, so the checker can complete field names there.
    checker: the translation only type checkers see; works around their bugs (see KW_PRELUDE). Never run.
    known: names with fields known from the file (known_fields); set by the checker translation's second pass."""
    toks = [
        t
        for t in tokenize.generate_tokens(io.StringIO(src).readline)
        if t.type not in (tokenize.COMMENT, tokenize.NL)
    ]

    # matching bracket for every opener (and back)
    pair: dict[int, int] = {}
    parent: dict[int, int] = {}  # token -> innermost enclosing opener
    stack: list[int] = []
    for i, t in enumerate(toks):
        if stack:
            parent[i] = stack[-1]
        if t.type == tokenize.OP and t.string in "([{":
            stack.append(i)
        elif t.type == tokenize.OP and t.string in ")]}":
            pair[stack.pop()] = i

    rpair = {c: o for o, c in pair.items()}

    line_starts = [0]
    for line in src.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    def off(rc: tuple[int, int]) -> int:
        return line_starts[rc[0] - 1] + rc[1]

    def err(msg: str, t: tokenize.TokenInfo) -> SyntaxError:
        return BynameError(msg, (path, t.start[0], t.start[1] + 1, t.line))

    def is_name(t: tokenize.TokenInfo) -> bool:
        return t.type == tokenize.NAME and not keyword.iskeyword(t.string)

    def items(open_i: int) -> list[list[int]]:
        # top-level comma-separated items inside a bracket, as token index lists
        return split(open_i + 1, pair[open_i])

    def split(j: int, stop: int) -> list[list[int]]:
        # top-level comma-separated items in toks[j:stop], as token index lists
        out, cur = [], []
        while j < stop:
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
    types: dict[tuple[tuple[str, ...], bool], None] = {}  # explicit record types: (fields, is_open)
    problems: list[tuple[int, int, str]] = []
    standins: list[tuple[int, int, str]] = []
    field_spans: list[tuple[int, int]] = []  # every record field name: in records, patterns and record types
    label_spans: list[tuple[int, int]] = []  # `x` in `return (x := ..., ...)`: bound to name a position, never read

    def span(a: int, b: int | None = None) -> tuple[int, int]:
        return off(toks[a].start), off(toks[a if b is None else b].end)

    def expand_shorthand(its: list[list[int]]) -> None:
        for it in its:
            if is_short(it):
                at = off(toks[it[1]].end)
                edits.append(Edit(at, at, toks[it[0]].string, span(it[0]), kind="shorthand"))
                standins.append((at, at, SHORT))

    kw_used = builds = False
    type_groups: set[int] = set()  # `(` of record types, for inline construction `(name: str)(**u)`

    def wrap_kw(its: list[list[int]]) -> None:
        # checker only: `**x` -> `**_byname_kw(x)` in calls and dict displays, so the checker sees a record's
        # fields one by one (see RESERVED); what runs is untouched
        nonlocal kw_used
        if not checker:
            return
        for it in its:
            if len(it) >= 2 and toks[it[0]].string == "**":
                a, b = off(toks[it[1]].start), off(toks[it[-1]].end)
                edits.append(Edit(a, a, "_byname_kw(", span(it[1], it[-1])))
                edits.append(Edit(b, b, ")", span(it[1], it[-1])))
                kw_used = True

    def one_line(text: str) -> str:
        """`text` (a type) translated, on one line, for copying into generated code."""
        body = transform(text, path, portable=portable).body.strip()
        try:
            return ast.unparse(ast.parse(body, mode="eval"))
        except SyntaxError:
            return " ".join(body.split())

    def is_argument(i: int, close: int) -> bool:
        """The group `toks[i..close]` is a whole argument of a call, `f(x)` or `f(a, k=x)`."""
        o = parent.get(i)
        if o is None or toks[o].string != "(" or o == 0 or toks[close + 1].string not in (",", ")"):
            return False
        if not (is_name(toks[o - 1]) or toks[o - 1].string in (")", "]")):
            return False
        before = i - 2 if toks[i - 1].string == "=" and is_name(toks[i - 2]) else i
        return toks[before - 1].string in ("(", ",")

    def known_build(i: int, close: int, its: list[list[int]]) -> bool:
        """Checker only: a spread record whose `**` items are all names with known fields (known_fields) is
        built as a plain record, `(**u, age=27)` -> `_rec_name__age(name=u.name, age=27)`, so it has its
        exact type without an expected type. A later item wins and the first position stays, as at runtime.
        False: left to the generic path, which also reports any mistakes."""
        order: dict[str, list[int]] = {}  # field -> the item that gives it
        for it in its:
            first = toks[it[0]]
            if first.string == "**":
                if len(it) != 2 or not is_name(toks[it[1]]) or toks[it[1]].string not in (known or {}):
                    return False
                for f in (known or {})[toks[it[1]].string].fields:
                    if f in order and toks[order[f][0]].string != "**":
                        return False  # overrides a field written before it: rare, generic path
                    order[f] = it
            elif is_kw(it):
                f = first.string
                if f.startswith("_") or f in RESERVED or (f in order and toks[order[f][0]].string != "**"):
                    return False
                order[f] = it
            else:
                return False
        given = {it[0]: [f for f, src in order.items() if src is it] for it in its if toks[it[0]].string == "**"}
        if not all(given.values()):
            return False  # a spread whose every field is overridden: generic path
        fields = tuple(order)
        records[fields] = None
        g0 = span(i)[0]
        edits.append(Edit(g0, g0, record_class(fields), (g0, g0 + 1)))
        for it in its:
            if toks[it[0]].string == "**":
                # `**u` -> `name=u.name, age=u.age`: the user's `u` stays real text (hover, colour, rename).
                # A dict from `_asdict()` is read by key: `name=d["name"]`
                name, (first, *rest) = toks[it[1]].string, given[it[0]]
                read = (lambda f: f'["{f}"]') if (known or {})[name].is_dict else (lambda f: f".{f}")
                at = off(toks[it[1]].end)
                edits.append(Edit(off(toks[it[0]].start), off(toks[it[0]].end), f"{first}=", span(it[1])))
                tail = read(first) + "".join(f", {f}={name}{read(f)}" for f in rest)
                edits.append(Edit(at, at, tail, span(it[1])))
            else:
                # the field name as generated text, like the generic path: the checker would colour a
                # keyword argument over the record-field colour (field_spans)
                f = toks[it[0]].string
                edits.append(Edit(off(toks[it[0]].start), off(toks[it[1]].end), f"{f}=", span(it[0])))
                field_spans.append(span(it[0]))
        expand_shorthand(its)
        standins.append((g0, g0, PAT))
        return True

    def build(i: int, close: int, its: list[list[int]], target: str | None = None, inline_at: int | None = None) -> None:
        """A record built from spreads and fields, `(**u, age=27)`, as `_byname_rec({**u, 'age': 27})`.
        target: the inline record type `(name: str)(**u)` (source text) to check it against."""
        nonlocal builds, kw_used
        if target is None and checker and known and known_build(i, close, its):
            return
        builds = True
        names = []
        # checker, no type written: `**name` spreads are read through a lambda default (see below)
        hoist = [it for it in its if toks[it[0]].string == "**" and len(it) == 2 and is_name(toks[it[1]])]
        hoist = hoist if target is None and checker else []
        wrap_kw([it for it in its if it not in hoist])
        for it in its:
            first = toks[it[0]]
            if len(it) >= 2 and first.string == "**":
                pass
            elif is_kw(it):
                if first.string.startswith("_") or first.string in RESERVED:
                    raise err(field_error(first.string), first)
                names.append(first.string)
                field_spans.append(span(it[0]))
                edits.append(Edit(off(first.start), off(toks[it[1]].end), repr(first.string) + ": ", span(it[0])))
                if is_short(it):
                    at = off(toks[it[1]].end)
                    edits.append(Edit(at, at, first.string, span(it[0]), kind="shorthand"))
                    standins.append((at, at, SHORT))
            else:
                raise err("a record built from spreads takes `**record` and `name=value` items", first)
        if len(set(names)) != len(names):
            raise err(f"duplicate record field in {tuple(names)}", toks[i])
        g0, g1 = span(i, close)
        if target is None and checker:  # checked against the type expected where it stands, if any
            ctx = "_byname_arg" if is_argument(i, close) else "_byname_ctx"
            # Inside a lambda pyright drops narrowing of a name that's reassigned later, so a rebound `r`
            # would be its whole union there. A default is evaluated where the record stands, narrowed:
            # `lambda _byname_t, _byname_s0=_byname_kw(r): ...{**_byname_pick(r, _byname_s0)}`. The user's
            # `r` stays real text in place (hover, colour); the copy in the default is generated.
            defaults = ""
            for k, it in enumerate(hoist):
                defaults += f", _byname_s{k}=_byname_kw({toks[it[1]].string})"
                a, b = span(it[1])
                edits.append(Edit(a, a, "_byname_pick(", (a, b)))
                edits.append(Edit(b, b, f", _byname_s{k})", (a, b)))
                kw_used = True
            pre, post = f"{ctx}(lambda _byname_t{defaults}: _byname_check(lambda: _byname_t)({{", "}))"
        elif target is None:
            pre, post = "_byname_rec({", "})"
        else:
            t = one_line(target)
            pre, post = f"_byname_rec(_byname_check(lambda: _cast({t}, _byname_anyv))({{", "})))"
            if inline_at is None:
                pre = f"_cast({t}, " + pre
            else:  # `(name: str)(**u)`: the type in the source is the cast's first argument
                edits.append(Edit(inline_at, inline_at, "_cast(", (inline_at, g1)))
                pre = ", " + pre
        edits.append(Edit(g0, g0 + 1, pre, (g0, g1)))
        edits.append(Edit(g1 - 1, g1, post, (g0, g1)))
        if inline_at is None:
            standins.append((g0, g0, PAT))  # formatter: a call, `__P(**u, age=27)`

    consumed: set[int] = set()  # `(` of nested pattern groups, handled with their outer pattern

    def pattern_items(open_i: int) -> tuple[list[list[int]], list[bool]] | None:
        """A group's items if they read as pattern items: `field=...`, plus (half-typed) bare names."""
        its = items(open_i)
        kw = [is_kw(it) for it in its]
        bare = [len(it) == 1 and is_name(toks[it[0]]) for it in its]
        if not (its and any(kw) and all(k or b for k, b in zip(kw, bare))):
            return None
        return its, bare

    def pattern(open_i: int) -> list[tuple[list[int], str, object]]:
        """Parse a pattern group into (item, kind, value) entries, kind one of: short (`f=`),
        target (`f=t`, value the target text), nested (`f=(...)`, value its entries), bare (half-typed).
        Records the group's stand-ins and field spans as it goes."""
        its, bare = pattern_items(open_i)
        node: list[tuple[list[int], str, object]] = []
        for it, b in zip(its, bare):
            f = toks[it[0]]
            if b:
                if not tolerant:
                    raise err(f"pattern item {f.string!r} needs '=': write {f.string}= or {f.string}=target", f)
                problems.append((*span(it[0]), f"pattern item {f.string!r} needs '='"))
                node.append((it, "bare", None))
                continue
            field_spans.append(span(it[0]))
            if is_short(it):
                node.append((it, "short", None))
                standins.append((*span(it[1]), ":" + SHORT))
                continue
            standins.append((*span(it[1]), ":"))
            v = it[2]
            if toks[v].string == "(" and pair[v] == it[-1] and pattern_items(v) is not None:
                consumed.add(v)
                node.append((it, "nested", pattern(v)))
                continue
            text = src[off(toks[v].start) : off(toks[it[-1]].end)]
            if not is_target(text):
                raise err(f"cannot bind field {f.string!r} to {text!r}", toks[v])
            node.append((it, "target", text))
        # stand-in: __P[a:__p, b:target, c:__P[d:__p]] = expr / for __P[...] in xs (a subscript is a valid target)
        standins.append((off(toks[open_i].start), off(toks[open_i].end), PAT + "["))
        standins.append((off(toks[pair[open_i]].start), off(toks[pair[open_i]].end), "]"))
        return node

    def access(chain: list[int], text: str, marks: list[Mark]) -> str:
        """Append `_ds.a.b` for field tokens `chain`, each name marked back to where it was written."""
        text += DS
        for k in chain:
            f = toks[k].string
            text += "."
            marks.append(Mark(len(text), len(text) + len(f), *span(k)))
            text += f
        return text

    def bindings(node: list, chain: list[int], text: str, marks: list[Mark]) -> str:
        """Append `; local = _ds.field` for each item of a parsed pattern, marked back to the source.
        (field=) binds local `field`; (field=target) binds `target`; (field=(...)) reads deeper."""
        for it, kind, val in node:
            if kind == "nested":
                text = bindings(val, [*chain, it[0]], text, marks)
                continue
            text += "; "
            if kind == "short":  # target is implicit: insertion point right after `field=`
                ts, at = len(text), off(toks[it[1]].end)
                text += toks[it[0]].string
                marks.append(Mark(ts, len(text), at, at, span(it[0])))
                text += " = "
            elif kind == "target":
                ts = len(text)
                text += val
                marks.append(Mark(ts, len(text), *span(it[2], it[-1])))
                text += " = "
            # bare (half-typed): just the attribute access, for completion
            text = access([*chain, it[0]], text, marks)
        return text

    params = 0  # parameter patterns so far: each becomes PARAM_NAME + its index
    inserted: list[int] = []

    def no_default_shorthand(its: list[list[int]], level: int | None) -> None:
        """No shorthand in a parameter's default (`def` or `lambda`), however deep: a default is evaluated
        where the function is defined, so `name=` there would quietly take the outer `name`, though in a
        signature it reads as the parameter. `name=name` says so."""
        for it in its:
            eq = next((k for k in it if toks[k].string == "=" and parent.get(k) == level), None)
            if eq is None:
                continue
            for k in range(eq + 1, it[-1]):
                if is_name(toks[k]) and toks[k + 1].string == "=" and toks[k + 2].string in (",", ")") and k + 1 in parent:
                    f = toks[k].string
                    raise err(f"{f}= in a parameter's default would take the outer {f!r}: write {f}={f} to mean that", toks[k])

    def lambda_defaults() -> None:
        """no_default_shorthand for every `lambda`: its parameters run to the `:` at its own level."""
        for l, t in enumerate(toks):
            if t.string != "lambda" or t.type != tokenize.NAME:
                continue
            k, inner = l + 1, 0
            while toks[k].type != tokenize.ENDMARKER:
                if parent.get(k) == parent.get(l):
                    if toks[k].string == "lambda":  # `lambda f=lambda x: x: ...`: that `:` is the inner one's
                        inner += 1
                    elif toks[k].string == ":":
                        if not inner:
                            break
                        inner -= 1
                k = pair.get(k, k) + 1
            no_default_shorthand(split(l + 1, k), parent.get(l))

    def parameter_patterns(o: int) -> None:
        """`def f((name=, age=): User, n=0):` -> `def f(_byname_p0: User, n=0):` with `_ds = _byname_p0;
        name = _ds.name; age = _ds.age` on a line of its own before the body (after a docstring), or before
        a one-line body. Python 3 has no destructuring parameters (PEP 3113). to_code puts line numbers back."""
        nonlocal params
        no_default_shorthand(items(o), o)
        found = []
        for it in items(o):
            g = it[0]
            if toks[g].string != "(" or g not in pair or pattern_items(g) is None:
                continue
            rest = it[it.index(pair[g]) + 1 :]
            if rest and toks[rest[0]].string not in (":", "="):
                continue
            consumed.add(g)
            tree = pattern(g)
            group = span(g, pair[g])
            # a pattern over several lines keeps its line breaks (valid inside the def's parentheses),
            # so every line after it keeps its number: tracebacks, source_ast
            name = f"{PARAM_NAME}{params}"
            params += 1
            edits.append(Edit(*group, name + "\n" * src.count("\n", *group), group))
            # formatter: `__D: (__P[...], T)`, or `__D: (__P[...],)` with no annotation
            standins.append((group[0], group[0], f"{PARAM}: ("))
            if rest and toks[rest[0]].string == ":":
                ann_end = next((k for k in rest if toks[k].string == "=" and parent.get(k) == o), None)
                last = rest[rest.index(ann_end) - 1] if ann_end is not None else rest[-1]
                standins.append((*span(rest[0]), ","))
                standins.append((off(toks[last].end), off(toks[last].end), ")"))
            else:  # a 1-tuple needs its comma; ruff would keep a 2-tuple with one split over lines
                standins.append((group[1], group[1], ",)"))
            found.append((name, tree, group))
        if not found:
            return
        # the body: after the `:` that ends the signature (skipping a `-> T`)
        c = pair[o] + 1
        while toks[c].string != ":":
            c = pair.get(c, c) + 1
        marks: list[Mark] = []
        text = ""
        for name, tree, group in found:
            text += ("; " if text else "") + f"{DS} = "
            text = bindings(tree, [], text + name, marks)
        display = (found[0][2][0], found[-1][2][1])
        first = c + 1
        if toks[first].type != tokenize.NEWLINE:  # one-line body: `def f((a=)): return a`
            at = off(toks[first].start)
            edits.append(Edit(at, at, text + "; ", display, marks))
            return
        indent = toks[first + 1].string  # INDENT
        stmt = first + 2
        if toks[stmt].type == tokenize.STRING and toks[stmt + 1].type == tokenize.NEWLINE:  # after a docstring
            line = toks[stmt + 1].start[0] + 1
        else:
            line = toks[stmt].start[0]
        at = line_starts[line - 1] if line - 1 < len(line_starts) - 1 else len(src)
        lead = "" if at == 0 or src[at - 1] == "\n" else "\n"
        for m in marks:
            m.ts, m.te = m.ts + len(lead) + len(indent), m.te + len(lead) + len(indent)
        edits.append(Edit(at, at, lead + indent + text + "\n", display, marks))
        inserted.append(line)

    def label_returns() -> dict[tuple[str, ...], None]:
        """Checker only: `return x, y` in a function with no return annotation -> `return _byname_tup_x__y(x, y)`,
        a tuple whose positions carry the names returned, so hover shows `tuple[x: Tensor, y: Tensor]`. An item
        that isn't a bare name has no label (""). What runs is untouched."""
        bodies: list[tuple[int, int, bool]] = []  # each def's body as a token range, and whether it's annotated
        for d, t in enumerate(toks):
            if t.string != "def" or t.type != tokenize.NAME or not is_name(toks[d + 1]):
                continue
            o = d + 2
            if toks[o].string == "[":
                o = pair[o] + 1
            if o not in pair:
                continue
            j = pair[o] + 1
            annotated = toks[j].string == "->"
            while toks[j].string != ":" and toks[j].type != tokenize.ENDMARKER:
                j = pair.get(j, j) + 1
            k = j + 1
            if toks[k].type == tokenize.NEWLINE:  # an indented body: up to its closing DEDENT
                depth, k = 0, k + 1
                while toks[k].type != tokenize.ENDMARKER:
                    depth += {tokenize.INDENT: 1, tokenize.DEDENT: -1}.get(toks[k].type, 0)
                    if depth == 0:
                        break
                    k += 1
            else:  # `def f(): return x, y`
                while toks[k].type not in (tokenize.NEWLINE, tokenize.ENDMARKER):
                    k += 1
            bodies.append((j, k, annotated))
        def label(it: list[int]) -> str:
            # a bare name, or the name a walrus binds: `x := x.to(d)`, `(x := x.to(d))`
            if len(it) >= 3 and it[0] in pair and pair[it[0]] == it[-1]:
                it = it[1:-1]
            if not it or not is_name(toks[it[0]]) or not (len(it) == 1 or toks[it[1]].string == ":="):
                return ""
            name = toks[it[0]].string
            # it must survive the class name's `__` separators: `a__b` or `_a` would split wrong
            if "__" in name or name.strip("_") != name:
                return ""
            if len(it) > 1:  # a walrus: the checker would call it unused (see Result.labels)
                label_spans.append(span(it[0]))
            return name

        found: dict[tuple[str, ...], None] = {}
        for r, t in enumerate(toks):
            if t.string != "return" or t.type != tokenize.NAME or toks[r - 1].string not in (";", ":") and toks[r - 1].type not in STMT_START:
                continue
            inside = [b for b in bodies if b[0] < r < b[1]]
            if not inside or max(inside)[2]:  # the innermost def, annotated: its annotation is what hover shows
                continue
            end = r + 1
            while toks[end].type not in (tokenize.NEWLINE, tokenize.ENDMARKER) and toks[end].string != ";":
                end = pair.get(end, end) + 1
            if end == r + 1:
                continue
            paren = toks[r + 1].string == "(" and pair.get(r + 1) == end - 1
            its = items(r + 1) if paren else split(r + 1, end)  # `return (x, y)` / `return x, y`
            if len(its) < 2:
                continue
            if any(is_kw(it) or toks[it[0]].string in ("*", "**") for it in its):
                continue  # a record, or an unpacked item: positions unknown
            labels = tuple(label(it) for it in its)
            if not any(labels):
                continue
            found[labels] = None
            whole = span(r + 1, end - 1)
            if paren:
                edits.append(Edit(whole[0], whole[0], tuple_maker(labels), whole))
            else:
                edits.append(Edit(whole[0], whole[0], tuple_maker(labels) + "(", whole))
                edits.append(Edit(whole[1], whole[1], ")", whole))
        return found

    def is_def_params(o: int) -> bool:  # `(` of `def f(...)` / `def f[T](...)`
        head = rpair[o - 1] - 1 if o and toks[o - 1].string == "]" else o - 1
        return head >= 1 and toks[head - 1].string == "def" and is_name(toks[head])

    def type_slot(i: int) -> bool:
        """Whether the group at `i` stands where a type does: after `->`, an annotation's `:` (a parameter,
        a record type's field, `target: T` at a statement's start) or `type X =`."""
        prev = toks[i - 1] if i else None
        if prev is None:
            return False
        if prev.string == "->":
            return True
        if prev.string == "=":  # `type X = (...)`
            return i >= 3 and toks[i - 3].string == "type" and (i == 3 or toks[i - 4].type in STMT_START)
        if prev.string != ":":
            return False
        if i - 1 in parent:  # inside brackets: a parameter's annotation, or a field of a record type
            o = parent[i - 1]
            return toks[o].string == "(" and (is_def_params(o) or o in type_groups)
        j = i - 2  # `name: T` / `a.b: T`, from the statement's start
        while j >= 1 and toks[j - 1].string == "." and is_name(toks[j - 2]):
            j -= 2
        return is_name(toks[j]) and (j == 0 or toks[j - 1].type in STMT_START or toks[j - 1].string == ";")

    lambda_defaults()
    for i, t in enumerate(toks):
        if t.type == tokenize.OP and t.string == "{" and i in pair:  # dict display: `{**u, **r}`
            wrap_kw(items(i))
            continue
        if not (t.type == tokenize.OP and t.string == "("):
            continue
        if i in consumed:
            continue
        prev = toks[i - 1] if i else None
        its = items(i)

        # def f(...) / def f[T](...) / class C(...): parameter lists, left alone but for parameter patterns
        head = rpair[i - 1] - 1 if prev is not None and prev.string == "]" else i - 1  # the name, past `[T]`
        if head >= 1 and toks[head - 1].string in ("def", "class") and is_name(toks[head]):
            if toks[head - 1].string == "def":
                parameter_patterns(i)
            continue

        # building a record type: `(name: str, age: int)(**u, **r)`
        if prev is not None and prev.string == ")" and rpair.get(i - 1) in type_groups:
            build(i, pair[i], its, src[off(toks[rpair[i - 1]].start) : off(prev.end)], off(toks[rpair[i - 1]].start))
            continue

        is_call = prev is not None and (is_name(prev) or prev.string in (")", "]"))
        if is_call:
            expand_shorthand(its)
            wrap_kw(its)
            continue

        if not its:
            continue
        close = pair[i]

        # record type, e.g. `-> (height: int, diameter: int)`: every item is `name: type` (after an optional
        # leading `...` for an open type), never valid Python. Becomes a Protocol, R[int, int]; separate small
        # edits, so nested types work. `(...)` alone is valid Python, so it's a type only where a type stands
        dots = [k for k, it in enumerate(its) if len(it) == 1 and toks[it[0]].string == "..."]
        is_open = dots[:1] == [0]
        named = its[1:] if is_open else its
        is_field = [len(it) >= 3 and is_name(toks[it[0]]) and toks[it[1]].string == ":" for it in its]
        if any(is_field) and all(f or k in dots for k, f in enumerate(is_field)) and dots not in ([], [0]):
            bad = toks[its[dots[1] if is_open else dots[0]][0]]
            raise err("`...` goes first in an open record type: (..., name: str)", bad)
        if is_open and not named and not type_slot(i):
            continue
        if (is_open or named) and all(is_field[1:] if is_open else is_field):
            fields = tuple(toks[it[0]].string for it in named)
            for it in named:
                name = toks[it[0]]
                if name.string.startswith("_") or name.string in RESERVED:
                    raise err(field_error(name.string), name)
            if len(set(fields)) != len(fields):
                raise err(f"duplicate record field in {fields}", t)
            types[(fields, is_open)] = None
            type_groups.add(i)
            group = span(i, close)
            standins.append((group[0], group[0] + 1, TYP + "["))
            standins.append((group[1] - 1, group[1], "]"))
            if not fields:  # `(...)`: a record with any fields, a class with no type parameters
                edits.append(Edit(*group, type_class(fields, is_open), group))
                continue
            edits.append(Edit(group[0], group[0] + 1, type_class(fields, is_open) + "[", group))
            edits.append(Edit(group[1] - 1, group[1], "]", group))
            for it in named:
                edits.append(Edit(off(toks[it[0]].start), off(toks[it[2]].start), "", span(it[0])))
                field_spans.append(span(it[0]))
            if is_open:  # drop `..., `: the Protocol's name says it's open
                d = its[0][0]
                edits.append(Edit(off(toks[d].start), off(toks[named[0][0]].start), "", span(d)))
            continue
        # a record built from spreads: `(**u, **r, age=27)`
        if any(toks[it[0]].string == "**" for it in its):
            build(i, close, its)
            continue
        at_stmt_start = prev is None or prev.type in STMT_START or prev.string == ";"
        is_pattern = at_stmt_start and toks[close + 1].string == "="
        is_for = prev is not None and prev.string == "for" and toks[close + 1].string == "in"
        kw = [is_kw(it) for it in its]
        bare = [len(it) == 1 and is_name(toks[it[0]]) for it in its]
        # a pattern with some `field=` items and some bare names: half-typed, or a mistake (pattern() says so)
        if not all(kw) and not ((is_pattern or is_for) and any(kw) and all(k or b for k, b in zip(kw, bare))):
            continue  # ordinary parenthesised expression / tuple / genexp

        fields = tuple(toks[it[0]].string for it in its)
        if is_pattern or is_for:
            tree = pattern(i)
            group = span(i, close)
        if is_for:
            # for (a=, b=t) in xs:  ->  for (a, t) in ((_ds.a, _ds.b) for _ds in xs):
            # one line, so line numbers hold; the generator keeps field types and scopes `_ds`.
            # Nested groups stay in the target, so the element mirrors them: (a=, b=(c=)) -> (a, (c))
            def unwrap(node: list) -> None:  # drop `field=` from the target, leaving locals
                for it, kind, val in node:
                    if kind == "bare":  # half-typed (tolerant): reads the field, binds nothing
                        edits.append(Edit(*span(it[0]), "_", span(it[0])))
                    elif kind == "short":
                        edits.append(Edit(*span(it[1]), "", span(it[0])))
                    else:
                        edits.append(Edit(off(toks[it[0]].start), off(toks[it[2]].start), "", span(it[0])))
                        if kind == "nested":
                            unwrap(val)

            unwrap(tree)
            # the iterable ends at the statement's `:`, or a comprehension's next clause or closing bracket
            pre = toks[i - 2] if i > 1 else None
            is_async = pre is not None and pre.string == "async"
            if is_async:  # an async iterable needs an async generator
                pre = toks[i - 3] if i > 2 else None
            stmt = pre is None or pre.type in STMT_START or pre.string == ";"
            first = j = close + 2
            comma = False
            while True:
                s = toks[j]
                if s.type in (tokenize.NEWLINE, tokenize.ENDMARKER) or s.string in (")", "]", "}"):
                    break
                if (s.string == ":") if stmt else (s.type == tokenize.NAME and s.string in ("if", "for", "async")):
                    break
                comma = comma or s.string == ","
                j = pair.get(j, j) + 1
            if j == first:
                raise err("missing iterable after 'in'", toks[close + 1])
            marks: list[Mark] = []

            def element(open_i: int, node: list, chain: list[int], text: str, marks: list[Mark]) -> str:
                # the same shape as the target: `(a=,)` stays a 1-tuple, `(a=)` a bare value
                single = len(node) == 1 and toks[pair[open_i] - 1].string != ","
                text += "" if single else "("
                for k, (it, kind, val) in enumerate(node):
                    text += ", " if k else ""
                    if kind == "nested":
                        text = element(it[2], val, [*chain, it[0]], text, marks)
                    else:
                        text = access([*chain, it[0]], text, marks)
                return text + ("" if single else ",)" if len(node) == 1 else ")")

            text = element(i, tree, [], " (", marks) + f" {'async ' if is_async else ''}for {DS} in"
            at = off(toks[close + 1].end)
            edits.append(Edit(at, at, text, group, marks))
            if comma:  # `for x in a, b:` is a tuple; a generator needs it parenthesised
                at = off(toks[first].start)
                edits.append(Edit(at, at, "(", group))
            at = off(toks[j - 1].end)
            edits.append(Edit(at, at, "))" if comma else ")", group))
            continue
        if is_pattern:
            # end of statement: NEWLINE or ';' at depth 0
            j = close + 2
            while toks[j].type not in (tokenize.NEWLINE, tokenize.ENDMARKER) and toks[j].string != ";":
                j = pair.get(j, j) + 1
            marks: list[Mark] = []
            binds = bindings(tree, [], "", marks)
            stmt_end = off(toks[j - 1].end)
            edits.append(Edit(*group, DS, group))
            edits.append(Edit(stmt_end, stmt_end, binds, group, marks))
            continue

        for it in its:
            name = toks[it[0]]
            if name.string.startswith("_") or name.string in RESERVED:
                raise err(field_error(name.string), name)
        if len(set(fields)) != len(fields):
            raise err(f"duplicate record field in {fields}", t)
        records[fields] = None
        field_spans.extend(span(it[0]) for it in its)
        at = off(t.start)
        edits.append(Edit(at, at, record_class(fields), (at, at + 1)))
        standins.append((at, at, PAT))
        expand_shorthand(its)

    # `record(T, d)` with `from byname import record`: the checker sees `_cast(T, record(T, d))`, so the call
    # is a T (a record type alias isn't a `type[T]` to the checker, so a generic signature can't say it).
    # The T written stays real text in the cast, a type position, so hover shows the record type; the one
    # passed to `record` is a copy, and `record` itself is marked back to the name written.
    # Output files run without byname, so they can't call it.
    record_names: set[str] = set()
    for k in range(len(toks) - 2):
        if [toks[k].string, toks[k + 1].string, toks[k + 2].string] == ["from", "byname", "import"]:
            j = k + 3
            while toks[j].type not in (tokenize.NEWLINE, tokenize.ENDMARKER):
                if toks[j].string == "record" and toks[j - 1].string != "as":
                    record_names.add(toks[j + 2].string if toks[j + 1].string == "as" else "record")
                j += 1
    record_used = False
    for k, t in enumerate(toks):
        if t.string not in record_names or toks[k + 1].string != "(":
            continue
        if k > 0 and toks[k - 1].string in (".", "def", "class"):
            continue
        if portable:
            raise err("record() needs byname at runtime, and output files run without it", t)
        if not checker:
            continue
        its = items(k + 1)
        if not its:
            continue  # `record()` is a call error already
        first = its[0]
        a, b = off(toks[first[0]].start), off(toks[first[-1]].end)
        at, end = off(t.start), off(toks[pair[k + 1]].end)
        call = (at, end)
        name_end = off(t.end)
        edits.append(Edit(at, off(toks[k + 1].end), "_cast(", call))  # `record(` -> `_cast(`
        moved = f", {t.string}({one_line(src[a:b])}"  # `, record(T` after the T written
        mark = Mark(2, 2 + len(t.string), at, name_end)
        edits.append(Edit(b, b, moved, call, [mark]))
        edits.append(Edit(end, end, ")", call))
        record_used = True

    tuples = label_returns() if checker else {}

    # apply edits back-to-front
    edits.sort(key=lambda e: (e.start, e.end))
    body = src
    for e in reversed(edits):
        body = body[: e.start] + e.text + body[e.end :]
    # checker: spread records of names whose fields the file shows get their exact type (known_build)
    if checker and known is None and builds and (k := known_fields(body)):
        return transform(src, path, tolerant, portable, checker, known=k)

    prelude = ""
    if records or types:  # only files with records: plain Python is left exactly as written
        prelude = PRELUDE
        if not portable:
            dicts = dict.fromkeys([*records, *(f for f, o in types if not o)])
            prelude += CHECKER_DIRECTIVE + TYPED_PRELUDE + "".join(dict_def(f) for f in dicts)
        if not portable:  # in the checker, each record is its exact type (see record_def)
            types = {**types, **dict.fromkeys((f, False) for f in records)}
        # types first: a record's checker function returns its type, and before Python 3.14 an annotation
        # naming a class defined further down is an undefined name (every record would be Unknown)
        if not portable:
            prelude += "".join(f"type {key_alias(f)} = str\n" for f in dict.fromkeys(f for f, _ in types))
        prelude += "".join(type_def(f, o, portable) for f, o in types)
        prelude += "".join(record_def(f, portable) for f in records)
    if builds or kw_used:
        if not prelude:
            prelude = PRELUDE
        if not portable:
            prelude += HASDICT + (KW_PRELUDE if kw_used else "")
        if builds and portable:
            prelude += BUILD_PORTABLE + BUILD_REC.replace("{sig}", "def _byname_rec(d):")
        elif builds:
            prelude += BUILD_PRELUDE + BUILD_REC.replace("{sig}", "def _byname_rec(d: _Mp[str, _A], /) -> _A:")
            prelude += CTX_PRELUDE if checker else ""
    if tuples:
        prelude += "".join(tuple_def(t) for t in tuples)
    if record_used:
        prelude += "from typing import cast as _cast\n"
    standins.sort()
    return Result(prelude, body, edits, problems, standins, sorted(field_spans), sorted(inserted), sorted(label_spans))


def pattern_slot(src: str, at: int) -> tuple[int, int, int, list[str]] | None:
    """If offset `at` is a field position in a destructuring pattern `(...) = expr`, `for (...) in xs` or a
    parameter `def f((...): T)` (an empty item, or the field name being typed):
    (word_start, word_end, close_paren, fields_already_listed).
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
    parent: dict[int, int] = {}
    stack: list[int] = []
    for i, t in enumerate(toks):
        if stack:
            parent[i] = stack[-1]
        if t.type == tokenize.OP and t.string in "([{":
            stack.append(i)
        elif t.type == tokenize.OP and t.string in ")]}" and stack:
            pair[stack.pop()] = i

    def group_slot(i: int) -> tuple[int, int, int, list[str]] | None:
        close = pair[i]
        lo, hi = off(toks[i].end), off(toks[close].start)
        items, start, cur, j = [], lo, [], i + 1  # (start, end, token indices) per top-level item
        while j < close:
            if toks[j].string == ",":
                items.append((start, off(toks[j].start), cur))
                start, cur = off(toks[j].end), []
            else:
                cur.append(j)
                if j in pair:
                    if off(toks[j].start) < at <= off(toks[pair[j]].start):
                        # inside a nested bracket: a slot only if it's a nested pattern `field=(...)`
                        is_nested = toks[j].string == "(" and len(cur) == 3 and toks[cur[1]].string == "="
                        return group_slot(j) if is_nested else None
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

    for i, t in enumerate(toks):
        if t.string != "(" or i not in pair:
            continue
        prev = toks[i - 1] if i else None
        close = pair[i]
        if close + 1 >= len(toks):
            continue
        o = parent.get(i)
        if o is not None and o >= 2 and toks[o - 2].string == "def" and prev is not None and prev.string in ("(", ","):
            if toks[close + 1].string not in (":", ",", ")", "="):  # def f((...): T)
                continue
        elif prev is not None and prev.string == "for":  # for (...) in xs
            if toks[close + 1].string != "in":
                continue
        elif not (prev is None or prev.type in STMT_START or prev.string == ";") or toks[close + 1].string != "=":
            continue
        if not off(t.end) <= at <= off(toks[close].start):
            continue
        return group_slot(i)
    return None


def is_target(text: str) -> bool:
    """A plain assignment target: name, attribute or subscript (nested patterns are handled before this)."""
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


def source_ast(src: str, path: str = "<pyn>") -> ast.Module:
    """The translation's AST with the .pyn file's own line numbers, without the prelude. For tools that
    match checker output back to source constructs (e.g. which function a line is in)."""
    return _body_ast(transform(src, path), path)


def _body_ast(r: Result, path: str) -> ast.Module:
    tree = ast.parse(r.body, path)
    if r.inserted:  # generated lines (parameter patterns): count as the line before them, the `def`
        extra = [line + k for k, line in enumerate(r.inserted)]

        def source_line(n: int) -> int:
            while n in extra:
                n -= 1
            return n - bisect.bisect_left(extra, n)

        for node in ast.walk(tree):
            if isinstance(getattr(node, "lineno", None), int):
                node.lineno = source_line(node.lineno)
                if isinstance(getattr(node, "end_lineno", None), int):
                    node.end_lineno = source_line(node.end_lineno)
    return tree


def to_ast(src: str, path: str) -> ast.Module:
    """The module that runs, as an AST, prelude included: what to_code compiles. Body line numbers match
    the .pyn file exactly. For tools that rewrite it first (the pytest plugin rewrites asserts)."""
    r = transform(src, path)
    tree = _body_ast(r, path)
    if r.prelude:
        k = prelude_index(tree)
        tree.body[k:k] = ast.parse(r.prelude).body
    return tree


def to_code(src: str, path: str):
    """Compile .pyn source. Body line numbers match the .pyn file exactly."""
    return compile(to_ast(src, path), path, "exec", dont_inherit=True)


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
