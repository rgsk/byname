"""byname lsp: language server for .pyn, .py and notebooks, proxying a Python type checker.

    editor <--LSP--> byname lsp <--LSP--> checker (basedpyright-langserver by default)

The checker runs on a mirror of the project (tools.mirror, as `byname tool` does): each .pyn is its
translation there, .py and config files are symlinks, so the project's checker config, import paths
and executionEnvironments apply to a .pyn as to a .py beside it. Every URI is mapped between the
project and the mirror; open documents are sent in memory under their mirror URI. Positions in a .pyn
are mapped both ways; everything else passes through untouched.

.py files go to the checker as they are: byname syntax can't run there. So do notebooks, unless a
cell loads byname (`%load_ext byname`, see notebook.py): then every cell is translated on its own,
as the kernel runs it, under its URI in the mirror, where the checker chains cells itself.
"""

import ast
import copy
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import url2pathname

from .output import output_path, render
from .srcmap import PROBE_MARK, PROBE_ROUNDS, PROBED, LineIndex, Probed, Translation, probed_from
from .transform import DS, FIELDSET, ORDER, REPR, BynameError, pattern_slot

DEFAULT_CHECKER = ["basedpyright-langserver", "--stdio"]
DROP = object()  # a result element whose position fell inside generated-only code
WRITE_OUTPUT = "byname.server.writeOutput"  # executeCommand: write <file>.pyn.py now (Alt+C)
PLACEHOLDER = "__byname_slot"  # stands in for an empty pattern item while completing
EXTRA_TRIGGERS = ["(", ","]  # pop completion in pattern slots, like TS does after `{` / `,`
FIX_ALL_KIND, ORGANIZE_KIND = "source.fixAll", "source.organizeImports"  # ruff code actions, served by us
# whole-notebook actions, asked on save once, on the first cell (notebook.codeActionsOnSave's `notebook.*`
# kinds; notebook.defaultFormatter picks among `notebook.format` ones, which it doesn't do for cells: they go
# to the cell language's default formatter). The first cell may be markdown, so the VS Code extension offers
# them and asks us on the first code cell. Ruff sees the whole notebook: an import used in a later cell is used
NOTEBOOK_FORMAT, NOTEBOOK_FIX_ALL, NOTEBOOK_ORGANIZE = (
    "notebook.format",
    "notebook.source.fixAll",
    "notebook.source.organizeImports",
)
METHOD, VARIABLE, CONSTANT = 2, 6, 21  # CompletionItemKind
# a cell that makes the notebook's cells .pyn (IPython takes one module per %load_ext)
LOAD_EXT_RE = re.compile(r"^[ \t]*%load_ext[ \t]+byname[ \t]*(?:#.*)?$", re.MULTILINE)
# methods records and tuples bring along (byname's `keys`, tuple's); a class's own methods stay
MACHINERY = {"keys", "count", "index"}


# --- JSON-RPC framing ---------------------------------------------------------


def read_message(stream) -> dict | None:
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode("ascii").partition(":")
        if key.lower() == "content-length":
            length = int(value)
    if length is None:
        return None
    return json.loads(stream.read(length))


OWN_REQUEST = "byname-"  # the id prefix of the proxy's own requests to the editor, whose answers it drops


class Writer:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def send(self, msg: dict) -> None:
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with self.lock:
            self.stream.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self.stream.flush()


# --- helpers -----------------------------------------------------------------


def source_kind(params: dict) -> str | None:
    """FIX_ALL_KIND / ORGANIZE_KIND if this codeAction request asks only for one of them (as
    codeActionsOnSave does), else None: the checker's quick fixes go through as before."""
    only = params.get("context", {}).get("only") or []
    for kind in (FIX_ALL_KIND, ORGANIZE_KIND):
        if only and all(k == kind or k.startswith(kind + ".") for k in only):
            return kind
    return None


def uri_to_path(uri: str) -> Path:
    return Path(url2pathname(urlparse(uri).path))


REC_RE = re.compile(r"_(rec|dct|typ|opn|tup)_(\w+)|_(opn|typ|rec|dct)_(?!\w)")
GENERATED_PREFIXES = ("_rec_", "_dct_", "_typ_", "_opn_", "_tup_")
# an exact record type's field-set mismatch, as pyright reports it: "...Literal['a,b,c']" ... "...Literal['a,b']"
FIELDSET_RE = re.compile(r"""Literal\['([\w,]*)'\]" is not assignable to type "(?:\(\) -> )?Literal\['([\w,]*)'\]""")
TARGET_RE = re.compile(r'is incompatible with protocol "_typ_(\w+?)\[')
# both full field sets from the first protocol line; the Literal lines can be cut short ('...vocab_si…')
PROTOCOL_RE = re.compile(r'"_(?:rec|typ|opn)_(\w+?)\[[^"]*" is incompatible with protocol "_typ_(\w+?)\[')
# `(name=, **rest) = x` where x isn't a record (see omit_def in transform.py)
OMIT_RE = re.compile(
    r'Argument of type "(.*?)" cannot be assigned to parameter "x" of type "[^"]*" in function "_byname_omit_\w*"'
)
LITERAL_RE = re.compile(r"Literal\['([\w,]*)'\]")
DICT_MISSING_RE = re.compile(r'"(\w+)" is required in "_dct_')
DICT_EXTRA_RE = re.compile(r'"(\w+)" is an undefined item in type "_dct_')
# a record's field type mismatch: records are their exact type in the checker (see record_def), so two of the
# same field set are compared by type argument, and pyright names the type parameter rather than the field
# a record read by position, which records don't allow (see record_def in transform.py)
# (`rec[0]`: `__getitem__` takes a `_key_<fields>`, see key_alias in transform.py)
POSITIONAL_RE = re.compile(
    r'"_(?:typ|opn)_(\w+?)\[[^"]*" is not iterable|of type "_key_(\w+)" in function "__getitem__"'
)
# a record ordered (`sorted(recs)`, `max(recs)`, `a < b`), which records don't allow: pyright's message is a page
# about SupportsRichComparison
UNORDERED_RE = re.compile(
    r'"_(?:typ|opn)_(\w+?)\[[^"]*" is incompatible with protocol "SupportsDunder[LG]T'
    r'|Operator "[<>]=?" not supported for types "_(?:typ|opn)_(\w+?)\['
)
# a target list read by position: `x, y` / `(x, _)` / `x,`
TARGETS_RE = re.compile(r"\(?\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*,?\s*\)?")
VARIANCE_RE = re.compile(
    r'Type parameter "_byname_T(\d+)@_(?:typ|opn)_(\w+?)" is \w+, but "(.*)" is not (?:the same as|a subtype of|assignable to) "(.*)"'
)
KEY_RE = re.compile(r'"_key_\w*"')
# a field read with a dot on an open record type that doesn't declare it (after unsite)
UNDECLARED_RE = re.compile(r'Cannot access attribute "(\w+)" for class "(_opn_\w*(?:\[[^"]*\])?)"')
RULE_RE = re.compile(r"\s*\((report\w+)\)\s*$")
CODE_KEYS = {"newText", "insertText", "filterText", "sortText", "uri", "targetUri", "data"}
# keys whose string values are document URIs (a notebook cell's `document` is one, in a structure change)
URI_KEYS = {"uri", "targetUri", "document", "oldUri", "newUri"}
URI_SCHEMES = ("file:", "vscode-notebook-cell:")


def call_after_comma(src: str, cursor: int) -> int | None:
    """The offset of the call's `(` if `cursor` is an empty argument right after a comma in a call,
    `f(a=1, |)`; else None. Brackets in strings and comments before the cursor can mislead it."""
    j = cursor
    while j > 0 and src[j - 1] in " \t":
        j -= 1
    if j == 0 or src[j - 1] != ",":
        return None
    depth, k = 0, j - 1
    while k > 0:
        k -= 1
        ch = src[k]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth == 0:
                break
            depth -= 1
    else:
        return None
    if src[k] != "(":
        return None
    before = src[:k].rstrip()
    if not before or not (before[-1].isidentifier() or before[-1].isdigit() or before[-1] in ")]"):
        return None  # a tuple or a group, not a call
    words = before.split()
    if keyword.iskeyword(re.split(r"\W", words[-1])[-1]):
        return None  # `return (a=1, |)`: a group after a keyword, not a call
    if len(words) >= 2 and words[-2] in ("def", "class"):
        return None  # a signature, not a call
    return k


RECORD_TARGET_RE = re.compile(r"^\s*[A-Za-z_][\w.]*\s*:(?P<annotation>[^=]+)=\s*$")
FLOW_RE = re.compile(r"\b(?P<kw>return|yield(?P<frm>\s+from)?)\s*$")  # `return (`, `yield (`, `yield from [`
ARGUMENT_RE = re.compile(r"(?:[(,]|[A-Za-z_]\w*\s*=)\s*$")  # what comes before an argument: `f(`, `, `, `name=`
RECORD_ITEM_RE = re.compile(r"^\s*(\*\*\s*[\w.]+|[A-Za-z_]\w*\s*=.*)\s*$", re.DOTALL)


SEQUENCES = {"list", "set", "frozenset", "Sequence", "MutableSequence", "Iterable", "Collection", "AbstractSet"}
SEQUENCES |= {"MutableSet", "Iterator", "deque"}
MAPPINGS = {"dict", "Mapping", "MutableMapping", "defaultdict", "OrderedDict"}
YIELDS = {"Iterator", "Iterable", "AsyncIterator", "AsyncIterable", "Generator", "AsyncGenerator"}


def split_top(text: str, sep: str) -> list[str]:
    """`text` split at each `sep` outside brackets."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def container_step(src: str, k: int, close: int) -> tuple[int, int, tuple[str, int | str] | None] | None:
    """The list, set, dict or tuple that src[k:close+1] is a whole item of, or the comprehension it's the element
    of: (its opener, its closer, the step in: ("item", 0) in a list or set or a comprehension's element, ("key", 0)
    or ("value", 0) in a dict, ("tuple", i) in a tuple, ("field", name) as `name=` in a record literal, None in a
    plain group); None if it's an item of nothing (a call's argument, a subscript, part of a larger expression)."""
    depth, o = 0, k
    while o > 0:
        o -= 1
        if src[o] in ")]}":
            depth += 1
        elif src[o] in "([{":
            if depth == 0:
                break
            depth -= 1
    else:
        return None
    if src[o] not in "([{":
        return None
    depth, c = 0, None
    for j in range(o + 1, len(src)):
        if src[j] in "([{":
            depth += 1
        elif src[j] in ")]}":
            if depth == 0:
                c = j
                break
            depth -= 1
    if c is None:
        return None
    before = src[:o].rstrip()
    word = re.search(r"\w*$", before)[0]
    if src[o] in "([" and before and (before[-1] in ")]" or (word and not keyword.iskeyword(word))):
        return None  # a call or a subscript
    items = split_top(src[o + 1 : k] + "\0" + src[close + 1 : c], ",")
    i = next(n for n, it in enumerate(items) if "\0" in it)
    head, tail = items[i].split("\0")
    comprehension = len(items) == 1 and re.match(r"\s*(async\s+)?for\b", tail)
    if tail.strip() and not comprehension:
        if src[o] == "{" and not head.strip() and tail.lstrip().startswith(":"):
            return o, c, ("key", 0)
        return None
    if src[o] == "{":
        colons = split_top(head, ":")
        if len(colons) == 1 and not head.strip():
            return o, c, ("item", 0)
        if len(colons) == 2 and colons[0].strip() and not colons[1].strip() and "**" not in colons[0]:
            return o, c, ("value", 0)
        return None
    if src[o] == "(" and (m := re.fullmatch(r"\s*([A-Za-z_]\w*)\s*=\s*", head)):
        if all(RECORD_ITEM_RE.match(it) or not it.strip() for n, it in enumerate(items) if n != i):
            return o, c, ("field", m[1])
        return None
    if head.strip():
        return None
    if src[o] == "[" or comprehension:
        return o, c, ("item", 0)
    return o, c, (("tuple", i) if len(items) > 1 else None)


def element_type(written: str, steps: list[tuple[str, int]]) -> str | None:
    """The type expected at the end of `steps` in a value of type `written`, as written: list[User] and
    ("item", 0) -> User; a union keeps the members that have one (`list[User] | None` -> User); None if none do.
    Besides container_step's steps, ("yield", 0) is what a generator annotated `written` yields and ("result", 0)
    what it returns (Generator[Y, S, R] -> R)."""
    for kind, i in steps:
        found = []
        for member in split_top(written, "|"):
            m = re.fullmatch(r"\s*([\w.]+)\s*\[(.*)\]\s*", member, re.DOTALL)
            if m is None:
                continue
            name, args = m[1].rsplit(".", 1)[-1], [a.strip() for a in split_top(m[2], ",")]
            if kind == "item" and name in SEQUENCES and len(args) == 1:
                found.append(args[0])
            elif kind in ("key", "value") and name in MAPPINGS and len(args) == 2:
                found.append(args[kind == "value"])
            elif kind == "yield" and name in YIELDS:
                found.append(args[0])
            elif kind == "result" and name == "Generator" and len(args) == 3:
                found.append(args[2])
            elif kind == "tuple" and name in SEQUENCES and len(args) == 1:
                found.append(args[0])
            elif kind == "tuple" and name in ("tuple", "Tuple"):
                if len(args) == 2 and args[1] == "...":
                    found.append(args[0])
                elif i < len(args):
                    found.append(args[i])
        if not found:
            return None
        written = " | ".join(found)
    return written


def expected_value(written: str, steps: list[tuple[str, int | str]]) -> str | None:
    """An expression of the type expected at the end of `steps` in a value of type `written`, for the checker to
    complete on: `_t.cast(T, None)`, T read off `written` while the steps can be (element_type, which keeps a
    union's members that fit: `list[User] | None`). A field's type is the checker's to know (`(lead: User)` may be
    named `Team`), as is an alias's (`type Users = list[User]`), so from the first step that can't be read off the
    text each step is an expression: `.lead`, `next(iter(...))` for an item or what a generator yields, `[i]` for a
    tuple's. A generator's return type has no expression: None. `None` in a union is left out of the cast: a
    literal there isn't it, and `iter()` of an Optional has no element type (`Users | None`)."""
    n = 0
    while n < len(steps) and steps[n][0] != "field" and (inner := element_type(written, steps[n : n + 1])):
        written, n = inner, n + 1
    written = " | ".join(m.strip() for m in split_top(written, "|") if m.strip() != "None") or written
    value = f"_t.cast({written}, None)"
    for kind, i in steps[n:]:
        if kind == "field":
            value = f"{value}.{i}"
        elif kind in ("item", "key", "yield"):
            value = f"next(iter({value}))"
        elif kind == "value":
            value = f"next(iter({value}.values()))"
        elif kind == "tuple":
            value = f"{value}[{i}]"
        else:  # a generator's return
            return None
    return value


