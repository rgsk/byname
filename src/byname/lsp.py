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

import json
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
from .srcmap import LineIndex, Translation
from .transform import DS, FIELDSET, ORDER, REPR, pattern_slot

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
NOTEBOOK_FORMAT, NOTEBOOK_FIX_ALL, NOTEBOOK_ORGANIZE = "notebook.format", "notebook.source.fixAll", "notebook.source.organizeImports"
METHOD = 2  # CompletionItemKind
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


REC_RE = re.compile(r"_(rec|dct|typ|opn|tup)_(\w+)|_(opn)_()")
GENERATED_PREFIXES = ("_rec_", "_dct_", "_typ_", "_opn_", "_tup_")
# an exact record type's field-set mismatch, as pyright reports it: "...Literal['a,b,c']" ... "...Literal['a,b']"
FIELDSET_RE = re.compile(r"""Literal\['([\w,]*)'\]" is not assignable to type "(?:\(\) -> )?Literal\['([\w,]*)'\]""")
TARGET_RE = re.compile(r'is incompatible with protocol "_typ_(\w+?)\[')
# both full field sets from the first protocol line; the Literal lines can be cut short ('...vocab_si…')
PROTOCOL_RE = re.compile(r'"_(?:rec|typ|opn)_(\w+?)\[[^"]*" is incompatible with protocol "_typ_(\w+?)\[')
LITERAL_RE = re.compile(r"Literal\['([\w,]*)'\]")
DICT_MISSING_RE = re.compile(r'"(\w+)" is required in "_dct_')
DICT_EXTRA_RE = re.compile(r'"(\w+)" is an undefined item in type "_dct_')
PARAM_RE = re.compile(r"\b_byname_p\d+\b")
# a record's field type mismatch: records are their exact type in the checker (see record_def), so two of the
# same field set are compared by type argument, and pyright names the type parameter rather than the field
# a record read by position, which records don't allow (see record_def in transform.py)
# (`rec[0]`: `__getitem__` takes a `_key_<fields>`, see key_alias in transform.py)
POSITIONAL_RE = re.compile(r'"_(?:typ|opn)_(\w+?)\[[^"]*" is not iterable|of type "_key_(\w+)" in function "__getitem__"')
# a record ordered (`sorted(recs)`, `max(recs)`, `a < b`), which records don't allow: pyright's message is a page
# about SupportsRichComparison
UNORDERED_RE = re.compile(
    r'"_(?:typ|opn)_(\w+?)\[[^"]*" is incompatible with protocol "SupportsDunder[LG]T'
    r'|Operator "[<>]=?" not supported for types "_(?:typ|opn)_(\w+?)\['
)
# a target list read by position: `x, y` / `(x, _)` / `x,`
TARGETS_RE = re.compile(r"\(?\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*,?\s*\)?")
VARIANCE_RE = re.compile(r'Type parameter "T(\d+)@_(?:typ|opn)_(\w+?)" is \w+, but "(.*)" is not (?:the same as|a subtype of|assignable to) "(.*)"')
KEY_RE = re.compile(r'"_key_\w*"')
RULE_RE = re.compile(r"\s*\((report\w+)\)\s*$")
CODE_KEYS = {"newText", "insertText", "filterText", "sortText", "uri", "targetUri", "data"}
# keys whose string values are document URIs (a notebook cell's `document` is one, in a structure change)
URI_KEYS = {"uri", "targetUri", "document", "oldUri", "newUri"}
URI_SCHEMES = ("file:", "vscode-notebook-cell:")


def pretty(text: str) -> str:
    """Display form of record types: _rec_name__age[str, int] -> (name: str, age: int), and of the
    TypedDict a record's `_asdict()` returns: _dct_name__age[str, int] -> {name: str, age: int}, and of
    explicit record types: _typ_... -> (name: str, age: int), _opn_... -> (..., name: str, age: int), _opn_ -> (...),
    and of a spread record whose fields aren't known: _byname_AnyRec -> (...), and of a parameter pattern's
    parameter: _byname_p0 -> (...)."""
    out, i = [], 0
    while m := REC_RE.search(text, i):
        out.append(text[i : m.start()])
        if m.group(3):  # `(...)`: an open type with no fields
            out.append("(...)")
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
    out_s = "".join(out).replace("_byname_AnyRec", "(...)")  # a spread record whose fields aren't known
    return PARAM_RE.sub("(...)", out_s)  # a parameter pattern's parameter: `f((...): User)`


