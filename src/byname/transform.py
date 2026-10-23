"""Source transform: .pyn -> plain Python.

Everything is built on the `name=` shorthand from PEP 736 (rejected):

    fn(name=, age=)           call         -> fn(name=name, age=age)
    (name=, age=)             record       -> _rec_name__age(name=name, age=age)
    (name=, age=) = expr      destructure  -> _ds = expr; name = _ds.name; age = _ds.age
    (name=n) = expr           rename       -> _ds = expr; n = _ds.name
    (name=): Final = expr     final        -> _ds = expr; name: Final = _ds.name
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
import io
import keyword
import linecache
import tokenize
import types
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import ITER_CALLERS, iter_callers

DS = "_ds"
# formatting stand-ins: `x=` -> `x=__p`, record `(` -> `__P(`, pattern -> `__P[...]`, record type -> `__T[...]`
SHORT, PAT, TYP = "__p", "__P", "__T"
REPR = "_byname_repr"
ORDER = "_byname_order"  # a record class's fields in the order written; `_fields` is the sorted storage order
PRELUDE = (
    "from typing import NamedTuple as _NT\n"
    "from collections import namedtuple as _ntf\n"
    "import sys as _byname_sys\n"
    f"def {REPR}(self) -> str: return '(' + ', '.join(f'{{k}}={{getattr(self, k)!r}}' for k in self.{ORDER}) + ')'\n"
    # records are mappings by field name at runtime, so `f(**rec)` and `{**rec}` work (see RESERVED)
    f"def _byname_keys(self): return self.{ORDER}\n"
    "def _byname_item(self, k):\n"
    "    if not isinstance(k, str):\n"
    "        if _byname_lib(): return tuple.__getitem__(self, k)\n"
    "        _byname_positional(self)\n"
    f"    if k in self.{ORDER}: return getattr(self, k)\n"
    "    raise KeyError(k)\n"
    f"def _byname_has(self, k): return k in self.{ORDER}\n"
    # Records are read by name only, at runtime too: the tuple underneath stores the fields sorted by name, so
    # iterating, indexing, ordering or concatenating would expose an order nobody wrote (`a, b = (name=, age=)`
    # gave age, name). pickle, copy and _replace iterate in NamedTuple's own versions, hence their own here.
    # The one exception is reading by position from inside the iter callers (config.ITER_CALLERS and the
    # project's `iter-callers`), libraries that rebuild the record. `{iter_callers}` is them, each with a dot.
    "def _byname_positional(self, *_):\n"
    "    raise TypeError(f'{type(self).__name__} is read by name only: use .field, keys() or _asdict()' + _byname_hint())\n"
    # pandas reads a NamedTuple by position, with `_fields` (the sorted storage order) as the columns: not an
    # iter caller, as its columns would come out sorted, not as written. The error says what keeps them
    "def _byname_hint():\n"
    "    f = _byname_sys._getframe(2)\n"
    "    while f and f.f_code.co_name.startswith('_byname_'): f = f.f_back\n"
    "    if f and f.f_globals.get('__name__', '').startswith('pandas.'):\n"
    "        return '; for a DataFrame, pd.DataFrame(map(dict, rows)) keeps the written order'\n"
    "    return ''\n"
    # whether the code reading by position is in an iter caller (see above): the frame two up from here,
    # one up from __iter__ or __getitem__, is the Python code that asked, also through list(), zip() or *
    "def _byname_lib():\n"
    "    return (_byname_sys._getframe(2).f_globals.get('__name__', '') + '.').startswith({iter_callers})\n"
    "def _byname_iter(self):\n"
    "    if _byname_lib(): return tuple.__iter__(self)\n"
    "    _byname_positional(self)\n"
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
    "    c.__iter__ = _byname_iter\n"
    "    c.__add__ = c.__mul__ = c.__rmul__ = _byname_positional\n"
    "    c.__lt__ = c.__le__ = c.__gt__ = c.__ge__ = _byname_unordered\n"
    "    c.__getnewargs__, c._replace = _byname_args, _byname_replace\n"
    # what error messages and type(r).__name__ say; __qualname__ stays the module global's name, which
    # pickle finds the class by
    "    c.__name__ = 'record(' + ', '.join(order) + ')'\n"
    "    c.__reduce__ = _byname_reduce\n"
    "    _byname_cls.setdefault(order, c)\n"
    # one class per field tuple, in written order: the literals' classes (registered by _byname_setup), and
    # those of records built from a dict (spreads, rest, unpickling) made here on first use
    "_byname_cls = {}\n"
    "def _byname_rec(d):\n"
    "    k = tuple(d)\n"
    "    c = _byname_cls.get(k)\n"
    "    if c is None:\n"
    "        c = _ntf('_rec_' + '__'.join(k), sorted(k))\n"
    "        _byname_setup(c, k)\n"
    "    return c(**d)\n"
    # pickled by field, not by class: a spread's class isn't a module global for pickle to find. Unpickling
    # rebuilds it with this module's _byname_rec, the same class as before
    "def _byname_reduce(self): return _byname_rec, (_byname_asdict(self),)\n"
)


def prelude_for(callers: tuple[str, ...]) -> str:
    """PRELUDE letting the modules in callers read a record by position (config.iter_callers)."""
    return PRELUDE.replace("{iter_callers}", repr(tuple(m + "." for m in callers)))


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
# so the error would vanish; one inside a lambda's body doesn't fail the call. With an overloaded callee it's
# unchecked; with no type expected at all (untyped_builds) it's an open record instead (open_build).
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
    "from collections.abc import Callable as _Cl, Mapping as _Mp\n"
    "@_ov\ndef _byname_check[D](t: _Cl[[], _byname_HasDict[D]], /) -> _Cl[[D], D]: ...  # pyright: ignore\n"
    "@_ov\ndef _byname_check(t: _Cl[[], object], /) -> _Cl[[_A], _A]: ...  # pyright: ignore\n"
    "def _byname_check(t: _A, /) -> _A: return _byname_same  # pyright: ignore\n"
    "def _byname_same(d: _A, /) -> _A: return d\n"
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
    # a spread with no type expected (open_build): its fields aren't known, so it's any mapping
    "def _byname_loose(x: object, /) -> _Mp[str, object]: ...  # pyright: ignore\n"
)
# checker only: _byname_rec (PRELUDE), typed
BUILD_REC_TYPED = "def _byname_rec(d: _Mp[str, _A], /) -> _A: ...  # pyright: ignore\n"
OMIT_REC = (  # `(name=, **rest) = r`: a record of r's fields but those named, in r's written order
    "{sig}\n"
    f"    if not hasattr(type(r), {ORDER!r}):\n"
    "        raise TypeError(f'**rest takes the rest of a record, not of {type(r).__name__}')\n"
    f"    return _byname_rec({{k: getattr(r, k) for k in r.{ORDER} if k not in taken}})\n"
)
FIELDSET = "_byname_fieldset"  # a record's field names, sorted: what an exact record type matches on
STMT_START = {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}


def annotation_eq(toks: list[tokenize.TokenInfo], pair: dict[int, int], k: int) -> int | None:
    """If toks[k] is the `:` of an annotated pattern, `(...): Final = r`, the index of its `=`, so the
    annotation is toks[k + 1 : eq]. None with no annotation, or no value after it."""
    if toks[k].string != ":":
        return None
    j = k + 1
    while toks[j].type not in (tokenize.NEWLINE, tokenize.ENDMARKER) and toks[j].string not in ("=", ";"):
        j = pair.get(j, j) + 1
    return j if j > k + 1 and toks[j].string == "=" else None


def is_final(toks: list[tokenize.TokenInfo]) -> bool:
    """`Final` or a dotted name ending in it, `typing.Final`: the one annotation a pattern takes."""
    names = [t.string for t in toks[::2]]
    dots = [t.string for t in toks[1::2]]
    return (
        len(toks) % 2 == 1
        and names[-1] == "Final"
        and all(t.type == tokenize.NAME for t in toks[::2])
        and all(d == "." for d in dots)
    )


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
    # (start, end, text): plain-Python stand-ins, for formatting
    standins: list[tuple[int, int, str]] = field(default_factory=list)
    fields: list[tuple[int, int]] = field(default_factory=list)  # source spans of record field names, for highlighting
    # source spans of walrus names labelling a returned tuple
    labels: list[tuple[int, int]] = field(default_factory=list)
    # source spans of every bare `x=` (calls, records, patterns, loops): `x` is also the local, so it's
    # coloured as that local (lsp.remap_tokens)
    shorthands: list[tuple[int, int]] = field(default_factory=list)
    # checker: (class, field, start, end) for each field name written in a record type, `name` in
    # `(name: str)`: its own class's field, so renaming that field reaches the text (see site_class)
    type_fields: list[tuple[str, str, int, int]] = field(default_factory=list)
    # bracket reads of a field the reader's written record type declares, `p["msg"]` with
    # `p: (..., msg: str)`: (end of `p`, end of the read, field), source offsets (see declared_reads)
    declared_reads: list[tuple[int, int, str]] = field(default_factory=list)
    # checker: source spans of each `**rest` whose source's fields the file doesn't show, typed `(...)` (rest_of)
    unknown_rests: list[tuple[int, int]] = field(default_factory=list)
    # checker: (name, line) of each bare-name spread source and each rest's `_ds` whose fields the file doesn't
    # show; a probe translation wraps each in `reveal_type(...)`, to ask the checker (srcmap.probed_from)
    unseen: list[tuple[str, int]] = field(default_factory=list)


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


def covariant(n: int) -> str:
    """The type parameters of a record type, its TypedDict and its sites: `_byname_T0, _byname_T1`, declared
    covariant in the prelude (covariant_defs), so `(dropout: int)` fits `(dropout: float)` as `tuple[int]` fits
    `tuple[float]`. PEP 695's `class C[T0]` infers variance, and `_replace`'s keywords and `_asdict()`'s
    TypedDict make it invariant; covariance is sound all the same, as both build a new object."""
    return ", ".join(f"_byname_T{i}" for i in range(n))


def covariant_defs(n: int) -> str:
    return "".join(f"_byname_T{i} = _t.TypeVar('_byname_T{i}', covariant=True)\n" for i in range(n))


def site_class(name: str, n: int | str) -> str:
    """Checker only: the class of one record literal or written record type, the n-th in the file,
    `_typ_name___3`. Records match by shape, so sites with the same fields are still the same type; but each
    has its own fields, so a rename reaches only the records that came from that site: renaming `r.name`
    renames the literal `r` was made from, not another record that happens to have a `name` field.
    n may carry SPREAD's marker, `3_spread__u`: the error on a field it lacks says where it came from."""
    return f"{name}___{n}"