def record_literal_slot(src: str, cursor: int) -> tuple[int, int, int, int, set[str]] | None:
    """(the `(`, its `)`, word start, word end, the fields given) if `cursor` is at a field position of a record
    literal, `(name=, |)`; else None. Every other item must be `name=...` or `**spread`, so a parenthesized
    expression is left alone."""
    ws = cursor
    while ws > 0 and (src[ws - 1].isalnum() or src[ws - 1] == "_"):
        ws -= 1
    we = cursor
    while we < len(src) and (src[we].isalnum() or src[we] == "_"):
        we += 1
    depth, k = 0, ws
    while k > 0:
        k -= 1
        ch = src[k]
        if ch in ")]}":
            depth += 1
        elif ch in "([{":
            if depth == 0:
                break
            depth -= 1
    else:
        return None
    if src[k] != "(":
        return None
    depth, close = 0, None
    for j in range(k + 1, len(src)):
        ch = src[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                close = j
                break
            depth -= 1
    if close is None:
        return None
    inner = src[k + 1 : ws] + "\0" + src[we:close]  # \0: the item being typed
    items, depth, cur = [], 0, ""
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    given = set()
    for it in items:
        if "\0" in it:
            if it.strip() != "\0":
                return None  # typing a value, not a field name
        elif it.strip() and not RECORD_ITEM_RE.match(it):
            return None
        elif "=" in it:
            given.add(it.split("=", 1)[0].strip())
    return k, close, ws, we, given


def parameter_type(help: dict | None) -> str | None:
    """The active parameter's type in a signatureHelp result, written as byname: `schedule: T = d` -> T."""
    sigs = (help or {}).get("signatures") or []
    if not sigs:
        return None
    sig = sigs[min(help.get("activeSignature") or 0, len(sigs) - 1)]
    active, params = sig.get("activeParameter", help.get("activeParameter")), sig.get("parameters") or []
    if not isinstance(active, int) or not 0 <= active < len(params):
        return None
    label = params[active].get("label")
    if isinstance(label, list):
        label = sig.get("label", "")[label[0] : label[1]]
    _, colon, written = label.partition(": ")
    return pretty(written.split(" = ", 1)[0].strip()) if colon else None


SITE_RE = re.compile(r"(_(?:rec|typ|opn)_\w*?)___\d+(?:_spread\w*)?\b")  # and transform.SPREAD's marker
# a record a spread built where no type was expected, from sources whose fields byname can't see (transform.SPREAD)
# a spread of what's neither a record nor a mapping, with no type expected (transform's `_byname_loose`)
LOOSE_RE = re.compile(
    r'Argument of type "(.*?)" cannot be assigned to parameter "x" of type "[^"]*"\s+in function "_byname_loose"'
)
SPREAD_RE = re.compile(r"_opn_\w*?___\d+_spread((?:__\w+?)*)(?![\w])")
# a record type repeated in a union, once site numbers are gone: `(a: int) | (a: int)`
COVARIANT_RE = re.compile(r"\b_byname_T(\d+)\b")
REPEAT_RE = re.compile(r"(_(?:rec|typ|opn)_\w+(?:\[(?:[^\[\]]|\[[^\[\]]*\])*\])?)(?: \| \1(?![\w\[]))+")


def unsite(text: str) -> str:
    """Checker text with each record literal's and record type's own class (transform.site_class) back to
    its fields' class, `_typ_name___3` -> `_typ_name`, and a union of records of one shape back to one."""
    return REPEAT_RE.sub(r"\1", SITE_RE.sub(r"\1", text))


def pretty(text: str) -> str:
    """Display form of record types: _rec_name__age[str, int] -> (name: str, age: int), and of the
    TypedDict a record's `_asdict()` returns: _dct_name__age[str, int] -> {name: str, age: int}, and of
    explicit record types: _typ_... -> (name: str, age: int), _opn_... -> (..., name: str, age: int), _opn_ -> (...),
    and of a spread record whose fields aren't known: _byname_AnyRec -> (...), and of a record type's type
    parameters (transform.covariant): _byname_T0 -> T0."""
    text = unsite(text)
    out, i = [], 0
    while m := REC_RE.search(text, i):
        out.append(text[i : m.start()])
        if m.group(3):  # no fields: `(...)`, any record, or `()`, the rest of a record once every field is taken
            out.append({"opn": "(...)", "dct": "{}"}.get(m.group(3), "()"))
            i = m.end()
            continue
        fields = m.group(2).split("__")
        lp, rp = {"dct": ("{", "}"), "opn": ("(..., ", ")"), "tup": ("tuple[", "]")}.get(m.group(1), ("(", ")"))
        j = m.end()
        if j < len(text) and text[j] == "[":
            args, depth, start = [], 0, j + 1
            for k in range(j, len(text)):  # split [..] at top-level commas
                if text[k] == "[":
                    depth += 1
                elif text[k] == "]":
                    depth -= 1
                    if depth == 0:
                        args.append(text[start:k].strip())
                        j = k + 1
                        break
                elif text[k] == "," and depth == 1:
                    args.append(text[start:k].strip())
                    start = k + 1
            out.append(lp + ", ".join(f"{f}: {pretty(a)}" if f else pretty(a) for f, a in zip(fields, args)) + rp)
        else:
            out.append(lp + ", ".join(fields) + rp)
        i = j
    out.append(text[i:])
    return COVARIANT_RE.sub(r"T\1", "".join(out).replace("_byname_AnyRec", "(...)"))


def function_type(src: str, k: int, close: int, path) -> tuple[str, bool] | None:
    """The return annotation, as written, of the innermost def around `return (...)`, `yield (...)` or
    `yield from [...]` whose value is src[k:close+1], and whether that def is a generator; None in a lambda or a def
    without one. The def is found in the translation, `0` standing in for the value."""
    tr = Translation(src[:k] + "0" + src[close + 1 :], path)
    if tr.error is not None:
        return None
    hidden = tr.hidden
    try:
        tree = ast.parse(hidden)
    except SyntaxError:
        return None
    h = tr._to_hidden(k, False)
    line = hidden.count("\n", 0, h) + 1
    col = len(hidden[hidden.rfind("\n", 0, h) + 1 : h].encode())
    starts = LineIndex(hidden).starts
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

    def offset(lineno: int, col_offset: int) -> int:
        start = starts[lineno - 1]
        return start + len(hidden[start:].encode()[:col_offset].decode(errors="ignore"))

    def yields(node: ast.AST) -> bool:
        """`node`'s body yields, not counting the defs, lambdas and classes in it."""
        return any(
            isinstance(child, (ast.Yield, ast.YieldFrom)) or (not isinstance(child, scopes) and yields(child))
            for child in ast.iter_child_nodes(node)
        )

    def find(node: ast.AST, fn: ast.FunctionDef | ast.AsyncFunctionDef | None) -> tuple[str, bool] | None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node
        elif isinstance(node, ast.Lambda):
            fn = None
        value = getattr(node, "value", None) if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom)) else None
        if value is not None and (value.lineno, value.col_offset) == (line, col):
            if fn is None or fn.returns is None:
                return None
            r = fn.returns
            a = tr._from_hidden(offset(r.lineno, r.col_offset), False)
            b = tr._from_hidden(offset(r.end_lineno or r.lineno, r.end_col_offset or 0), True)
            written = None if a is None or b is None else src[a[0] : b[0]].strip()
            return (written, yields(fn)) if written else None
        for child in ast.iter_child_nodes(node):
            if (got := find(child, fn)) is not None:
                return got
        return None

    return find(tree, None)


def positional_fields(msg: str) -> list[str] | None:
    """The fields, in the order written, of a record the checker refused to read by position."""
    m = POSITIONAL_RE.search(unsite(msg))
    return (m[1] or m[2]).split("__") if m else None


def quick_fix(tr: Translation, d: dict, sure: bool = False) -> tuple[str, int, int, str] | None:
    """byname's quick fix for a diagnostic, as (title, start, end, text) in the source: for a record read by
    position (`bynameFields`), read it by name; for a field an open type doesn't declare (`bynameUnchecked`),
    read it unchecked, `p["msg"]`, which says so in the text, where pyright's own fix, an ignore comment,
    leaves `p.msg` looking checked; and the other way, for a bracket read of a declared field
    (`bynameChecked`), read it checked, `p.msg`. sure: only fixes with one right answer (fix all), so only
    reading checked: reading unchecked has declaring the field as the other answer, and reading by name pairs
    targets with fields in written order, a guess (`y, x = batch()` -> `(x=y, y=x)`) to see before taking."""
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    src, r = tr.source, d["range"]
    start = tr.src_lines.offset(r["start"]["line"], r["start"]["character"])
    end = tr.src_lines.offset(r["end"]["line"], r["end"]["character"])
    if (fields := data.get("bynameFields")) and not sure:
        fix = by_name_edit(src, start, end, fields)
        return fix and (f"Read by name: {fix[2]}", *fix)
    if checked := data.get("bynameChecked"):  # the diagnostic is on `["msg"]` (srcmap: declared_reads)
        receiver = re.search(r"\w*$", src[:start])[0]  # `p` in `p["msg"]`
        return f"Read it checked: {receiver}.{checked}", start, end, f".{checked}"
    if (unchecked := data.get("bynameUnchecked")) and not sure:
        fix = unchecked_edit(src, start, end, unchecked)
        receiver = re.search(r"\w*$", src[: fix[0]])[0] if fix else ""  # `p` in `p.msg`
        return fix and (f"Read it unchecked: {receiver}{fix[2]}", *fix)
    return None


def unchecked_edit(src: str, start: int, end: int, field: str) -> tuple[int, int, str] | None:
    """`p.msg` -> `p["msg"]`, src[start:end] being the field name read: (start, end, text) of the edit from
    the dot. None if it isn't a dot read."""
    m = re.search(r"\.\s*$", src[:start])
    if src[start:end] != field or m is None:
        return None
    return m.start(), end, f'["{field}"]'


def add_line(msg: str, text: str) -> str:
    """Put `text` on a line of its own after the message's first, keeping the CLI's rule name at the end."""
    lines = msg.split("\n")
    indent = lines[1][: len(lines[1]) - len(lines[1].lstrip())] if len(lines) > 1 else "  "
    if len(lines) > 1:
        return "\n".join([lines[0], indent + text, *lines[1:]])
    rule = RULE_RE.search(msg)
    head = msg[: rule.start()] if rule else msg
    return f"{head}\n{indent}{text}" + (f" ({rule[1]})" if rule else "")


def by_name_edit(src: str, start: int, end: int, fields: list[str]) -> tuple[int, int, str] | None:
    """Quick fix for a record read by position: `x, y = rec` (the error is on `rec`) or `for x, y in recs`
    (on `x, y`) -> the same names bound by field, `(x=, y=)`, matched in the order the fields were written,
    which is what the positional read meant. `_` drops its field. (a, b, text): replace src[a:b] with text."""
    line_start = src.rfind("\n", 0, start) + 1
    before = src[line_start:start]
    if re.search(r"\bfor\s+$", before):
        a, b = start, end
    elif m := re.fullmatch(r"\s*([^=]*?)\s*=\s*", before):
        a, b = line_start + m.start(1), line_start + m.end(1)
    else:
        return None
    t = TARGETS_RE.fullmatch(src[a:b])
    if t is None:
        return None
    names = [n.strip() for n in t[1].split(",")]
    if len(names) != len(fields):
        return None
    items = [f if n == f else f"{f}={n}" for f, n in zip(fields, names) if n != "_"]
    if not items:
        return None
    return a, b, "(" + ", ".join(f"{i}=" if "=" not in i else i for i in items) + ")"


def spread_source(msg: str) -> str | None:
    """If msg is about a record a spread built where no type was expected, from sources whose fields byname
    can't see (transform.SPREAD): where its fields went, and how to get them back."""
    if (m := SPREAD_RE.search(msg)) is None:
        return None
    names = [x for x in m[1].split("__") if x]
    if not names:
        return "it's built from a spread whose fields byname can't see. To check it, give what you spread a record type"
    return (
        f"it's built from a spread of {', '.join(names)}, whose fields byname can't see. "
        f"To check it, give {' and '.join(names)} a record type"
    )


def explain_fields(msg: str) -> str:
    """An exact record type rejected a record whose field set differs. pyright's message is a page about
    FIELDSET, a generated property; keep the first line and say which fields are extra or missing.
    Likewise a record built from spreads, checked as a dict against the type's TypedDict (`_dct_`).
    And a spread of what's neither a record nor a mapping (transform's `_byname_loose`), `None` above all,
    said as a dict display says it, not as an argument to byname's helper."""
    if m := LOOSE_RE.search(msg):
        typ = pretty(m[1])
        rule = RULE_RE.search(msg.split("\n")[-1])
        why = "a record or a mapping is spread by name"
        if "None" in [t.strip() for t in split_top(typ, "|")]:
            why = f"it may be None. Narrow it first; {why}"
        return msg[: m.start()] + f"can't spread {typ}: {why}" + (f" ({rule[1]})" if rule else "")
    spread = spread_source(msg)
    msg = unsite(msg)
    if spread and (m := UNDECLARED_RE.search(msg)):
        # not a type anyone wrote, so not one to declare the field on: the fields are the spread source's
        f, typ = m[1], pretty(m[2])
        rule = RULE_RE.search(msg.split("\n")[-1])
        return (
            msg[: m.start()]
            + f'"{f}" isn\'t on {typ}: {spread}; or read it unchecked with ["{f}"]'
            + (f" ({rule[1]})" if rule else "")
        )
    if m := UNDECLARED_RE.search(msg):
        # the gradual path for open records: `p: (...)` read with brackets, unchecked, until its fields are
        # declared; a dot read is the checked one, so say how to get there
        f, typ = m[1], pretty(m[2])
        declared = f"{typ[:-1]}, {f}: <type>)"  # `(...)` -> `(..., msg: <type>)`
        rule = RULE_RE.search(msg.split("\n")[-1])
        return (
            msg[: m.start()]
            + f'"{f}" isn\'t declared on {typ}: declare it, {declared}, or read it unchecked with ["{f}"]'
            + (f" ({rule[1]})" if rule else "")
        )
    if "_dct_" in msg and (DICT_MISSING_RE.search(msg) or DICT_EXTRA_RE.search(msg)):
        lines = msg.split("\n")
        indent = lines[1][: len(lines[1]) - len(lines[1].lstrip())] if len(lines) > 1 else "  "
        out = [lines[0]]
        if extra := DICT_EXTRA_RE.findall(msg):
            out.append(f"{indent}extra field{'s' if len(extra) > 1 else ''}: {', '.join(extra)}")
        if missing := DICT_MISSING_RE.findall(msg):
            out.append(f"{indent}missing field{'s' if len(missing) > 1 else ''}: {', '.join(missing)}")
        if rule := RULE_RE.search(lines[-1]):
            out[-1] += f" ({rule[1]})"
        return "\n".join(out)
    if m := OMIT_RE.search(msg):  # the CLI's message starts with its location
        rule = RULE_RE.search(msg.split("\n")[-1])
        return (
            msg[: m.start()]
            + f'"{m[1]}" is not a record: **rest takes the rest of a record'
            + (f" ({rule[1]})" if rule else "")
        )
    if fields := positional_fields(msg):
        msg = add_line(msg, f"records are read by name: ({', '.join(f + '=' for f in fields)})")
    msg = KEY_RE.sub('"str"', msg)  # a record's key type, aliased only to carry its fields
    if m := UNORDERED_RE.search(msg):
        lines = msg.split("\n")
        rule = RULE_RE.search(lines[-1])
        head = lines[0][: rule.start()] if rule and len(lines) == 1 else lines[0]
        field = (m[1] or m[2]).split("__")[0]
        msg = add_line(head, f"records have no order: compare or sort by a field (key=lambda r: r.{field})")
        return msg + (f" ({rule[1]})" if rule else "")
    msg = VARIANCE_RE.sub(
        lambda m: f'"{m[2].split("__")[int(m[1])]}" is an incompatible type: "{m[3]}" is not "{m[4]}"', msg
    )
    if FIELDSET not in msg:
        return msg
    # pyright cuts literals over 50 characters to `…`, so prefer the class names, which it doesn't cut
    if p := PROTOCOL_RE.search(msg):
        got, want = set(p[1].split("__")), set(p[2].split("__"))
    elif m := FIELDSET_RE.search(msg):
        got, want = set(filter(None, m[1].split(","))), set(filter(None, m[2].split(",")))
        # Which side is which: between two record types pyright also compares them the other way round, and
        # the innermost Literal line can be that reversed check. The first protocol line names the target.
        if t := TARGET_RE.search(msg):
            target = set(t[1].split("__"))
            sets = [set(filter(None, x.split(","))) for x in LITERAL_RE.findall(msg)]
            if target in sets and (others := [x for x in sets if x != target]):
                got, want = others[0], target
    else:
        return msg
    lines = msg.split("\n")
    indent = lines[1][: len(lines[1]) - len(lines[1].lstrip())] if len(lines) > 1 else "  "
    out = [lines[0]]
    if extra := sorted(got - want):
        out.append(f"{indent}extra field{'s' if len(extra) > 1 else ''}: {', '.join(extra)}")
    if missing := sorted(want - got):
        out.append(f"{indent}missing field{'s' if len(missing) > 1 else ''}: {', '.join(missing)}")
        if spread:  # they may well be there: the checker can't see them
            out.append(f"{indent}{spread}")
    if rule := RULE_RE.search(lines[-1]):  # the CLI puts the rule name at the end; keep it there
        out[-1] += f" ({rule[1]})"
    return "\n".join(out)