def positional_fields(msg: str) -> list[str] | None:
    """The fields, in the order written, of a record the checker refused to read by position."""
    m = POSITIONAL_RE.search(msg)
    return (m[1] or m[2]).split("__") if m else None


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


def explain_fields(msg: str) -> str:
    """An exact record type rejected a record whose field set differs. pyright's message is a page about
    FIELDSET, a generated property; keep the first line and say which fields are extra or missing.
    Likewise a record built from spreads, checked as a dict against the type's TypedDict (`_dct_`)."""
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
    msg = VARIANCE_RE.sub(lambda m: f'"{m[2].split("__")[int(m[1])]}" is an incompatible type: "{m[3]}" is not "{m[4]}"', msg)
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
    if rule := RULE_RE.search(lines[-1]):  # the CLI puts the rule name at the end; keep it there
        out[-1] += f" ({rule[1]})"
    return "\n".join(out)


def is_generated_name(name) -> bool:
    return isinstance(name, str) and (
        name in (DS, REPR, FIELDSET, ORDER, "_NT", "_cast", "_Cl", "_Mp", "_ntf", "_t", "_TD", "_PR", "_L", "_S", "_Fi", "_ov", "_A", "_TV") or name.startswith((*GENERATED_PREFIXES, "_byname_"))
    )


def is_range(v) -> bool:
    return isinstance(v, dict) and isinstance(v.get("start"), dict) and isinstance(v.get("end"), dict) and "line" in v["start"]


def is_position(v) -> bool:
    return isinstance(v, dict) and "line" in v and "character" in v and len(v) == 2