# a record built from a spread whose source's fields byname can't see, where no type is expected (open_build):
# its site's marker, with the sources that are bare names, `_opn_zip___3_spread__u`. A field read off it that
# isn't there is then explained as a spread's (lsp.explain_fields), in the editor and `byname tool` alike
SPREAD = "_spread"


def class_fields(suffix: str) -> tuple[str, ...]:
    """The fields in a generated class name after its prefix: `name__age___3` -> (name, age). A field
    can't start with `_` (field_error), so the site number can't be one."""
    return tuple(f for f in suffix.split("__") if f and not f.startswith("_"))


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


def type_ref(fields: tuple[str, ...], is_open: bool, args: list[str]) -> str:
    """A record type applied to its field types, `_typ_a__b[T0, T1]`; a type with no fields takes none."""
    return type_class(fields, is_open) + (f"[{', '.join(args)}]" if fields else "")


def omit_name(taken: tuple[str, ...]) -> str:
    return "_byname_omit_" + "__".join(taken)


def omit_def(taken: tuple[str, ...]) -> str:
    """Checker only: the rest of a source whose fields the file doesn't show (rest_of): any record, `(...)`,
    unchecked. Not a record: an error on `x` (lsp's OMIT_RE words it)."""
    return f"def {omit_name(taken)}(x: _opn_, /) -> _opn_: ...  # pyright: ignore\n"


def rest_def(name: str, fields: tuple[str, ...], is_open: bool, taken: tuple[str, ...]) -> str:
    """Checker only: the rest of an open record type the file shows, `(..., name, age, city)` -> `(..., age,
    city)`, or of an exact one when nothing is left, `(name)` -> `()` (rest_of): one function, for that type."""
    params = [f"T{k}" for k in range(len(fields))]
    keep = [k for k, f in enumerate(fields) if f not in taken]
    rest = type_ref(tuple(fields[k] for k in keep), is_open, [params[k] for k in keep])
    return f"def {name}[{', '.join(params)}](x: {type_ref(fields, is_open, params)}, /) -> {rest}: ...\n"


def fieldset(fields: tuple[str, ...]) -> str:
    return ",".join(sorted(fields))


def dict_def(fields: tuple[str, ...]) -> str:
    if not fields:  # the rest of a record once every field is taken: `(a=, **rest) = (a=1)`
        return f"class {dict_class(fields)}(_TD): pass\n"
    body = "; ".join(f"{f}: _byname_T{i}" for i, f in enumerate(fields))
    return f"class {dict_class(fields)}(_TD, _t.Generic[{covariant(len(fields))}]):\n    {body}\n"


def type_def(fields: tuple[str, ...], is_open: bool, portable: bool = False) -> str:
    """An explicit record type as a Protocol, so field order doesn't matter: inferred records keep their
    written order, explicit types match any order. Exact types also require the same field set (FIELDSET),
    so an extra field is an error; open types (`...`) accept records with at least these fields.
    `rec["name"]` is the unchecked read: any string key, typed Any; `rec.name` is the checked one."""
    name = type_class(fields, is_open)
    if portable:  # annotations are evaluated on old Pythons: a subscriptable stand-in is all they need
        return f"class {name}: __class_getitem__ = classmethod(lambda cls, _: cls)\n"
    params = covariant(len(fields))
    # the checker warns that _replace and _asdict want them invariant (see covariant)
    out = (
        f"class {name}(_PR[{params}]):  # pyright: ignore[reportInvalidTypeVarUse]\n"
        if fields
        else f"class {name}(_PR):\n"
    )
    # read-only fields, so records (immutable) match; Final rather than @property, so `p.age` is coloured
    # like a record field (see remap_tokens). Checkers object to a type variable in Final: silenced.
    for i, f in enumerate(fields):
        out += f"    {f}: _Fi[_byname_T{i}]  # pyright: ignore\n"
    out += f"    def __getitem__(self, k: {key_alias(fields)}, /) -> _Ay: ...\n"  # a str key, never a position
    out += "    def __contains__(self, k: object, /) -> bool: ...\n"  # a field name
    out += "    def keys(self) -> tuple[str, ...]: ...\n"  # what `**rec` reads
    if is_open:
        return out
    kw = "".join(f", {f}: _byname_T{i} = ..." for i, f in enumerate(fields))
    kw = f", *{kw}" if fields else ""
    asdict = dict_class(fields) + (f"[{params}]" if fields else "")
    return out + (  # no __iter__, and __getitem__ takes no position: a record is read by name (see record_def)
        f"    @property\n    def {FIELDSET}(self) -> _L[{fieldset(fields)!r}]: ...\n"
        f"    def __len__(self) -> int: ...\n"
        "    @property\n    def _fields(self) -> tuple[str, ...]: ...\n"
        f"    def _replace(self{kw}) -> _S: ...  # pyright: ignore[reportGeneralTypeIssues]\n"  # covariant
        f"    def _asdict(self) -> {asdict}: ...\n"
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


def site_def(fields: tuple[str, ...], is_open: bool, n: int | str, literal: bool) -> str:
    """Checker only: one record literal's or written record type's own class (site_class): the shared type of
    its fields, with the fields (and `_replace`'s keywords) declared again, so they're its own symbols. For
    a literal, also its function, as record_def's checker side; an open one (open_build) takes any other
    keywords too."""
    if not fields:  # open_build's `(**u)`: a record, fields unknown
        return f"if _t.TYPE_CHECKING:\n    def {site_class(record_class(fields), n)}(**_: object) -> _opn_: ...\n"
    params = covariant(len(fields))
    typ = site_class(type_class(fields, is_open), n)
    out = f"class {typ}({type_class(fields, is_open)}[{params}], _PR[{params}]):  # pyright: ignore[reportInvalidTypeVarUse]\n"
    out += "".join(f"    {f}: _Fi[_byname_T{i}]  # pyright: ignore\n" for i, f in enumerate(fields))
    if not is_open:
        out += f"    def _replace(self, *{''.join(f', {f}: _byname_T{i} = ...' for i, f in enumerate(fields))}) -> _S: ...  # pyright: ignore[reportGeneralTypeIssues]\n"
    params = ", ".join(f"T{i}" for i in range(len(fields)))  # the literal's function: its own, PEP 695
    if literal:
        init = ", ".join(f"{f}: T{i}" for i, f in enumerate(fields)) + (", **_: object" if is_open else "")
        head = f"{site_class(record_class(fields), n)}[{params}]"
        out += f"if _t.TYPE_CHECKING:\n    def {head}(*, {init}) -> {typ}[{params}]: ...\n"
    return out


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
    so a spread reads it as `d["name"]`, not `d.name`. returns: it's a function, and these are the fields
    of what a call to it returns. is_open: an open record type, `(..., name: str)`, which may hold other
    fields too: a rest takes these apart (rest_of), a spread doesn't build from them (known_build)."""

    fields: tuple[str, ...]
    is_dict: bool = False
    returns: bool = False
    is_open: bool = False


# the record fields of a name another module defines: (module, level, name) -> Known, or None if it shows none
Lookup = Callable[[str, int, str], "Known | None"]  # imports.Modules
# where a binding's fields show (_Bind.source): ("value" | "type", expr), ("import", (module, level, name)),
# ("def", the function), whose fields are those of what it returns
Source = tuple[str, ast.expr] | tuple[str, tuple[str, int, str]] | tuple[str, ast.FunctionDef]


@dataclass(eq=False)
class _Bind:
    """One binding of a name in a scope (known_fields). at: the index of the scope's statement it's in (-1 for
    parameters); straight: that statement is simple (no `if`, loop, `try`, ...), so the binding has run once
    the scope reaches the next statement; source: ("value" | "type", node) when it shows the fields (a
    parameter's annotation is read where the function is defined), ("import", (module, level, name)) for
    `from module import name`; anywhere: a function's binding of a `global` name, which can run before any
    read of it."""

    at: int
    straight: bool
    source: "Source | None"
    where: tuple["_Scope", int]  # the scope and statement the source is read in
    anywhere: bool = False
    line: int = 0  # an assignment target's line: a pattern's `_ds` is read on the line binding it


@dataclass(eq=False)
class _Scope:
    """A scope in known_fields. at: the parent's statement it stands in; now: its code runs there and then (a
    class body, a comprehension, byname's own lambdas), so a name it reads from outside is read at `at`."""

    parent: "_Scope | None"
    at: int = 0
    now: bool = False
    is_class: bool = False
    binds: dict[str, list[_Bind]] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)  # names in its `global` statements
    # names annotated in it, `u: User`, with the statement: every binding of the name there, an assignment or a
    # loop's `for u in users`, has that type, as for the checker (of_bind)
    declared: dict[str, tuple[ast.expr, int]] = field(default_factory=dict)


COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.TryStar, ast.With, ast.AsyncWith, ast.Match)
COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def untyped_builds(body: str) -> set[tuple[int, int]]:
    """Checker translation, first pass: the spread records built where no type is expected, as (line, k),
    the k-th spread record built on that line: the whole value of a `return` in a function with no return
    annotation (not a generator), or of `x = ...` where `x` has no annotation in its scope. There
    `_byname_ctx` has nothing to solve its type from, so the record would be Unknown; open_build types it
    instead."""
    tree = parse_around_errors(body)
    if tree is None:
        return set()
    ctx = ("_byname_ctx", "_byname_arg")
    calls = sorted(
        (n.lineno, n.col_offset, n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ctx
    )
    index: dict[int, tuple[int, int]] = {}
    per_line: dict[int, int] = {}
    for line, _, n in calls:
        index[id(n)] = (line, per_line.get(line, 0))
        per_line[line] = per_line.get(line, 0) + 1
    out: set[tuple[int, int]] = set()

    def own_nodes(scope: ast.AST):
        """The scope's own nodes, not those of the functions, classes, lambdas and comprehensions in it."""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            n = stack.pop()
            yield n
            if not isinstance(
                n,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda, ast.ListComp, ast.SetComp)
                + (ast.DictComp, ast.GeneratorExp),
            ):
                stack.extend(ast.iter_child_nodes(n))

    def visit(scope: ast.AST) -> None:
        nodes = list(own_nodes(scope))
        declared = {t.target.id for t in nodes if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
        declared |= {x for t in nodes if isinstance(t, (ast.Global, ast.Nonlocal)) for x in t.names}
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = scope.args
            declared |= {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if x}
            generator = any(isinstance(t, (ast.Yield, ast.YieldFrom)) for t in nodes)
            if scope.returns is None and not generator:
                out.update(index[id(t.value)] for t in nodes if isinstance(t, ast.Return) and id(t.value) in index)
        for t in nodes:
            if (
                isinstance(t, ast.Assign)
                and len(t.targets) == 1
                and isinstance(t.targets[0], ast.Name)
                and t.targets[0].id not in declared
                and id(t.value) in index
            ):
                out.add(index[id(t.value)])
            if isinstance(t, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(t)

    visit(tree)
    return out


def known_fields(
    body: str,
    lookup: Lookup | None = None,
    exports: bool = False,
    probed: dict[tuple[str, int], Known] | None = None,
) -> dict[tuple[str, int], Known]:
    """The record fields of names read in the translation, by (name, line), where a binding shows them: a
    record literal, a spread record of such names, the rest of such a name (`**rest`), an annotation with a record type (inline, a `type`
    alias, one imported with `from module import name` that lookup finds, or a parameter's), or
    `x._asdict()` of such a name (a TypedDict: its keys can't be removed, so its fields stay). Names are
    scoped as in Python (function, lambda, comprehension, class). A read is known when every binding that
    may reach it shows the same fields (reaching): any of them may be the one that ran, and a wrong field
    set would be worse than the generic spread path. exports: instead, the module's own names as another
    module importing it sees them, by (name, 0). probed: what the checker said a read the file doesn't show
    is, by (name, line) (srcmap.probed_from): the fields of a read byname can't tell. Checker translation only
    (see known_build)."""
    tree = parse_around_errors(body)
    if tree is None:
        return {}
    shared: set[str] = set()  # names in a `nonlocal`: rebound across scopes, never known
    sources: dict[int, tuple[str, ast.expr]] = {}  # id(Name target) -> its binding's source
    reads: list[tuple[str, int, _Scope, int]] = []  # name, line, scope, statement index
    scopes: dict[int, _Scope] = {}  # id(def) -> its body's scope
    returns: dict[int, list[tuple[ast.expr, int]]] = {}  # id(scope) -> its functions' `return` values, statement index
    no_record: set[int] = set()  # id(scope) of functions that may return no record: a bare `return`, or a generator

    def bind(
        scope: _Scope,
        name: str,
        at: int,
        straight: bool,
        source: Source | None = None,
        where: tuple[_Scope, int] | None = None,
        line: int = 0,
    ) -> None:
        if name in scope.globals:  # the module's name, rebound whenever the function runs
            while scope.parent is not None:
                scope = scope.parent
            scope.binds.setdefault(name, []).append(_Bind(at, False, source, where or (scope, at), anywhere=True))
            return
        scope.binds.setdefault(name, []).append(_Bind(at, straight, source, where or (scope, at), line=line))

    def body_of(stmts: list[ast.stmt], scope: _Scope) -> None:
        for at, s in enumerate(stmts):
            visit(s, scope, at, not isinstance(s, COMPOUND))

    def params(args: ast.arguments, scope: _Scope, inner: _Scope, at: int, straight: bool) -> None:
        for d in [*args.defaults, *(d for d in args.kw_defaults if d)]:
            visit(d, scope, at, straight)
        for a in [
            *args.posonlyargs,
            *args.args,
            *([args.vararg] if args.vararg else []),
            *args.kwonlyargs,
            *([args.kwarg] if args.kwarg else []),
        ]:
            if a.annotation is not None:
                visit(a.annotation, scope, at, straight)
            source = ("type", a.annotation) if a.annotation is not None else None
            bind(inner, a.arg, -1, True, source, (scope, at))

    def visit(node: ast.AST, scope: _Scope, at: int, straight: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            ours = isinstance(node, ast.Lambda) and node.args.args and node.args.args[0].arg.startswith("_byname_")
            inner = _Scope(scope, at, now=bool(ours))
            if not isinstance(node, ast.Lambda):
                for d in node.decorator_list:
                    visit(d, scope, at, straight)
                if node.returns is not None:
                    visit(node.returns, scope, at, straight)
                # a call to a plain `def` returns what its annotation or its `return`s show; not an `async def`
                # (a coroutine) or a decorated one (the decorator may return anything)
                plain = isinstance(node, ast.FunctionDef) and not node.decorator_list
                bind(scope, node.name, at, straight, ("def", node) if plain else None)
                scopes[id(node)] = inner
            params(node.args, scope, inner, at, straight)
            if isinstance(node, ast.Lambda):
                visit(node.body, inner, 0, True)
            else:
                body_of(node.body, inner)
            return
        if isinstance(node, ast.ClassDef):
            for e in [*node.decorator_list, *node.bases, *node.keywords]:
                visit(e, scope, at, straight)
            bind(scope, node.name, at, straight)
            body_of(node.body, _Scope(scope, at, now=True, is_class=True))
            return
        if isinstance(node, COMPS):
            inner = _Scope(scope, at, now=True)
            visit(node.generators[0].iter, scope, at, straight)
            for i, g in enumerate(node.generators):
                if i:
                    visit(g.iter, inner, 0, True)
                for n in ast.walk(g.target):
                    if isinstance(n, ast.Name):
                        bind(inner, n.id, -1, True)
                for e in g.ifs:
                    visit(e, inner, 0, True)
            for e in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
                visit(e, inner, 0, True)
            return
        if isinstance(node, ast.Return):
            if node.value is None:
                no_record.add(id(scope))
            else:
                returns.setdefault(id(scope), []).append((node.value, at))
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            no_record.add(id(scope))
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                reads.append((node.id, node.lineno, scope, at))
            else:
                bind(scope, node.id, at, straight, sources.get(id(node)), line=node.lineno)
        elif isinstance(node, ast.NamedExpr):
            bind(scope, node.target.id, at, False)  # rebinds mid-statement: never the nearest one alone
            visit(node.value, scope, at, straight)
            return
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bind(scope, a.asname or a.name, at, straight, ("import", (node.module or "", node.level, a.name)))
        elif isinstance(node, ast.Import):
            for a in node.names:
                bind(scope, (a.asname or a.name).split(".")[0], at, straight)
        elif isinstance(node, ast.Global):
            scope.globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            shared.update(node.names)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            bind(scope, node.name, at, straight)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bind(scope, node.rest, at, straight)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            sources[id(node.targets[0])] = ("value", node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            sources[id(node.target)] = ("type", node.annotation)
            scope.declared.setdefault(node.target.id, (node.annotation, at))
        elif isinstance(node, ast.TypeAlias):
            sources[id(node.name)] = ("type", node.value)
        inside = straight and not isinstance(node, COMPOUND)
        for child in ast.iter_child_nodes(node):
            visit(child, scope, at, inside)

    module = _Scope(None)
    body_of(tree.body, module)

    def reaching(name: str, scope: _Scope, at: int) -> list[_Bind]:
        """The bindings that may reach a read of `name` in `scope` at statement `at`. Read from a function
        (or a user's lambda), which can run after any of them: every binding of the name. Read where it
        stands: the nearest straight binding in an earlier statement, and every binding after it in a
        branch, loop, `try`, ... up to and in the read's own statement (a loop runs its later lines
        before the next pass), plus a function's `global` ones. A later statement's can't have run yet."""
        if name in shared:
            return []
        s, now = scope, True  # now: no function between the read and the scope binding it
        while s is not None and (name not in s.binds or (s is not scope and s.is_class)):  # classes don't enclose
            now, at, s = now and s.now, s.at, s.parent
        if s is None:
            return []
        bs = s.binds[name]
        if len(bs) == 1 or not now:
            return bs
        up = [b for b in bs if not b.anywhere and (b.at < at or (b.at == at and not b.straight))]
        last = max((b.at for b in up if b.straight), default=-2)
        return [b for b in up if b.at >= last] + [b for b in bs if b.anywhere]

    memo: dict[int, Known | None] = {}

    def resolve(name: str, scope: _Scope, at: int) -> Known | None:
        got = [of_bind(b, name) for b in reaching(name, scope, at)]
        return got[0] if got and all(k is not None and k == got[0] for k in got) else None

    def of_bind(b: _Bind, name: str) -> Known | None:
        if id(b) in memo:
            return memo[id(b)]
        source, where = b.source, b.where
        # a name declared in its scope, `r: T`, has that type at every binding, an assignment's or a loop's,
        # as for the checker: each must fit it (_Scope.declared)
        if (source is None or source[0] == "value") and (decl := where[0].declared.get(name)) is not None:
            source, where = ("type", decl[0]), (where[0], decl[1])
        if source is None:
            return None
        memo[id(b)] = None  # a cycle stays unknown
        kind, node = source
        if isinstance(node, tuple):  # ("import", (module, level, name))
            memo[id(b)] = lookup(*node) if lookup is not None else None
        elif isinstance(node, ast.FunctionDef):
            got = of_def(node, *where)
            memo[id(b)] = Known(got.fields, returns=True) if got is not None else None
        else:
            memo[id(b)] = of_value(node, *where) if kind == "value" else of_type(node, *where)
        return memo[id(b)]

    def of_def(d: ast.FunctionDef, scope: _Scope, at: int) -> Known | None:
        """The fields of what a call to d returns: its return annotation's (read where d is defined), or else
        those every `return` shows, when they all show the same ones (a function returning another's result
        is followed). A bare `return` or a `yield`: none."""
        if d.returns is not None:
            got = of_type(d.returns, scope, at)
            return got if got is not None and not got.is_dict else None
        inner = scopes[id(d)]
        if id(inner) in no_record or not (rs := returns.get(id(inner))):
            return None
        got = [of_value(v, inner, k) for v, k in rs]
        first = got[0]
        if first is None or first.is_dict or any(k != first for k in got):
            return None
        return first

    def of_value(e: ast.expr, scope: _Scope, at: int) -> Known | None:
        if isinstance(e, ast.Name):
            got = resolve(e.id, scope, at) or (probed or {}).get((e.id, e.lineno))
            return got if got is None or not got.returns else None  # a function isn't a record
        if (  # `rec._asdict()`: a dict with the record's fields
            isinstance(e, ast.Call)
            and isinstance(e.func, ast.Attribute)
            and e.func.attr == "_asdict"
            and isinstance(e.func.value, ast.Name)
            and not e.args
            and not e.keywords
        ):
            got = of_value(e.func.value, scope, at)
            return Known(got.fields, is_dict=True) if got is not None and not (got.is_dict or got.is_open) else None
        if not isinstance(e, ast.Call) or not isinstance(e.func, ast.Name):
            return None
        if e.func.id.startswith("_rec_") and all(k.arg for k in e.keywords) and not e.args:
            return Known(tuple(k.arg for k in e.keywords if k.arg))  # a record literal
        if e.func.id.startswith("_byname_omit_") and len(e.args) == 1 and isinstance(e.args[0], ast.Name):
            # `rest` of `(name=, **rest) = u`: u's fields but those taken (omit_name)
            got = of_value(e.args[0], scope, at)
            taken = set(e.func.id.removeprefix("_byname_omit_").split("__"))
            if got is None or got.is_dict:
                return None
            return Known(tuple(f for f in got.fields if f not in taken), is_open=got.is_open)
        if e.func.id in ("_byname_ctx", "_byname_arg"):  # a spread record: its dict display, in order
            d = next((n for n in ast.walk(e) if isinstance(n, ast.Dict)), None)
            if d is None:
                return None
            out: dict[str, None] = {}
            for k, v in zip(d.keys, d.values):
                if k is None:
                    inner = v.args[0] if isinstance(v, ast.Call) and v.args else v  # _byname_kw(x) / _pick(x, ...)
                    got = of_value(inner, scope, at)
                    if got is None or got.is_open:  # it may hold fields its type doesn't name
                        return None
                    out.update(dict.fromkeys(got.fields))
                elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                    out[k.value] = None
                else:
                    return None
            return Known(tuple(out))
        got = resolve(e.func.id, scope, at)  # a call to a function that shows what it returns (of_def)
        return Known(got.fields) if got is not None and got.returns else None

    def of_type(t: ast.expr, scope: _Scope, at: int) -> Known | None:
        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id.startswith(("_typ_", "_opn_")):
            return Known(class_fields(t.value.id[5:]), is_open=t.value.id.startswith("_opn_"))
        if isinstance(t, ast.Name):
            got = resolve(t.id, scope, at)
            return got if got is None or not (got.is_dict or got.returns) else None  # a dict isn't a type
        return None

    if exports:
        names = (resolve(name, module, len(tree.body)) for name in module.binds)
        return {(name, 0): k for name, k in zip(module.binds, names) if k is not None}
    out: dict[tuple[str, int], Known | None] = {}
    for name, line, scope, at in reads:
        key = (name, line)
        if name == DS:  # byname's own, read on the line of the pattern statement binding it: that binding
            own = [b for b in scope.binds.get(DS, []) if b.line == line]
            got = (of_bind(own[0], DS) if len(own) == 1 else None) or (probed or {}).get(key)
        else:
            got = resolve(name, scope, at) or (probed or {}).get(key)
        out[key] = got if out.get(key, got) == got else None  # two reads on a line that disagree: unknown
    return {key: k for key, k in out.items() if k is not None}


def transform(
    src: str,
    path: str = "<pyn>",
    tolerant: bool = False,
    portable: bool = False,
    checker: bool = False,
    known: dict[tuple[str, int], Known] | None = None,
    lookup: Lookup | None = None,
    untyped: set[tuple[int, int]] | None = None,
    probed: dict[tuple[str, int], Known] | None = None,
    probe: bool = False,
) -> Result:
    """tolerant (editor only): a half-typed pattern item like `na` in `(name=, na) = r` becomes
    `_ds.na` instead of an error, so the checker can complete field names there.
    checker: the translation only type checkers see; works around their bugs (see KW_PRELUDE). Never run.
    known: fields of names read in the file, by (name, line) (known_fields); set by the checker translation's second pass.
    untyped: spread records built where no type is expected (untyped_builds); set by that second pass too.
    lookup: fields of record types imported from other modules (imports.Modules), for known_fields.
    probed: the checker's types of what the file doesn't show (Result.unseen), for known_fields.
    probe: wrap each unseen source in `reveal_type(...)`, so the checker prints its type (srcmap.probed_from)."""
    toks = [
        t for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type not in (tokenize.COMMENT, tokenize.NL)
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
    shorthand_spans: list[tuple[int, int]] = []  # every bare `x=`, see Result.shorthands
    sites: list[tuple[tuple[str, ...], bool, bool]] = []  # checker: (fields, is_open, is_literal), see site_class
    spread_marks: dict[int, str] = {}  # checker: site -> SPREAD's marker (open_build)
    unseen: list[tuple[str, int]] = []  # checker: Result.unseen
    type_fields: list[tuple[str, str, int, int]] = []  # see Result.type_fields

    def literal_class(fields: tuple[str, ...]) -> str:
        if not checker or portable:
            return record_class(fields)
        sites.append((fields, False, True))
        return site_class(record_class(fields), len(sites) - 1)

    label_spans: list[tuple[int, int]] = []  # `x` in `return (x := ..., ...)`: bound to name a position, never read

    def span(a: int, b: int | None = None) -> tuple[int, int]:
        return off(toks[a].start), off(toks[a if b is None else b].end)

    def expand_shorthand(its: list[list[int]]) -> None:
        for it in its:
            if is_short(it):
                at = off(toks[it[1]].end)
                edits.append(Edit(at, at, toks[it[0]].string, span(it[0]), kind="shorthand"))
                shorthand_spans.append(span(it[0]))
                standins.append((at, at, SHORT))

    kw_used = builds = False
    calls = 0  # `_byname_v<n>`: a spread call's value in a known build
    type_groups: set[int] = set()  # `(` of record types, for their fields' types and `(name: str)(...)`
    group_fields: dict[int, tuple[str, ...]] = {}  # `(` of a record type -> its fields (declared_reads)

    def expr_key(it: list[int]) -> tuple[str, int]:
        """A spread of an expression, `**users[0]`, as Result.unseen and probed key it: by where it starts."""
        line, col = toks[it[1]].start
        return f"**{col}", line

    def ask_expr(it: list[int]) -> bool:
        """`**expr`, not a bare name, whose fields the checker wasn't asked yet: listed in unseen. Whether to
        wrap it in `reveal_type(...)` (probe)."""
        if len(it) == 2 and is_name(toks[it[1]]) or expr_key(it) in (probed or {}):
            return False
        unseen.append(expr_key(it))
        return probe

    def wrap_kw(its: list[list[int]], ask: bool = False) -> None:
        # checker only: `**x` -> `**_byname_kw(x)` in calls and dict displays, so the checker sees a record's
        # fields one by one (see RESERVED); what runs is untouched. ask: a spread record's, asked (ask_expr)
        nonlocal kw_used
        if not checker:
            return
        for it in its:
            if len(it) >= 2 and toks[it[0]].string == "**":
                a, b = off(toks[it[1]].start), off(toks[it[-1]].end)
                reveal = ask and ask_expr(it)
                edits.append(Edit(a, a, "_byname_kw(" + "reveal_type(" * reveal, span(it[1], it[-1])))
                edits.append(Edit(b, b, ")" * reveal + ")", span(it[1], it[-1])))
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
        """Checker only: a spread record whose `**` items are all names or calls with known fields
        (known_fields) is built as a plain record, `(**u, age=27)` -> `_rec_name__age(name=u.name, age=27)`,
        `(**make(), age=27)` -> `_rec_name__age(name=(_byname_v0 := make()).name, age=27)`, so it has its
        exact type without an expected type. A later item wins and the first position stays, as at runtime.
        False: left to the generic path, which also reports any mistakes."""
        nonlocal calls
        order: dict[str, list[int]] = {}  # field -> the item that gives it

        def of(t: tokenize.TokenInfo) -> Known | None:
            return (known or {}).get((t.string, t.start[0]))  # the translation keeps the source's lines

        def is_call(it: list[int]) -> bool:  # `**f(...)`
            return len(it) >= 4 and toks[it[2]].string == "(" and pair.get(it[2]) == it[-1]

        once: set[int] = set()  # `**` items read once into a walrus: a call, or an expression the checker typed
        for it in its:
            first = toks[it[0]]
            if first.string == "**":
                bare = len(it) == 2 and is_name(toks[it[1]])
                got = of(toks[it[1]]) if bare or (is_name(toks[it[1]]) and is_call(it)) else None
                if got is not None and got.returns != is_call(it):  # a call to a function showing its fields
                    got = None
                if got is None and not bare:  # what the checker said it is (probed)
                    got = (probed or {}).get(expr_key(it))
                    if got is not None and (got.is_dict or got.returns):
                        got = None
                if got is None or got.is_open:  # open: it may hold fields its type doesn't name
                    return False
                if not bare:
                    once.add(it[0])
                for f in got.fields:
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
        edits.append(Edit(g0, g0, literal_class(fields), (g0, g0 + 1)))
        for it in its:
            if toks[it[0]].string == "**":
                # `**u` -> `name=u.name, age=u.age`: the user's `u` stays real text (hover, colour, rename).
                # A dict from `_asdict()` is read by key: `name=d["name"]`
                name, (first, *rest) = toks[it[1]].string, given[it[0]]
                is_dict = it[0] not in once and (got := of(toks[it[1]])) is not None and got.is_dict
                read = (lambda f: f'["{f}"]') if is_dict else (lambda f: f".{f}")
                head, at, close_call = f"{first}=", off(toks[it[1]].end), ""
                if it[0] in once:  # `**make()`: the call once, as written, its value kept for the other fields
                    name, at, close_call = f"_byname_v{calls}", off(toks[it[-1]].end), ")"
                    head = f"{first}=({name} := "
                    calls += 1
                edits.append(Edit(off(toks[it[0]].start), off(toks[it[0]].end), head, span(it[1])))
                tail = close_call + read(first) + "".join(f", {f}={name}{read(f)}" for f in rest)
                edits.append(Edit(at, at, tail, span(it[1])))
            else:
                # the field name as generated text, like the generic path: the checker would colour a
                # keyword argument over the record-field colour (field_spans)
                f = toks[it[0]].string
                edits.append(Edit(off(toks[it[0]].start), off(toks[it[0]].end), f, span(it[0])))
                field_spans.append(span(it[0]))
        expand_shorthand(its)
        standins.append((g0, g0, PAT))
        return True

    def open_build(i: int, close: int, its: list[list[int]]) -> None:
        """Checker only: a spread record where no type is expected (untyped_builds) is an open record of the
        fields written after its last spread, `(**rest, name=x)` -> `_rec_name___3(**_byname_loose(rest),
        name=x)`, typed `(..., name: str)`: the spreads' fields aren't known, and they may hold any field
        written before them. Its own site (site_class), so `out.name` renames `name=` here."""
        names = []
        for it in its:  # the generic path's checks (build)
            first = toks[it[0]]
            if is_kw(it):
                if first.string.startswith("_") or first.string in RESERVED:
                    raise err(field_error(first.string), first)
                names.append(first.string)
            elif not (len(it) >= 2 and first.string == "**"):
                raise err("a record built from spreads takes `**record` and `name=value` items", first)
        if len(set(names)) != len(names):
            raise err(f"duplicate record field in {tuple(names)}", toks[i])
        last = max(k for k, it in enumerate(its) if toks[it[0]].string == "**")
        fields = tuple(toks[it[0]].string for it in its[last + 1 :])

        def is_open_known(it: list[int]) -> bool:
            got = (known or {}).get((toks[it[1]].string, toks[it[1]].start[0])) if len(it) == 2 else None
            return got is not None and got.is_open

        types[(fields, True)] = None
        sites.append((fields, True, True))
        # the sources whose fields byname can't see: not an open record it knows, which is open by design
        unseen_its = [it for it in its if toks[it[0]].string == "**" and not is_open_known(it)]
        if unseen_its:
            bare = [toks[it[1]].string for it in unseen_its if len(it) == 2 and is_name(toks[it[1]])]
            spread_marks[len(sites) - 1] = SPREAD + "".join(f"__{x}" for x in bare)
        g0 = span(i)[0]
        site = f"{len(sites) - 1}{spread_marks.get(len(sites) - 1, '')}"
        edits.append(Edit(g0, g0, site_class(record_class(fields), site), (g0, g0 + 1)))
        for it in its:
            if toks[it[0]].string == "**":
                a, b = off(toks[it[1]].start), off(toks[it[-1]].end)
                ask = it in unseen_its and len(it) == 2 and is_name(toks[it[1]])
                if ask:
                    unseen.append((toks[it[1]].string, toks[it[1]].start[0]))
                ask = ask and probe or ask_expr(it)
                edits.append(Edit(a, a, "_byname_loose(" + "reveal_type(" * ask, span(it[1], it[-1])))
                edits.append(Edit(b, b, ")" + ")" * ask, span(it[1], it[-1])))
            else:  # the field name as generated text, as in known_build
                f = toks[it[0]].string
                edits.append(Edit(off(toks[it[0]].start), off(toks[it[0]].end), f, span(it[0])))
                field_spans.append(span(it[0]))
        expand_shorthand(its)
        standins.append((g0, g0, PAT))

    built_on: dict[int, int] = {}  # spread records built so far per line, for untyped_builds' (line, k)

    def build(i: int, close: int, its: list[list[int]]) -> None:
        """A record built from spreads and fields, `(**u, age=27)`, as `_byname_rec({**u, 'age': 27})`."""
        nonlocal builds, kw_used
        line = toks[i].start[0]
        at = (line, built_on.get(line, 0))
        built_on[line] = at[1] + 1
        if checker and known and known_build(i, close, its):
            return
        builds = True
        if checker and not portable and untyped and at in untyped:
            open_build(i, close, its)
            return
        names = []
        # checker, no type written: `**name` spreads are read through a lambda default (see below)
        hoist = [it for it in its if toks[it[0]].string == "**" and len(it) == 2 and is_name(toks[it[1]])]
        hoist = hoist if checker else []
        wrap_kw([it for it in its if it not in hoist], ask=checker)
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
                    shorthand_spans.append(span(it[0]))
                    standins.append((at, at, SHORT))
            else:
                raise err("a record built from spreads takes `**record` and `name=value` items", first)
        if len(set(names)) != len(names):
            raise err(f"duplicate record field in {tuple(names)}", toks[i])
        g0, g1 = span(i, close)
        if checker:  # checked against the type expected where it stands, if any
            ctx = "_byname_arg" if is_argument(i, close) else "_byname_ctx"
            # Inside a lambda pyright drops narrowing of a name that's reassigned later, so a rebound `r`
            # would be its whole union there. A default is evaluated where the record stands, narrowed:
            # `lambda _byname_t, _byname_s0=_byname_kw(r): ...{**_byname_pick(r, _byname_s0)}`. The user's
            # `r` stays real text in place (hover, colour); the copy in the default is generated.
            defaults = ""
            for k, it in enumerate(hoist):
                defaults += f", _byname_s{k}=_byname_kw({toks[it[1]].string})"
                a, b = span(it[1])
                name = toks[it[1]]
                ask = (name.string, name.start[0]) not in (known or {})
                if ask:
                    unseen.append((name.string, name.start[0]))
                ask = ask and probe
                edits.append(Edit(a, a, "_byname_pick(" + "reveal_type(" * ask, (a, b)))
                edits.append(Edit(b, b, ")" * ask + f", _byname_s{k})", (a, b)))
                kw_used = True
            pre, post = f"{ctx}(lambda _byname_t{defaults}: _byname_check(lambda: _byname_t)({{", "}))"
        else:
            pre, post = "_byname_rec({", "})"
        edits.append(Edit(g0, g0 + 1, pre, (g0, g1)))
        edits.append(Edit(g1 - 1, g1, post, (g0, g1)))
        standins.append((g0, g0, PAT))  # formatter: a call, `__P(**u, age=27)`

    consumed: set[int] = set()  # `(` of nested pattern groups, handled with their outer pattern
    omits: dict[tuple[str, ...], None] = {}  # checker: field sets an unknown `**rest` leaves out (omit_def)
    rest_defs: dict[str, tuple[tuple[str, ...], bool, tuple[str, ...]]] = {}  # checker: rest_def's, by name
    unknown_rests: list[tuple[int, int]] = []  # checker: Result.unknown_rests
    pattern_line: int | None = None  # the line a pattern statement's bindings go on (rest_of)
    omit_used = False

    def is_rest(item: list[int]) -> bool:
        return len(item) >= 2 and toks[item[0]].string == "**"

    def pattern_items(open_i: int) -> tuple[list[list[int]], list[bool]] | None:
        """A group's items if they read as pattern items: `field=...` and `**rest`, plus (half-typed) bare names."""
        its = items(open_i)
        kw = [is_kw(it) or is_rest(it) for it in its]
        bare = [len(it) == 1 and is_name(toks[it[0]]) for it in its]
        if not (its and any(kw) and all(k or b for k, b in zip(kw, bare))):
            return None
        return its, bare

    def pattern(open_i: int) -> list[tuple[list[int], str, object]]:
        """Parse a pattern group into (item, kind, value) entries, kind one of: short (`f=`),
        target (`f=t`, value the target text), nested (`f=(...)`, value its entries), bare (half-typed),
        rest (`**t`, value the target text). Records the group's stand-ins and field spans as it goes."""
        its, bare = pattern_items(open_i)
        node: list[tuple[list[int], str, object]] = []
        for k, (it, b) in enumerate(zip(its, bare)):
            f = toks[it[0]]
            if is_rest(it):
                # `**rest` collects the fields not named before it, so it goes last, as in `match`'s
                # `{"name": n, **rest}`; a record, in the order the source wrote its fields
                if k != len(its) - 1:
                    raise err("`**rest` goes last in a pattern: it collects the fields not named before it", f)
                text = src[off(toks[it[1]].start) : off(toks[it[-1]].end)]
                if not is_target(text):
                    raise err(f"cannot bind the rest to {text!r}", toks[it[1]])
                standins.append((*span(it[0]), "*"))  # `__P[a:__p, *rest]`: a starred item is valid in a subscript
                node.append((it, "rest", text))
                continue
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

    def omit(node: list, chain: list[int], text: str, marks: list[Mark], line: int | None = None) -> str:
        """Append the rest of `_ds.a.b` (chain) without the fields `node` names: `_byname_omit(_ds, ('name',))`.
        The checker's is rest_of's. line: where a pattern statement's `_ds` is read (None in a loop)."""
        nonlocal omit_used
        taken = tuple(sorted({toks[it[0]].string for it, kind, _ in node if kind != "rest"}))
        omit_used = True
        if checker:
            return rest_of(node, taken, chain, text, marks, line)
        return access(chain, text + "_byname_omit(", marks) + f", {taken!r})"

    def rest_of(node: list, taken: tuple[str, ...], chain: list[int], text: str, marks: list[Mark], line) -> str:
        """Checker only: the rest's type, from the fields of the record it takes apart, `_ds`, as known_fields
        reads them on the second pass. A checker can't take fields out of a type, so the rest is built as a
        spread is (known_build): exact fields known, `(name=, **rest) = u` -> `rest = _rec_age__city(age=_ds.age,
        city=_ds.city)`; an open type, one function from it to its rest (rest_def). Otherwise, a loop's or a
        nested pattern's `_ds`, `users[0]`, a name bound to different shapes: `(...)`, any record, unchecked,
        and the editor says how to give it a type (unknown_rests)."""
        got = (known or {}).get((DS, line)) if line is not None and not chain else None
        if got is not None and not got.is_dict and not got.returns:
            keep = tuple(f for f in got.fields if f not in taken)
            if keep and not got.is_open:
                records[keep] = None
                return text + literal_class(keep) + "(" + ", ".join(f"{f}={DS}.{f}" for f in keep) + ")"
            name = f"_byname_rest{len(rest_defs)}"
            rest_defs[name] = (got.fields, got.is_open, taken)
            types.update(dict.fromkeys([(got.fields, got.is_open), (keep, got.is_open)]))
            return text + name + f"({DS})"
        rest = next(it for it, kind, _ in node if kind == "rest")
        unknown_rests.append(span(rest[0], rest[-1]))
        omits[taken] = None
        types[(), True] = None
        if line is not None and not chain:
            unseen.append((DS, line))
            if probe:
                return text + omit_name(taken) + f"(reveal_type({DS}))"
        return access(chain, text + omit_name(taken) + "(", marks) + ")"

    def bindings(node: list, chain: list[int], text: str, marks: list[Mark], ann: tuple[int, int] | None) -> str:
        """Append `; local = _ds.field` for each item of a parsed pattern, marked back to the source.
        (field=) binds local `field`; (field=target) binds `target`; (field=(...)) reads deeper;
        (**rest) binds `rest` to a record of the fields not named.
        ann: the source span of the pattern's `Final`, copied onto every local it binds, `local: Final = ...`."""

        def assign(text: str) -> str:
            if ann is not None:
                text += ": "
                marks.append(Mark(len(text), len(text) + ann[1] - ann[0], *ann))
                text += src[ann[0] : ann[1]]
            return text + " = "

        for it, kind, val in node:
            if kind == "nested":
                text = bindings(val, [*chain, it[0]], text, marks, ann)
                continue
            text += "; "
            if kind == "rest":
                ts = len(text)
                text += val
                marks.append(Mark(ts, len(text), *span(it[1], it[-1])))
                text = omit(node, chain, assign(text), marks, pattern_line)
                continue
            if kind == "short":  # target is implicit: insertion point right after `field=`
                shorthand_spans.append(span(it[0]))
                ts, at = len(text), off(toks[it[1]].end)
                text += toks[it[0]].string
                marks.append(Mark(ts, len(text), at, at, span(it[0])))
                text = assign(text)
            elif kind == "target":
                ts = len(text)
                text += val
                marks.append(Mark(ts, len(text), *span(it[2], it[-1])))
                text = assign(text)
            # bare (half-typed): just the attribute access, for completion
            text = access([*chain, it[0]], text, marks)
        return text

    def no_default_shorthand(its: list[list[int]], level: int | None) -> None:
        """No shorthand in a parameter's default (`def` or `lambda`), however deep: a default is evaluated
        where the function is defined, so `name=` there would quietly take the outer `name`, though in a
        signature it reads as the parameter. `name=name` says so."""
        for it in its:
            eq = next((k for k in it if toks[k].string == "=" and parent.get(k) == level), None)
            if eq is None:
                continue
            for k in range(eq + 1, it[-1]):
                if (
                    is_name(toks[k])
                    and toks[k + 1].string == "="
                    and toks[k + 2].string in (",", ")")
                    and k + 1 in parent
                ):
                    f = toks[k].string
                    raise err(
                        f"{f}= in a parameter's default would take the outer {f!r}: write {f}={f} to mean that", toks[k]
                    )

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

    def no_parameter_patterns(o: int) -> None:
        """No pattern in a signature: `def f((name=, age=): User)` is an error.
        In a signature `name=` means a default and nothing else; Python 3 removed tuple parameters too
        (PEP 3113): the parameter would have no name, so it couldn't be passed by keyword or shown."""
        no_default_shorthand(items(o), o)
        for it in items(o):
            g = it[0]
            if toks[g].string == "(" and g in pair and pattern_items(g) is not None:
                raise err("can't destructure a parameter: give it a name and destructure it in the body", toks[g])

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
            if (
                t.string != "return"
                or t.type != tokenize.NAME
                or toks[r - 1].string not in (";", ":")
                and toks[r - 1].type not in STMT_START
            ):
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

        # def f(...) / def f[T](...) / class C(...): parameter lists, left alone
        head = rpair[i - 1] - 1 if prev is not None and prev.string == "]" else i - 1  # the name, past `[T]`
        if head >= 1 and toks[head - 1].string in ("def", "class") and is_name(toks[head]):
            if toks[head - 1].string == "def":
                no_parameter_patterns(i)
            continue

        # calling a record type, `(name: str, age: int)(name="r", age=1)`: a type alias can't be called, and
        # a literal (annotated if it needs the type) or record(T, d) already builds one
        if prev is not None and prev.string == ")" and rpair.get(i - 1) in type_groups:
            raise err("can't call a record type: write the record, (name=...), annotated if it needs the type", t)

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
            group_fields[i] = fields
            group = span(i, close)
            standins.append((group[0], group[0] + 1, TYP + "["))
            standins.append((group[1] - 1, group[1], "]"))
            if not fields:  # `(...)`: a record with any fields, a class with no type parameters
                edits.append(Edit(*group, type_class(fields, is_open), group))
                continue
            name = type_class(fields, is_open)
            if checker and not portable:
                sites.append((fields, is_open, False))
                name = site_class(name, len(sites) - 1)
                type_fields.extend((name, toks[it[0]].string, *span(it[0])) for it in named)
            edits.append(Edit(group[0], group[0] + 1, name + "[", group))
            edits.append(Edit(group[1] - 1, group[1], "]", group))
            for it in named:
                edits.append(Edit(off(toks[it[0]].start), off(toks[it[2]].start), "", span(it[0])))
                field_spans.append(span(it[0]))
            if is_open:  # drop `..., `: the Protocol's name says it's open
                d = its[0][0]
                edits.append(Edit(off(toks[d].start), off(toks[named[0][0]].start), "", span(d)))
            continue
        at_stmt_start = prev is None or prev.type in STMT_START or prev.string == ";"
        eq = annotation_eq(toks, pair, close + 1) if at_stmt_start else None  # `(...): Final = r`
        is_pattern = at_stmt_start and (toks[close + 1].string == "=" or eq is not None)
        is_for = prev is not None and prev.string == "for" and toks[close + 1].string == "in"
        if prev is not None and prev.string == "for" and toks[close + 1].string == ":" and pattern_items(i):
            raise err("a loop pattern can't be Final: the loop binds its names again on every pass", toks[close + 1])
        # a record built from spreads: `(**u, **r, age=27)`; as a pattern, `(name=, **rest) = u`, it's a rest
        if any(toks[it[0]].string == "**" for it in its) and not (is_pattern or is_for):
            build(i, close, its)
            continue
        kw = [is_kw(it) for it in its]
        # a pattern with some `field=` items and some bare names: half-typed, or a mistake (pattern() says so)
        if not all(kw) and not ((is_pattern or is_for) and pattern_items(i) is not None):
            if (is_pattern or is_for) and any(toks[it[0]].string == "**" for it in its):
                raise err("a pattern takes `field=`, `field=target` and a last `**rest`", t)
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
                    elif kind == "rest":  # `**rest` -> `rest`
                        edits.append(Edit(*span(it[0]), "", span(it[0])))
                    elif kind == "short":
                        edits.append(Edit(*span(it[1]), "", span(it[0])))
                        shorthand_spans.append(span(it[0]))
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
                    elif kind == "rest":  # read on the line of `in`, where the element goes (rest_of)
                        text = omit(node, chain, text, marks, toks[close + 1].start[0])
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
            # `(...): Final = r`: the annotation leaves `_ds` (each pattern rebinds it) for every local bound
            ann = None
            replaced = group
            if eq is not None:
                ann = span(close + 2, eq - 1)
                replaced = (group[0], ann[1])
                if not is_final(toks[close + 2 : eq]):
                    msg = "a pattern's annotation can only be Final: its fields have their types already"
                    if not tolerant:
                        raise err(msg, toks[close + 2])
                    problems.append((*ann, msg))  # half-typed, `(a=): Fin = r`: bound as if unannotated
                    ann = None
            pattern_line = toks[j - 1].end[0]  # where the bindings go, so where `_ds` is read (rest_of)
            binds = bindings(tree, [], "", marks, ann)
            stmt_end = off(toks[j - 1].end)
            # a pattern over several lines keeps its line breaks, `(_ds\n\n)`, so the lines after it keep
            # their numbers (tracebacks, source_ast)
            breaks = src.count("\n", *replaced)
            edits.append(Edit(*replaced, f"({DS}{chr(10) * breaks})" if breaks else DS, group))
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
        edits.append(Edit(at, at, literal_class(fields), (at, at + 1)))
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
    # checker: spread records and rests of names whose fields the file shows get their exact type (known_build,
    # rest_of)
    if checker and known is None and (builds or omit_used):
        k, u = known_fields(body, lookup, probed=probed), untyped_builds(body)
        if k or u or probe or probed:
            return transform(src, path, tolerant, portable, checker, k, lookup, u, probed, probe)

    def base_prelude() -> str:
        # the checker's translation never runs, so it doesn't read the project's config
        return prelude_for(ITER_CALLERS if checker else iter_callers(path))

    prelude = ""
    # checker: the rests' functions (rest_of); the types they take and return are in `types`
    omit_defs = "".join(omit_def(taken) for taken in omits)
    omit_defs += "".join(rest_def(name, *d) for name, d in rest_defs.items())
    if records or types:  # only files with records: plain Python is left exactly as written
        prelude = base_prelude()
        if not portable:
            dicts = dict.fromkeys([*records, *(f for f, o in types if not o)])
            arity = max(len(f) for f in [*records, *(f for f, _ in types), *(s[0] for s in sites)])
            prelude += CHECKER_DIRECTIVE + TYPED_PRELUDE + covariant_defs(arity)
            prelude += "".join(dict_def(f) for f in dicts)
        if not portable:  # in the checker, each record is its exact type (see record_def)
            types = {**types, **dict.fromkeys((f, False) for f in records)}
        # types first: a record's checker function returns its type, and before Python 3.14 an annotation
        # naming a class defined further down is an undefined name (every record would be Unknown)
        if not portable:
            prelude += "".join(f"type {key_alias(f)} = str\n" for f in dict.fromkeys(f for f, _ in types))
        prelude += "".join(type_def(f, o, portable) for f, o in types)
        prelude += "".join(record_def(f, portable) for f in records)
        for n, (f, is_open, literal) in enumerate(sites):
            prelude += site_def(f, is_open, f"{n}{spread_marks.get(n, '')}", literal)
    if builds or kw_used or omit_used:
        if not prelude:
            prelude = base_prelude()
        if not portable:
            prelude += HASDICT + (KW_PRELUDE if kw_used else "")
        rec = builds or (omit_used and not checker)  # the checker types a rest with rest_of instead
        if rec and not portable:
            prelude += BUILD_PRELUDE + (BUILD_REC_TYPED if checker else "")
            prelude += CTX_PRELUDE if checker and builds else ""
        if omit_used and not checker:
            sig = (
                "def _byname_omit(r, taken):"
                if portable
                else "def _byname_omit(r: _A, taken: tuple[str, ...], /) -> _A:"
            )
            prelude += OMIT_REC.replace("{sig}", sig)
        prelude += omit_defs
    if tuples:
        prelude += "".join(tuple_def(t) for t in tuples)
    if record_used:
        prelude += "from typing import cast as _cast\n"

    def declared_reads() -> list[tuple[int, int, str]]:
        """`p["msg"]` where `p` has a written record type declaring `msg`: a parameter (`p: (..., msg: str)`,
        read in its function's body) or an annotated name (read in the rest of its block), the type inline
        or a `type` alias of one. From the tokens, no parse: it runs per edit. Brackets are the unchecked
        read, so a declared field is better read with a dot (the editor hints so, with a quick fix)."""
        depth, d = [], 0
        for t in toks:
            d += t.type == tokenize.INDENT
            d -= t.type == tokenize.DEDENT
            depth.append(d)
        aliases = {  # `type Cfg = (..., lr: float)`
            toks[k + 1].string: group_fields[k + 3]
            for k in range(len(toks) - 3)
            if toks[k].string == "type" and toks[k + 2].string == "=" and k + 3 in group_fields
        }

        def body(o: int) -> tuple[int, int]:
            """A def's body, its parameter list opening at toks[o]: an indented block or the rest of the line."""
            start = next(j for j in range(pair[o] + 1, len(toks)) if toks[j].string == ":" and j not in parent)
            if toks[start + 1].type == tokenize.NEWLINE and toks[start + 2].type == tokenize.INDENT:
                return start, next((j for j in range(start + 3, len(toks)) if depth[j] <= depth[start]), len(toks))
            return start, next((j for j in range(start, len(toks)) if toks[j].type == tokenize.NEWLINE), len(toks))

        # a nested def, lambda or comprehension binding the name again: there it's another variable
        shadows: list[tuple[str, int, int]] = []  # (name, first token, end token)
        for k, t in enumerate(toks):
            if t.string == "def" and k + 2 < len(toks) and toks[k + 2].string == "(":
                start, end = body(k + 2)
                names = [toks[it[0]].string for it in items(k + 2) if is_name(toks[it[0]])]
                names += [toks[it[1]].string for it in items(k + 2) if len(it) > 1 and toks[it[0]].string in "**"]
                # and the names its body binds, which are its locals unless declared global or nonlocal
                kept = {toks[j + 1].string for j in range(start, end) if toks[j].string in ("global", "nonlocal")}
                for j in range(start + 1, end - 1):
                    prev, nxt = toks[j - 1], toks[j + 1]
                    stmt = prev.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT) or prev.string == ";"
                    if is_name(toks[j]) and (
                        (stmt and (nxt.string == "=" or nxt.string.endswith("=") and nxt.string not in ("==", "!=")))
                        or nxt.string in (":=",)
                        or (prev.string in ("for", "as", "import") and j not in parent)
                    ):
                        names.append(toks[j].string)
                shadows += [(n, start, end) for n in names if n not in kept]
            elif t.string in ("lambda", "for") and (t.string == "lambda" or k in parent):
                stop = ":" if t.string == "lambda" else "in"
                j = k + 1
                while j < len(toks) and not (toks[j].string == stop and parent.get(j) == parent.get(k)):
                    j += 1
                o = parent.get(k)  # the bracket a lambda or comprehension stands in, or else its line
                end = (
                    pair[o]
                    if o is not None
                    else next((x for x in range(k, len(toks)) if toks[x].type == tokenize.NEWLINE), len(toks))
                )
                first = k if t.string == "lambda" else o
                shadows += [(toks[x].string, first, end) for x in range(k + 1, j) if is_name(toks[x])]

        declared: list[tuple[str, tuple[str, ...], int, int]] = []  # (name, fields, first token, end token)
        for k, t in enumerate(toks[:-2]):
            if t.string != ":" or not k or not is_name(toks[k - 1]):
                continue
            ann = toks[k + 1]
            fields = group_fields.get(k + 1) or (aliases.get(ann.string) if is_name(ann) else None)
            if not fields:
                continue
            o = parent.get(k)
            if o is not None and toks[o].string == "(" and o >= 2 and toks[o - 2].string == "def":
                start, end = body(o)  # a parameter: read in its function's body
            else:  # an annotated name: read in the rest of its block
                start = k
                end = next((j for j in range(k + 1, len(toks)) if depth[j] < depth[k]), len(toks))
            declared.append((toks[k - 1].string, fields, start, end))
        out = []
        for k in range(len(toks) - 3):
            name, opening, key, closing = toks[k : k + 4]
            if not (is_name(name) and opening.string == "[" and key.type == tokenize.STRING and closing.string == "]"):
                continue
            after = toks[k + 4].string if k + 4 < len(toks) else ""
            if (
                after == "="
                or (after.endswith("=") and after not in ("==", "<=", ">=", "!="))
                or toks[k - 1].string == "del"
            ):
                continue  # a store, not a read
            try:
                f = ast.literal_eval(key.string)
            except (ValueError, SyntaxError):
                continue  # an f-string
            if any(
                n == name.string
                and f in fields
                and a < k < b
                and not any(m == n and a < s < k < e for m, s, e in shadows)  # bound again in between
                for n, fields, a, b in declared
            ):
                out.append((off(name.end), off(closing.end), f))
        return out

    standins.sort()
    return Result(
        prelude,
        body,
        edits,
        problems,
        standins,
        sorted(field_spans),
        sorted(label_spans),
        sorted(shorthand_spans),
        type_fields,
        declared_reads() if checker and tolerant else [],
        unknown_rests,
        unseen,
    )


def pattern_slot(src: str, at: int) -> tuple[int, int, int, list[str]] | None:
    """If offset `at` is a field position in a destructuring pattern `(...) = expr`, `(...): Final = expr` or
    `for (...) in xs` (an empty item, or the field name being typed):
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
    stack: list[int] = []
    for i, t in enumerate(toks):
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
        if prev is not None and prev.string == "for":  # for (...) in xs
            if toks[close + 1].string != "in":
                continue
        elif not (prev is None or prev.type in STMT_START or prev.string == ";") or (
            toks[close + 1].string != "=" and annotation_eq(toks, pair, close + 1) is None
        ):
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
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        i = 1
    while i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__":
        i += 1
    return i


def source_ast(src: str, path: str = "<pyn>") -> ast.Module:
    """The translation's AST with the .pyn file's own line numbers, without the prelude. For tools that
    match checker output back to source constructs (e.g. which function a line is in)."""
    return ast.parse(transform(src, path).body, path)


# The prelude is compiled into the module, so its functions would carry the .pyn's name and their own line
# numbers, lines the file doesn't have (or other lines of it). Its lines are moved past this one before
# compiling, then its code objects are given back their own lines under their own name (compile_ast).
PRELUDE_LINE = 1_000_000


def prelude_file(path: str) -> str:
    """The filename tracebacks, debuggers and coverage show for the prelude's code: its own per file, as each
    file's prelude differs (its record classes, its iter callers)."""
    return f"<byname prelude: {path}>"


def to_ast(src: str, path: str) -> ast.Module:
    """The module that runs, as an AST, prelude included: what to_code compiles, with compile_ast. Body line
    numbers match the .pyn file exactly; the prelude's are past PRELUDE_LINE. For tools that rewrite it first
    (the pytest plugin rewrites asserts). Puts the prelude's text in linecache, so tracebacks show its lines."""
    r = transform(src, path)
    tree = ast.parse(r.body, path)
    if r.prelude:
        prelude = ast.parse(r.prelude)
        ast.increment_lineno(prelude, PRELUDE_LINE)
        k = prelude_index(tree)
        tree.body[k:k] = prelude.body
        lines = r.prelude.splitlines(keepends=True)
        linecache.cache[prelude_file(path)] = (len(r.prelude), None, lines, prelude_file(path))
    return tree


def compile_ast(tree: ast.Module, path: str) -> types.CodeType:
    """Compile to_ast's module: each code object of the prelude's (its functions, and what's nested in them)
    gets the prelude's filename and lines. Line numbers are stored relative to co_firstlineno, so moving that
    moves them all. The prelude's module-level statements stay in the module's code, past PRELUDE_LINE."""

    def relabel(code: types.CodeType) -> types.CodeType:
        consts = tuple(relabel(c) if isinstance(c, types.CodeType) else c for c in code.co_consts)
        if code.co_firstlineno > PRELUDE_LINE:
            first = code.co_firstlineno - PRELUDE_LINE
            return code.replace(co_consts=consts, co_filename=prelude_file(path), co_firstlineno=first)
        return code.replace(co_consts=consts)

    return relabel(compile(tree, path, "exec", dont_inherit=True))


def to_code(src: str, path: str) -> types.CodeType:
    """Compile .pyn source. Body line numbers match the .pyn file exactly."""
    return compile_ast(to_ast(src, path), path)


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