def is_generated_name(name) -> bool:
    return isinstance(name, str) and (
        name
        in (
            DS,
            REPR,
            FIELDSET,
            ORDER,
            "_NT",
            "_cast",
            "_Cl",
            "_Mp",
            "_ntf",
            "_t",
            "_TD",
            "_PR",
            "_L",
            "_S",
            "_Fi",
            "_ov",
            "_A",
            "_TV",
        )
        or name.startswith((*GENERATED_PREFIXES, "_byname_"))
    )


FIELDS_CLASS_RE = re.compile(r"class _(typ|opn)_(\w+?)\(")
STUB_RE = re.compile(r"    def _rec_(\w+?)\[")


def field_declarations(hidden: str, pos: dict) -> list[dict]:
    """A record field is several symbols to the checker: the attribute of its `_typ_` Protocol (reads,
    patterns, record types), the keyword of its `_rec_` stub (literals) and `_replace`'s keyword. If `pos`
    (hidden) is on one of those declarations, where all of them are. Not the `_dct_` key of `_asdict()`:
    one TypedDict serves every record of that shape in the file (transform.site_class)."""
    decls: dict[tuple[str, str], list[dict]] = {}  # (field set, field) -> its declarations
    cls = None
    for i, text in enumerate(hidden.split("\n")):
        if m := FIELDS_CLASS_RE.match(text):
            cls = m
            continue
        if not text.startswith("    "):
            cls = None
        if m := STUB_RE.match(text):
            kind, owner = "rec", m[1]
        elif cls:
            kind, owner = cls[1], cls[2]
        else:
            continue
        for d in re.finditer(r"(\w+):", text):
            if d[1] in owner.split("__") and (kind == "rec" or text.startswith((f"    {d[1]}:", "    def _replace("))):
                decls.setdefault((owner, d[1]), []).append({"line": i, "character": d.start()})
    return next(
        (
            ps
            for (_, f), ps in decls.items()
            if any(
                p["line"] == pos["line"] and p["character"] <= pos["character"] <= p["character"] + len(f) for p in ps
            )
        ),
        [],
    )


TYPE_CLASS_RE = re.compile(r"class (_(?:typ|opn)_\w+?)\(")


def type_field_at(hidden: str, pos: dict) -> tuple[str, str] | None:
    """(class, field) if `pos` (hidden) is on a field declared in a record type's Protocol."""
    lines = hidden.split("\n")
    if pos["line"] >= len(lines) or not (m := re.match(r"    (\w+): _Fi\[", lines[pos["line"]])):
        return None
    for k in range(pos["line"] - 1, -1, -1):
        if c := TYPE_CLASS_RE.match(lines[k]):
            return c[1], m[1]
        if not lines[k].startswith("    "):
            return None
    return None


def field_annotation(src: str, end: int) -> str | None:
    """The annotation written after a record type's field name ending at `end`, `list[Item]` in
    `(items: list[Item], ...)`: up to the next top-level `,` or the closing bracket."""
    j = end
    while j < len(src) and src[j] in " \t":
        j += 1
    if j >= len(src) or src[j] != ":":
        return None
    start, depth, j = j + 1, 0, j + 1
    while j < len(src):
        c = src[j]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if not depth:
                break
            depth -= 1
        elif c == "," and not depth:
            break
        j += 1
    return " ".join(src[start:j].split()) or None


def field_hover(result: dict, doc: "Doc | None", pos: dict) -> None:
    """A record literal's field is its stub's keyword to the checker, `(parameter) sku: str`: shown as a
    read of it is, `(constant) sku: str` (records' fields are Final). Only on a field name written in a
    record (Translation.fields)."""
    if doc is None:
        return
    off = doc.tr.src_lines.offset(pos["line"], pos["character"])
    if not any(a <= off <= b for a, b in doc.tr.fields):
        return
    contents = result.get("contents")
    if isinstance(contents, dict) and isinstance(contents.get("value"), str):
        contents["value"] = re.sub(r"\(parameter\) (\w+):", r"(constant) \1:", contents["value"], count=1)


def word_at(text: str, lines: LineIndex, pos: dict) -> str:
    """The identifier at `pos` (source), or ''."""
    at = lines.offset(pos["line"], pos["character"])
    a = b = at
    while a > 0 and (text[a - 1].isalnum() or text[a - 1] == "_"):
        a -= 1
    while b < len(text) and (text[b].isalnum() or text[b] == "_"):
        b += 1
    return text[a:b]


def written_field(doc: "Doc", pos: dict) -> tuple[str, str, int, int] | None:
    """If `pos` (source) is on a field name written in a record type, `label` in `(label: str)`: its
    (class, field, start, end) in the site translation (Result.type_fields). The checker has no text there
    (the type becomes `_typ_label___3[str]`), so a rename from it starts at its class's declaration."""
    tr = doc.tr if doc.tr.type_fields else Translation(doc.tr.source, doc.path, probed=doc.tr.probed, sites=True)
    off = tr.src_lines.offset(pos["line"], pos["character"])
    return next((f for f in tr.type_fields if f[2] <= off <= f[3]), None)


def type_field_declaration(hidden: str, cls: str, field: str) -> dict | None:
    """Where `field` is declared in the record type class `cls` (hidden), `    label: _Fi[...]`."""
    lines = hidden.split("\n")
    for k, text in enumerate(lines):
        if text.startswith(f"class {cls}("):
            for j in range(k + 1, len(lines)):
                if not lines[j].startswith("    "):
                    return None
                if lines[j].startswith(f"    {field}: _Fi["):
                    return {"line": j, "character": 4}
    return None


def workspace_edits(edit) -> list[tuple[str, dict]]:
    """(uri, TextEdit) for every text edit of a WorkspaceEdit, `changes` or `documentChanges`."""
    if not isinstance(edit, dict):
        return []
    out = [(u, e) for u, es in (edit.get("changes") or {}).items() for e in es]
    for dc in edit.get("documentChanges") or []:
        if "edits" in dc:
            out += [(dc["textDocument"]["uri"], e) for e in dc["edits"]]
    return out


def is_output(uri: str) -> bool:
    """An output file, `sol.pyn.py` (output.output_path): generated from its .pyn on save. The mirror leaves
    it out (tools.mirrored), but one open in the editor reaches the checker as itself."""
    return uri.startswith("file:") and uri.endswith(".pyn.py")


def drop_outputs(edit: dict) -> None:
    """A rename doesn't edit output files: the next save writes them again from the .pyn (is_output)."""
    if changes := edit.get("changes"):
        for u in [u for u in changes if is_output(u)]:
            del changes[u]
    if "documentChanges" in edit:
        edit["documentChanges"] = [
            dc for dc in edit["documentChanges"] if not is_output(dc.get("textDocument", {}).get("uri", ""))
        ]


def drop_generated(edit: dict, doc_for) -> None:
    """A rename's edits that fall on generated text only (Translation.only_generated) are removed: the
    checker renamed a field byname reads for the user, and the source has nothing there to rename."""

    def keep(uri: str, e: dict) -> bool:
        target = doc_for(uri)
        return target is None or not target.tr.only_generated(e["range"])

    for u, es in (edit.get("changes") or {}).items():
        es[:] = [e for e in es if keep(u, e)]
    for dc in edit.get("documentChanges") or []:
        if "edits" in dc:
            dc["edits"] = [e for e in dc["edits"] if keep(dc["textDocument"]["uri"], e)]


def add_workspace_edit(edit: dict, uri: str, e: dict) -> None:
    """Add a TextEdit for `uri` to a WorkspaceEdit, in the shape it already has."""
    if "documentChanges" in edit:
        dcs = edit["documentChanges"]
        if dc := next((d for d in dcs if "edits" in d and d["textDocument"]["uri"] == uri), None):
            dc["edits"].append(e)
        else:
            dcs.append({"textDocument": {"uri": uri, "version": None}, "edits": [e]})
    else:
        edit.setdefault("changes", {}).setdefault(uri, []).append(e)


def is_range(v) -> bool:
    return (
        isinstance(v, dict)
        and isinstance(v.get("start"), dict)
        and isinstance(v.get("end"), dict)
        and "line" in v["start"]
    )


def is_position(v) -> bool:
    return isinstance(v, dict) and "line" in v and "character" in v and len(v) == 2


def checker_cli(cmd: list[str]) -> str | None:
    """The checker's command-line tool beside its language server, `basedpyright` for `basedpyright-langserver`:
    what asks it about the files not open (Proxy.probe_project). None if there's none."""
    server = Path(cmd[0])
    if not server.name.endswith("-langserver"):
        return None
    cli = server.with_name(server.name.removesuffix("-langserver"))
    return str(cli) if cli.is_file() else shutil.which(cli.name)


def resolve_checker(cmd: list[str]) -> list[str]:
    """Find the checker next to this interpreter first, so a venv's basedpyright works without PATH."""
    local = Path(sys.executable).parent / cmd[0]
    if local.exists():
        return [str(local), *cmd[1:]]
    found = shutil.which(cmd[0])
    if not found:
        sys.exit(f"byname lsp: checker {cmd[0]!r} not found (install it, e.g. `uv add basedpyright`)")
    return [found, *cmd[1:]]


class Doc:
    def __init__(
        self,
        uri: str,
        text: str,
        version: int | None,
        probe: bool = False,
        probed: Probed | None = None,
        carry: Probed | None = None,
    ):
        """probe: the text goes to the checker as the document; if its spreads or rests read sources byname
        can't see, it goes as a probe translation, and its diagnostics bring the final one (Proxy.probed).
        carry: the answers the file had (its previous version's): the probe is built with them, so it's
        typed while it's in the checker (completion, hover), and asks each again, as it may be stale.
        probed: the checker's answers, for the final one."""
        self.uri = uri
        self.version = version  # the editor's version
        self.sent: int | None = None  # version we sent to the checker (we number them ourselves)
        # the file (a cell's notebook), where the record types it imports are looked up from
        self.path = uri_to_path(uri) if urlparse(uri).scheme in ("file", "vscode-notebook-cell") else None
        self.unanswered = 0  # the probe's publishes without an answer (Proxy.probed)
        self.round = 0  # probes of this text before it (Proxy.probed)
        self.probing = False
        if probe and probed is None:
            # the background run's answers for this very text (PROBED) are newer than the last version's
            got = PROBED.get(self.path) if self.path is not None else None
            start = {**(carry or {}), **(got[1] if got is not None and got[0] == text else {})}
            self.tr = Translation(text, self.path, probe=True, probed=start)
            self.probing = bool(self.tr.unseen)
            if self.probing:
                return
            probed = start
        self.tr = Translation(text, self.path, probed=probed)


class Notebook:
    def __init__(self, uri: str, version: int | None, cells: list[str]):
        self.uri = uri
        self.version = version
        self.cells = cells  # cell document uris, in order (markdown cells too)
        self.byname = False  # a cell runs `%load_ext byname`: cells are translated


def apply_changes(text: str, changes: list[dict]) -> str:
    """Apply LSP content changes, ranged (incremental) or whole-text, in order."""
    for ch in changes:
        if "range" not in ch:
            text = ch["text"]
            continue
        lines = LineIndex(text)
        s, e = (lines.offset(ch["range"][k]["line"], ch["range"][k]["character"]) for k in ("start", "end"))
        text = text[:s] + ch["text"] + text[e:]
    return text


# --- the proxy ---------------------------------------------------------------