def has_pyright_config(root: Path) -> bool:
    """The project configures the checker itself: pyrightconfig.json, or [tool.basedpyright] / [tool.pyright]."""
    if (root / "pyrightconfig.json").exists():
        return True
    pp = root / "pyproject.toml"
    return pp.exists() and re.search(r"^\[tool\.(based)?pyright", pp.read_text(), re.MULTILINE) is not None


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
    def __init__(self, uri: str, text: str, version: int | None):
        self.uri = uri
        self.version = version  # the editor's version
        self.sent: int | None = None  # version we sent to the checker (we number them ourselves)
        self.tr = Translation(text)


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
        if real.suffix == ".py" and ((pyn := real.with_suffix(".pyn")) in self.docs or pyn.is_file() or rel.parts[0] == "_abs"):
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
            return self.pick(self.docs[uri], version)
        if not self.is_pyn(uri):
            return None
        pyn = uri_to_path(uri)
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
            write_if_changed(self.to_mirror(path), Translation(path.read_text(encoding="utf-8")).hidden)

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
        return {k: self.uri_to_checker(v) if k in URI_KEYS and isinstance(v, str) else v if k == "data" else self.uris_to_checker(v) for k, v in obj.items()}

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
                    changes[target.uri if target else self.uri_to_editor(u)] = self.to_editor(edits, target)
                out[k] = changes
                continue
            nv = self.to_editor(v, outer if k == "originSelectionRange" else doc, display and "newText" not in obj, k)
            if nv is DROP:
                return DROP
            out[k] = nv
        return out

    # editor -> checker --------------------------------------------------

    def on_client(self, msg: dict) -> None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}

        if method is None:  # response to a checker request
            if mid in self.server_requests and isinstance(msg.get("result"), list):
                msg = {**msg, "result": self.inject_config(self.server_requests.pop(mid), msg["result"])}
            self.server.send(msg)
            return

        if method == "initialize":
            root = params.get("rootUri") or next((f["uri"] for f in params.get("workspaceFolders") or []), None)
            self.root = uri_to_path(root) if root else Path.cwd()
            from .tools import mirror_dir

            self.mirror = mirror_dir(self.root, "lsp")
            self.sync()
            caps = params.get("capabilities", {})
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
        if method == "textDocument/codeAction" and uri in self.cell_text and (kind := next((k for k in (NOTEBOOK_FORMAT, NOTEBOOK_FIX_ALL, NOTEBOOK_ORGANIZE) if k in only), None)):
            self.client.send({"jsonrpc": "2.0", "id": mid, "result": self.notebook_action(self.notebooks[self.cell_nb[uri]], kind)})
            return
        if uri in self.cell_text and method == "textDocument/codeAction" and source_kind(params):
            # a cell's own fixAll / organizeImports: nothing, the notebook-wide actions do it (a cell alone
            # can't say which imports are unused: later cells may use them)
            self.client.send({"jsonrpc": "2.0", "id": mid, "result": []})
            return
        if key is None:
            own = method == "textDocument/formatting" or (method == "textDocument/codeAction" and source_kind(params))
            if method == "textDocument/formatting" and uri in self.cell_text:  # a plain notebook's cell: Ruff on it as it is
                self.client.send(self.cell_action(msg, Doc(uri, self.cell_text[uri], None), uri_to_path(self.cell_nb[uri])))
                return
            if own and uri in self.py_text:  # a .py file: Ruff on it (its translation is itself)
                doc, path = Doc(uri, self.py_text[uri], None), uri_to_path(uri)
                kind = source_kind(params)
                self.client.send(self.format(mid, path, doc) if method == "textDocument/formatting" else self.fix(mid, path, doc, kind))
                return
            if own:  # nothing of ours (an untitled buffer): the checker has neither
                self.client.send({"jsonrpc": "2.0", "id": mid, "result": []})
                return
            if isinstance(uri, str) and uri.startswith("file:") and uri.endswith(".py"):
                if method in ("textDocument/didOpen", "textDocument/didChange"):  # full sync: the whole text
                    self.py_text[uri] = td["text"] if method == "textDocument/didOpen" else params["contentChanges"][-1]["text"]
                elif method == "textDocument/didClose":
                    self.py_text.pop(uri, None)
            if method == "workspace/didChangeWatchedFiles":
                if changes := self.watched(params.get("changes", [])):
                    self.send_server({**msg, "params": {"changes": changes}})
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
        if isinstance(key, str) and (method == "textDocument/formatting" or (method == "textDocument/codeAction" and source_kind(params))):
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
        mapped = self.to_checker(params, doc)
        # on shorthand `x=`: definition -> the local x (value half); declaration -> the parameter/field
        if method == "textDocument/definition" and (vp := doc.tr.value_position(params["position"])):
            mapped["position"] = vp
        elif method == "textDocument/declaration":
            msg = {**msg, "method": "textDocument/definition"}
        self.send_server({**msg, "params": mapped})

    def on_pyn_sync(self, msg: dict, path: Path) -> bool:
        """didOpen/didChange/didSave/didClose of a .pyn: True if it was one."""
        method, params = msg.get("method"), msg.get("params") or {}
        td = params.get("textDocument") or {}
        uri = td.get("uri")

        if method == "textDocument/didOpen":
            doc = Doc(uri, td["text"], td.get("version"))
            doc.sent = self.next_version()
            self.docs[path] = [doc]
            self.saved[path] = doc.version  # opened from disk, so this text is the saved one
            self.place(path)  # a file made since the last sync: the others import it from the mirror
            sent = {"uri": uri, "languageId": "python", "version": doc.sent, "text": doc.tr.hidden}
            self.send_server({**msg, "params": {"textDocument": sent}})
            return True
        if method == "textDocument/didChange":
            text = params["contentChanges"][-1]["text"]  # we advertise full sync
            doc = Doc(uri, text, td.get("version"))
            self.docs.setdefault(path, []).append(doc)
            del self.docs[path][:-5]
            self.send_text(path, doc.tr.hidden, doc)
            return True
        if method == "textDocument/didSave":
            versions = self.docs.get(path)
            self.place(path)
            self.send_server({**msg, "params": {"textDocument": {"uri": uri}}})
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
            self.client.send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}})
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
        doc = Doc(uri, text, self.cell_version.get(uri))
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
            self.client.send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}})

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
            cells["textContent"] = [{"document": {"uri": c["uri"], "version": c["version"]}, "changes": [{"text": c["text"]}]} for c in content]
        self.send_server({**msg, "params": {**params, "change": {**change, "cells": cells}}})

    def next_version(self) -> int:
        self.counter += 1
        return self.counter

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
            self.send_server({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {"textDocument": td, "contentChanges": changes}})
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
            if ctx.get("triggerKind") == 2 and ctx.get("triggerCharacter") in self.own_triggers:
                self.client.send({"jsonrpc": "2.0", "id": mid, "result": {"isIncomplete": False, "items": []}})
                return True  # our extra trigger outside a pattern: nothing to offer
            return False
        ws, we, close, listed = slot
        src = tr.source
        word = src[ws:cursor] or PLACEHOLDER
        temp = src[:ws] + word + src[we:]
        close += len(word) - (we - ws)
        temp = temp[:close] + ", __byname_kw=" + temp[close:]  # makes `(x) = y` a pattern too
        ttr = Translation(temp)
        if ttr.error is not None:
            return False
        h = ttr._to_hidden(ws + len(word), False, touch=True)
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
        from .fmt import FormatError, format_pyn

        src = doc.tr.source
        try:
            out = format_pyn(src, *self.ruff_at(path))
        except (FormatError, SyntaxError) as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"byname format: {e}"}}
        if out == src:
            return {"jsonrpc": "2.0", "id": mid, "result": []}
        whole = {"start": {"line": 0, "character": 0}, "end": doc.tr.src_lines.position(len(src))}
        return {"jsonrpc": "2.0", "id": mid, "result": [{"range": whole, "newText": out}]}

    def fix(self, mid, path: Path, doc: Doc, kind: str) -> dict:
        """source.fixAll / source.organizeImports (e.g. codeActionsOnSave): Ruff's safe fixes, see fix.py."""
        from .fix import ORGANIZE, FixError, fix_pyn

        src = doc.tr.source
        try:
            out = fix_pyn(src, *self.ruff_at(path), select=ORGANIZE if kind == ORGANIZE_KIND else None)
        except (FixError, ValueError):
            out = src  # mid-edit code: offer nothing rather than fail the save
        if out == src:
            return {"jsonrpc": "2.0", "id": mid, "result": []}
        whole = {"start": {"line": 0, "character": 0}, "end": doc.tr.src_lines.position(len(src))}
        title = "byname: organize imports (ruff)" if kind == ORGANIZE_KIND else "byname: fix all (ruff)"
        edit = {"changes": {doc.uri: [{"range": whole, "newText": out}]}}
        return {"jsonrpc": "2.0", "id": mid, "result": [{"title": title, "kind": kind + ".byname", "edit": edit}]}

    def cell_action(self, msg: dict, doc: Doc, path: Path) -> dict:
        """Formatting for one notebook cell, byname's or plain (a plain cell's translation is the cell
        itself). A cell Ruff can't parse alone (a magic: `%time x = 1`) is left as it is rather than
        failing the save."""
        mid = msg["id"]
        reply = self.format(mid, path, doc) if msg["method"] == "textDocument/formatting" else {"jsonrpc": "2.0", "id": mid, "result": []}
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
                fixed = fix_notebook(texts, nb.byname, *self.ruff_at(path), select=ORGANIZE if kind == NOTEBOOK_ORGANIZE else None)
            except (FixError, ValueError):
                fixed = texts  # mid-edit code: offer nothing rather than fail the save
            for u, old, new in zip(code, texts, fixed):
                if new != old:
                    whole = {"start": {"line": 0, "character": 0}, "end": LineIndex(old).position(len(old))}
                    changes[u] = [{"range": whole, "newText": new}]
        if not changes:
            return []
        title = {NOTEBOOK_FORMAT: "format notebook", NOTEBOOK_FIX_ALL: "fix all", NOTEBOOK_ORGANIZE: "organize imports"}[kind]
        return [{"title": f"byname: {title} (ruff)", "kind": kind + ".byname", "edit": {"changes": changes}}]

    def by_name_actions(self, params: dict, doc: Doc) -> list[dict]:
        """Quick fixes for records read by position (diagnostics carrying `bynameFields`): read them by name."""
        out = []
        lines = doc.tr.src_lines
        for d in params.get("context", {}).get("diagnostics", []):
            fields = (d.get("data") or {}).get("bynameFields") if isinstance(d.get("data"), dict) else None
            if not fields:
                continue
            r = d["range"]
            start = lines.offset(r["start"]["line"], r["start"]["character"])
            end = lines.offset(r["end"]["line"], r["end"]["character"])
            if (fix := by_name_edit(doc.tr.source, start, end, fields)) is None:
                continue
            a, b, text = fix
            edit = {"range": {"start": lines.position(a), "end": lines.position(b)}, "newText": text}
            out.append({
                "title": f"Read by name: {text}", "kind": "quickfix", "diagnostics": [d], "isPreferred": True,
                "edit": {"changes": {doc.uri: [edit]}},
            })
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
        it. Events outside the project (site-packages) pass through."""
        from .tools import mirrored

        assert self.root is not None and self.mirror is not None
        made: list = []
        self.sync(made)
        out = {p.as_uri(): t for p, t in made}
        passed = []
        for c in changes:
            path = uri_to_path(c.get("uri", ""))
            if not path.is_relative_to(self.root):
                passed.append(c)
            elif c.get("type") != 3 and (rel := mirrored(self.root, path)) is not None and (self.mirror / rel).exists():
                out.setdefault((self.mirror / rel).as_uri(), c.get("type", 2))
        return [{"uri": u, "type": t} for u, t in out.items()] + passed

    def inject_config(self, items: list[dict], result: list) -> list:
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
            if method_doc and method_doc[0] == "slot":
                key, doc, rng, listed = method_doc[1]
                self.client.send({**msg, "result": self.finish_slot(msg.get("result"), key, doc, rng, listed)})
                return
            if (own := self.own_actions.pop(mid, None)) and "result" in msg:
                result = self.to_editor(msg["result"], method_doc[1]) if msg["result"] else []
                self.client.send({**msg, "result": [*(result if isinstance(result, list) else []), *own]})
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
                    caps["notebookDocumentSync"] = {"notebookSelector": [{"notebook": "jupyter-notebook", "cells": [{"language": "python"}]}]}
                    cp = caps.setdefault("completionProvider", {})
                    have = cp.setdefault("triggerCharacters", [])
                    self.own_triggers = {c for c in EXTRA_TRIGGERS if c not in have}
                    have.extend(sorted(self.own_triggers))
                elif req.startswith("textDocument/semanticTokens"):
                    msg = {**msg, "result": {"data": remap_tokens(msg["result"].get("data", []), doc, self.field_type, self.final_field, self.callable_types)}}
                else:
                    result = self.to_editor(msg["result"], doc)
                    msg = {**msg, "result": None if result is DROP else result}
                    if req == "textDocument/completion":
                        self.last_completion = doc
            self.client.send(msg)
            return

        if method == "workspace/configuration" and mid is not None:
            self.server_requests[mid] = msg.get("params", {}).get("items", [])
        elif method == "workspace/workspaceFolders" and mid is not None:  # we serve one root: the mirror
            assert self.mirror is not None and self.root is not None
            self.server.send({"jsonrpc": "2.0", "id": mid, "result": [{"uri": self.mirror.as_uri(), "name": self.root.name}]})
            return
        elif mid is not None and method in ("workspace/applyEdit", "window/showDocument"):  # mirror uris -> the editor's
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
            kept = [{**d, "relatedInformation": self.to_editor(d["relatedInformation"], None)} if "relatedInformation" in d else d for d in kept]
            params = {**params, "uri": self.uri_to_editor(params["uri"]), "diagnostics": kept}
            msg = {**msg, "params": params}
            if params["uri"] in self.cell_text:  # a plain notebook's cell: the version is ours, not the editor's
                return {**msg, "params": {k: v for k, v in params.items() if k != "version"}}
            return msg
        key = self.key(doc.uri)
        if key not in self.docs:
            return None  # closed .pyn: its translation's diagnostics have nowhere to go
        out = list(doc.tr.problems)
        flagged = {(p["range"]["start"]["line"], p["range"]["start"]["character"]) for p in out}
        for d in params.get("diagnostics", []):
            r = doc.tr.range_from_hidden(d["range"], display=True)
            if r is None:
                continue
            if (r["start"]["line"], r["start"]["character"]) in flagged:
                continue  # half-typed pattern item: our message says it; drop the checker's echo
            if doc.tr.quiet(doc.tr.src_lines.offset(r["start"]["line"], r["start"]["character"]), d.get("message", "")):
                continue
            fields = positional_fields(d.get("message", ""))
            d = {**d, "range": r, "message": pretty(explain_fields(d.get("message", "")))}
            if fields and "data" not in d:
                d["data"] = {"bynameFields": fields}  # for the quick fix (by_name_actions)
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
    label reads an attribute (`(decode=d) = tok` is `d = tok.decode`), so it looks like `tok.decode`; a
    shorthand (`(encode=) = tok`, `(f=)`) is also the local. Record-literal labels are keyword arguments."""
    if doc is None:
        return data
    tokens, line, col = [], 0, 0
    for i in range(0, len(data) - 4, 5):
        dl, dc, length, typ, mods = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        r = doc.tr.range_from_hidden_exact(
            {"start": {"line": line, "character": col}, "end": {"line": line, "character": col + length}}
        )
        if r is None or r["start"]["line"] != r["end"]["line"] or r["end"]["character"] <= r["start"]["character"]:
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
        callables = {t[:3] for t in tokens if t[3] in callable_types and t[:3] in spans}
        tokens = [t for t in tokens if t[:3] not in callables or t[3] in callable_types]
        tokens += [(*sp, field_type, 0) for sp in spans - callables]
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
