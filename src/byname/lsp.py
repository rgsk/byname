"""byname lsp: language server for .pyn, proxying a Python type checker.

    editor <--LSP--> byname lsp <--LSP--> checker (basedpyright-langserver by default)

The checker sees each .pyn as a shadow .py file holding its translation: open documents are sent
in memory, and every .pyn in the workspace is also written to a shadow directory so imports
resolve. Positions are mapped both ways; everything else passes through untouched.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from .output import output_path, render
from .srcmap import LineIndex, Translation
from .transform import DS, REPR, pattern_slot

DEFAULT_CHECKER = ["basedpyright-langserver", "--stdio"]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
DROP = object()  # a result element whose position fell inside generated-only code
WRITE_OUTPUT = "byname.server.writeOutput"  # executeCommand: write <file>.pyn.py now (Alt+C)
PLACEHOLDER = "__byname_slot"  # stands in for an empty pattern item while completing
EXTRA_TRIGGERS = ["(", ","]  # pop completion in pattern slots, like TS does after `{` / `,`
FIX_ALL_KIND, ORGANIZE_KIND = "source.fixAll", "source.organizeImports"  # ruff code actions, served by us
SKIP_KINDS = {2, 3}  # CompletionItemKind Method, Function: not fields


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


REC_RE = re.compile(r"_rec_(\w+)")
CODE_KEYS = {"newText", "insertText", "filterText", "sortText", "uri", "targetUri", "data"}


def pretty(text: str) -> str:
    """Display form of record types: _rec_name__age[str, int] -> (name: str, age: int)."""
    out, i = [], 0
    while m := REC_RE.search(text, i):
        out.append(text[i : m.start()])
        fields = m.group(1).split("__")
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
            out.append("(" + ", ".join(f"{f}: {pretty(a)}" for f, a in zip(fields, args)) + ")")
        else:
            out.append("(" + ", ".join(fields) + ")")
        i = j
    out.append(text[i:])
    return "".join(out)


def is_generated_name(name) -> bool:
    return isinstance(name, str) and (name in (DS, REPR, "_NT") or name.startswith("_rec_"))


def is_range(v) -> bool:
    return isinstance(v, dict) and isinstance(v.get("start"), dict) and isinstance(v.get("end"), dict) and "line" in v["start"]


def is_position(v) -> bool:
    return isinstance(v, dict) and "line" in v and "character" in v and len(v) == 2


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


# --- the proxy ---------------------------------------------------------------


class Proxy:
    def __init__(self, checker: list[str]):
        self.client = Writer(sys.stdout.buffer)
        self.proc = subprocess.Popen(checker, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.server = Writer(self.proc.stdin)
        self.root: Path | None = None
        self.shadow_root: Path | None = None
        self.docs: dict[Path, list[Doc]] = {}  # open .pyn by path; recent versions, newest last
        self.pending: dict = {}  # editor request id -> (method, Doc)
        self.server_requests: dict = {}  # checker request id -> configuration items
        self.last_completion: Doc | None = None
        self.counter = 0  # checker-side document versions
        self.temp_versions: set[int] = set()  # versions holding a completion-only temporary text
        self.own_triggers: set[str] = set()
        self.output_on_save = False  # byname.outputOnSave: write <file>.pyn.py on every save
        self.strip_main = False  # byname.outputStripMain: drop `if __name__ == "__main__":` from it

    # paths --------------------------------------------------------------

    def shadow_path(self, pyn: Path) -> Path:
        assert self.shadow_root is not None
        try:
            rel = pyn.relative_to(self.root)
        except ValueError:
            rel = Path("_abs") / pyn.relative_to(pyn.anchor)
        return (self.shadow_root / rel).with_suffix(".py")

    def pyn_path(self, shadow: Path) -> Path | None:
        if self.shadow_root is None:
            return None
        try:
            rel = shadow.relative_to(self.shadow_root)
        except ValueError:
            return None
        if rel.parts and rel.parts[0] == "_abs":
            return (Path("/") / Path(*rel.parts[1:])).with_suffix(".pyn")
        return (self.root / rel).with_suffix(".pyn")

    def is_pyn(self, uri) -> bool:
        return isinstance(uri, str) and uri.startswith("file:") and uri.endswith(".pyn")

    def doc_for_shadow(self, uri: str, version=None) -> Doc | None:
        if not (isinstance(uri, str) and uri.startswith("file:")):
            return None
        pyn = self.pyn_path(uri_to_path(uri))
        if pyn is None:
            return None
        if versions := self.docs.get(pyn):
            for d in reversed(versions):
                if version is None or d.sent == version:
                    return d
            return versions[-1]
        try:
            return Doc(pyn.as_uri(), pyn.read_text(encoding="utf-8"), None)
        except OSError:
            return None

    # shadows on disk ----------------------------------------------------

    def write_shadow(self, pyn: Path, text: str | None = None) -> None:
        try:
            text = pyn.read_text(encoding="utf-8") if text is None else text
        except OSError:
            return
        out = self.shadow_path(pyn)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(Translation(text).hidden, encoding="utf-8")

    def scan(self) -> None:
        if self.root is None:
            return
        shutil.rmtree(self.shadow_root, ignore_errors=True)
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                if f.endswith(".pyn"):
                    self.write_shadow(Path(dirpath) / f)

    def extra_paths(self) -> list[str]:
        """Dirs holding .pyn: the shadow dir (sibling .pyn imports) and the real one (sibling .py imports)."""
        if self.shadow_root is None or not self.shadow_root.exists():
            return []
        out = [str(self.shadow_root)]
        for py in self.shadow_root.rglob("*.py"):
            for d in (str(py.parent), str(self.pyn_path(py).parent)):
                if d not in out:
                    out.append(d)
        return out

    # rewriting ----------------------------------------------------------

    def to_checker(self, obj, doc: Doc):
        """Request params: .pyn uri -> shadow uri, source positions -> hidden positions."""
        if isinstance(obj, list):
            return [self.to_checker(x, doc) for x in obj]
        if not isinstance(obj, dict):
            return obj
        if is_range(obj):
            return doc.tr.range_to_hidden(obj)
        if is_position(obj):
            return doc.tr.position_to_hidden(obj)
        out = {}
        for k, v in obj.items():
            if k == "uri" and self.is_pyn(v):
                out[k] = self.shadow_path(uri_to_path(v)).as_uri()
            elif k == "data":
                out[k] = v
            else:
                out[k] = self.to_checker(v, doc)
        return out

    def to_editor(self, obj, doc: Doc | None, display: bool = True, key: str = ""):
        """Results: shadow uri -> .pyn uri, hidden ranges -> source ranges (DROP if in the prelude).
        Locations widen to the source they came from (display); TextEdits keep exact insertion points."""
        if isinstance(obj, list):
            items = [self.to_editor(x, doc, display, key) for x in obj]
            return [x for x in items if x is not DROP]
        if isinstance(obj, str):
            return obj if key in CODE_KEYS or "_rec_" not in obj else pretty(obj)
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
            target = self.doc_for_shadow(uri)
            if target is not None:
                if uri_key:
                    out[uri_key] = target.uri
                else:
                    out["textDocument"] = {**td, "uri": target.uri}
            doc = target
        for k, v in obj.items():
            if k in ("data", "textDocument") or k == uri_key:
                continue
            if k == "changes" and isinstance(v, dict):  # WorkspaceEdit: {uri: [TextEdit]}
                changes = {}
                for u, edits in v.items():
                    target = self.doc_for_shadow(u)
                    changes[target.uri if target else u] = self.to_editor(edits, target)
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
            key = hashlib.sha1(str(self.root).encode()).hexdigest()[:12]
            cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            self.shadow_root = cache / "byname" / key
            self.scan()
            caps = params.get("capabilities", {})
            caps.get("general", {}).pop("positionEncodings", None)  # we map in UTF-16
            opts = params.pop("initializationOptions", None) or {}  # ours, from the editor's byname.* settings
            self.output_on_save = bool(opts.get("outputOnSave"))
            self.strip_main = bool(opts.get("outputStripMain"))
            self.pending[mid] = ("initialize", None)
            self.server.send(msg)
            return

        if method == "workspace/executeCommand" and params.get("command") == WRITE_OUTPUT:
            self.client.send(self.run_write_output(mid, (params.get("arguments") or [None])[0]))
            return

        td = params.get("textDocument") or {}
        uri = td.get("uri")
        if not self.is_pyn(uri):
            if method == "workspace/didChangeWatchedFiles":
                msg = {**msg, "params": {"changes": [self.watched(c) for c in params.get("changes", [])]}}
            elif mid is not None:  # e.g. workspace/symbol: results may still point into shadows
                self.pending[mid] = (method, self.last_completion if method == "completionItem/resolve" else None)
            self.server.send(msg)
            return

        path = uri_to_path(uri)
        shadow = self.shadow_path(path).as_uri()

        if method == "textDocument/didOpen":
            doc = Doc(uri, td["text"], td.get("version"))
            doc.sent = self.next_version()
            self.docs[path] = [doc]
            self.write_shadow(path, td["text"])
            sent = {"uri": shadow, "languageId": "python", "version": doc.sent, "text": doc.tr.hidden}
            self.server.send({**msg, "params": {"textDocument": sent}})
            return
        if method == "textDocument/didChange":
            text = params["contentChanges"][-1]["text"]  # we advertise full sync
            doc = Doc(uri, text, td.get("version"))
            self.docs.setdefault(path, []).append(doc)
            del self.docs[path][:-5]
            self.send_text(path, doc.tr.hidden, doc)
            return
        if method == "textDocument/didSave":
            versions = self.docs.get(path)
            self.write_shadow(path, versions[-1].tr.source if versions else None)
            self.server.send({**msg, "params": {"textDocument": {"uri": shadow}}})
            if self.output_on_save and versions:
                self.write_output(path, versions[-1])
            return
        if method == "textDocument/didClose":
            self.docs.pop(path, None)
            self.server.send({**msg, "params": {"textDocument": {"uri": shadow}}})
            self.client.send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": []}})
            return

        versions = self.docs.get(path)
        doc = versions[-1] if versions else Doc(uri, path.read_text(encoding="utf-8"), None)
        if mid is not None:
            self.pending[mid] = (method, doc)
        if method == "textDocument/completion" and self.complete_slot(msg, path, doc):
            return
        if method == "textDocument/formatting":
            self.client.send(self.format(mid, path, doc))
            return
        if method == "textDocument/codeAction" and (kind := source_kind(params)):
            self.client.send(self.fix(mid, path, doc, kind))
            return
        mapped = self.to_checker(params, doc)
        # on shorthand `x=`: definition -> the local x (value half); declaration -> the parameter/field
        if method == "textDocument/definition" and (vp := doc.tr.value_position(params["position"])):
            mapped["position"] = vp
        elif method == "textDocument/declaration":
            msg = {**msg, "method": "textDocument/definition"}
        self.server.send({**msg, "params": mapped})

    def next_version(self) -> int:
        self.counter += 1
        return self.counter

    def send_text(self, path: Path, hidden: str, doc: Doc | None) -> None:
        """didChange to the checker; doc=None marks a temporary completion-only text."""
        v = self.next_version()
        if doc is None:
            self.temp_versions.add(v)
        else:
            doc.sent = v
        td = {"uri": self.shadow_path(path).as_uri(), "version": v}
        self.server.send({"jsonrpc": "2.0", "method": "textDocument/didChange", "params": {"textDocument": td, "contentChanges": [{"text": hidden}]}})

    def complete_slot(self, msg: dict, path: Path, doc: Doc) -> bool:
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
        self.send_text(path, hidden, None)
        hpos = LineIndex(hidden).position(h)
        rng = {"start": tr.src_lines.position(ws), "end": tr.src_lines.position(we)}
        self.pending[mid] = ("slot", (path, doc, rng, set(listed)))
        sent = {"textDocument": {"uri": self.shadow_path(path).as_uri()}, "position": hpos}
        self.server.send({**msg, "params": sent})
        return True

    def finish_slot(self, result, path: Path, doc: Doc, rng: dict, listed: set[str]) -> dict:
        latest = self.docs.get(path, [doc])[-1]
        self.send_text(path, latest.tr.hidden, latest)  # restore the real text
        items = result.get("items", []) if isinstance(result, dict) else (result or [])
        out = []
        for it in items:
            label = it.get("label", "")
            if label.startswith("_") or label in listed or it.get("kind") in SKIP_KINDS:
                continue
            it = {k: v for k, v in it.items() if k not in ("textEdit", "additionalTextEdits", "data")}
            it["textEdit"] = {"range": rng, "newText": it.get("insertText") or label}
            it.pop("insertText", None)
            out.append(it)
        return {"isIncomplete": False, "items": out}

    def format(self, mid, path: Path, doc: Doc) -> dict:
        """Whole-document ruff format via stand-ins (see fmt.py); answered here, not by the checker."""
        from .fmt import FormatError, format_pyn

        src = doc.tr.source
        try:
            out = format_pyn(src, str(path), cwd=self.root)
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
            out = fix_pyn(src, str(path), cwd=self.root, select=ORGANIZE if kind == ORGANIZE_KIND else None)
        except (FixError, ValueError):
            out = src  # mid-edit code: offer nothing rather than fail the save
        if out == src:
            return {"jsonrpc": "2.0", "id": mid, "result": []}
        whole = {"start": {"line": 0, "character": 0}, "end": doc.tr.src_lines.position(len(src))}
        title = "byname: organize imports (ruff)" if kind == ORGANIZE_KIND else "byname: fix all (ruff)"
        edit = {"changes": {doc.uri: [{"range": whole, "newText": out}]}}
        return {"jsonrpc": "2.0", "id": mid, "result": [{"title": title, "kind": kind + ".byname", "edit": edit}]}

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

    def watched(self, change: dict) -> dict:
        uri = change.get("uri")
        if not self.is_pyn(uri):
            return change
        path = uri_to_path(uri)
        shadow = self.shadow_path(path)
        if change.get("type") == 3:  # deleted
            shadow.unlink(missing_ok=True)
        elif path not in self.docs:  # open docs are synced via didChange
            self.write_shadow(path)
        return {**change, "uri": shadow.as_uri()}

    def inject_config(self, items: list[dict], result: list) -> list:
        """Add our extraPaths (and defaults) to the checker's settings, whichever shape it asks in:
        pyright asks for `python.analysis`, basedpyright for `python` / `basedpyright` with a nested `analysis`."""

        def analysis(cfg: dict, based: bool) -> dict:
            cfg = dict(cfg or {})
            cfg["extraPaths"] = list(cfg.get("extraPaths") or []) + self.extra_paths()
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
                path, doc, rng, listed = method_doc[1]
                self.client.send({**msg, "result": self.finish_slot(msg.get("result"), path, doc, rng, listed)})
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
                        "codeActionKinds": [*kinds, *(k for k in (FIX_ALL_KIND, ORGANIZE_KIND) if k not in kinds)],
                    }
                    ecp = caps.setdefault("executeCommandProvider", {"commands": []})
                    ecp["commands"] = [*ecp.get("commands", []), WRITE_OUTPUT]
                    if stp := caps.get("semanticTokensProvider"):
                        stp["full"] = True  # we remap whole token lists; no delta support
                    caps.pop("notebookDocumentSync", None)
                    cp = caps.setdefault("completionProvider", {})
                    have = cp.setdefault("triggerCharacters", [])
                    self.own_triggers = {c for c in EXTRA_TRIGGERS if c not in have}
                    have.extend(sorted(self.own_triggers))
                elif req.startswith("textDocument/semanticTokens"):
                    msg = {**msg, "result": {"data": remap_tokens(msg["result"].get("data", []), doc)}}
                else:
                    result = self.to_editor(msg["result"], doc)
                    msg = {**msg, "result": None if result is DROP else result}
                    if req == "textDocument/completion":
                        self.last_completion = doc
            self.client.send(msg)
            return

        if method == "workspace/configuration" and mid is not None:
            self.server_requests[mid] = msg.get("params", {}).get("items", [])
        elif method == "textDocument/publishDiagnostics":
            msg = self.diagnostics(msg)
            if msg is None:
                return
        self.client.send(msg)

    def diagnostics(self, msg: dict) -> dict | None:
        params = msg["params"]
        if params.get("version") in self.temp_versions:
            return None  # computed on a completion-only temporary text
        doc = self.doc_for_shadow(params["uri"], params.get("version"))
        if doc is None:
            return msg
        if uri_to_path(doc.uri) not in self.docs:
            return None  # closed .pyn: its shadow's diagnostics have nowhere to go
        out = list(doc.tr.problems)
        flagged = {(p["range"]["start"]["line"], p["range"]["start"]["character"]) for p in out}
        for d in params.get("diagnostics", []):
            r = doc.tr.range_from_hidden(d["range"], display=True)
            if r is None:
                continue
            if (r["start"]["line"], r["start"]["character"]) in flagged:
                continue  # half-typed pattern item: our message says it; drop the checker's echo
            d = {**d, "range": r, "message": pretty(d.get("message", ""))}
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
        return {**msg, "params": params}

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


def remap_tokens(data: list[int], doc: Doc | None) -> list[int]:
    """Semantic tokens come as 5-int groups, positions relative to the previous token.
    Decode, map each token to the source, drop those on generated text, re-encode."""
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
        tokens.append((s["line"], s["character"], e["character"] - s["character"], typ, mods))
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