class Proxy:
    def __init__(self, checker: list[str]):
        self.client = Writer(sys.stdout.buffer)
        self.proc = subprocess.Popen(checker, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.cli = checker_cli(checker)
        # the files not open are asked in the background (probe_project): one run at a time, and one more if
        # something changed meanwhile
        self.probe_lock = threading.Lock()
        self.probe_running = False
        self.probe_again = False
        # its mirror, lsp-probe, is synced whole on the first run (and after a bulk change), then file by file:
        # a walk of a big project takes seconds
        self.probe_full = True
        self.probe_changed: set[Path] = set()
        self.probe_pyns: set[Path] = set()  # the project's .pyn files, as of the last sync
        self.server = Writer(self.proc.stdin)
        self.root: Path | None = None
        self.mirror: Path | None = None  # what the checker runs on, see tools.mirror
        self.back: dict[str, str] = {}  # mirror uri -> the editor's uri, as the editor wrote it
        # open .pyn by path, and cells of byname notebooks by uri; recent versions, newest last
        self.docs: dict[Path | str, list[Doc]] = {}
        self.notebooks: dict[str, Notebook] = {}  # open notebooks by uri
        self.cell_text: dict[str, str] = {}  # every open notebook cell's text, by uri
        self.cell_version: dict[str, int | None] = {}  # its editor version
        self.cell_nb: dict[str, str] = {}  # its notebook's uri
        self.py_text: dict[str, str] = {}  # open .py files' text, by uri: they go to the checker as they are,
        # but we format them and run Ruff's fixes, as for .pyn
        self.pending: dict = {}  # editor request id -> (method, Doc)
        self.own_actions: dict = {}  # codeAction request id -> byname's quick fixes, added to the checker's
        self.rename_to: dict = {}  # rename request id -> its newName, for rename_fields
        # while a rename runs: each open document as the checker holds it then, with site classes (start_rename)
        self.site_docs: dict[Path | str, Doc] = {}
        self.site_opened: list[Path] = []  # closed .pyn files opened in the checker for a rename (start_rename)
        self.definition_asks: dict = {}  # definition request id -> (key, params, doc), for definition_in_sites
        self.hover_at: dict = {}  # hover request id -> its source position, for field_hover
        self.hover_markdown = False  # the editor reads hovers as markdown (initialize)
        self.server_requests: dict = {}  # checker request id -> configuration items
        self.last_completion: Doc | None = None
        self.counter = 0  # checker-side document versions
        self.temp_versions: set[int] = set()  # versions holding a completion-only temporary text
        self.own_triggers: set[str] = set()
        self.field_type: int | None = None  # semantic token type for record field names ("property")
        self.final_field: tuple[int, int] | None = None  # (readonly, static) modifier bits
        self.callable_types: set[int] = set()  # "function", "method": beat the field colour (see remap_tokens)
        self.output_on_save = False  # byname.outputOnSave: write <file>.pyn.py on every save
        self.strip_main = False  # byname.outputStripMain: drop `if __name__ == "__main__":` from it
        # byname.diagnosticsOnSave: hold diagnostics back while typing, show the saved text's ones
        self.diags_on_save = False
        # byname.pythonDiagnostics: the checker's findings on .py files and plain notebooks in the editor.
        # Off: only syntax errors and faded unused code there (Alt+L still checks them); .pyn always shows all
        self.python_diags = True
        self.saved: dict[Path, int | None] = {}  # editor version of the last saved text, per open .pyn
        self.held: dict[Path, dict] = {}  # newest diagnostics not shown yet, per open .pyn
        self.shown: dict[Path, list] = {}  # diagnostics the editor shows now, per open .pyn
        self.held_lock = threading.Lock()  # diagnostics arrive on the checker thread, saves on ours
        # a probe's answer sends the final text from the checker thread: never after a newer edit (probed)
        self.text_lock = threading.RLock()
        self.token_refresh = False  # the editor re-asks for semantic tokens when told to (refresh_tokens)
        self.own_requests = 0

    # paths --------------------------------------------------------------

    def to_mirror(self, path: Path) -> Path:
        """Where the checker sees an editor file: its copy in the mirror (a .pyn's translation), or the file
        itself if the mirror doesn't hold it (.venv, site-packages). A .pyn it doesn't hold (outside the
        project) goes under _abs."""
        from .tools import mirrored

        assert self.root is not None and self.mirror is not None
        if path.is_relative_to(self.mirror):
            return path
        if (rel := mirrored(self.root, path)) is not None:
            return self.mirror / rel
        if path.suffix == ".pyn":
            return (self.mirror / "_abs" / path.relative_to(path.anchor)).with_suffix(".py")
        return path

    def from_mirror(self, path: Path) -> Path:
        """The editor file a checker path stands for: a mirror .py is its .pyn's translation if there is one."""
        if self.mirror is None or self.root is None or not path.is_relative_to(self.mirror):
            return path
        rel = path.relative_to(self.mirror)
        real = Path("/", *rel.parts[1:]) if rel.parts and rel.parts[0] == "_abs" else self.root / rel
        if real.suffix == ".py" and (
            (pyn := real.with_suffix(".pyn")) in self.docs or pyn.is_file() or rel.parts[0] == "_abs"
        ):
            return pyn
        return real

    def uri_to_checker(self, uri):
        if self.mirror is None or not (isinstance(uri, str) and uri.startswith(URI_SCHEMES)):
            return uri
        p = urlparse(uri)
        path = Path(url2pathname(p.path))
        if (m := self.to_mirror(path)) == path:
            return uri
        out = m.as_uri() if p.scheme == "file" else uri.replace(p.path, quote(str(m)), 1)  # a cell keeps its form
        self.back[out] = uri
        return out

    def uri_to_editor(self, uri):
        if self.mirror is None or not (isinstance(uri, str) and uri.startswith(URI_SCHEMES)):
            return uri
        if uri in self.back:
            return self.back[uri]
        p = urlparse(uri)
        path = Path(url2pathname(p.path))
        if (real := self.from_mirror(path)) == path:
            return uri
        return real.as_uri() if p.scheme == "file" else uri.replace(p.path, quote(str(real)), 1)

    def is_pyn(self, uri) -> bool:
        return isinstance(uri, str) and uri.startswith("file:") and uri.endswith(".pyn")

    def key(self, uri) -> Path | str | None:
        """Where an editor document's versions are kept in self.docs: a .pyn by path, a byname
        notebook's cell by uri. None: the document goes to the checker as it is."""
        if self.is_pyn(uri):
            return uri_to_path(uri)
        if isinstance(uri, str) and uri in self.docs:
            return uri
        return None

    def editor_uri(self, key: Path | str) -> str:
        if isinstance(key, str):
            return key
        return self.docs[key][-1].uri if self.docs.get(key) else key.as_uri()

    def doc_for(self, uri: str, version=None) -> Doc | None:
        """The .pyn or byname cell a checker uri holds the translation of, at that checker version."""
        uri = self.uri_to_editor(uri)
        if isinstance(uri, str) and uri in self.docs:  # a byname notebook's cell
            return self.site_docs.get(uri) or self.pick(self.docs[uri], version)
        if not self.is_pyn(uri):
            return None
        pyn = uri_to_path(uri)
        if site := self.site_docs.get(pyn):
            return site
        if versions := self.docs.get(pyn):
            return self.pick(versions, version)
        try:
            return Doc(uri, pyn.read_text(encoding="utf-8"), None)
        except OSError:
            return None

    @staticmethod
    def pick(versions: list[Doc], version) -> Doc:
        for d in reversed(versions):
            if version is None or d.sent == version:
                return d
        return versions[-1]

    # the mirror on disk -------------------------------------------------

    def sync(self, changes: list | None = None) -> None:
        """Bring the whole mirror up to date (0.04s on a 2000-file project: walking, translating each .pyn)."""
        from .tools import mirror

        assert self.root is not None
        mirror(self.root, "lsp", default_config=False, changes=changes)

    def place(self, path: Path) -> None:
        """One file's copy in the mirror up to date (a .pyn from outside the project too)."""
        from .tools import place, write_if_changed

        assert self.root is not None and self.mirror is not None
        if place(self.root, self.mirror, path) is None and path.suffix == ".pyn" and path.is_file():
            write_if_changed(self.to_mirror(path), Translation(path.read_text(encoding="utf-8"), path).hidden)

    def send_server(self, msg: dict) -> None:
        """To the checker, every document uri in the params as the checker knows it (see to_mirror)."""
        if "params" in msg:
            msg = {**msg, "params": self.uris_to_checker(msg["params"])}
        self.server.send(msg)

    def uris_to_checker(self, obj):
        if isinstance(obj, list):
            return [self.uris_to_checker(x) for x in obj]
        if not isinstance(obj, dict):
            return obj
        return {
            k: self.uri_to_checker(v)
            if k in URI_KEYS and isinstance(v, str)
            else v
            if k == "data"
            else self.uris_to_checker(v)
            for k, v in obj.items()
        }

    # rewriting ----------------------------------------------------------

    def to_checker(self, obj, doc: Doc):
        """Request params: source positions -> hidden positions (uris: send_server)."""
        if isinstance(obj, list):
            return [self.to_checker(x, doc) for x in obj]
        if not isinstance(obj, dict):
            return obj
        if is_range(obj):
            return doc.tr.range_to_hidden(obj)
        if is_position(obj):
            return doc.tr.position_to_hidden(obj)
        return {k: v if k == "data" else self.to_checker(v, doc) for k, v in obj.items()}

    def to_editor(self, obj, doc: Doc | None, display: bool = True, key: str = ""):
        """Results: mirror uri -> editor uri, hidden ranges -> source ranges (DROP if in the prelude).
        Locations widen to the source they came from (display); TextEdits keep exact insertion points."""
        if isinstance(obj, list):
            items = [self.to_editor(x, doc, display, key) for x in obj]
            return [x for x in items if x is not DROP]
        if isinstance(obj, str):
            if key in CODE_KEYS:
                return obj
            if key == "message":
                obj = explain_fields(obj)
            return pretty(obj) if any(g in obj for g in (*GENERATED_PREFIXES, "_byname_")) else obj
        if not isinstance(obj, dict):
            return obj
        if is_range(obj):
            if doc is None:
                return obj
            r = doc.tr.range_from_hidden(obj, display=display)
            return DROP if r is None else r
        if is_position(obj):
            if doc is None:
                return obj
            p = doc.tr.position_from_hidden(obj)
            return DROP if p is None else p

        if "kind" in obj and (is_generated_name(obj.get("name")) or is_generated_name(obj.get("label"))):
            return DROP  # symbols and completions for _ds / record classes

        # a uri in this object decides which document its ranges belong to
        uri_key = next((k for k in ("uri", "targetUri") if k in obj), None)
        td = obj.get("textDocument")
        out = dict(obj)
        outer = doc
        if uri_key or (isinstance(td, dict) and "uri" in td):
            uri = obj[uri_key] if uri_key else td["uri"]
            target = self.doc_for(uri)
            editor = target.uri if target is not None else self.uri_to_editor(uri)
            if uri_key:
                out[uri_key] = editor
            else:
                out["textDocument"] = {**td, "uri": editor}
            doc = target
        for k, v in obj.items():
            if k in ("data", "textDocument") or k == uri_key:
                continue
            if k in ("oldUri", "newUri"):  # a file operation in a WorkspaceEdit
                out[k] = self.uri_to_editor(v)
                continue
            if k == "changes" and isinstance(v, dict):  # WorkspaceEdit: {uri: [TextEdit]}
                changes = {}
                for u, edits in v.items():
                    target = self.doc_for(u)
                    mapped = self.to_editor(edits, target)
                    if target is not None:
                        mapped = target.tr.split_shorthands(mapped)
                    changes[target.uri if target else self.uri_to_editor(u)] = mapped
                out[k] = changes
                continue
            nv = self.to_editor(v, outer if k == "originSelectionRange" else doc, display and "newText" not in obj, k)
            if nv is DROP:
                return DROP
            if k == "edits" and isinstance(nv, list) and doc is not None and isinstance(td, dict):
                nv = doc.tr.split_shorthands(nv)  # documentChanges: {textDocument, edits: [TextEdit]}
            out[k] = nv
        return out

    # editor -> checker --------------------------------------------------

    def on_client(self, msg: dict) -> None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}

        if method is None:  # response to a checker request
            if isinstance(mid, str) and mid.startswith(OWN_REQUEST):
                return  # to ours (refresh_tokens)
            if mid in self.server_requests and isinstance(msg.get("result"), list):
                msg = {**msg, "result": self.inject_config(self.server_requests.pop(mid), msg["result"])}
            self.server.send(msg)
            return

        if method == "initialized":  # the checker is up: ask about the files not open
            self.send_server(msg)
            self.probe_project(None)
            return

        if method == "initialize":
            root = params.get("rootUri") or next((f["uri"] for f in params.get("workspaceFolders") or []), None)
            self.root = uri_to_path(root) if root else Path.cwd()
            from .tools import mirror_dir

            self.mirror = mirror_dir(self.root, "lsp")
            self.sync()
            caps = params.get("capabilities", {})
            self.token_refresh = bool(caps.get("workspace", {}).get("semanticTokens", {}).get("refreshSupport"))
            formats = caps.get("textDocument", {}).get("hover", {}).get("contentFormat") or []
            self.hover_markdown = bool(formats) and formats[0] == "markdown"  # for the hovers we answer
            caps.get("general", {}).pop("positionEncodings", None)  # we map in UTF-16
            # one root, the mirror, given every way: with rootUri alone the checker doesn't read the project's
            # config (pyproject.toml, pyrightconfig.json; measured, 1.40.1). No folder support, so it doesn't
            # ask the editor for its folders
            params["rootUri"] = self.mirror.as_uri()
            params["rootPath"] = str(self.mirror)
            params["workspaceFolders"] = [{"uri": self.mirror.as_uri(), "name": self.root.name}]
            caps.get("workspace", {}).pop("workspaceFolders", None)
            # no pull diagnostics: VS Code would ask for them on every edit and get the checker's answer
            # straight back; pushed ones go through diagnostics(), which maps and maybe holds them
            caps.get("textDocument", {}).pop("diagnostic", None)
            caps.get("workspace", {}).pop("diagnostics", None)
            opts = params.pop("initializationOptions", None) or {}  # ours, from the editor's byname.* settings
            self.output_on_save = bool(opts.get("outputOnSave"))
            self.strip_main = bool(opts.get("outputStripMain"))
            self.diags_on_save = bool(opts.get("diagnosticsOnSave"))
            self.python_diags = opts.get("pythonDiagnostics") is not False
            self.pending[mid] = ("initialize", None)
            self.server.send(msg)  # its uris are the mirror's already
            return

        if method == "workspace/didChangeWorkspaceFolders":
            return  # we serve one root (see initialize)
        if method == "workspace/executeCommand" and params.get("command") == WRITE_OUTPUT:
            self.client.send(self.run_write_output(mid, (params.get("arguments") or [None])[0]))
            return

        if method and method.startswith("notebookDocument/"):
            self.on_notebook(msg)
            return

        td = params.get("textDocument") or {}
        uri = td.get("uri")
        key = self.key(uri)
        only = params.get("context", {}).get("only") or []
        if (
            method == "textDocument/codeAction"
            and uri in self.cell_text
            and (kind := next((k for k in (NOTEBOOK_FORMAT, NOTEBOOK_FIX_ALL, NOTEBOOK_ORGANIZE) if k in only), None))
        ):
            self.client.send(
                {"jsonrpc": "2.0", "id": mid, "result": self.notebook_action(self.notebooks[self.cell_nb[uri]], kind)}
            )
            return
        if uri in self.cell_text and method == "textDocument/codeAction" and source_kind(params):
            # a cell's own fixAll / organizeImports: nothing, the notebook-wide actions do it (a cell alone
            # can't say which imports are unused: later cells may use them)
            self.client.send({"jsonrpc": "2.0", "id": mid, "result": []})
            return
        if key is None:
            own = method == "textDocument/formatting" or (method == "textDocument/codeAction" and source_kind(params))
            # a plain notebook's cell: Ruff on it as it is
            if method == "textDocument/formatting" and uri in self.cell_text:
                self.client.send(
                    self.cell_action(msg, Doc(uri, self.cell_text[uri], None), uri_to_path(self.cell_nb[uri]))
                )
                return
            if own and uri in self.py_text:  # a .py file: Ruff on it (its translation is itself)
                doc, path = Doc(uri, self.py_text[uri], None), uri_to_path(uri)
                kind = source_kind(params)
                self.client.send(
                    self.format(mid, path, doc)
                    if method == "textDocument/formatting"
                    else self.fix(mid, path, doc, kind)
                )
                return
            if own:  # nothing of ours (an untitled buffer): the checker has neither
                self.client.send({"jsonrpc": "2.0", "id": mid, "result": []})
                return
            if isinstance(uri, str) and uri.startswith("file:") and uri.endswith(".py"):
                if method in ("textDocument/didOpen", "textDocument/didChange"):  # full sync: the whole text
                    self.py_text[uri] = (
                        td["text"] if method == "textDocument/didOpen" else params["contentChanges"][-1]["text"]
                    )
                elif method == "textDocument/didClose":
                    self.py_text.pop(uri, None)
            if method == "workspace/didChangeWatchedFiles":
                if changes := self.watched(params.get("changes", [])):
                    self.send_server({**msg, "params": {"changes": changes}})
                # a module changed outside the editor (a script, git, an agent): what spreads of it are is asked again
                if any(c.get("uri", "").endswith(".pyn") for c in params.get("changes", [])):
                    saved = self.one_saved(params.get("changes", []))
                    self.probe_project(None if saved is None else [saved])
                return
            elif mid is not None:  # e.g. workspace/symbol: results point into the mirror
                self.pending[mid] = (method, self.last_completion if method == "completionItem/resolve" else None)
            self.send_server(msg)
            return

        if isinstance(key, Path) and self.on_pyn_sync(msg, key):
            return

        versions = self.docs.get(key)  # a cell's are always there; a .pyn's while it's open
        doc = versions[-1] if versions else Doc(uri, Path(key).read_text(encoding="utf-8"), None)
        # a cell's notebook file stands in for its path (Ruff's config, error messages)
        path = key if isinstance(key, Path) else uri_to_path(self.cell_nb[key])
        if mid is not None:
            self.pending[mid] = (method, doc)
        if method == "textDocument/completion" and self.complete_slot(msg, key, doc):
            return
        if isinstance(key, str) and (
            method == "textDocument/formatting" or (method == "textDocument/codeAction" and source_kind(params))
        ):
            self.client.send(self.cell_action(msg, doc, path))
            return
        if method == "textDocument/formatting":
            self.client.send(self.format(mid, path, doc))
            return
        if method == "textDocument/codeAction" and (kind := source_kind(params)):
            self.client.send(self.fix(mid, path, doc, kind))
            return
        if method == "textDocument/codeAction" and mid is not None and (actions := self.by_name_actions(params, doc)):
            self.own_actions[mid] = actions
        if method == "textDocument/hover" and mid is not None:
            if (at := written_field(doc, params["position"])) and (typ := field_annotation(doc.tr.source, at[3])):
                # the checker has no text at a field written in a type (it's the type's class): it hovered `str`
                self.pending.pop(mid, None)
                rng = {"start": doc.tr.src_lines.position(at[2]), "end": doc.tr.src_lines.position(at[3])}
                text = f"(constant) {at[1]}: {typ}"
                value = f"```python\n{text}\n```" if self.hover_markdown else text
                kind = "markdown" if self.hover_markdown else "plaintext"
                self.client.send(
                    {"jsonrpc": "2.0", "id": mid, "result": {"contents": {"kind": kind, "value": value}, "range": rng}}
                )
                return
            self.hover_at[mid] = params["position"]
        if (
            method == "textDocument/prepareRename"
            and mid is not None
            and (at := written_field(doc, params["position"]))
        ):
            self.pending.pop(mid, None)
            rng = {"start": doc.tr.src_lines.position(at[2]), "end": doc.tr.src_lines.position(at[3])}
            self.client.send({"jsonrpc": "2.0", "id": mid, "result": {"range": rng, "placeholder": at[1]}})
            return
        if method == "textDocument/references" and mid is not None:
            self.references_in_sites(msg, key, doc)
            return
        if method == "textDocument/rename" and mid is not None:
            self.rename_to[mid] = params.get("newName")
            doc = self.start_rename(key, doc, word_at(doc.tr.source, doc.tr.src_lines, params["position"]))
            self.pending[mid] = (method, doc)
        mapped = self.to_checker(params, doc)
        if method == "textDocument/rename" and (at := written_field(doc, params["position"])):
            mapped["position"] = type_field_declaration(doc.tr.hidden, at[0], at[1]) or mapped["position"]
        # on shorthand `x=`: definition -> the local x (value half); declaration -> the parameter/field
        if method == "textDocument/definition" and (vp := doc.tr.value_position(params["position"])):
            mapped["position"] = vp
        elif method == "textDocument/declaration":
            msg = {**msg, "method": "textDocument/definition"}
        if method in ("textDocument/definition", "textDocument/declaration") and mid is not None:
            self.definition_asks[mid] = (key, params, doc)
        self.send_server({**msg, "params": mapped})

    def on_pyn_sync(self, msg: dict, path: Path) -> bool:
        """didOpen/didChange/didSave/didClose of a .pyn: True if it was one."""
        method, params = msg.get("method"), msg.get("params") or {}
        td = params.get("textDocument") or {}
        uri = td.get("uri")

        if method == "textDocument/didOpen":
            doc = Doc(uri, td["text"], td.get("version"), probe=True)
            doc.sent = self.next_version()
            self.docs[path] = [doc]
            self.saved[path] = doc.version  # opened from disk, so this text is the saved one
            self.place(path)  # a file made since the last sync: the others import it from the mirror
            sent = {"uri": uri, "languageId": "python", "version": doc.sent, "text": doc.tr.hidden}
            self.send_server({**msg, "params": {"textDocument": sent}})
            return True
        if method == "textDocument/didChange":
            text = params["contentChanges"][-1]["text"]  # we advertise full sync
            last = self.docs.get(path)
            doc = Doc(uri, text, td.get("version"), probe=True, carry=last[-1].tr.probed if last else None)
            with self.text_lock:
                self.docs.setdefault(path, []).append(doc)
                del self.docs[path][:-5]
                self.send_text(path, doc.tr.hidden, doc)
            return True
        if method == "textDocument/didSave":
            versions = self.docs.get(path)
            self.place(path)
            self.send_server({**msg, "params": {"textDocument": {"uri": uri}}})
            self.probe_project([path])  # what other files' spreads of it are may have changed: asked again
            if self.output_on_save and versions:
                self.write_output(path, versions[-1])
            if versions:
                self.release_held(path, versions[-1].version)
            return True
        if method == "textDocument/didClose":
            self.docs.pop(path, None)
            self.saved.pop(path, None)
            self.held.pop(path, None)
            self.shown.pop(path, None)
            self.send_server({**msg, "params": {"textDocument": {"uri": uri}}})
            self.client.send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": uri, "diagnostics": []},
                }
            )
            return True
        return False

    # notebooks ----------------------------------------------------------

    def is_byname(self, nb: Notebook) -> bool:
        return any(LOAD_EXT_RE.search(self.cell_text.get(u, "")) for u in nb.cells)

    def cell_for_checker(self, nb: Notebook, uri: str) -> dict:
        """A cell's text and version as the checker gets them: translated in a byname notebook (and
        kept in self.docs for mapping), as typed otherwise. Versions are ours, as for .pyn."""
        text, v = self.cell_text[uri], self.next_version()
        if not nb.byname:
            self.docs.pop(uri, None)
            return {"uri": uri, "version": v, "text": text}
        doc = Doc(uri, text, self.cell_version.get(uri), probe=True)
        doc.sent = v
        self.docs.setdefault(uri, []).append(doc)
        del self.docs[uri][:-5]
        return {"uri": uri, "version": v, "text": doc.tr.hidden}

    def open_cell(self, nb: Notebook, td: dict) -> None:
        self.cell_text[td["uri"]] = td["text"]
        self.cell_version[td["uri"]] = td.get("version")
        self.cell_nb[td["uri"]] = nb.uri

    def close_cell(self, uri: str) -> None:
        for d in (self.cell_text, self.cell_version, self.cell_nb):
            d.pop(uri, None)
        if self.docs.pop(uri, None) is not None:  # its translated diagnostics have nowhere to go
            self.client.send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": uri, "diagnostics": []},
                }
            )

    def on_notebook(self, msg: dict) -> None:
        method, params = msg["method"], msg.get("params") or {}
        nbd = params.get("notebookDocument") or {}
        if method == "notebookDocument/didOpen":
            nb = Notebook(nbd["uri"], nbd.get("version"), [c["document"] for c in nbd.get("cells", [])])
            self.notebooks[nb.uri] = nb
            for td in params.get("cellTextDocuments", []):
                self.open_cell(nb, td)
            nb.byname = self.is_byname(nb)
            cells = [{**td, **self.cell_for_checker(nb, td["uri"])} for td in params.get("cellTextDocuments", [])]
            self.send_server({**msg, "params": {**params, "cellTextDocuments": cells}})
            return
        nb = self.notebooks.get(nbd.get("uri"))
        if nb is None:
            self.send_server(msg)
            return
        if method == "notebookDocument/didClose":
            del self.notebooks[nb.uri]
            for u in nb.cells:
                self.close_cell(u)
            self.send_server(msg)
            return
        if method != "notebookDocument/didChange":
            self.send_server(msg)
            return
        nb.version = nbd.get("version", nb.version)
        change = params.get("change") or {}
        cells = dict(change.get("cells") or {})
        opened: list[dict] = []
        changed: list[str] = []
        if st := cells.get("structure"):
            arr = st["array"]
            nb.cells[arr["start"] : arr["start"] + arr["deleteCount"]] = [c["document"] for c in arr.get("cells") or []]
            for td in st.get("didClose") or []:
                self.close_cell(td["uri"])
            opened = st.get("didOpen") or []
            for td in opened:
                self.open_cell(nb, td)
        for tc in cells.get("textContent") or []:
            u = tc["document"]["uri"]
            self.cell_text[u] = apply_changes(self.cell_text.get(u, ""), tc["changes"])
            self.cell_version[u] = tc["document"].get("version")
            changed.append(u)
        was, nb.byname = nb.byname, self.is_byname(nb)
        if nb.byname != was:  # `%load_ext byname` added or removed: every cell changes meaning
            new = {td["uri"] for td in opened}
            changed = [u for u in nb.cells if u in self.cell_text and u not in new]
        if st:
            cells["structure"] = {**st, "didOpen": [{**td, **self.cell_for_checker(nb, td["uri"])} for td in opened]}
        if changed:
            content = [self.cell_for_checker(nb, u) for u in dict.fromkeys(changed)]
            cells["textContent"] = [
                {"document": {"uri": c["uri"], "version": c["version"]}, "changes": [{"text": c["text"]}]}
                for c in content
            ]
        self.send_server({**msg, "params": {**params, "change": {**change, "cells": cells}}})

    def next_version(self) -> int:
        with self.text_lock:
            self.counter += 1
            return self.counter

    def probed(self, doc: Doc, key: Path | str, diagnostics: list[dict]) -> bool:
        """The checker's diagnostics on a probe translation: its `reveal_type` answers (probed_from). The
        answers it was built with (Doc's carry), all asked again: if they hold, the probe is the file's text,
        and False says its diagnostics are to be shown (but its reveals'). Else the answers make the final
        translation, sent in its place unless a newer text went since, whose diagnostics follow: True."""
        if not diagnostics:  # the checker clears a file before it analyses it; a translation always gets some
            return True
        # a reopened file's first publish can be of the text before the probe (the mirror's). The probe's own
        # has the answer to its last line (srcmap.PROBE_MARK), which module-level code always gets; twice
        # without, the module's end is unreachable, so go on
        if not any(PROBE_MARK in d.get("message", "") for d in diagnostics) and doc.unanswered < 2:
            doc.unanswered += 1
            return True
        found = [
            (d["range"]["start"]["line"], d["range"]["start"]["character"], d.get("message", "")) for d in diagnostics
        ]
        waiting: set = set()
        got, asked = probed_from(found, doc.tr, waiting), set(doc.tr.unseen)
        if waiting and doc.round < PROBE_ROUNDS - 1:
            # an answer that's an open spread of this file's own unanswered ones (`(**first_moved())` of a
            # function returning `(**u, ...)`): built with this round's answers, the next probe answers it
            final = Doc(doc.uri, doc.tr.source, doc.version, probe=True, carry=got)
            final.round = doc.round + 1
        elif got == {k: v for k, v in doc.tr.probed.items() if k in asked}:
            doc.probing = False  # as it was built: the probe is the file's translation
            return False
        else:
            final = Doc(doc.uri, doc.tr.source, doc.version, probed=got)
        with self.text_lock:
            versions = self.docs.get(key)
            if not versions or versions[-1] is not doc:
                return True
            versions.append(final)
            del versions[:-5]
            self.send_text(key, final.tr.hidden, final)
        self.refresh_tokens()
        return True

    def probe_project(self, changed: list[Path] | None) -> None:
        """Ask the checker about the spreads of the files not open, as `byname tool` does (tools.probe), in the
        background: the editor's checker reads those from the mirror, which had no answers, so a spread of
        what they return stayed open while they were closed. It runs over a mirror of its own, `lsp-probe`, so
        the editor's checker never reads a probe; the answers (PROBED) go into the editor's mirror, the
        checker is told which copies changed, and the open files are asked again. One run at a time.
        changed: the files changed since the last run, None if not known (the mirror is synced whole)."""
        if self.cli is None or self.root is None:
            return
        with self.probe_lock:
            if changed is None:
                self.probe_full = True
            else:
                self.probe_changed.update(changed)
            if self.probe_running:
                self.probe_again = True
                return
            self.probe_running = True
        threading.Thread(target=self.run_probe, daemon=True).start()

    def run_probe(self) -> None:
        from .tools import mirror, mirror_dir, mirrored, place, probe

        assert self.root is not None and self.cli is not None
        while True:
            with self.probe_lock:
                full, changed = self.probe_full, self.probe_changed
                self.probe_full, self.probe_changed = False, set()
            try:
                before = dict(PROBED)
                out = mirror_dir(self.root, "lsp-probe")
                if full:
                    found: set[Path] = set()
                    mirror(self.root, "lsp-probe", sources=found)
                    self.probe_pyns = {p for p in found if p.suffix == ".pyn"}
                for path in changed if not full else ():
                    place(self.root, out, path)
                    if path.suffix == ".pyn" and path.is_file() and mirrored(self.root, path) is not None:
                        self.probe_pyns.add(path)
                    else:
                        self.probe_pyns.discard(path)
                probe(self.root, out, self.cli, self.probe_pyns)
                self.answers_in([p for p, got in PROBED.items() if before.get(p) != got])
            except Exception:  # a broken run leaves the files as they were: open spreads; the next syncs whole
                with self.probe_lock:
                    self.probe_full = True
            with self.probe_lock:
                if not self.probe_again:
                    self.probe_running = False
                    return
                self.probe_again = False

    def answers_in(self, files: list[Path]) -> None:
        """New answers for these files (probe_project): their copies in the editor's mirror again, the checker
        told; and every open file that asks the checker anything asked again: what it spreads may be of a
        file just saved, or of these (with the answers it had, unchanged ones cost one analysis)."""
        from .tools import write_if_changed

        changed = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            existed = (dst := self.to_mirror(path)).exists()
            if write_if_changed(dst, Translation(text, path).hidden):  # a copy it hadn't: created, not changed
                changed.append({"uri": dst.as_uri(), "type": 2 if existed else 1})
        if changed:
            self.send_server(
                {"jsonrpc": "2.0", "method": "workspace/didChangeWatchedFiles", "params": {"changes": changed}}
            )
        with self.text_lock:
            for key, versions in self.docs.items():
                doc = versions[-1]
                if (doc.tr.unseen or doc.tr.probed) and not doc.probing:
                    again = Doc(doc.uri, doc.tr.source, doc.version, probe=True, carry=doc.tr.probed)
                    versions.append(again)
                    del versions[:-5]
                    self.send_text(key, again.tr.hidden, again)

    def refresh_tokens(self) -> None:
        """Tell the editor to ask for semantic tokens again. It asked right after the edit, so it got the
        probe translation's: there a probed spread's fields weren't known, and `moved.name` had no colour."""
        if self.token_refresh:
            self.own_requests += 1
            self.client.send(
                {
                    "jsonrpc": "2.0",
                    "id": f"{OWN_REQUEST}{self.own_requests}",
                    "method": "workspace/semanticTokens/refresh",
                }
            )

    def send_text(self, key: Path | str, hidden: str, doc: Doc | None) -> None:
        """didChange to the checker; doc=None marks a temporary completion-only text. A cell changes
        through its notebook."""
        v = self.next_version()
        if doc is None:
            self.temp_versions.add(v)
        else:
            doc.sent = v
        td = {"uri": self.editor_uri(key), "version": v}
        changes = [{"text": hidden}]
        if isinstance(key, Path):
            self.send_server(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didChange",
                    "params": {"textDocument": td, "contentChanges": changes},
                }
            )
            return
        nb = self.notebooks[self.cell_nb[key]]
        cells = {"textContent": [{"document": td, "changes": changes}]}
        params = {"notebookDocument": {"uri": nb.uri, "version": nb.version}, "change": {"cells": cells}}
        self.send_server({"jsonrpc": "2.0", "method": "notebookDocument/didChange", "params": params})

    def complete_slot(self, msg: dict, key: Path | str, doc: Doc) -> bool:
        """Completion at a field position of `(...) = expr`. The checker is shown a temporary text
        where the slot reads `_ds.<typed>` and asked there; the real text is restored on response."""
        params, mid = msg["params"], msg["id"]
        tr = doc.tr
        cursor = tr.src_lines.offset(params["position"]["line"], params["position"]["character"])
        slot = pattern_slot(tr.source, cursor)
        ctx = params.get("context") or {}
        if slot is None:
            if (open_at := call_after_comma(tr.source, cursor)) is not None:
                # basedpyright offers nothing at an empty argument right after a comma, even when asked
                # (`f(a=1, |)`, `rec._replace(x=1, |)`), but right after the call's `(` it offers the
                # same names and the keyword parameters not yet used: ask there, answer at the cursor
                at_paren = {**params, "position": tr.src_lines.position(open_at + 1)}
                self.pending[mid] = ("after_comma", (doc, params["position"]))
                self.send_server({**msg, "params": self.to_checker(at_paren, doc)})
                return True
            lit = record_literal_slot(tr.source, cursor)
            if lit is not None and self.complete_record_literal(msg, key, doc, lit):
                return True
            if ctx.get("triggerKind") == 2 and ctx.get("triggerCharacter") in self.own_triggers:
                self.client.send({"jsonrpc": "2.0", "id": mid, "result": {"isIncomplete": False, "items": []}})
                return True  # our extra trigger outside a pattern, a call and a record literal: nothing to offer
            return False
        ws, we, _, listed = slot
        src = tr.source
        word = src[ws:cursor] or PLACEHOLDER
        # an item before the word makes `(x) = y` a pattern too; not after it, where a `**rest` must stay last
        kw = "__byname_kw=, "
        ttr = Translation(src[:ws] + kw + word + src[we:], doc.path)
        if ttr.error is not None:
            return False
        h = ttr._to_hidden(ws + len(kw) + len(word), False, touch=True)
        hidden = ttr.hidden
        if word == PLACEHOLDER:  # leave `_ds.` with nothing after it: complete every field
            hidden, h = hidden[: h - len(word)] + hidden[h:], h - len(word)
        self.send_text(key, hidden, None)
        hpos = LineIndex(hidden).position(h)
        rng = {"start": tr.src_lines.position(ws), "end": tr.src_lines.position(we)}
        self.pending[mid] = ("slot", (key, doc, rng, set(listed)))
        sent = {"textDocument": {"uri": self.editor_uri(key)}, "position": hpos}
        self.send_server({**msg, "params": sent})
        return True

    def complete_record_type(
        self, msg: dict, key: Path | str, doc: Doc, rec: tuple[int, int, str, int, int, set[str]]
    ) -> bool:
        """Field names in `user: User = (name=, |)`. The checker is shown `user: User = _t.cast(User, None).|`
        in place of the literal and asked for User's members: a record's fields, closed or open, are its
        read-only members, offered as constants (an open record type has no `_replace` to ask instead; `user.`
        won't do: `user` is unbound in its own assignment). `_t` is the prelude's `import typing as _t`, in
        every file with a record type. `value` is that cast, or an expression on it for a literal nested in another
        (expected_value)."""
        open_at, close, value, ws, we, given = rec
        tr = doc.tr
        src = tr.source
        prefix = f"{value}."
        word = src[ws:we] or PLACEHOLDER
        ttr = Translation(src[:open_at] + prefix + word + src[close + 1 :], doc.path)
        if ttr.error is not None:
            return False
        h = ttr._to_hidden(open_at + len(prefix) + len(word), False, touch=True)
        hidden = ttr.hidden
        if word == PLACEHOLDER:  # leave `_t.cast(User, None).` with nothing after it: every member
            hidden, h = hidden[: h - len(word)] + hidden[h:], h - len(word)
        self.send_text(key, hidden, None)
        rng = {"start": tr.src_lines.position(ws), "end": tr.src_lines.position(we)}
        self.pending[msg["id"]] = ("record_type", (msg, key, doc, rng, given))
        sent = {"textDocument": {"uri": self.editor_uri(key)}, "position": LineIndex(hidden).position(h)}
        self.send_server({**msg, "params": sent})
        return True

    def complete_record_literal(
        self, msg: dict, key: Path | str, doc: Doc, lit: tuple[int, int, int, int, set[str]]
    ) -> bool:
        """Field names in a record literal whose type is known from where it stands: the value of an annotated
        assignment (`user: User = (|)`), an argument (`f(u=(|))`), a returned or yielded value (`return (|)`,
        `yield (|)`, `yield from [(|)]`), or an item of a list, set, dict or tuple, or a comprehension's element,
        standing there (`users: list[User] = [(|)]`), whose element type is read off the annotation as written
        (element_type), so an alias of `list[User]` gets plain completion."""
        k, close, ws, we, given = lit
        src = doc.tr.source
        o, c, steps = k, close, []
        while (step := container_step(src, o, c)) is not None:
            o, c, kind = step
            if kind is not None:
                steps.insert(0, kind)
        before = src[:o]
        if m := RECORD_TARGET_RE.match(before[before.rfind("\n") + 1 :]):
            written = m["annotation"].strip()
        elif flow := FLOW_RE.search(before):
            if (got := function_type(src, o, c, doc.path)) is None:
                return False
            written, generator = got
            if flow["kw"] == "return":
                steps = [("result", 0)] * generator + steps
            elif flow["frm"]:  # `yield from` takes an iterable of what the generator yields: its items are that
                if not steps or steps[0][0] not in ("item", "tuple", "key"):
                    return False
                steps = [("yield", 0)] + steps[1:]
            else:
                steps = [("yield", 0)] + steps
        elif ARGUMENT_RE.search(before):
            return self.ask_record_argument(msg, key, doc, lit, o, c, steps)
        else:
            return False
        value = written and expected_value(written, steps)
        return bool(value) and self.complete_record_type(msg, key, doc, (k, close, value, ws, we, given))

    def ask_record_argument(
        self, msg: dict, key: Path | str, doc: Doc, lit: tuple, o: int, c: int, steps: list
    ) -> bool:
        """Field names in `f(s, schedule=(|))` or `f(users=[(|)])`. With no annotation to read, the checker is
        first asked for the signature there, `0` standing in for the argument src[o:c+1]; the parameter's type
        then serves as the annotation (answer_record_argument)."""
        src = doc.tr.source
        ttr = Translation(src[:o] + "0" + src[c + 1 :], doc.path)
        if ttr.error is not None:
            return False
        h = ttr._to_hidden(o + 1, False, touch=True)
        self.send_text(key, ttr.hidden, None)
        sid = f"byname-signature-{msg['id']}"
        self.pending[sid] = ("record_argument", (msg, key, doc, lit, steps))
        position = LineIndex(ttr.hidden).position(h)
        params = {"textDocument": {"uri": self.editor_uri(key)}, "position": position}
        self.send_server({"jsonrpc": "2.0", "id": sid, "method": "textDocument/signatureHelp", "params": params})
        return True

    def answer_record_argument(self, help, msg: dict, key: Path | str, doc: Doc, lit: tuple, steps: list) -> None:
        """Completes the fields of the parameter's type; not in a call or no parameter there, plain completion."""
        k, close, ws, we, given = lit
        value = (written := parameter_type(help)) and expected_value(written, steps)
        if value and self.complete_record_type(msg, key, doc, (k, close, value, ws, we, given)):
            return
        latest = self.docs.get(key, [doc])[-1]
        self.send_text(key, latest.tr.hidden, latest)  # restore the real text
        self.complete_plainly(msg, doc)

    def start_rename(self, key: Path | str, doc: Doc, word: str) -> Doc:
        """The checker gets every open document translated with site classes (transform's `sites`) until the
        rename is answered (end_rename), so renaming `r.name` reaches only the literal or type `r` came from.
        The real translation shares one class per shape: with a class per site, a list of literals wouldn't
        fit `list[Item]` (list is invariant). The project's other .pyn files that hold `word` (the name
        renamed) are opened in it too: the mirror's copy on disk is the real translation, where a literal
        written against another file's type isn't tied to it. Returns the document the request is mapped
        through."""
        with self.text_lock:
            for k, versions in self.docs.items():
                d = versions[-1]
                site = copy.copy(d)
                site.tr = Translation(d.tr.source, d.path, probed=d.tr.probed, sites=True)
                self.site_docs[k] = site
                self.send_text(k, site.tr.hidden, None)
            for pyn in self.closed_pyns(word):
                try:
                    text = pyn.read_text(encoding="utf-8")
                except OSError:
                    continue
                site = Doc(pyn.as_uri(), text, None)
                site.tr = Translation(text, pyn, sites=True)
                self.site_docs[pyn] = site
                self.site_opened.append(pyn)
                v = self.next_version()
                self.temp_versions.add(v)
                td = {"uri": pyn.as_uri(), "languageId": "python", "version": v, "text": site.tr.hidden}
                self.send_server({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": td}})
        return self.site_docs.get(key, doc)

    def closed_pyns(self, word: str) -> list[Path]:
        """The project's .pyn files not open in the editor whose text holds `word` (start_rename)."""
        if self.root is None or self.mirror is None or not word:
            return []
        from .tools import walk

        # walked each time (0.08s on dream-robot's 63k files, excludes skipped): probe_pyns lags a file made since
        pyns = {d / f for d, fs in walk(self.root, self.mirror) for f in fs if f.endswith(".pyn")}
        out = []
        for pyn in sorted(pyns):
            if pyn in self.docs:
                continue
            try:
                if word in pyn.read_text(encoding="utf-8"):
                    out.append(pyn)
            except OSError:
                pass
        return out

    def references_in_sites(self, msg: dict, key: Path | str, doc: Doc) -> None:
        """Find All References in the rename translation (start_rename, closed files opened too), as a rename
        finds its edits: a literal written against a type reads that type's fields, and a written type has its
        own class, so a field's references are its type's, its reads and the literals written against it, as in
        TypeScript. On a field written in a type it starts at that class's field (written_field), as the
        checker has no text there. A field's other declarations are asked too (more_references)."""
        params = msg["params"]
        site = self.start_rename(key, doc, word_at(doc.tr.source, doc.tr.src_lines, params["position"]))
        at = written_field(site, params["position"])
        hidden = (at and type_field_declaration(site.tr.hidden, at[0], at[1])) or site.tr.position_to_hidden(
            params["position"]
        )
        state = {"msg": msg, "site": site, "found": [], "asked": set(), "left": 0}
        self.ask_references(state, self.editor_uri(key), hidden)

    def ask_references(self, state: dict, uri: str, position: dict) -> None:
        state["asked"].add((uri, position["line"], position["character"]))
        state["left"] += 1
        sid = f"byname-references-{state['msg']['id']}-{len(state['asked'])}"
        self.pending[sid] = ("references_site", state)
        context = state["msg"]["params"].get("context", {"includeDeclaration": True})
        params = {"textDocument": {"uri": uri}, "position": position, "context": context}
        self.send_server({"jsonrpc": "2.0", "id": sid, "method": "textDocument/references", "params": params})

    def more_references(self, answer: dict, state: dict) -> None:
        """One answer of references_in_sites: a location on one of a field's declarations (field_declarations)
        asks the others too, as `r.name` and `(name=)` are two symbols to the checker; when every answer is
        in, they're mapped (finish_references)."""
        state["left"] -= 1
        for loc in answer.get("result") or []:
            state["found"].append(loc)
            if (target := self.doc_for(loc["uri"])) is None:
                continue
            for p in field_declarations(target.tr.hidden, loc["range"]["start"]):
                if (self.editor_uri_of(loc["uri"]), p["line"], p["character"]) not in state["asked"]:
                    self.ask_references(state, self.editor_uri_of(loc["uri"]), p)
        if not state["left"]:
            self.finish_references(state)

    def editor_uri_of(self, uri: str) -> str:
        target = self.doc_for(uri)
        return target.uri if target is not None else uri

    def finish_references(self, state: dict) -> None:
        """Each location once, in the source: a written type's field is the name written (Result.type_fields);
        one on generated text that isn't a copy of the user's (Translation.only_generated) is dropped."""
        out, seen = [], set()
        for loc in state["found"]:
            if is_output(loc["uri"]):
                continue
            target = self.doc_for(loc["uri"])
            if target is not None and (at := type_field_at(target.tr.hidden, loc["range"]["start"])):
                lines = target.tr.src_lines
                got = [
                    {"uri": target.uri, "range": {"start": lines.position(a), "end": lines.position(b)}}
                    for name, f, a, b in target.tr.type_fields
                    if (name, f) == at
                ]
            elif target is not None and target.tr.only_generated(loc["range"]):
                got = []
            else:
                mapped = self.to_editor(loc, state["site"])
                got = [] if mapped is DROP else [mapped]
            for g in got:
                if (k := json.dumps(g, sort_keys=True)) not in seen:
                    seen.add(k)
                    out.append(g)
        self.end_rename()
        self.client.send({"jsonrpc": "2.0", "id": state["msg"]["id"], "result": out})

    def definition_in_sites(self, msg: dict, ask: tuple) -> bool:
        """Go to definition on a record field the checker answers with generated code only (a literal's field,
        its stub's keyword; a read, the class of its shape): asked again in the rename translation
        (start_rename), where a literal written against a type reads that type's fields and a written type
        has its own class, so it lands on the field written in the type, as in TypeScript (finish_definition).
        False: the answer goes as it is."""
        result = msg.get("result")
        locations = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
        if not locations or self.to_editor(locations, ask[2]):
            return False
        key, params, doc = ask
        site = self.start_rename(key, doc, word_at(doc.tr.source, doc.tr.src_lines, params["position"]))
        sid = f"byname-definition-{msg['id']}"
        self.pending[sid] = ("definition_site", (msg, site))
        mapped = {**params, "position": site.tr.position_to_hidden(params["position"])}
        self.send_server({"jsonrpc": "2.0", "id": sid, "method": "textDocument/definition", "params": mapped})
        return True

    def finish_definition(self, answer: dict, asked: dict, site: Doc) -> None:
        """The editor's definition request answered from the rename translation (definition_in_sites): a
        written type's field is the name written (Result.type_fields); the rest maps as usual."""
        result = answer.get("result")
        out = []
        for loc in result if isinstance(result, list) else [result] if isinstance(result, dict) else []:
            uri, rng = loc.get("uri") or loc.get("targetUri"), loc.get("range") or loc.get("targetSelectionRange")
            target = self.doc_for(uri) if uri else None
            if target is not None and rng and (at := type_field_at(target.tr.hidden, rng["start"])):
                lines = target.tr.src_lines
                out += [
                    {"uri": target.uri, "range": {"start": lines.position(a), "end": lines.position(b)}}
                    for name, f, a, b in target.tr.type_fields
                    if (name, f) == at
                ]
            elif (mapped := self.to_editor(loc, site)) is not DROP:
                out.append(mapped)
        self.end_rename()
        self.client.send({"jsonrpc": "2.0", "id": asked["id"], "result": out})

    def end_rename(self) -> None:
        """The real translations back to the checker (start_rename): open documents get theirs again, and
        the files opened for the rename are closed, so the checker reads the mirror's copy again."""
        with self.text_lock:
            for k in self.site_docs:
                if versions := self.docs.get(k):
                    self.send_text(k, versions[-1].tr.hidden, versions[-1])
            for pyn in self.site_opened:
                td = {"uri": pyn.as_uri()}
                self.send_server({"jsonrpc": "2.0", "method": "textDocument/didClose", "params": {"textDocument": td}})
            self.site_opened.clear()
            self.site_docs.clear()

    def rename_fields(self, msg: dict, doc: Doc | None) -> bool:
        """A rename that reaches a record field's declaration (field_declarations) renames its other
        declarations too, so a read and the literal it reads go together: `r.name` and `(name=)` are two
        symbols to the checker. Each other declaration is a rename request of its own, answered in
        finish_rename. False: not a WorkspaceEdit, the answer goes as it is."""
        if not isinstance(msg["result"], dict):
            return False
        new = self.rename_to.pop(msg["id"], None)
        have = {
            (u, e["range"]["start"]["line"], e["range"]["start"]["character"])
            for u, e in workspace_edits(msg["result"])
        }
        asks = []
        for u, e in workspace_edits(msg["result"]):
            if new is None or (target := self.doc_for(u)) is None:
                continue
            for p in field_declarations(target.tr.hidden, e["range"]["start"]):
                if (u, p["line"], p["character"]) not in have:
                    have.add((u, p["line"], p["character"]))
                    asks.append({"textDocument": {"uri": u}, "position": p, "newName": new})
        if not asks:
            self.finish_rename(msg, doc, [])
            return True
        state = {"msg": msg, "doc": doc, "more": [], "left": len(asks)}
        for k, params in enumerate(asks):
            sid = f"byname-rename-{msg['id']}-{k}"
            self.pending[sid] = ("rename_field", state)
            self.server.send({"jsonrpc": "2.0", "id": sid, "method": "textDocument/rename", "params": params})
        return True

    def finish_rename(self, msg: dict, doc: Doc | None, more: list) -> None:
        """The first rename's edits and the other declarations' ones, each edit once, mapped to the source."""
        edit = msg["result"]
        seen = {(u, json.dumps(e["range"], sort_keys=True)) for u, e in workspace_edits(edit)}
        for other in more:
            for u, e in workspace_edits(other):
                if (u, json.dumps(e["range"], sort_keys=True)) not in seen:
                    seen.add((u, json.dumps(e["range"], sort_keys=True)))
                    add_workspace_edit(edit, u, e)
        # a written record type's field is only its class's to the checker: the name written is edited here
        written = []
        for u, e in workspace_edits(edit):
            if (target := self.doc_for(u)) is not None and (at := type_field_at(target.tr.hidden, e["range"]["start"])):
                lines = target.tr.src_lines
                written += [
                    (
                        target.uri,
                        {"range": {"start": lines.position(a), "end": lines.position(b)}, "newText": e["newText"]},
                    )
                    for name, f, a, b in target.tr.type_fields
                    if (name, f) == at
                ]
        drop_generated(edit, self.doc_for)
        drop_outputs(edit)
        result = self.to_editor(edit, doc)
        if result is not DROP:
            for u, e in written:
                add_workspace_edit(result, u, e)
        self.end_rename()
        self.client.send({**msg, "result": None if result is DROP else result})

    def complete_plainly(self, msg: dict, doc: Doc) -> None:
        """The checker's own completion at the editor's position, on the real text."""
        self.pending[msg["id"]] = (msg["method"], doc)
        self.send_server({**msg, "params": self.to_checker(msg["params"], doc)})

    def finish_record_type(self, result, key: Path | str, doc: Doc, rng: dict, given: set[str]) -> dict | None:
        """The fields not yet given, as `name=`; None if the type had none (not a record type)."""
        out = self.finish_slot(result, key, doc, rng, set())
        fields = [it for it in out["items"] if it.get("kind") == CONSTANT]
        if not fields:
            return None
        items = [
            {
                **it,
                "label": f"{it['label']}=",
                "kind": VARIABLE,
                "textEdit": {**it["textEdit"], "newText": f"{it['label']}="},
            }
            for it in fields
            if it["label"] not in given
        ]
        return {"isIncomplete": False, "items": items}

    def finish_after_comma(self, result, doc: Doc, cursor: dict) -> dict:
        """Items asked for right after the call's `(`, inserted at the cursor instead."""
        mapped = self.to_editor(result, doc) if result else None
        if isinstance(mapped, dict):
            items = mapped.get("items", [])
        elif isinstance(mapped, list):
            items = mapped
        else:  # no result, or DROP
            items = []
        at = {"start": cursor, "end": cursor}
        out = []
        for item in items:
            if not isinstance(item, dict):  # a CompletionItem is always an object
                continue
            edit = item.get("textEdit") or {}
            it = {k: v for k, v in item.items() if k not in ("textEdit", "insertText")}
            it["textEdit"] = {"range": at, "newText": edit.get("newText") or it.get("label", "")}
            out.append(it)
        return {"isIncomplete": False, "items": out}

    def finish_slot(self, result, key: Path | str, doc: Doc, rng: dict, listed: set[str]) -> dict:
        latest = self.docs.get(key, [doc])[-1]
        self.send_text(key, latest.tr.hidden, latest)  # restore the real text
        items = result.get("items", []) if isinstance(result, dict) else (result or [])
        out = []
        for it in items:
            label = it.get("label", "")
            if label.startswith("_") or label in listed or (label in MACHINERY and it.get("kind") == METHOD):
                continue
            it = {k: v for k, v in it.items() if k not in ("textEdit", "additionalTextEdits", "data")}
            it["textEdit"] = {"range": rng, "newText": it.get("insertText") or label}
            it.pop("insertText", None)
            out.append(it)
        return {"isIncomplete": False, "items": out}

    def ruff_at(self, path: Path) -> tuple[str, Path | None]:
        """(filename, cwd) for Ruff on an editor file: its copy in the mirror, as `byname tool ruff` sees it.
        Ruff tells first-party imports by the files it finds, and a .pyn is a module only in the mirror."""
        if self.mirror is None:
            return str(path), self.root
        return str(self.to_mirror(path)), self.mirror

    def format(self, mid, path: Path, doc: Doc) -> dict:
        """Whole-document ruff format via stand-ins (see fmt.py); answered here, not by the checker."""
        from .fmt import FormatError, UnparsableError, format_pyn

        src = doc.tr.source
        try:
            out = format_pyn(src, *self.ruff_at(path))
        except (SyntaxError, UnparsableError):  # code that doesn't translate or parse: its diagnostic says why
            return {"jsonrpc": "2.0", "id": mid, "result": []}
        except FormatError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"byname format: {e}"}}
        if out == src:
            return {"jsonrpc": "2.0", "id": mid, "result": []}
        whole = {"start": {"line": 0, "character": 0}, "end": doc.tr.src_lines.position(len(src))}
        return {"jsonrpc": "2.0", "id": mid, "result": [{"range": whole, "newText": out}]}

    def fix(self, mid, path: Path, doc: Doc, kind: str) -> dict:
        """source.fixAll / source.organizeImports (e.g. codeActionsOnSave): Ruff's safe fixes, see fix.py.
        Fix all also applies byname's quick fixes that have one right answer (quick_fix, sure), first. Those
        come from byname's own hints on this very text (Translation.hints), so a save right after an edit,
        before the checker has answered, gets them too."""
        from .fix import ORGANIZE, FixError, fix_pyn

        src = ours = doc.tr.source
        if kind != ORGANIZE_KIND:
            done = len(src)
            fixes = [f[1:] for d in doc.tr.hints if (f := quick_fix(doc.tr, d, sure=True))]
            for a, b, text in sorted(fixes, reverse=True):
                if b <= done:  # back to front, skipping any that overlaps one applied
                    ours, done = ours[:a] + text + ours[b:], a
        try:
            out = fix_pyn(ours, *self.ruff_at(path), select=ORGANIZE if kind == ORGANIZE_KIND else None)
        except (FixError, ValueError):
            out = ours  # mid-edit code: no Ruff fixes rather than fail the save
        if out == src:
            return {"jsonrpc": "2.0", "id": mid, "result": []}
        whole = {"start": {"line": 0, "character": 0}, "end": doc.tr.src_lines.position(len(src))}
        title = "byname: organize imports (ruff)" if kind == ORGANIZE_KIND else "byname: fix all (byname, ruff)"
        edit = {"changes": {doc.uri: [{"range": whole, "newText": out}]}}
        return {"jsonrpc": "2.0", "id": mid, "result": [{"title": title, "kind": kind + ".byname", "edit": edit}]}

    def cell_action(self, msg: dict, doc: Doc, path: Path) -> dict:
        """Formatting for one notebook cell, byname's or plain (a plain cell's translation is the cell
        itself). A cell Ruff can't parse alone (a magic: `%time x = 1`) is left as it is rather than
        failing the save."""
        mid = msg["id"]
        reply = (
            self.format(mid, path, doc)
            if msg["method"] == "textDocument/formatting"
            else {"jsonrpc": "2.0", "id": mid, "result": []}
        )
        if "error" in reply:
            return {"jsonrpc": "2.0", "id": mid, "result": []}
        if not doc.tr.source.endswith("\n"):  # a cell's last line has no newline, as Ruff writes notebooks
            for item in reply["result"]:
                for edit in [item] if "newText" in item else [e for es in item["edit"]["changes"].values() for e in es]:
                    edit["newText"] = edit["newText"].removesuffix("\n")
            if any(item.get("newText") == doc.tr.source for item in reply["result"]):  # only that newline changed
                return {"jsonrpc": "2.0", "id": mid, "result": []}
        return reply

    def notebook_action(self, nb: Notebook, kind: str) -> list[dict]:
        """A whole-notebook code action (NOTEBOOK_*), as one edit over its code cells."""
        from .fix import ORGANIZE, FixError, fix_notebook

        path = uri_to_path(nb.uri)
        code = [u for u in nb.cells if u in self.cell_text]  # not markdown cells
        changes = {}
        if kind == NOTEBOOK_FORMAT:
            for u in code:
                doc = self.docs[u][-1] if u in self.docs else Doc(u, self.cell_text[u], None)
                if (reply := self.cell_action({"id": None, "method": "textDocument/formatting"}, doc, path))["result"]:
                    changes[u] = reply["result"]
        else:
            texts = [self.cell_text[u] for u in code]
            try:
                fixed = fix_notebook(
                    texts, nb.byname, *self.ruff_at(path), select=ORGANIZE if kind == NOTEBOOK_ORGANIZE else None
                )
            except (FixError, ValueError):
                fixed = texts  # mid-edit code: offer nothing rather than fail the save
            for u, old, new in zip(code, texts, fixed):
                if new != old:
                    whole = {"start": {"line": 0, "character": 0}, "end": LineIndex(old).position(len(old))}
                    changes[u] = [{"range": whole, "newText": new}]
        if not changes:
            return []
        title = {
            NOTEBOOK_FORMAT: "format notebook",
            NOTEBOOK_FIX_ALL: "fix all",
            NOTEBOOK_ORGANIZE: "organize imports",
        }[kind]
        return [{"title": f"byname: {title} (ruff)", "kind": kind + ".byname", "edit": {"changes": changes}}]

    def by_name_actions(self, params: dict, doc: Doc) -> list[dict]:
        """Quick fixes for byname's diagnostics (quick_fix), offered with the checker's."""
        out = []
        lines = doc.tr.src_lines
        for d in params.get("context", {}).get("diagnostics", []):
            if (fix := quick_fix(doc.tr, d)) is None:
                continue
            title, a, b, text = fix
            edit = {"range": {"start": lines.position(a), "end": lines.position(b)}, "newText": text}
            out.append(
                {
                    "title": title,
                    "kind": "quickfix",
                    "diagnostics": [d],
                    "isPreferred": True,
                    "edit": {"changes": {doc.uri: [edit]}},
                }
            )
        return out

    def write_output(self, path: Path, doc: Doc) -> str | None:
        """Write <file>.pyn.py next to the .pyn. Returns an error message, or None on success.
        A .pyn that doesn't translate leaves the previous output alone."""
        if doc.tr.error is not None:
            return f"not written: {path.name} has an error ({doc.tr.error})"
        try:
            text = render(doc.tr.source, path, strip_main=self.strip_main)
            output_path(path).write_text(text, encoding="utf-8")
        except (OSError, SyntaxError) as e:
            return f"not written: {e}"
        return None

    def run_write_output(self, mid, uri) -> dict:
        """Alt+C: write the output now, whatever outputOnSave says."""
        if not self.is_pyn(uri):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "byname: not a .pyn file"}}
        path = uri_to_path(uri)
        versions = self.docs.get(path)
        doc = versions[-1] if versions else Doc(uri, path.read_text(encoding="utf-8"), None)
        if err := self.write_output(path, doc):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"byname: {err}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": str(output_path(path))}

    def watched(self, changes: list[dict]) -> list[dict]:
        """The editor's file events as the checker's: the mirror is synced, and each copy that sync changed is
        an event, as is a change to a file the mirror links to. A folder made or removed shows as the files in
        it. Events outside the project (site-packages) pass through. One file changed (a save) is placed alone:
        a sync walks the project, seconds in a big one, on the thread that answers the editor."""
        from .tools import mirrored, place

        assert self.root is not None and self.mirror is not None
        made: list = []
        if (saved := self.one_saved(changes)) is None:
            self.sync(made)
        elif saved.is_relative_to(self.root) and (placed := place(self.root, self.mirror, saved)) and placed[1]:
            made.append(placed)
        out = {p.as_uri(): t for p, t in made}
        passed = []
        for c in changes:
            path = uri_to_path(c.get("uri", ""))
            if not path.is_relative_to(self.root):
                passed.append(c)
            elif c.get("type") != 3 and (rel := mirrored(self.root, path)) is not None and (self.mirror / rel).exists():
                out.setdefault((self.mirror / rel).as_uri(), c.get("type", 2))
        return [{"uri": u, "type": t} for u, t in out.items()] + passed

    @staticmethod
    def one_saved(changes: list[dict]) -> Path | None:
        """The file, when a file event is one file changed (a save, here or elsewhere); else None: something
        made, removed or many at once (git, a script) may be anywhere, so the mirror is synced whole."""
        if len(changes) == 1 and changes[0].get("type") == 2 and changes[0].get("uri", "").startswith("file:"):
            return uri_to_path(changes[0]["uri"])
        return None

    @staticmethod
    def inject_config(items: list[dict], result: list) -> list:
        """byname's defaults in the checker's settings, whichever shape it asks in: pyright asks for
        `python.analysis`, basedpyright for `python` / `basedpyright` with a nested `analysis`. A project config
        decides over all of them (the checker ignores the editor's analysis settings then)."""

        def analysis(cfg: dict, based: bool) -> dict:
            cfg = dict(cfg or {})
            if based:
                cfg.setdefault("typeCheckingMode", "standard")  # pyright's default; "recommended" is noisy
            return cfg

        out = list(result)
        for i, item in enumerate(items[: len(out)]):
            section = item.get("section")
            if section in ("python.analysis", "basedpyright.analysis"):
                out[i] = analysis(out[i], section.startswith("based"))
            elif section in ("python", "basedpyright"):
                cfg = dict(out[i] or {})
                cfg["analysis"] = analysis(cfg.get("analysis"), section == "basedpyright")
                if section == "python":
                    cfg.setdefault("pythonPath", sys.executable)
                out[i] = cfg
        return out

    # checker -> editor --------------------------------------------------

    def on_server(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")

        if method is None:  # response to an editor request
            method_doc = self.pending.pop(mid, None)
            ask = self.definition_asks.pop(mid, None)
            if (at := self.hover_at.pop(mid, None)) is not None and method_doc and isinstance(msg.get("result"), dict):
                field_hover(msg["result"], method_doc[1], at)
            if ask is not None and self.definition_in_sites(msg, ask):
                return
            if method_doc and method_doc[0] == "references_site":
                self.more_references(msg, method_doc[1])
                return
            if method_doc and method_doc[0] == "definition_site":
                self.finish_definition(msg, *method_doc[1])
                return
            if method_doc and method_doc[0] == "slot":
                key, doc, rng, listed = method_doc[1]
                self.client.send({**msg, "result": self.finish_slot(msg.get("result"), key, doc, rng, listed)})
                return
            if method_doc and method_doc[0] == "record_type":
                asked, key, doc, rng, given = method_doc[1]
                if (out := self.finish_record_type(msg.get("result"), key, doc, rng, given)) is None:
                    self.complete_plainly(asked, doc)
                else:
                    self.client.send({**msg, "result": out})
                return
            if method_doc and method_doc[0] == "record_argument":
                self.answer_record_argument(msg.get("result"), *method_doc[1])
                return
            if method_doc and method_doc[0] == "rename_field":
                state = method_doc[1]
                state["more"].append(msg.get("result"))
                state["left"] -= 1
                if not state["left"]:
                    self.finish_rename(state["msg"], state["doc"], state["more"])
                return
            if method_doc and method_doc[0] == "after_comma":
                doc, cursor = method_doc[1]
                self.client.send({**msg, "result": self.finish_after_comma(msg.get("result"), doc, cursor)})
                return
            if method_doc and method_doc[0] == "textDocument/rename" and not isinstance(msg.get("result"), dict):
                self.end_rename()  # no edits (or an error): nothing to map through the site translation
            if (own := self.own_actions.pop(mid, None)) and "result" in msg:
                result = self.to_editor(msg["result"], method_doc[1]) if msg["result"] else []
                # ours first: they keep the code readable, where the checker offers an ignore comment
                self.client.send({**msg, "result": [*own, *(result if isinstance(result, list) else [])]})
                return
            if method_doc and "result" in msg and msg["result"] is not None:
                req, doc = method_doc
                if req == "initialize":
                    caps = msg["result"].setdefault("capabilities", {})
                    caps["textDocumentSync"] = {"openClose": True, "change": 1, "save": {"includeText": False}}
                    caps["declarationProvider"] = True  # served as definition on the keyword half of `x=`
                    caps["documentFormattingProvider"] = True  # ruff via stand-ins, served by us
                    cap = caps.get("codeActionProvider")  # add ruff's source actions, served by us
                    kinds = cap.get("codeActionKinds", []) if isinstance(cap, dict) else (["quickfix"] if cap else [])
                    caps["codeActionProvider"] = {
                        **(cap if isinstance(cap, dict) else {}),
                        # not the NOTEBOOK_* kinds: the extension offers those itself, on every cell (see notebookAction)
                        "codeActionKinds": [*kinds, *(k for k in (FIX_ALL_KIND, ORGANIZE_KIND) if k not in kinds)],
                    }
                    ecp = caps.setdefault("executeCommandProvider", {"commands": []})
                    ecp["commands"] = [*ecp.get("commands", []), WRITE_OUTPUT]
                    if stp := caps.get("semanticTokensProvider"):
                        stp["full"] = True  # we remap whole token lists; no delta support
                        types = stp.get("legend", {}).get("tokenTypes", [])
                        self.field_type = types.index("property") if "property" in types else None
                        self.callable_types = {types.index(t) for t in ("function", "method") if t in types}
                        mods = stp.get("legend", {}).get("tokenModifiers", [])
                        if "readonly" in mods and "static" in mods:
                            self.final_field = (1 << mods.index("readonly"), 1 << mods.index("static"))
                    # notebooks: their cells are synced whole through notebookDocument/*, see on_notebook
                    caps["notebookDocumentSync"] = {
                        "notebookSelector": [{"notebook": "jupyter-notebook", "cells": [{"language": "python"}]}]
                    }
                    cp = caps.setdefault("completionProvider", {})
                    have = cp.setdefault("triggerCharacters", [])
                    self.own_triggers = {c for c in EXTRA_TRIGGERS if c not in have}
                    have.extend(sorted(self.own_triggers))
                elif req.startswith("textDocument/semanticTokens"):
                    msg = {
                        **msg,
                        "result": {
                            "data": remap_tokens(
                                msg["result"].get("data", []),
                                doc,
                                self.field_type,
                                self.final_field,
                                self.callable_types,
                            )
                        },
                    }
                elif req == "textDocument/rename" and self.rename_fields(msg, doc):
                    return
                else:
                    result = self.to_editor(msg["result"], doc)
                    msg = {**msg, "result": None if result is DROP else result}
                    if req == "textDocument/completion":
                        self.last_completion = doc
            self.client.send(msg)
            return

        if method == "workspace/configuration" and mid is not None:
            self.server_requests[mid] = msg.get("params", {}).get("items", [])
        elif mid is not None and method in ("workspace/applyEdit", "window/showDocument"):
            # mirror uris -> the editor's
            msg = {**msg, "params": self.to_editor(msg.get("params"), None)}
        elif method == "textDocument/publishDiagnostics":
            msg = self.diagnostics(msg)
            if msg is None:
                return
        self.client.send(msg)

    def diagnostics(self, msg: dict) -> dict | None:
        params = msg["params"]
        if params.get("version") in self.temp_versions:
            return None  # computed on a completion-only temporary text
        doc = self.doc_for(params["uri"], params.get("version"))
        if doc is None:  # a .py file or a plain notebook's cell, as the checker saw it
            kept = params.get("diagnostics", [])
            if not self.python_diags:  # keep syntax errors (no rule) and hints (faded unused code)
                kept = [d for d in kept if not d.get("code") or d.get("severity", 1) == 4]
            kept = [
                {**d, "relatedInformation": self.to_editor(d["relatedInformation"], None)}
                if "relatedInformation" in d
                else d
                for d in kept
            ]
            params = {**params, "uri": self.uri_to_editor(params["uri"]), "diagnostics": kept}
            msg = {**msg, "params": params}
            if params["uri"] in self.cell_text:  # a plain notebook's cell: the version is ours, not the editor's
                return {**msg, "params": {k: v for k, v in params.items() if k != "version"}}
            return msg
        key = self.key(doc.uri)
        if key not in self.docs:
            return None  # closed .pyn: its translation's diagnostics have nowhere to go
        if doc.probing and (key is None or self.probed(doc, key, params.get("diagnostics", []))):
            return None
        if doc.tr.reveals:  # the probe's own reveal_type notes, and what an ignore comment it blanked hid
            kept = [
                d
                for d in params.get("diagnostics", [])
                if (d["range"]["start"]["line"], d["range"]["start"]["character"]) not in doc.tr.reveals
                and d["range"]["start"]["line"] not in doc.tr.unignored
            ]
            params = {**params, "diagnostics": kept}
        out = [*doc.tr.problems, *doc.tr.hints]
        flagged = {(p["range"]["start"]["line"], p["range"]["start"]["character"]) for p in out}
        # a broken byname rule: the checker got the raw .pyn, so all it says is noise (`"(" was not closed`)
        for d in [] if isinstance(doc.tr.error, BynameError) else params.get("diagnostics", []):
            r = doc.tr.range_from_hidden(d["range"], display=True)
            if r is None:
                continue
            if (r["start"]["line"], r["start"]["character"]) in flagged:
                continue  # half-typed pattern item: our message says it; drop the checker's echo
            if doc.tr.quiet(doc.tr.src_lines.offset(r["start"]["line"], r["start"]["character"]), d.get("message", "")):
                continue
            fields = positional_fields(d.get("message", ""))
            undeclared = UNDECLARED_RE.search(unsite(d.get("message", "")))
            d = {**d, "range": r, "message": pretty(explain_fields(d.get("message", "")))}
            if fields and "data" not in d:
                d["data"] = {"bynameFields": fields}  # for the quick fix (by_name_actions)
            elif undeclared and "data" not in d:
                d["data"] = {"bynameUnchecked": undeclared[1]}  # likewise
            if "relatedInformation" in d:
                d["relatedInformation"] = self.to_editor(d["relatedInformation"], None)
            out.append(d)
        if doc.tr.error is not None:
            out.append(error_diagnostic(doc.tr.error))
        params = {**params, "uri": doc.uri, "diagnostics": out}
        if doc.version is not None:
            params["version"] = doc.version
        else:
            params.pop("version", None)
        msg = {**msg, "params": params}
        if self.diags_on_save and isinstance(key, Path):  # a notebook saves as a whole: cells show them at once
            path = key
            with self.held_lock:
                if doc.version != self.saved.get(path):
                    # unsaved text: new errors wait for the save, but fixed ones go away now
                    self.held[path] = msg
                    out = still_shown(self.shown.get(path, []), out)
                    msg = {**msg, "params": {**params, "diagnostics": out}}
                else:
                    self.held.pop(path, None)
                self.shown[path] = out
        return msg

    def release_held(self, path: Path, version: int | None) -> None:
        """On save: show the held diagnostics if they're for the saved text. If the checker hasn't
        caught up yet, its diagnostics for this version go straight through when they arrive."""
        with self.held_lock:
            self.saved[path] = version
            held = self.held.get(path)
            if held is None or held["params"].get("version") != version:
                return
            del self.held[path]
            self.shown[path] = held["params"]["diagnostics"]
        self.client.send(held)

    # run ----------------------------------------------------------------

    def pump_server(self) -> None:
        while (msg := read_message(self.proc.stdout)) is not None:
            self.on_server(msg)
        os._exit(0)

    def run(self) -> None:
        threading.Thread(target=self.pump_server, daemon=True).start()
        stdin = sys.stdin.buffer
        while (msg := read_message(stdin)) is not None:
            self.on_client(msg)
        self.proc.terminate()


def still_shown(shown: list, new: list) -> list:
    """The diagnostics in `new` that the editor already shows: same message, code and severity
    (positions move as you type, so they don't count). Each shown one matches at most once."""

    def key(d: dict) -> tuple:
        return d.get("message"), json.dumps(d.get("code")), d.get("severity")

    left = Counter(key(d) for d in shown)
    out = []
    for d in new:
        if left[key(d)] > 0:
            left[key(d)] -= 1
            out.append(d)
    return out


def remap_tokens(
    data: list[int],
    doc: Doc | None,
    field_type: int | None = None,
    final_field: tuple[int, int] | None = None,
    callable_types: frozenset[int] | set[int] = frozenset(),
) -> list[int]:
    """Semantic tokens come as 5-int groups, positions relative to the previous token.
    Decode, map each token to the source, drop those on generated text, re-encode.
    field_type: the legend index to give record field names (the checker gives them none).
    final_field: (readonly, static) modifier bits. A field read through a record type (`p.age` where
    `p: (age: int, ...)`) is a Final Protocol attribute, `readonly static`; a record's own field is just
    `static`. Dropping `readonly` from that pair colours both alike.
    callable_types: a field name the checker colours as a function or method keeps that colour. A pattern
    label reads an attribute (`(decode=d) = tok` is `d = tok.decode`), so it looks like `tok.decode`.
    A bare `x=` (call, record, pattern, loop) is also the local `x`, and Ctrl+click goes there, so it takes
    the checker's colour for that local: `step=` passing a Final `step` is readonly like every other `step`,
    `(age=) = r` declares `age`. Only a renamed item's label stays a field or parameter."""
    if doc is None:
        return data
    tokens, line, col = [], 0, 0
    local_colours: dict[tuple[int, int, int], tuple[int, int]] = {}  # shorthand `x` -> its local's (type, mods)
    for i in range(0, len(data) - 4, 5):
        dl, dc, length, typ, mods = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        r = doc.tr.range_from_hidden_exact(
            {"start": {"line": line, "character": col}, "end": {"line": line, "character": col + length}}
        )
        if r is None:  # generated text; the implicit local of a shorthand gives the `x` written its colour
            rng = {"start": {"line": line, "character": col}, "end": {"line": line, "character": col + length}}
            if (sh := doc.tr.shorthand_from_hidden(rng)) is not None and sh["start"]["line"] == sh["end"]["line"]:
                s, e = sh["start"], sh["end"]
                local_colours[(s["line"], s["character"], e["character"] - s["character"])] = (typ, mods)
            continue
        if r["start"]["line"] != r["end"]["line"] or r["end"]["character"] <= r["start"]["character"]:
            continue
        s, e = r["start"], r["end"]
        if final_field and typ == field_type and mods & (both := final_field[0] | final_field[1]) == both:
            mods &= ~final_field[0]
        tokens.append((s["line"], s["character"], e["character"] - s["character"], typ, mods))
    if field_type is not None:
        spans = set()
        for fs, fe in doc.tr.fields:
            p = doc.tr.src_lines.position(fs)
            spans.add((p["line"], p["character"], fe - fs))
        for fs, fe in doc.tr.shorthands:  # coloured as their locals, below; a loop's is real text already
            p = doc.tr.src_lines.position(fs)
            spans.discard((p["line"], p["character"], fe - fs))
        callables = {t[:3] for t in tokens if t[3] in callable_types and t[:3] in spans}
        tokens = [t for t in tokens if t[:3] not in callables or t[3] in callable_types]
        tokens += [(*sp, field_type, 0) for sp in spans - callables]
    tokens = [t for t in tokens if t[:3] not in local_colours]
    tokens += [(*sp, *colour) for sp, colour in local_colours.items()]
    out, pl, pc, last_end = [], 0, 0, (-1, -1)
    for ln, c, length, typ, mods in sorted(tokens):
        if (ln, c) < last_end:
            continue  # overlaps the previous token
        out += [ln - pl, c - pc if ln == pl else c, length, typ, mods]
        pl, pc, last_end = ln, c, (ln, c + length)
    return out


def error_diagnostic(err: Exception) -> dict:
    line, col = 0, 0
    if isinstance(err, SyntaxError) and err.lineno:
        line, col = err.lineno - 1, max((err.offset or 1) - 1, 0)
    elif len(err.args) > 1 and isinstance(err.args[1], tuple):  # tokenize.TokenError
        line, col = err.args[1][0] - 1, err.args[1][1]
    msg = err.msg if isinstance(err, SyntaxError) else str(err.args[0] if err.args else err)
    pos = {"line": max(line, 0), "character": col}
    return {"range": {"start": pos, "end": pos}, "severity": 1, "source": "byname", "message": msg}


def main(argv: list[str]) -> None:
    checker = argv[argv.index("--") + 1 :] if "--" in argv else DEFAULT_CHECKER
    Proxy(resolve_checker(checker)).run()
