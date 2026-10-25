"""Drive `byname lsp` + basedpyright like an editor would, on a small two-file workspace."""

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from byname.lsp import Doc, Proxy, pretty, read_message

pytestmark = pytest.mark.skipif(
    not (Path(sys.executable).parent / "basedpyright-langserver").exists()
    and not shutil.which("basedpyright-langserver"),
    reason="basedpyright not installed (uv sync --all-extras)",
)

PEOPLE = """\
def make(*, name: str, age: int):
    greeting = f"hi {name}"
    return (name=, age=, greeting=)
"""

MAIN = """\
from people import make
from helpers import shout

name, age = "Rahul", 26
res = make(name=, age=)
(greeting=, age=years) = res
(nope=) = res
print(shout(greeting), years)
"""

HELPERS = """\
def shout(s: str) -> str:
    return s.upper()
"""


def test_pretty_record_types():
    assert pretty("-> _rec_name__age[str, int]") == "-> (name: str, age: int)"
    # nested generics and nested records: split only at top-level commas
    assert pretty("_rec_a__b[dict[str, int], list[_rec_x[int]]]") == "(a: dict[str, int], b: list[(x: int)])"
    assert pretty('class "_rec_name__age"') == 'class "(name, age)"'  # unparametrised
    assert pretty("no records here") == "no records here"
    assert pretty("-> _tup_lo__hi[int, int]") == "-> tuple[lo: int, hi: int]"  # a tuple returned by name
    assert pretty("-> _tup_x__[Tensor, Tensor]") == "-> tuple[x: Tensor, Tensor]"  # the 2nd wasn't a bare name
    assert pretty("-> _dct_name__age[str, int]") == "-> {name: str, age: int}"  # what _asdict() returns


class Client:
    def __init__(self, root: Path, cache: Path, settings: dict | None = None):
        env = {"XDG_CACHE_HOME": str(cache), "PATH": str(Path(sys.executable).parent)}
        self.p = subprocess.Popen(
            [sys.executable, "-m", "byname", "lsp"],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.q: queue.Queue = queue.Queue()
        self.next_id = 0
        self.diags: dict[str, list] = {}
        self.asked: list[str] = []  # the methods of the server's requests, in order
        self.shown: list[str] = []  # window/showMessage texts
        self.settings = settings or {}  # the editor's, by section: what workspace/configuration gets
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self):
        while (msg := read_message(self.p.stdout)) is not None:
            self.q.put(msg)

    def send(self, msg):
        body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
        self.p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.p.stdin.flush()

    def handle(self, msg) -> dict | None:
        """Answer checker->editor requests; record diagnostics; return responses."""
        if "method" in msg and "id" in msg:  # server request
            self.asked.append(msg["method"])
            if msg["method"] == "workspace/configuration":
                self.send(
                    {"id": msg["id"], "result": [self.settings.get(i.get("section")) for i in msg["params"]["items"]]}
                )
            else:
                self.send({"id": msg["id"], "result": None})
        elif msg.get("method") == "textDocument/publishDiagnostics":
            self.diags[msg["params"]["uri"]] = msg["params"]["diagnostics"]
        elif msg.get("method") == "window/showMessage":
            self.shown.append(msg["params"]["message"])
        elif "id" in msg:
            return msg
        return None

    def request(self, method, params, timeout=60):
        self.next_id += 1
        rid = self.next_id
        self.send({"id": rid, "method": method, "params": params})
        end = time.time() + timeout
        while time.time() < end:
            msg = self.handle(self.q.get(timeout=end - time.time()))
            if msg is not None and msg["id"] == rid:
                return msg.get("result")
        raise TimeoutError(method)

    def wait_diags(self, uri, timeout=60):
        end = time.time() + timeout
        while uri not in self.diags and time.time() < end:
            try:
                self.handle(self.q.get(timeout=0.5))
            except queue.Empty:
                pass
        return self.diags[uri]

    def close(self):
        self.p.kill()

    def initialize(self, root: Path, caps: dict, **opts):
        """As VS Code starts it: rootUri, rootPath and the workspace folder all given."""
        folders = [{"uri": root.as_uri(), "name": root.name}]
        params = {
            "processId": None,
            "rootPath": str(root),
            "rootUri": root.as_uri(),
            "workspaceFolders": folders,
            "capabilities": caps,
        }
        result = self.request("initialize", {**params, "initializationOptions": opts})
        self.send({"method": "initialized", "params": {}})
        return result

    def open(self, path: Path, text: str | None = None) -> str:
        uri = path.as_uri()
        lang = "pyn" if path.suffix == ".pyn" else "python"
        td = {"uri": uri, "languageId": lang, "version": 1, "text": path.read_text() if text is None else text}
        self.send({"method": "textDocument/didOpen", "params": {"textDocument": td}})
        return uri


def pos(text: str, needle: str, nth: int = 0, delta: int = 0) -> dict:
    off = -1
    for _ in range(nth + 1):
        off = text.index(needle, off + 1)
    off += delta
    return {"line": text.count("\n", 0, off), "character": off - (text.rfind("\n", 0, off) + 1)}


def snippet(text: str, r: dict) -> str:
    line = text.splitlines()[r["start"]["line"]]
    return line[r["start"]["character"] : r["end"]["character"]]


@pytest.fixture(scope="module")
def lsp(tmp_path_factory):
    root = tmp_path_factory.mktemp("ws")
    (root / "people.pyn").write_text(PEOPLE)
    (root / "main.pyn").write_text(MAIN)
    (root / "helpers.py").write_text(HELPERS)
    c = Client(root, tmp_path_factory.mktemp("cache"))
    caps = {  # like VS Code: the checker asks us for settings, and we accept semantic tokens
        "workspace": {"configuration": True, "workspaceFolders": True, "semanticTokens": {"refreshSupport": True}},
        "notebookDocument": {"synchronization": {}},
        "textDocument": {
            "semanticTokens": {
                "requests": {"full": {"delta": True}},
                "tokenTypes": [],
                "tokenModifiers": [],
                "formats": ["relative"],
            }
        },
    }
    c.init = c.initialize(root, caps)
    uri = (root / "main.pyn").as_uri()
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": uri, "languageId": "pyn", "version": 1, "text": MAIN}},
        }
    )
    yield c, root, uri
    c.close()


def hover_text(c, uri, p, laid_out: bool = False) -> str:
    """A hover's text; a record laid out a field per line (lsp.layout_records) joined back onto one line, as
    most tests are about what a hover says, not its layout (laid_out: as sent)."""
    h = c.request("textDocument/hover", {"textDocument": {"uri": uri}, "position": p})
    text = h["contents"]["value"] if h else ""
    if laid_out:
        return text
    text = re.sub(r",\n\s*\)", ")", text)  # a record's closing line first: `,\n` before it isn't between fields
    return re.sub(r",\n\s+", ", ", re.sub(r"\(\n\s+", "(", text))


def test_spread_of_an_imported_record_type_follows_its_module(lsp):
    # `c: Cfg` with Cfg from another .pyn: the spread has Cfg's fields, and saving that module retranslates this file
    c, root, _ = lsp
    (root / "settings.pyn").write_text("type Cfg = (a: int)\n")
    c.open(root / "settings.pyn")  # in the checker's mirror, as a file made in the editor is
    text = "from settings import Cfg\ndef f(c: Cfg):\n    v = (**c)\n    return v\n"
    uri = c.open(root / "spreads.pyn", text)
    c.wait_diags(uri)
    assert "v: (a: int)" in hover_text(c, uri, pos(text, "v ="))
    new, settings = "type Cfg = (a: int, b: str)\n", (root / "settings.pyn").as_uri()
    (root / "settings.pyn").write_text(new)  # edited, then saved
    td = {"uri": settings, "version": 2}
    c.send({"method": "textDocument/didChange", "params": {"textDocument": td, "contentChanges": [{"text": new}]}})
    c.send({"method": "textDocument/didSave", "params": {"textDocument": {"uri": settings}}})
    for _ in range(50):
        if "v: (a: int, b: str)" in (got := hover_text(c, uri, pos(text, "v ="))):
            break
        time.sleep(0.1)
    assert "v: (a: int, b: str)" in got
    # changed outside the editor (a script, git) while not open: the file watcher's event does it, with no save
    c.send({"method": "textDocument/didClose", "params": {"textDocument": {"uri": settings}}})
    (root / "settings.pyn").write_text("type Cfg = (a: int, c: float)\n")
    c.send({"method": "workspace/didChangeWatchedFiles", "params": {"changes": [{"uri": settings, "type": 2}]}})
    for _ in range(50):
        if "v: (a: int, c: float)" in (got := hover_text(c, uri, pos(text, "v ="))):
            break
        time.sleep(0.1)
    assert "v: (a: int, c: float)" in got


def test_hover_destructured_local_has_field_type(lsp):
    # `greeting` comes from another .pyn, through a record, through destructuring: still `str`
    c, root, uri = lsp
    assert "greeting: str" in hover_text(c, uri, pos(MAIN, "greeting="))


RECORD_CALL = """\
from byname import record
type Config = (n_layer: int, dropout: float)
cfg = record(Config, {"n_layer": 4, "dropout": 0.2})
"""


def test_hover_in_a_record_call_shows_the_record_type_and_the_function(lsp):
    # the T written is the cast's (a type position), not record's argument, which would show TypeAliasType
    c, root, _ = lsp
    path = root / "rec_call.pyn"
    path.write_text(RECORD_CALL)
    rec_uri = c.open(path)
    want = "Config = (\n    n_layer: int,\n    dropout: float,\n)"  # as declared, a field a line (Proxy.alias_hover)
    assert want in hover_text(c, rec_uri, pos(RECORD_CALL, "Config,"), laid_out=True)
    assert "def record(" in hover_text(c, rec_uri, pos(RECORD_CALL, "record(C"))
    assert "cfg: Config" in hover_text(c, rec_uri, pos(RECORD_CALL, "cfg ="))  # named by its alias, as with cast


def test_hover_on_a_record_field_reads_the_same_everywhere(lsp):
    # a literal's field is its stub's keyword to the checker ("(parameter)"), and a field written in a type has
    # no text there (it hovered `str`): both shown as a read is, `(constant) sku: str`
    c, root, _ = lsp
    src = "type Item = (sku: str, items: list[int])\nit: Item = (sku='a', items=[])\nsku = 'b'\nr = (sku=)\nprint(it.sku)\n"
    path = root / "hovers.pyn"
    path.write_text(src)
    uri = c.open(path)
    c.wait_diags(uri)
    assert hover_text(c, uri, pos(src, "sku: str")) == "(constant) sku: str"
    assert hover_text(c, uri, pos(src, "items: list")) == "(constant) items: list[int]"
    assert hover_text(c, uri, pos(src, "sku='a'")) == "(constant) sku: str"
    assert hover_text(c, uri, pos(src, "sku=)")) == "(constant) sku: str"
    assert hover_text(c, uri, pos(src, "it.sku", delta=3)) == "(constant) sku: str"


def test_a_shape_first_typed_in_the_editor_is_typed(lsp):
    # a record type's class is in its shape's module (transform.SHAPES), written when the shape first appears:
    # the checker hears of the new file before the text importing it (Proxy.shapes_made), else the import
    # wouldn't resolve and the record would be Unknown, with no error at all
    c, root, _ = lsp
    path = root / "new_shape.pyn"
    uri = c.open(path, "x = 1\n")
    c.wait_diags(uri)
    new = "r = (zqx_field=1)\ns: str = r.zqx_field\n"
    c.diags.pop(uri)
    td = {"uri": uri, "version": 2}
    c.send({"method": "textDocument/didChange", "params": {"textDocument": td, "contentChanges": [{"text": new}]}})
    for _ in range(50):
        if any("not assignable" in d["message"] for d in c.wait_diags(uri)):
            break
        c.diags.pop(uri, None)
        time.sleep(0.1)
    assert any('"int" is not assignable to declared type "str"' in d["message"] for d in c.diags[uri])


def test_hover_shows_each_record_in_the_order_it_was_written(lsp):
    # every order of a field set is one class to the checker (transform.canonical); as in TypeScript, a name
    # bound from a literal shows that literal's order, and a list of both orders is one type, shown in the
    # order the file first writes the fields in (lsp.display_orders)
    c, root, _ = lsp
    src = "u2 = (age=26, name='r')\nu1 = (name='r', age=26)\nusers = [u1, u2]\nprint(u1, u2, users)\n"
    path = root / "orders.pyn"
    path.write_text(src)
    uri = c.open(path)
    c.wait_diags(uri)
    assert hover_text(c, uri, pos(src, "u1 =")) == "(variable) u1: (name: str, age: int)"
    assert hover_text(c, uri, pos(src, "u2 =")) == "(variable) u2: (age: int, name: str)"
    assert hover_text(c, uri, pos(src, "users =")) == "(variable) users: list[(age: int, name: str)]"


def test_hover_on_a_record_s_brackets_shows_the_record(lsp):
    # the checker answers with what byname made of it: a literal's generic `_rec_` function, a written type's
    # `_typ_` alias or class. On the `(`, shown as the record: a literal as its fields' types in its own order
    # (Proxy.record_hover), a type as written; on a blank between a literal's brackets, nothing
    c, root, _ = lsp
    src = (
        "def total(items: list[(sku: str, qty: int)], b: (age: int, n: str)) -> int:\n    return 0\n\n\n"
        "rows = [\n    (\n        name='rahul',\n        age=(n=1),\n    ),\n]\nx1 = (qty=1, sku='a')\n"
    )
    path = root / "brackets.pyn"
    path.write_text(src)
    uri = c.open(path)
    c.wait_diags(uri)
    assert hover_text(c, uri, pos(src, "(\n        name")) == "(name: str, age: (n: int))"
    assert hover_text(c, uri, {"line": 6, "character": 2}) == ""  # a blank inside it: nothing, the `(` is where
    assert hover_text(c, uri, pos(src, "(n=1)")) == "(n: int)"
    assert hover_text(c, uri, pos(src, "(qty=1")) == "(qty: int, sku: str)"  # its own order
    assert hover_text(c, uri, pos(src, "(sku: str")) == "(type) (sku: str, qty: int)"
    assert hover_text(c, uri, pos(src, "(age: int")) == "(type) (age: int, n: str)"
    assert hover_text(c, uri, pos(src, "name=")) == "(constant) name: str"


def test_hover_on_a_record_type_alias_shows_it_as_written(lsp):
    # the checker expands every alias inside an alias's hover (`items: list[(sku: str, qty: int)]`); shown as the
    # declaration is written instead, `items: list[Item]` (Proxy.alias_hover), from its own file or another, closed
    c, root, _ = lsp
    (root / "alias_types.pyn").write_text(
        "type Item = (sku: str, qty: int)\ntype Order = (\n    id: int,\n    items: list[Item],\n)\n"
    )
    made = [{"uri": (root / "alias_types.pyn").as_uri(), "type": 1}]
    c.send({"method": "workspace/didChangeWatchedFiles", "params": {"changes": made}})
    src = "from alias_types import Order\n\norder: Order = (id=1, items=[])\n"
    path = root / "alias_uses.pyn"
    path.write_text(src)
    uri = c.open(path)
    c.wait_diags(uri)
    want = "(type) Order = (\n    id: int,\n    items: list[Item],\n)"  # a field a line, as TypeScript shows it
    assert hover_text(c, uri, pos(src, "Order\n"), laid_out=True) == want
    assert hover_text(c, uri, pos(src, "Order ="), laid_out=True) == want
    types = c.open(root / "alias_types.pyn")  # and on its own declaration
    c.wait_diags(types)
    assert hover_text(c, types, {"line": 1, "character": 6}, laid_out=True) == want


def test_a_record_type_in_a_hover_has_a_field_a_line(lsp):
    # as TypeScript shows an object type (lsp.layout_records), wherever it is: a variable's, a list's item's, a
    # literal's on its `(`, a parameter's (under its line); a record in a field one level deeper. A parameter
    # list isn't a record, nor is a callable's
    c, root, _ = lsp
    src = (
        "u = (name='r', home=(city='p'))\nus = [u]\n\n\ndef f(a: int) -> int:\n    return a\n\n\n"
        "def g(user: (name: str, age: int), sex: str) -> None: ...\n"
    )
    path = root / "layout.pyn"
    path.write_text(src)
    uri = c.open(path)
    c.wait_diags(uri)
    record = "(\n    name: str,\n    home: (\n        city: str,\n    ),\n)"
    assert hover_text(c, uri, pos(src, "u ="), laid_out=True) == f"(variable) u: {record}"
    assert hover_text(c, uri, pos(src, "us ="), laid_out=True) == f"(variable) us: list[{record}]"
    assert hover_text(c, uri, pos(src, "(name"), laid_out=True) == record
    assert "(\n" not in hover_text(c, uri, pos(src, "f(a"), laid_out=True)
    assert "    user: (\n        name: str,\n        age: int,\n    )," in hover_text(
        c, uri, pos(src, "g(u"), laid_out=True
    )


def test_hover_renamed_local(lsp):
    c, root, uri = lsp
    assert "years: int" in hover_text(c, uri, pos(MAIN, "years", nth=0))


def test_hover_record_returning_function(lsp):
    # the inferred return type is the generated record class, shown as its fields
    c, root, uri = lsp
    text = hover_text(c, uri, pos(MAIN, "make", nth=1))
    assert "-> (name: str, age: int, greeting: str)" in text
    assert "_rec_" not in text


def test_error_message_shows_record_fields(lsp):
    c, root, uri = lsp
    msg = next(d["message"] for d in c.wait_diags(uri) if "nope" in d["message"])
    assert "(name: str, age: int, greeting: str)" in msg and "_rec_" not in msg


def test_unknown_field_is_an_error_on_the_field(lsp):
    c, root, uri = lsp
    diags = c.wait_diags(uri)
    errors = [d for d in diags if "nope" in d["message"]]
    assert errors, diags
    assert snippet(MAIN, errors[0]["range"]) == "nope"


def test_no_spurious_errors(lsp):
    # everything else in MAIN is valid, including imports of a .pyn and a .py sibling
    c, root, uri = lsp
    diags = [d for d in c.wait_diags(uri) if d.get("severity", 1) == 1 and "nope" not in d["message"]]
    assert diags == []


def test_definition_jumps_into_other_pyn(lsp):
    c, root, uri = lsp
    res = c.request("textDocument/definition", {"textDocument": {"uri": uri}, "position": pos(MAIN, "make", nth=1)})
    locs = res if isinstance(res, list) else [res]
    target = locs[0].get("targetUri") or locs[0]["uri"]
    rng = locs[0].get("targetSelectionRange") or locs[0]["range"]
    assert target == (root / "people.pyn").as_uri()
    assert snippet(PEOPLE, rng) == "make"


def test_completion_on_record_lists_fields(lsp):
    c, root, uri = lsp
    text = MAIN + "res.\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": text}]},
        }
    )
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": pos(text, "res.\n", delta=4)})
    labels = {i["label"] for i in (res["items"] if isinstance(res, dict) else res)}
    assert {"name", "age", "greeting"} <= labels


def test_completion_hides_generated_names(lsp):
    c, root, uri = lsp
    text = MAIN + "res.\n"
    res = c.request(
        "textDocument/completion",
        {"textDocument": {"uri": uri}, "position": {"line": text.count("\n"), "character": 0}},
    )
    labels = {i["label"] for i in (res["items"] if isinstance(res, dict) else res)}
    assert "greeting" in labels  # module-level names are offered...
    assert "_ds" not in labels and not any(l.startswith("_rec_") for l in labels)  # ...generated ones aren't


def test_definition_of_destructured_local_covers_the_word(lsp):
    # `greeting` was bound by `(greeting=, ...)`; the jump lands on that word, not a zero-width point
    c, root, uri = lsp
    res = c.request(
        "textDocument/definition", {"textDocument": {"uri": uri}, "position": pos(MAIN, "shout(greeting", delta=7)}
    )
    rng = res[0].get("targetSelectionRange") or res[0]["range"]
    assert MAIN.splitlines()[rng["start"]["line"]].startswith("(greeting=, age=years)")
    assert snippet(MAIN, rng) == "greeting"


def test_the_editors_mode_applies_standard_by_default():
    # basedpyright.analysis.typeCheckingMode from the editor, "standard" if unset rather than basedpyright's
    # noisy "recommended" (a project config decides over both: test_a_project_config_decides_over_the_editors_settings)
    from byname.lsp import Proxy

    items = [{"section": "basedpyright"}, {"section": "basedpyright.analysis"}]
    for editor, want in (("off", "off"), ("strict", "strict"), (None, "standard")):
        sent = {"analysis": {"typeCheckingMode": editor}} if editor else None
        out = Proxy.inject_config(items, [sent, {"typeCheckingMode": editor} if editor else None])
        assert out[0]["analysis"]["typeCheckingMode"] == out[1]["typeCheckingMode"] == want


def test_disable_language_services_reaches_bynames_checker_as_sent():
    # basedpyright.disableLanguageServices means the same for byname's checker as for the BasedPyright
    # extension. byname doesn't use it to turn the extension off: it only stops completion, definitions and
    # references there, and the extension's errors stay (see offerToDisableBasedPyright in extension.js)
    from byname.lsp import Proxy

    sent = {"disableLanguageServices": True, "analysis": {"typeCheckingMode": "strict"}}
    (based,) = Proxy.inject_config([{"section": "basedpyright"}], [sent])
    assert based == sent


def test_the_checker_runs_on_bynames_interpreter_unless_the_editor_names_one():
    # the venv byname runs from, so the project's packages resolve. "python" is the BasedPyright extension's
    # declared default for python.pythonPath, sent once it's installed: the python on PATH, without them
    from byname.lsp import Proxy

    items = [{"section": "python"}]
    for sent in (None, {}, {"pythonPath": ""}, {"pythonPath": "python"}):
        (python,) = Proxy.inject_config(items, [sent])
        assert python["pythonPath"] == sys.executable
    chosen = "/opt/other/bin/python3"
    (python,) = Proxy.inject_config(items, [{"pythonPath": chosen}])
    assert python["pythonPath"] == chosen


def test_a_project_config_decides_over_the_editors_settings(tmp_path):
    # with [tool.basedpyright], the checker ignores the editor's analysis settings, as for a .py in a plain
    # Python project: a global basedpyright.analysis.typeCheckingMode "off" doesn't silence .py or .pyn
    root = tmp_path / "ws"
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.basedpyright]\ntypeCheckingMode = "standard"\n')
    (root / "a.py").write_text("x: int = 'a'\n")
    (root / "b.pyn").write_text("x: int = 'a'\n")
    off = {"typeCheckingMode": "off"}
    c = Client(root, tmp_path / "cache", {"basedpyright": {"analysis": off}, "basedpyright.analysis": off})
    c.initialize(root, {"workspace": {"configuration": True, "workspaceFolders": True}})
    for name in ("a.py", "b.pyn"):
        uri = c.open(root / name)
        c.wait_diags(uri)
        want = [(0, 'Type "Literal[\'a\']" is not assignable to declared type "int"')]
        assert wait_errors(c, uri, want) == want, name
    c.close()


def test_checker_commands_are_advertised_with_ours(lsp):
    # byname is the only Python server in the editor, so basedpyright's commands come through too, under
    # byname.checker.: the BasedPyright extension registers basedpyright.* itself, and VS Code refuses a name
    # registered twice (whichever client starts second fails)
    c, root, uri = lsp
    commands = c.init["capabilities"]["executeCommandProvider"]["commands"]
    assert "byname.server.writeOutput" in commands
    assert "byname.checker.basedpyright.createtypestub" in commands
    assert not any(cmd.startswith("basedpyright.") for cmd in commands)
    # run under the new name, it reaches the checker by its own
    res = c.request(
        "workspace/executeCommand", {"command": "byname.checker.basedpyright.organizeimports", "arguments": [uri]}
    )
    assert res == {"changes": {}}


def test_only_the_checkers_own_commands_get_the_prefix():
    from byname.lsp import prefix_commands

    checker = {"title": "Create stub", "command": "basedpyright.createtypestub", "arguments": ["x"]}
    editors = {"title": "", "command": "editor.action.triggerParameterHints"}
    result = [{"title": "Create stub", "kind": "quickfix", "command": checker}, {"label": "f", "command": editors}]
    action, item = prefix_commands(result, {"basedpyright.createtypestub"})
    assert action["command"] == {**checker, "command": "byname.checker.basedpyright.createtypestub"}
    assert item["command"] == editors
    tokens = {"data": [0, 1, 2, 3, 4]}
    assert prefix_commands(tokens, {"basedpyright.createtypestub"}) == tokens


def test_semantic_tokens_cover_only_source_text(lsp):
    # tokens are remapped from the hidden file; none may point at generated code like `_ds`
    c, root, uri = lsp
    caps = c.init["capabilities"]
    assert caps["semanticTokensProvider"]["full"] is True
    legend = caps["semanticTokensProvider"]["legend"]["tokenTypes"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    text = MAIN + "res.\n"  # current document (completion test appended this)
    lines = text.splitlines()
    seen, line, col = {}, 0, 0
    for i in range(0, len(data), 5):
        dl, dc, length, typ, _ = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        seen.setdefault(lines[line][col : col + length], set()).add(legend[typ])
    assert all(word.isidentifier() for word in seen), seen
    assert "_ds" not in seen and not any(w.startswith("_rec_") for w in seen)
    assert "variable" in seen["years"]  # the renamed local gets variable colouring
    assert "function" in seen["make"]
    assert seen["greeting"] == {"variable"}  # `greeting=` declares the local, so it's coloured as one
    assert "property" in seen["age"]  # a renamed item's label is only the field


def test_spread_names_and_fields_keep_their_colours(lsp):
    # a spread of a known record is rebuilt field by field for the checker; the user's `d` must stay real
    # text (variable colour, not dropped), and field names written next to it are record fields (property)
    c, root, uri = lsp
    text = "d = (a=1, b=2)\nx = (**d, c=3)\ny = (**load(), c=3)\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 992}, "contentChanges": [{"text": text}]},
        }
    )
    legend = c.init["capabilities"]["semanticTokensProvider"]["legend"]["tokenTypes"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    lines, line, col, got = text.splitlines(), 0, 0, {}
    for i in range(0, len(data), 5):
        dl, dc, length, typ, _ = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        got[(line, lines[line][col : col + length])] = legend[typ]
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 993}, "contentChanges": [{"text": MAIN}]},
        }
    )
    assert got.get((1, "d")) == "variable" and got.get((1, "c")) == "property", got
    assert got.get((2, "c")) == "property", got  # the generic path, for comparison


def test_pattern_labels_reading_methods_are_coloured_as_methods(lsp):
    # a renamed item's label reads an attribute, so it's coloured like one: `decode=` as `tok.decode`
    # (method). A shorthand is also the local, so it takes the local's colour: `encode=` like `renamed`
    # (a bound method in a local is a function), `n=` a variable
    c, root, uri = lsp
    text = (
        "class Tok:\n    def __init__(self): self.n = 65\n    def encode(self, s: str) -> int: return 1\n"
        "    def decode(self, i: int) -> str: return ''\n(encode=, decode=renamed, n=) = Tok()\nr = (encode=)\n"
    )
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 994}, "contentChanges": [{"text": text}]},
        }
    )
    legend = c.init["capabilities"]["semanticTokensProvider"]["legend"]["tokenTypes"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    lines, line, col, got = text.splitlines(), 0, 0, {}
    for i in range(0, len(data), 5):
        dl, dc, length, typ, _ = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        got[(line, lines[line][col : col + length])] = legend[typ]
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 995}, "contentChanges": [{"text": MAIN}]},
        }
    )
    assert got[(4, "decode")] == "method" and got[(4, "encode")] == got[(4, "renamed")] == "function", got
    assert got[(4, "n")] == "variable", got
    assert got[(5, "encode")] == "function", got  # a record built from the local `encode`


def test_a_shorthand_is_coloured_as_the_local_it_passes(lsp):
    # `x=` is also the local `x` (Ctrl+click goes there), so it takes that local's colour in calls, records,
    # patterns and loops: a Final `step` is readonly in `log(step=)` as everywhere else. A renamed item's
    # label is only the parameter or field
    c, root, uri = lsp
    text = (
        "from typing import Final\n"
        "def log(*, step: int, lr: float): ...\n"
        "def run(lr: float):\n"
        "    step: Final = 1\n"
        "    log(step=, lr=)\n"
        "    rec = (step=)\n"
        "    (step=s): Final = rec\n"
        "    (rate=) = (rate=lr)\n"
        "    for (x=) in [(x=s)]: print(x, rate)\n"
    )
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 996}, "contentChanges": [{"text": text}]},
        }
    )
    legend = c.init["capabilities"]["semanticTokensProvider"]["legend"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    lines, line, col, got = text.splitlines(), 0, 0, {}
    for i in range(0, len(data), 5):
        dl, dc, length, typ, mods = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        named = {m for k, m in enumerate(legend["tokenModifiers"]) if mods >> k & 1}
        got.setdefault((line, lines[line][col : col + length]), []).append((legend["tokenTypes"][typ], named))
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 997}, "contentChanges": [{"text": MAIN}]},
        }
    )
    declared, call, record, renamed, pattern, loop = 3, 4, 5, 6, 7, 8
    final_step = got[(declared, "step")][0]
    assert final_step[0] == "variable" and "readonly" in final_step[1], got
    assert got[(call, "step")] == [final_step], got  # the call passes the Final local
    assert got[(call, "lr")] == [("parameter", {"parameter"})], got  # it passes run's parameter
    assert got[(record, "step")] == [final_step], got
    # renamed: `step` is only the field, coloured as a read of it, `rec.step` (a record's field is static)
    assert got[(renamed, "step")] == [("property", {"classMember", "static"})], got
    assert got[(pattern, "rate")][0][0] == "variable", got  # `(rate=) = ...` declares the local `rate`
    assert got[(loop, "x")][0][0] == "variable", got


def test_fields_read_through_record_types_look_like_record_fields(lsp):
    # `p.age` through a record type reads a Final Protocol attribute (readonly static); a record's own
    # field is static. Both must get the same token, or themes colour them differently
    c, root, uri = lsp
    text = "def f(p: (..., age: int), q: (age: int, name: str)) -> int:\n    return p.age + q.age\nr = (age=1, name='x')\nprint(r.age)\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 990}, "contentChanges": [{"text": text}]},
        }
    )
    legend = c.init["capabilities"]["semanticTokensProvider"]["legend"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    lines, line, col, got = text.splitlines(), 0, 0, []
    for i in range(0, len(data), 5):
        dl, dc, length, typ, mods = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        if lines[line][col : col + length] == "age" and line in (1, 3):
            got.append((legend["tokenTypes"][typ], mods))
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 991}, "contentChanges": [{"text": MAIN}]},
        }
    )
    assert len(got) == 3 and len(set(got)) == 1, got


def lines_of(res) -> list[tuple[str, int]]:
    locs = res if isinstance(res, list) else [res]
    out = []
    for l in locs:
        uri = l.get("targetUri") or l["uri"]
        rng = l.get("targetSelectionRange") or l["range"]
        out.append((Path(uri_path(uri)).name, rng["start"]["line"]))
    return out


def uri_path(uri: str) -> str:
    from byname.lsp import uri_to_path

    return str(uri_to_path(uri))


def test_ctrl_click_shorthand_goes_to_local(lsp):
    # `name` in `make(name=, age=)` stands for the local `name`; definition jumps there
    c, root, uri = lsp
    res = c.request("textDocument/definition", {"textDocument": {"uri": uri}, "position": pos(MAIN, "name=,")})
    assert lines_of(res) == [("main.pyn", MAIN.splitlines().index('name, age = "Rahul", 26'))]


def test_go_to_declaration_shorthand_goes_to_parameter(lsp):
    # the same click via Go to Declaration reaches make's parameter in people.pyn
    c, root, uri = lsp
    assert c.init["capabilities"]["declarationProvider"] is True
    res = c.request("textDocument/declaration", {"textDocument": {"uri": uri}, "position": pos(MAIN, "name=,")})
    assert lines_of(res) == [("people.pyn", 0)]


def test_completion_inside_pattern_offers_fields(lsp):
    # typing `gr` as a pattern item suggests the record's matching field; `greeting` replaces exactly `gr`
    c, root, uri = lsp
    text = MAIN + "(name=, gr) = res\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 10}, "contentChanges": [{"text": text}]},
        }
    )
    p = pos(text, "gr) = res", delta=2)
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": p})
    items = res["items"] if isinstance(res, dict) else res
    labels = {i["label"] for i in items}
    assert labels == {"greeting"}  # filtered by the typed prefix; no keywords like `and`/`assert`
    greeting = next(i for i in items if i["label"] == "greeting")
    if "textEdit" in greeting:
        rng = greeting["textEdit"].get("range") or greeting["textEdit"]["replace"]
        assert snippet(text, rng) == "gr"
    c.diags.pop(uri, None)
    diags = c.wait_diags(uri)
    on_line = [d for d in diags if d["range"]["start"]["line"] == p["line"]]
    assert [d["message"] for d in on_line] == ["pattern item 'gr' needs '='"]  # ours only, no checker echo


def complete(c, uri, text, marker="|"):
    at = text.index(marker)
    text = text.replace(marker, "")
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 100 + at}, "contentChanges": [{"text": text}]},
        }
    )
    p = {"line": text.count("\n", 0, at), "character": at - (text.rfind("\n", 0, at) + 1)}
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": p})
    return text, (res["items"] if isinstance(res, dict) else res)


def in_menu_order(items: list[dict]) -> list[str]:
    """Labels as VS Code lists them with nothing typed yet: by sortText, then by label."""
    return [i["label"] for i in sorted(items, key=lambda i: (i.get("sortText", i["label"]), i["label"]))]


def test_a_record_s_fields_complete_in_the_order_written(lsp):
    # records stand in for tuples, so the order they're written in is offered everywhere, as hover shows it,
    # not the checker's (its class is sorted): a pattern and a literal list them alike
    c, root, uri = lsp
    pre = (
        MAIN + "type Outcome = (success: bool, mode: str, steps: int)\no: Outcome = (success=True, mode='m', steps=1)\n"
    )
    _, items = complete(c, uri, pre + "(|) = o\n")
    assert in_menu_order(items) == ["success", "mode", "steps"]
    _, items = complete(c, uri, pre + "(mode=, |) = o\n")
    assert in_menu_order(items) == ["success", "steps"]
    _, items = complete(c, uri, pre + "x: Outcome = (|)\n")
    assert in_menu_order(items) == ["success=", "mode=", "steps="]
    # a record with no type: its literal's order
    _, items = complete(c, uri, MAIN + "u = (zip=1, city='c', age=2)\n(|) = u\n")
    assert in_menu_order(items) == ["zip", "city", "age"]
    # a name bound to a literal: that literal's order, as its hover shows, though the file wrote another first
    two = MAIN + "u1 = (zip=1, city='c')\nu2 = (city='d', zip=2)\n"
    _, items = complete(c, uri, two + "(|) = u2\n")
    assert in_menu_order(items) == ["city", "zip"]
    _, items = complete(c, uri, two + "(|) = u1\n")
    assert in_menu_order(items) == ["zip", "city"]
    _, items = complete(c, uri, two + "(|) = [u2][0]\n")  # not a name: the file's first order
    assert in_menu_order(items) == ["zip", "city"]


def test_a_spread_record_completes_while_a_line_is_half_typed(lsp):
    # `print(b2.)` doesn't parse: the known names are still read from the rest of the file, so b2 keeps
    # its exact type and `b2.` offers its fields, not the editor's word list
    c, root, uri = lsp
    _, items = complete(
        c, uri, "type User = (name: str, age: int)\nrec: User = (name='r', age=1)\nb2 = (**rec)\nprint(b2.|)\n"
    )
    assert {"name", "age"} <= {i["label"] for i in items}


def test_completion_in_empty_pattern_slot(lsp):
    # like TS `const { | } = fn()`: every field, nothing else; already-listed fields left out
    c, root, uri = lsp
    text, items = complete(c, uri, MAIN + "(name=, |) = res\n")
    assert {i["label"] for i in items} == {"age", "greeting"}


def test_completion_on_first_pattern_item(lsp):
    # `(gree|) = res` is still plain Python, but it's where a pattern starts
    c, root, uri = lsp
    text, items = complete(c, uri, MAIN + "(gree|) = res\n")
    assert [i["label"] for i in items] == ["greeting"]
    assert snippet(text, items[0]["textEdit"]["range"]) == "gree"


def test_completion_in_for_and_nested_slots(lsp):
    # same field completion in `for (...) in xs` targets and inside a nested group
    c, root, uri = lsp
    _, items = complete(c, uri, MAIN + "for (name=, |) in [res]: pass\n")
    assert {i["label"] for i in items} == {"age", "greeting"}
    _, items = complete(c, uri, MAIN + "(x=(gree|)) = (x=res)\n")
    assert [i["label"] for i in items] == ["greeting"]
    _, items = complete(c, uri, MAIN + "for (x=(age=, |)) in [(x=res)]: pass\n")
    assert {i["label"] for i in items} == {"name", "greeting"}


REST = """\
type User = (name: str, age: int, city: str)
def load() -> User: ...
(name=, **rest) = load()
"""


def test_a_rest_hovers_as_the_fields_not_named_and_fields_complete_before_it(lsp):
    c, root, uri = lsp
    _, items = complete(c, uri, REST.replace("(name=, **rest)", "(name=, |, **rest)"))
    assert {i["label"] for i in items} == {"age", "city"}
    path = root / "rest.pyn"
    path.write_text(REST)
    rest_uri = c.open(path)
    for _ in range(50):  # load()'s return is the checker's to say: a probe, then the final text
        if "rest: (age: int, city: str)" in (got := hover_text(c, rest_uri, pos(REST, "rest)"))):
            break
        time.sleep(0.1)
    assert "rest: (age: int, city: str)" in got
    assert [d["message"] for d in c.wait_diags(rest_uri) if d.get("severity", 1) == 1] == []


def test_completion_on_a_class_instance_offers_its_data_not_its_methods(lsp):
    # destructuring reads attributes, so properties and functions held in fields are slots too; a method isn't,
    # as the checker rejects one bound to its object. A staticmethod is a method to completion as well
    c, root, uri = lsp
    cls = (
        "from typing import Callable\n"
        "class Tok:\n    act: Callable[[int], int] = abs\n    def __init__(self): self.vocab_size = 65\n"
        "    def encode(self, s: str) -> list[int]: return []\n    @property\n    def n(self) -> int: return 1\n"
        "    @staticmethod\n    def zero() -> int: return 0\n"
    )
    _, items = complete(c, uri, MAIN + cls + "(|) = Tok()\n")
    assert {i["label"] for i in items} == {"act", "n", "vocab_size"}
    _, items = complete(c, uri, MAIN + "(|) = [1]\n")
    assert items == []  # a list has only methods


def test_replace_completes_and_checks_fields(lsp):
    # `_replace(` offers the record's fields; a stale `**cfg._asdict()` is an error in the editor
    c, root, uri = lsp
    _, items = complete(c, uri, MAIN + "res._replace(|)\n")
    assert {"name=", "age=", "greeting="} <= {i["label"] for i in items}
    text = MAIN + "res._replace(nme='x')\ndef h(*, name: str): ...\nh(**res._asdict())\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 900}, "contentChanges": [{"text": text}]},
        }
    )
    end, msgs = time.time() + 60, []
    while time.time() < end and not any("nme" in m for m in msgs):  # skip diagnostics for the previous text
        c.diags.pop(uri, None)
        msgs = [d["message"] for d in c.wait_diags(uri)]
    assert any('No parameter named "nme"' in m for m in msgs)
    assert any('No parameter named "age"' in m for m in msgs)  # age, greeting have no parameter in h


def test_mixed_record_list_is_checked_in_the_editor(lsp):
    # basedpyright would infer list[Unknown] here and stay silent; the strict-inference comment keeps it typed
    c, root, uri = lsp
    text = MAIN + 'rs = [(name="a", age="90"), (name="b", age=23)]\nrs[0]._replace(nme="x")\n'
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 950}, "contentChanges": [{"text": text}]},
        }
    )
    end, msgs = time.time() + 60, []
    while time.time() < end and not any("nme" in m for m in msgs):  # skip diagnostics for the previous text
        c.diags.pop(uri, None)
        msgs = [d["message"] for d in c.wait_diags(uri)]
    assert any('No parameter named "nme"' in m for m in msgs)


def test_a_walrus_labelling_a_returned_tuple_is_not_called_unused(lsp):
    # `(x := ...)` in a return names a position (hover shows tuple[x: ..., y: ...]); nothing reads it after
    c, root, uri = lsp
    text = (
        MAIN
        + "def pair(a: int):\n    return (lo := a, hi := a + 1)\ndef other(a: int):\n    unused = a\n    return a\n"
    )
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 960}, "contentChanges": [{"text": text}]},
        }
    )
    end, msgs = time.time() + 60, []
    while time.time() < end and not any('"unused"' in m for m in msgs):  # skip diagnostics for the previous text
        c.diags.pop(uri, None)
        msgs = [d["message"] for d in c.wait_diags(uri)]
    assert any('"unused" is not accessed' in m for m in msgs)  # other unused variables still are
    assert not any('"lo"' in m or '"hi"' in m for m in msgs)


def test_explicit_record_type_errors_in_the_editor(lsp):
    # any field order is fine for an explicit type; an extra field is named plainly, not as _byname_fieldset
    c, root, uri = lsp
    text = MAIN + "type U = (name: str, age: int)\nok: U = (age=1, name='x')\nbad: U = (age=1, name='x', po=2)\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 970}, "contentChanges": [{"text": text}]},
        }
    )
    end, diags = time.time() + 60, []
    while time.time() < end and not any("po" in d["message"] for d in diags):  # skip diagnostics for the previous text
        c.diags.pop(uri, None)
        diags = c.wait_diags(uri)
    last = text.count("\n") - 1
    mine = [d for d in diags if d["range"]["start"]["line"] in (last - 1, last)]
    assert len(mine) == 1 and mine[0]["range"]["start"]["line"] == last
    assert mine[0]["message"].endswith("extra field: po") and "_byname_fieldset" not in mine[0]["message"]


def test_comma_trigger_only_inside_patterns_and_calls(lsp):
    # `,` pops completion in a pattern or a call; anywhere else (a tuple) it returns nothing instead of noise
    c, root, uri = lsp
    assert "," in c.init["capabilities"]["completionProvider"]["triggerCharacters"]
    text = MAIN + "t = (1,)\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 500}, "contentChanges": [{"text": text}]},
        }
    )
    p = pos(text, "t = (1,", delta=7)
    ctx = {"triggerKind": 2, "triggerCharacter": ","}
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": p, "context": ctx})
    assert res["items"] == []


def test_completion_right_after_a_comma_in_a_call_offers_the_unused_keywords(lsp):
    # basedpyright offers nothing at `f(a=1, |)`, even when asked; we ask it right after `(` instead,
    # where it offers the keyword parameters not yet used, and insert them at the cursor
    c, _, uri = lsp
    for typed in [", ", ","]:  # asked for (Ctrl+Space), with and without a space
        text, items = complete(c, uri, MAIN + f"res._replace(name='x'{typed}|)\n")
        keywords = {i["label"] for i in items if i["label"].endswith("=")}
        assert keywords == {"age=", "greeting="}  # name= is already given
        cursor = pos(text, f"name='x'{typed}", delta=len(f"name='x'{typed}"))
        assert all(i["textEdit"]["range"] == {"start": cursor, "end": cursor} for i in items)
    # popped by typing `,`: our own trigger character
    text = MAIN + "res._replace(name='x',)\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 980}, "contentChanges": [{"text": text}]},
        }
    )
    p = pos(text, "name='x',", delta=len("name='x',"))
    ctx = {"triggerKind": 2, "triggerCharacter": ","}
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": p, "context": ctx})
    assert {"age=", "greeting="} <= {i["label"] for i in res["items"]}


def test_completion_in_an_annotated_record_literal_offers_the_types_fields(lsp):
    # `user: User = (|)`: the record literal's fields come from the annotation, minus those given
    c, _, uri = lsp
    pre = MAIN + "type User = (name: str, age: int, sex: str)\n"
    _, items = complete(c, uri, pre + "user: User = (|)\n")
    assert {i["label"] for i in items} == {"name=", "age=", "sex="}
    _, items = complete(c, uri, pre + "user: User = (name='x', |)\n")
    assert {i["label"] for i in items} == {"age=", "sex="}
    # on one line, right after a comma, as on a line of its own
    _, items = complete(c, uri, pre + "u = (name='r', age=1)\nuser: User = (**u, name='x', age=2, |)\n")
    assert {i["label"] for i in items} == {"sex="}
    # an open record type too: it has no `_replace`, but its fields are read-only members like a record's
    pre = MAIN + "type User = (..., name: str, age: int, sex: str)\n"
    _, items = complete(c, uri, pre + "user: User = (|)\n")
    assert {i["label"] for i in items} == {"name=", "age=", "sex="}
    _, items = complete(c, uri, pre + "user: User = (name='x', |)\n")
    assert {i["label"] for i in items} == {"age=", "sex="}
    # an argument: the fields of the parameter's record type, closed or open, named or written out
    pre = MAIN + "type User = (name: str, age: int)\ndef f(n: int, *, u: User, o: (..., x: int, y: str)): ...\n"
    _, items = complete(c, uri, pre + "f(1, u=(|))\n")
    assert {i["label"] for i in items} == {"name=", "age="}
    _, items = complete(c, uri, pre + "f(1, u=(name='x', |))\n")
    assert {i["label"] for i in items} == {"age="}
    _, items = complete(c, uri, pre + "f(1, u=(name='x'), o=(|))\n")
    assert {i["label"] for i in items} == {"x=", "y="}
    _, items = complete(c, uri, pre + "f((|), u=(name='x', age=1), o=(x=1, y=''))\n")
    assert "print" in {i["label"] for i in items}  # an int parameter: not a record
    # a returned literal: the fields of the innermost def's return type, closed or open, named or written out
    pre = MAIN + "type User = (name: str, age: int)\n"
    _, items = complete(c, uri, pre + "def g(u: User) -> User:\n    return (**u, name='x', |)\n")
    assert {i["label"] for i in items} == {"age="}
    text = "def g() -> User:\n    def h() -> (..., x: int, y: str):\n        return (|)\n    return (name='', age=1)\n"
    _, items = complete(c, uri, pre + text)
    assert {i["label"] for i in items} == {"x=", "y="}
    text = "def g() -> User:\n    def h() -> int:\n        return 1\n    return (|)\n"
    _, items = complete(c, uri, pre + text)
    assert {i["label"] for i in items} == {"name=", "age="}
    _, items = complete(c, uri, pre + "def g() -> int:\n    return (|)\n")
    assert "print" in {i["label"] for i in items}  # not a record
    _, items = complete(c, uri, pre + "def g():\n    return (|)\n")
    assert "print" in {i["label"] for i in items}  # no annotation
    # an item of a list, set, dict or tuple: the element type read off the annotation, in all three places
    _, items = complete(c, uri, pre + "users: list[User] = [(name='a', age=1), (name='b', |)]\n")
    assert {i["label"] for i in items} == {"age="}
    _, items = complete(c, uri, pre + "by: dict[str, list[User] | None] = {'a': [(^)]}\n", "^")
    assert {i["label"] for i in items} == {"name=", "age="}
    _, items = complete(c, uri, pre + "pair: tuple[int, (x: int, y: str)] = (1, (|))\n")
    assert {i["label"] for i in items} == {"x=", "y="}
    _, items = complete(c, uri, pre + "def g() -> list[User]:\n    return [(|)]\n")
    assert {i["label"] for i in items} == {"name=", "age="}
    _, items = complete(
        c, uri, pre + "from collections.abc import Sequence\ndef h(*, us: Sequence[User]): ...\nh(us=[(|)])\n"
    )
    assert {i["label"] for i in items} == {"name=", "age="}
    # a dict key, a comprehension's element, a tuple under Sequence[T]
    for text, want in [
        ("d: dict[User, int] = {(name='a', |): 1}\n", {"age="}),
        ("us: list[User] = [(name=n, |) for n in 'ab']\n", {"age="}),
        ("by: dict[str, User] = {n: (|) for n in 'ab'}\n", {"name=", "age="}),
        ("us: Sequence[User] = ((name='a', age=1), (|))\n", {"name=", "age="}),
    ]:
        _, items = complete(c, uri, pre + "from collections.abc import Sequence\n" + text)
        assert {i["label"] for i in items} == want
    # a literal in another's field: the field's type is the checker's to know, then items peeled as expressions
    team = "type Team = (lead: User, size: int, members: list[User])\n"
    for text, want in [
        ("t: Team = (lead=(name='a', |), size=1, members=[])\n", {"age="}),
        ("ts: list[Team] = [(lead=(|), size=1, members=[])]\n", {"name=", "age="}),
        ("t: Team = (size=1, members=[(name='a', age=1), (age=2, |)], lead=(name='b', age=3))\n", {"name="}),
        ("def g() -> Team:\n    return (lead=(name='a', |), size=1, members=[])\n", {"age="}),
        ("def h(*, t: Team): ...\nh(t=(lead=(name='a', |), size=1, members=[]))\n", {"age="}),
        ("p: (inner: (x: int, y: str)) = (inner=(x=1, |))\n", {"y="}),
    ]:
        _, items = complete(c, uri, pre + team + text)
        assert {i["label"] for i in items} == want
    # an alias of a container: its element type is the checker's to know, as a field's is
    alias = "type Users = list[User]\ntype Index = dict[str, Users]\ntype Pair = tuple[int, User]\n"
    alias += "from collections.abc import Iterator\ntype Stream = Iterator[User]\n"
    for text, want in [
        ("us: Users = [(name='a', |)]\n", {"age="}),
        ("ix: Index = {'a': [(|)]}\n", {"name=", "age="}),
        ("p: Pair = (1, (age=1, |))\n", {"name="}),
        ("def g() -> Stream:\n    yield (name='a', |)\n", {"age="}),
        ("def h(*, us: Users): ...\nh(us=[(name='a', |)])\n", {"age="}),
        ("def g() -> Stream:\n    yield from [(name='a', |)]\n", {"age="}),
        ("def g() -> Stream:\n    yield from ((name='a', age=1), (age=2, |))\n", {"name="}),
    ]:
        _, items = complete(c, uri, pre + alias + text)
        assert {i["label"] for i in items} == want
    # an alias in a union: None left out of the cast, which iter() of an Optional has no element type for
    _, items = complete(c, uri, pre + alias + "us: Users | None = [(name='a', ^)]\n", "^")
    assert {i["label"] for i in items} == {"age="}
    # yielded: Y in Iterator[Y] or Generator[Y, S, R], an iterable of Y after `yield from`; a generator returns R
    gen = "from collections.abc import AsyncIterator, Generator, Iterator\n"
    for text, want in [
        ("def g() -> Iterator[User]:\n    yield (name='a', |)\n", {"age="}),
        ("def g() -> Iterator[User]:\n    yield from [(|)]\n", {"name=", "age="}),
        ("async def g() -> AsyncIterator[User]:\n    yield (age=1, |)\n", {"name="}),
        (
            "def g() -> Generator[User, None, (n: int, ok: bool)]:\n    yield (name='a', age=1)\n    return (n=1, |)\n",
            {"ok="},
        ),
    ]:
        _, items = complete(c, uri, pre + gen + text)
        assert {i["label"] for i in items} == want
    # a generator's return under Iterator[Y] has no type to complete; a yield in a nested def isn't the outer one's
    for text in [
        "def g() -> Iterator[User]:\n    yield (name='a', age=1)\n    return (|)\n",
    ]:
        _, items = complete(c, uri, pre + gen + text)
        assert "print" in {i["label"] for i in items}
    text = "def g() -> User:\n    def h() -> Iterator[int]:\n        yield 1\n    return (|)\n"
    _, items = complete(c, uri, pre + gen + text)
    assert {i["label"] for i in items} == {"name=", "age="}
    # an annotation that isn't a record type: names in scope
    _, items = complete(c, uri, pre + "n: int = (|)\n")
    assert "print" in {i["label"] for i in items}
    # an ordinary parenthesized value is left alone: names in scope, not fields
    _, items = complete(c, uri, pre + "n: int = (1 + |)\n")
    assert "print" in {i["label"] for i in items}


def test_temporary_completion_text_leaves_no_diagnostics(lsp):
    # the placeholder text is never shown to the user as errors
    c, root, uri = lsp
    complete(c, uri, MAIN + "(name=, |) = res\n")
    c.diags.pop(uri, None)
    time.sleep(1.5)
    while not c.q.empty():
        c.handle(c.q.get())
    for d in c.diags.get(uri, []):
        assert "__byname" not in d["message"]


def test_format_document(lsp):
    # Format Document on a .pyn returns one whole-file edit with ruff's layout
    c, root, uri = lsp
    assert c.init["capabilities"]["documentFormattingProvider"] is True
    text = MAIN.replace("res = make(name=, age=)", "res=make( name= ,age= )")
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 900}, "contentChanges": [{"text": text}]},
        }
    )
    edits = c.request(
        "textDocument/formatting", {"textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": True}}
    )
    assert len(edits) == 1
    assert "res = make(name=, age=)\n" in edits[0]["newText"]


def test_formatting_code_that_does_not_translate_or_parse_offers_nothing(lsp):
    # rather than failing the save, as the Ruff extension does: the file's diagnostic says what's wrong.
    # A byname rule broken, and a half-typed line that byname translates but Ruff can't parse
    c, root, uri = lsp
    got = []
    for v, text in enumerate(["name = 1\ndef f(g=lambda v: (v=, name=name)): return g\n", "r = (a=1)\nprint(r.)\n"]):
        c.send(
            {
                "method": "textDocument/didChange",
                "params": {"textDocument": {"uri": uri, "version": 902 + v}, "contentChanges": [{"text": text}]},
            }
        )
        got.append(
            c.request(
                "textDocument/formatting",
                {"textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": True}},
            )
        )
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 909}, "contentChanges": [{"text": MAIN}]},
        }
    )
    assert got == [[], []]


def test_a_broken_byname_rule_shows_only_its_own_error(lsp):
    # the checker gets the raw .pyn then, so its `"(" was not closed` is noise
    c, root, uri = lsp
    text = "name = 1\ndef f(g=lambda v: (v=, name=name)): return g\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 904}, "contentChanges": [{"text": text}]},
        }
    )
    want = [(1, "v= in a parameter's default would take the outer 'v': write v=v to mean that")]
    got = wait_errors(c, uri, want)
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 905}, "contentChanges": [{"text": MAIN}]},
        }
    )
    assert got == want


def test_a_pattern_in_a_signature_is_an_error(lsp):
    c, root, uri = lsp
    text = "type U = (name: str, age: int)\ndef f((name=, age=): U):\n    return name\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 906}, "contentChanges": [{"text": text}]},
        }
    )
    want = [(1, "can't destructure a parameter: give it a name and destructure it in the body")]
    got = wait_errors(c, uri, want)
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 907}, "contentChanges": [{"text": MAIN}]},
        }
    )
    assert got == want


def test_fix_all_on_save(lsp):
    # codeActionsOnSave: source.fixAll is answered by byname with ruff's safe fixes, as for a .py
    c, root, uri = lsp
    assert "source.fixAll" in c.init["capabilities"]["codeActionProvider"]["codeActionKinds"]
    text = "from typing import List\n\n\ndef f(xs: List[int]):\n    n = len(xs)\n    return (n=, xs=)\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 901}, "contentChanges": [{"text": text}]},
        }
    )
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    actions = c.request(
        "textDocument/codeAction",
        {"textDocument": {"uri": uri}, "range": rng, "context": {"diagnostics": [], "only": ["source.fixAll"]}},
    )
    assert [a["kind"] for a in actions] == ["source.fixAll.byname"]
    new = actions[0]["edit"]["changes"][uri][0]["newText"]
    assert new == "\n\ndef f(xs: list[int]):\n    n = len(xs)\n    return (n=, xs=)\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 902}, "contentChanges": [{"text": MAIN}]},
        }
    )


def test_reading_a_record_by_position_offers_reading_it_by_name(lsp):
    # the error says how, and a quick fix rewrites the targets by field, matched in the order written
    c, root, uri = lsp
    text = MAIN + "def batch():\n    return (x=1, y=2)\nx, y = batch()\nfor a, _ in [batch()]: pass\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 990}, "contentChanges": [{"text": text}]},
        }
    )
    last = text.count("\n") - 1
    end, mine = time.time() + 60, []
    while time.time() < end and len(mine) < 2:  # skip diagnostics for the previous text
        c.diags.pop(uri, None)
        mine = [d for d in c.wait_diags(uri) if d["range"]["start"]["line"] in (last - 1, last)]
    assert all("records are read by name: (x=, y=)" in d["message"] for d in mine)
    fixes = []
    for d in sorted(mine, key=lambda d: d["range"]["start"]["line"]):
        actions = c.request(
            "textDocument/codeAction",
            {"textDocument": {"uri": uri}, "range": d["range"], "context": {"diagnostics": [d]}},
        )
        fixes += [
            (a["title"], a["edit"]["changes"][uri][0])
            for a in actions
            if a.get("kind") == "quickfix" and a["title"].startswith("Read by name")
        ]
    assert [t for t, _ in fixes] == ["Read by name: (x=, y=)", "Read by name: (x=a)"]
    assert [snippet(text, e["range"]) for _, e in fixes] == ["x, y", "a, _"]
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 991}, "contentChanges": [{"text": MAIN}]},
        }
    )


SOLUTION = """\
def solve(*, x: int):
    return (x=, double=x * 2)


if __name__ == "__main__":
    print(solve(x=2))
"""


def start(tmp_path, caps=None, **opts):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "sol.pyn").write_text(SOLUTION)
    c = Client(root, tmp_path / "cache")
    c.initialize(root, caps or {"workspace": {"configuration": True, "workspaceFolders": True}}, **opts)
    uri = (root / "sol.pyn").as_uri()
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": uri, "languageId": "pyn", "version": 1, "text": SOLUTION}},
        }
    )
    return c, root, uri


def save(c, uri, text, version):
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]},
        }
    )
    c.send({"method": "textDocument/didSave", "params": {"textDocument": {"uri": uri}}})
    # round trip: save handled
    c.request("textDocument/hover", {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 0}})


def test_output_on_save_off_by_default(tmp_path):
    c, root, uri = start(tmp_path)
    save(c, uri, SOLUTION, 2)
    assert not (root / "sol.pyn.py").exists()
    c.close()


def test_output_on_save_writes_next_to_file_keeping_main(tmp_path):
    # outputOnSave alone: the full translation, local runner included
    c, root, uri = start(tmp_path, outputOnSave=True)
    save(c, uri, SOLUTION, 2)
    out = (root / "sol.pyn.py").read_text()
    assert out.startswith("# pyn output of sol.pyn")
    assert "# ---- sol.pyn ----" in out and '__name__ == "__main__"' in out
    c.close()


def test_output_strip_main_and_broken_save_keeps_old_output(tmp_path):
    c, root, uri = start(tmp_path, outputOnSave=True, outputStripMain=True)
    save(c, uri, SOLUTION, 2)
    good = (root / "sol.pyn.py").read_text()
    assert "__main__" not in good and "_rec_x__double(x=x, double=x * 2)" in good
    save(c, uri, SOLUTION + "(oops=, nope) = solve(x=1)\n", 3)  # half-written pattern: an error
    assert (root / "sol.pyn.py").read_text() == good
    c.close()


def test_write_output_command_works_with_on_save_off(tmp_path):
    # Alt+C: writes now, whatever outputOnSave says; reports where
    c, root, uri = start(tmp_path)
    res = c.request("workspace/executeCommand", {"command": "byname.server.writeOutput", "arguments": [uri]})
    assert res == str(root / "sol.pyn.py")
    assert (root / "sol.pyn.py").exists()
    c.close()


def drain(c, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            c.handle(c.q.get(timeout=0.2))
        except queue.Empty:
            pass


def test_diagnostics_on_save_holds_them_while_typing(tmp_path):
    # diagnosticsOnSave: an error typed in shows up only once the file is saved
    # VS Code offers pull diagnostics, which the checker would use instead of pushing
    caps = {
        "workspace": {"configuration": True, "diagnostics": {"refreshSupport": True}},
        "textDocument": {"diagnostic": {"dynamicRegistration": True}},
    }
    c, root, uri = start(tmp_path, caps, diagnosticsOnSave=True)
    assert c.wait_diags(uri) == []  # the opened text counts as saved
    broken = SOLUTION + "print(nope)\n"
    c.diags.pop(uri)
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": broken}]},
        }
    )
    drain(c, 3)
    assert c.diags.pop(uri, []) == []
    c.send({"method": "textDocument/didSave", "params": {"textDocument": {"uri": uri}}})
    assert any("nope" in d["message"] for d in c.wait_diags(uri))
    c.close()


def test_diagnostics_while_typing_by_default(tmp_path):
    c, root, uri = start(tmp_path)
    c.wait_diags(uri)
    c.diags.pop(uri)
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": SOLUTION + "print(nope)\n"}],
            },
        }
    )
    assert any("nope" in d["message"] for d in c.wait_diags(uri))
    c.close()


def test_diagnostics_on_save_drops_fixed_errors_at_once(tmp_path):
    # fixing an error clears it while typing; a new error still waits for the save
    c, root, uri = start(tmp_path, diagnosticsOnSave=True)
    c.wait_diags(uri)
    c.diags.pop(uri)
    save(c, uri, SOLUTION + "print(nope)\n", 2)
    assert any("nope" in d["message"] for d in c.wait_diags(uri, timeout=10))
    c.diags.pop(uri)
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": uri, "version": 3},
                "contentChanges": [{"text": SOLUTION + "print(other)\n"}],
            },
        }
    )
    assert c.wait_diags(uri, timeout=10) == []  # nope is gone, other isn't shown yet
    c.diags.pop(uri)
    c.send({"method": "textDocument/didSave", "params": {"textDocument": {"uri": uri}}})
    assert any("other" in d["message"] for d in c.wait_diags(uri, timeout=10))
    c.close()


def test_pretty_record_type_protocols():
    assert pretty("p: _typ_name__age[str, int]") == "p: (name: str, age: int)"
    assert pretty("p: _opn_age[int]") == "p: (..., age: int)"
    assert pretty("p: _opn_") == "p: (...)"


def test_field_set_errors_say_which_fields():
    from byname.lsp import explain_fields

    raw = (
        'Type "(age: int, name: str, po: str)" is not assignable to declared type "User"\n'
        '  "(age: int, name: str, po: str)" is incompatible with protocol "(name: str, age: int)"\n'
        '    "_byname_fieldset" is an incompatible type\n'
        """      Type "() -> Literal['age,name,po']" is not assignable to type "() -> Literal['age,name']"\n"""
        "        ... (reportAssignmentType)"
    )
    assert explain_fields(raw) == (
        'Type "(age: int, name: str, po: str)" is not assignable to declared type "User"\n'
        "  extra field: po (reportAssignmentType)"
    )
    assert explain_fields("unrelated\n  message") == "unrelated\n  message"


def test_ordering_a_record_says_to_use_a_field():
    from byname.lsp import explain_fields

    raw = (
        'Argument of type "list[_typ_name__age[str, int]]" cannot be assigned to parameter "iterable" of type '
        '"Iterable[SupportsRichComparisonT@sorted]" in function "sorted"\n'
        '  "list[_typ_name__age[str, int]]" is not assignable to "Iterable[SupportsRichComparisonT@sorted]"\n'
        '    "_typ_name__age[str, int]" is incompatible with protocol "SupportsDunderLT[Any]" (reportArgumentType)'
    )
    hint = "  records have no order: compare or sort by a field (key=lambda r: r.name)"
    assert explain_fields(raw) == raw.split("\n")[0] + "\n" + hint + " (reportArgumentType)"
    raw = 'Operator "<" not supported for types "_typ_age[int]" and "_typ_age[int]" (reportOperatorIssue)'
    assert (
        explain_fields(raw)
        == raw.removesuffix(" (reportOperatorIssue)")
        + "\n"
        + hint.replace("r.name", "r.age")
        + " (reportOperatorIssue)"
    )


# --- .py files and notebooks ---------------------------------------------------


def test_py_files_go_to_the_checker_as_they_are(lsp):
    # byname syntax can't run in a .py file, so it isn't translated: hover is basedpyright's own
    c, root, uri = lsp
    text = "from helpers import shout\n\nn: int = shout('a')\n"
    py = (root / "script.py").as_uri()
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": py, "languageId": "python", "version": 1, "text": text}},
        }
    )
    assert "(s: str) -> str" in hover_text(c, py, pos(text, "shout", 1))
    assert errors(c.wait_diags(py)) == [(2, 'Type "str" is not assignable to declared type "int"')]


CELLS = [
    "%load_ext byname\n",
    "from people import make\n\nname, age = 'Rahul', 26\nres = make(name=, age=)\n",
    "(greeting=, age=years) = res\nres.nme\n",
]


def open_notebook(c, root, name, cells):
    nb = (root / name).as_uri()
    uris = [f"vscode-notebook-cell:{nb[len('file:') :]}#C{i}" for i in range(len(cells))]
    c.send(
        {
            "method": "notebookDocument/didOpen",
            "params": {
                "notebookDocument": {
                    "uri": nb,
                    "notebookType": "jupyter-notebook",
                    "version": 1,
                    "cells": [{"kind": 2, "document": u} for u in uris],
                },
                "cellTextDocuments": [
                    {"uri": u, "languageId": "python", "version": 1, "text": t} for u, t in zip(uris, cells)
                ],
            },
        }
    )
    return nb, uris


def edit_cell(c, nb, uri, version, start, end, text):
    rng = {"start": {"line": start[0], "character": start[1]}, "end": {"line": end[0], "character": end[1]}}
    c.diags.pop(uri, None)
    c.send(
        {
            "method": "notebookDocument/didChange",
            "params": {
                "notebookDocument": {"uri": nb, "version": version},
                "change": {
                    "cells": {
                        "textContent": [
                            {"document": {"uri": uri, "version": version}, "changes": [{"range": rng, "text": text}]}
                        ]
                    }
                },
            },
        }
    )


def wait_errors(c, uri, want, timeout=30, key=None) -> list[tuple[int, str]]:
    """The cell's errors once they match `want`: after an edit the checker may republish the old ones first.
    key: what of each message to compare."""
    end = time.time() + timeout
    while True:
        got = errors(c.diags.get(uri, []))
        if ([(n, key(m)) for n, m in got] if key else got) == want or time.time() > end:
            return got
        try:
            c.handle(c.q.get(timeout=0.5))
        except queue.Empty:
            pass


def errors(diags) -> list[tuple[int, str]]:
    return [(d["range"]["start"]["line"], d["message"].split("\n")[0]) for d in diags if d.get("severity", 1) == 1]


def test_byname_notebook_cells_are_translated_and_chained(lsp):
    # `%load_ext byname` in a cell: every cell is .pyn, each translated alone as the kernel runs it.
    # Names flow from cell to cell, and positions are the cell's own
    c, root, uri = lsp
    nb, cells = open_notebook(c, root, "byname.ipynb", CELLS)
    want = [(1, 'Cannot access attribute "nme" for class "(name: str, age: int, greeting: str)"')]
    assert wait_errors(c, cells[2], want) == want
    assert errors(c.wait_diags(cells[1])) == []
    assert "years: int" in hover_text(c, cells[2], pos(CELLS[2], "years"))
    assert "-> (name: str, age: int, greeting: str)" in hover_text(c, cells[1], pos(CELLS[1], "make", 1))


def test_plain_notebook_cells_are_not_translated(lsp):
    # without `%load_ext byname` the kernel runs cells as Python, so the checker sees them as typed
    c, root, uri = lsp
    nb, cells = open_notebook(c, root, "plain.ipynb", ["x = 1\n", "(a=x)\n"])
    assert errors(c.wait_diags(cells[1])) == [(0, '"(" was not closed')]
    assert errors(c.wait_diags(cells[0])) == []


def test_loading_byname_in_a_cell_translates_the_notebook(lsp):
    # typing `%load_ext byname` into a cell (an incremental edit) resends every cell, translated
    c, root, uri = lsp
    nb, cells = open_notebook(c, root, "later.ipynb", ["\n", "r = (a=1)\nr.b\n"])
    assert errors(c.wait_diags(cells[1])) == [(0, '"(" was not closed')]
    edit_cell(c, nb, cells[0], 2, (0, 0), (0, 0), "%load_ext byname")
    want = [(1, 'Cannot access attribute "b" for class "(a: int)"')]
    assert wait_errors(c, cells[1], want) == want
    # and an edit inside a byname cell is applied where it was typed
    edit_cell(c, nb, cells[1], 3, (1, 2), (1, 3), "a")
    assert wait_errors(c, cells[1], []) == []


def format_cell(c, uri) -> list:
    return c.request(
        "textDocument/formatting", {"textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": True}}
    )


def organize_cell(c, uri, text) -> list:
    rng = {"start": {"line": 0, "character": 0}, "end": pos(text, text[-1])}
    ctx = {"diagnostics": [], "only": ["source.organizeImports"], "triggerKind": 2}
    actions = c.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": ctx})
    return [e for a in actions for es in a["edit"]["changes"].values() for e in es]


def test_notebook_cells_are_formatted(lsp):
    # byname formats notebooks (notebook.defaultFormatter): Ruff on each cell, through the translation in a
    # byname notebook, as it is in a plain one. A cell's last line keeps having no newline
    c, root, uri = lsp
    nb, cells = open_notebook(
        c, root, "fmt.ipynb", ["%load_ext byname", "import sys\nimport os\nr=(a=os.sep,b= sys.argv)"]
    )
    assert [e["newText"] for e in format_cell(c, cells[1])] == ["import sys\nimport os\n\nr = (a=os.sep, b=sys.argv)"]
    # the notebook-wide action does it
    assert organize_cell(c, cells[1], "import sys\nimport os\nr=(a=os.sep,b= sys.argv)") == []
    assert format_cell(c, cells[0]) == []  # a magic: Ruff can't parse the cell alone, so it's left as it is
    nb, cells = open_notebook(c, root, "fmt_plain.ipynb", ["x=1"])
    assert [e["newText"] for e in format_cell(c, cells[0])] == ["x = 1"]


def test_format_notebook_on_save_is_one_code_action(lsp):
    # VS Code formats a notebook on save through a `notebook.format` code action, asked on its first cell
    # (notebook.defaultFormatter picks among those); the edit covers every cell
    c, root, uri = lsp
    nb, cells = open_notebook(c, root, "save.ipynb", ["%load_ext byname", "r=(a=1)", "y = 2"])
    ctx = {"diagnostics": [], "only": ["notebook.format"], "triggerKind": 2}
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    actions = c.request("textDocument/codeAction", {"textDocument": {"uri": cells[0]}, "range": rng, "context": ctx})
    assert [a["kind"] for a in actions] == ["notebook.format.byname"]
    changes = actions[0]["edit"]["changes"]
    assert {u: [e["newText"] for e in es] for u, es in changes.items()} == {cells[1]: ["r = (a=1)"]}


def notebook_action(c, uri, kind) -> dict:
    ctx = {"diagnostics": [], "only": [kind], "triggerKind": 2}
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    actions = c.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": ctx})
    assert [a["kind"] for a in actions] == [kind + ".byname"]
    return {u: [e["newText"] for e in es] for u, es in actions[0]["edit"]["changes"].items()}


def test_notebook_fix_all_and_organize_imports_see_the_whole_notebook(lsp):
    # notebook.source.fixAll / organizeImports: Ruff on the whole notebook, so `os` (used in the last cell)
    # stays and `sys` goes; in a byname notebook, through each cell's translation
    c, root, uri = lsp
    nb, cells = open_notebook(c, root, "fix.ipynb", ["%load_ext byname", "import sys\nimport os", "r = (a=os.sep)"])
    assert notebook_action(c, cells[0], "notebook.source.fixAll") == {cells[1]: ["import os"]}
    nb, cells = open_notebook(c, root, "sort.ipynb", ["import sys\nimport os", "print(os.sep, sys.argv)"])
    assert notebook_action(c, cells[0], "notebook.source.organizeImports") == {cells[0]: ["import os\nimport sys"]}


def test_py_files_are_formatted_and_fixed_by_byname(lsp):
    # byname is the workspace's Python server: Ruff's formatting and fixes on .py too (the file is its own
    # translation), so the Ruff extension isn't needed
    c, root, uri = lsp
    text = "import sys\nimport os\nfrom typing import List\nx: List[int]=[1]\nprint(os.sep, x)\n"
    py = (root / "fixme.py").as_uri()
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": py, "languageId": "python", "version": 1, "text": text}},
        }
    )
    assert [e["newText"] for e in format_cell(c, py)] == [
        text.replace("List\nx: List[int]=[1]", "List\n\nx: List[int] = [1]")
    ]
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    for kind, want in (
        ("source.fixAll", "import os\n\nx: list[int]=[1]\nprint(os.sep, x)\n"),
        (
            "source.organizeImports",
            "import os\nimport sys\nfrom typing import List\n\nx: List[int]=[1]\nprint(os.sep, x)\n",
        ),
    ):
        ctx = {"diagnostics": [], "only": [kind], "triggerKind": 2}
        actions = c.request("textDocument/codeAction", {"textDocument": {"uri": py}, "range": rng, "context": ctx})
        assert [e["newText"] for a in actions for es in a["edit"]["changes"].values() for e in es] == [want], kind


def test_python_diagnostics_off_keeps_only_syntax_errors_and_hints(tmp_path):
    # byname.pythonDiagnostics false: a .py file shows no type errors in the editor (Alt+L still checks it),
    # but a syntax error and faded unused code still show; .pyn always shows everything
    c, root, uri = start(tmp_path, pythonDiagnostics=False)
    py = (root / "x.py").as_uri()
    text = "import os\nx: int = 'a'\ndef f(:\n    pass\n"
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": py, "languageId": "python", "version": 1, "text": text}},
        }
    )
    end = time.time() + 30
    while not c.diags.get(py) and time.time() < end:  # the first publish can be the empty one before analysis
        c.diags.pop(py, None)
        c.wait_diags(py, timeout=5)
    diags = c.diags[py]
    assert {d.get("code") for d in diags} == {None, "reportUnusedImport"}
    assert not any(d.get("code") == "reportAssignmentType" for d in diags)
    bad = (root / "bad.pyn").as_uri()  # the same error in a .pyn is shown
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": bad, "languageId": "pyn", "version": 1, "text": "x: int = 'a'\n"}},
        }
    )
    assert [d.get("code") for d in c.wait_diags(bad)] == ["reportAssignmentType"]
    c.close()


def test_a_pyn_in_a_folder_made_after_startup_imports_its_neighbours(tmp_path):
    # file events sync the mirror: a .pyn in a folder made later (files moved, a new package) imports the .py and
    # .pyn next to it
    c, root, uri = start(tmp_path)
    c.wait_diags(uri)  # the checker is up, as when VS Code opens the first file
    (root / "pkg").mkdir()
    (root / "pkg" / "helper_py.py").write_text("def a() -> int:\n    return 1\n")
    (root / "pkg" / "helper_pyn.pyn").write_text("def b():\n    return (x=1)\n")
    changes = [{"uri": (root / "pkg" / f).as_uri(), "type": 1} for f in ("helper_py.py", "helper_pyn.pyn")]
    c.send({"method": "workspace/didChangeWatchedFiles", "params": {"changes": changes}})
    text = "from helper_py import a\nfrom helper_pyn import b\n\nprint(a(), b().x)\n"
    late = (root / "pkg" / "late.pyn").as_uri()
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": late, "languageId": "pyn", "version": 1, "text": text}},
        }
    )
    c.wait_diags(late)
    assert wait_errors(c, late, []) == []
    c.close()


def test_pyn_imports_pyn_with_a_project_config_as_vs_code_starts_it(tmp_path):
    # VS Code sends workspace folders and rootPath; with a project config setting extraPaths, a .pyn imports its
    # .pyn neighbour (the checker ignores editor extraPaths then, so they can't be how it finds it)
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[tool.basedpyright]\ntypeCheckingMode = "standard"\nextraPaths = ["src"]\n')
    (root / "src" / "people.pyn").write_text(PEOPLE)
    c = Client(root, tmp_path / "cache")
    c.initialize(root, {"workspace": {"configuration": True, "workspaceFolders": True}})
    text = "from people import make\n\nprint(make(name='a', age=1).nme)\n"
    uri = (root / "src" / "main.pyn").as_uri()
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {"textDocument": {"uri": uri, "languageId": "pyn", "version": 1, "text": text}},
        }
    )
    want = [(2, 'Cannot access attribute "nme" for class "(name: str, age: int, greeting: str)"')]
    c.wait_diags(uri)
    assert wait_errors(c, uri, want) == want
    c.close()


# --- parity: a .pyn is checked as a .py in its place --------------------------

PARITY_CONFIG = """\
[tool.basedpyright]
typeCheckingMode = "standard"
extraPaths = ["src"]
executionEnvironments = [{ root = "src/records" }]
"""
PARITY = """\
from fn_import_test import to_be_imported
from helper import h

a_to_be = []
print(to_be_imported(), h(), a_to_be)
h("wrong")
"""


def parity_project(tmp_path) -> Path:
    # llm's layout: a module on the extraPaths and a same-named one beside the file, in an execution
    # environment of its own, where it's the one imported (only it has to_be_imported)
    root = tmp_path / "ws"
    (root / "src" / "records").mkdir(parents=True)
    (root / "pyproject.toml").write_text(PARITY_CONFIG)
    (root / "src" / "helper.py").write_text("def h() -> int:\n    return 1\n")
    (root / "src" / "fn_import_test.py").write_text("x = 1\n")
    (root / "src" / "records" / "fn_import_test.py").write_text("def to_be_imported() -> int:\n    return 1\n")
    (root / "src" / "records" / "tc.pyn").write_text(PARITY)
    (root / "src" / "records" / "tc_py.py").write_text(PARITY)
    return root


def test_a_pyn_is_checked_like_a_py_in_its_place(tmp_path):
    # the project's mode (no "recommended" reportUnknown*), relative extraPaths and executionEnvironments apply
    # to the .pyn as to the .py beside it; definitions land in the project, not the mirror
    root = parity_project(tmp_path)
    c = Client(root, tmp_path / "cache")
    c.initialize(root, {"workspace": {"configuration": True, "workspaceFolders": True}})
    want = [(5, "Expected 0 positional arguments")]
    got = {}
    for name in ("tc.pyn", "tc_py.py"):
        uri = c.open(root / "src" / "records" / name)
        c.wait_diags(uri)
        assert wait_errors(c, uri, want) == want, name
        got[name] = sorted((d["range"]["start"]["line"], d.get("code"), d["message"]) for d in c.diags[uri])
        res = c.request(
            "textDocument/definition", {"textDocument": {"uri": uri}, "position": pos(PARITY, "to_be_imported", 1)}
        )
        assert [r.get("targetUri") or r["uri"] for r in res] == [
            (root / "src" / "records" / "fn_import_test.py").as_uri()
        ], name
    assert got["tc.pyn"] == got["tc_py.py"]
    c.close()


def test_the_editor_and_byname_tool_agree(tmp_path):
    # Alt+L (`byname tool basedpyright`) and the editor check the same mirror: the same findings
    root = parity_project(tmp_path)
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache"), "PATH": str(Path(sys.executable).parent)}
    out = subprocess.run(
        [sys.executable, "-m", "byname", "tool", "basedpyright", "src/records/tc.pyn"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    found = [re.match(r"\s*src/records/tc\.pyn:(\d+):\d+ - error: (.*?)(?: \(\w+\))?$", ln) for ln in out.splitlines()]
    tool = [(int(m[1]) - 1, m[2]) for m in found if m]
    c = Client(root, tmp_path / "cache")
    c.initialize(root, {"workspace": {"configuration": True, "workspaceFolders": True}})
    uri = c.open(root / "src" / "records" / "tc.pyn")
    c.wait_diags(uri)
    want = [(5, "Expected 0 positional arguments")]
    assert wait_errors(c, uri, want) == want
    assert tool == want
    c.close()


def test_a_py_file_is_sent_under_its_mirror_uri_and_comes_back_as_itself(tmp_path):
    # the checker sees the project's .py files in the mirror; the editor only ever sees the project's uris
    root = parity_project(tmp_path)
    c = Client(root, tmp_path / "cache")
    c.initialize(root, {"workspace": {"configuration": True, "workspaceFolders": True}})
    uri = c.open(root / "src" / "records" / "tc_py.py")
    c.wait_diags(uri)
    res = c.request("textDocument/definition", {"textDocument": {"uri": uri}, "position": pos(PARITY, "h", 3)})
    assert [r.get("targetUri") or r["uri"] for r in res] == [(root / "src" / "helper.py").as_uri()]
    assert all("byname/" not in u for u in c.diags)  # no diagnostics published for mirror paths
    c.close()


def test_a_deleted_pyn_is_gone_for_its_importers(tmp_path):
    # file events sync the mirror: once a .pyn is deleted, importing it is an error, as for a .py
    c, root, uri = start(tmp_path)
    (root / "lib.pyn").write_text("def f():\n    return (x=1)\n")
    c.send(
        {
            "method": "workspace/didChangeWatchedFiles",
            "params": {"changes": [{"uri": (root / "lib.pyn").as_uri(), "type": 1}]},
        }
    )
    user = c.open(root / "user.pyn", "from lib import f\n\nprint(f().x)\n")
    c.wait_diags(user)
    assert wait_errors(c, user, []) == []
    (root / "lib.pyn").unlink()
    c.send(
        {
            "method": "workspace/didChangeWatchedFiles",
            "params": {"changes": [{"uri": (root / "lib.pyn").as_uri(), "type": 3}]},
        }
    )
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": user, "version": 2},
                "contentChanges": [{"text": "from lib import f\n\nprint(f().x)\n\n"}],
            },
        }
    )
    want = [(0, 'Import "lib" could not be resolved')]
    assert wait_errors(c, user, want) == want
    c.close()


def test_organize_imports_on_save_agrees_with_byname_tool_ruff(tmp_path):
    # Ruff tells first-party modules by the files it finds; a .pyn is a module only in the mirror. Run in the
    # project, the save's organize imports took `lib_pyn` for third-party and left two blocks that Alt+L
    # (Ruff in the mirror) calls unsorted (I001), however often it was saved
    c, root, uri = start(tmp_path)
    (root / "pyproject.toml").write_text("[tool.ruff]\n")
    (root / "lib_pyn.pyn").write_text("x = 1\n")
    (root / "lib.py").write_text("y = 2\n")
    for f in ("pyproject.toml", "lib_pyn.pyn", "lib.py"):
        c.send(
            {
                "method": "workspace/didChangeWatchedFiles",
                "params": {"changes": [{"uri": (root / f).as_uri(), "type": 1}]},
            }
        )
    text = "from lib_pyn import x\n\nfrom lib import y\n\nprint(x, y)\n"
    (root / "user.pyn").write_text(text)
    user = c.open(root / "user.pyn")
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    ctx = {"diagnostics": [], "only": ["source.organizeImports"], "triggerKind": 2}
    actions = c.request("textDocument/codeAction", {"textDocument": {"uri": user}, "range": rng, "context": ctx})
    fixed = [e["newText"] for a in actions for es in a["edit"]["changes"].values() for e in es]
    assert fixed == ["from lib import y\nfrom lib_pyn import x\n\nprint(x, y)\n"]
    (root / "user.pyn").write_text(fixed[0])
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache"), "PATH": str(Path(sys.executable).parent)}
    out = subprocess.run(
        [sys.executable, "-m", "byname", "tool", "ruff", "check", "--select", "I", "user.pyn"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "All checks passed" in out, out
    c.close()


def test_records_of_one_shape_from_two_literals_show_as_one_type(lsp):
    # each literal has its own class for the checker (transform.site_class), so a list of two is a union of
    # two classes of one shape: shown once, like the type it is
    c, root, _ = lsp
    text = "xs = [(a=1), (a=2)]\nx = (a=1) if xs else (a=3)\n"
    uri = c.open(root / "shapes.pyn", text)
    c.wait_diags(uri)
    assert "xs: list[(a: int)]" in hover_text(c, uri, pos(text, "xs"))
    assert "x: (a: int)" in hover_text(c, uri, pos(text, "x ="))


def test_an_open_spread_record_shows_its_fields_and_colours_them(lsp):
    c, root, _ = lsp
    text = "def greet(u: (..., name: str)):\n    (name=, **rest) = u\n    return (**rest, name=name.upper())\n\n\nout = greet((name='a'))\n"
    uri = c.open(root / "open_spread.pyn", text)
    c.wait_diags(uri)
    assert "out: (..., name: str)" in hover_text(c, uri, pos(text, "out"))
    assert "rest: (...)" in hover_text(c, uri, pos(text, "rest)"))


def test_a_dot_read_of_an_undeclared_field_on_an_open_type_says_how_to_declare_it(lsp):
    c, root, _ = lsp
    text = "def log(p: (...)):\n    print(p.msg)\n"
    uri = c.open(root / "undeclared.pyn", text)
    want = '"msg" isn\'t declared on (...): declare it, (..., msg: <type>), or read it unchecked with ["msg"]'
    assert wait_errors(c, uri, [(1, want)]) == [(1, want)]


def test_a_spread_of_what_only_the_checker_knows_is_exact_in_the_editor(lsp):
    # u's type isn't on u: the file goes to the checker first as a probe, whose answer types the spread
    # (Proxy.probed); the probe's own diagnostics, its reveal_type notes, are never shown
    c, root, _ = lsp
    text = (
        "type User = (name: str, age: int)\n"
        "def f(users: list[User]):\n"
        "    for u in users:\n"
        "        moved = (**u, zip=1)\n"
        "        print(moved.name, moved.nmae)\n"
    )
    uri = c.open(root / "probed.pyn", text)
    want = [(4, 'Cannot access attribute "nmae" for class "(name: str, age: int, zip: int)"')]
    assert [(n, m.split("\n")[0]) for n, m in wait_errors(c, uri, want, key=lambda m: m.split("\n")[0])] == want
    assert not [d for d in c.diags[uri] if "Type of" in d["message"]]
    assert "moved: (name: str, age: int, zip: int)" in hover_text(c, uri, pos(text, "moved ="))


def test_a_save_updates_its_own_copy_without_walking_the_project(tmp_path, monkeypatch):
    # a save is one file changed: placed alone in the editor's mirror and the probe's, whose next run asks the
    # .pyn files it already knows. A sync walks the project, seconds in a big one, on every save it was slow
    import byname.tools as tools

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.pyn").write_text("x = (a=1)\n")
    (root / "b.pyn").write_text("def f(r):\n    return (**r, b=2)\n")
    proxy = Proxy(["cat"])
    proxy.root, proxy.mirror, proxy.cli = root, tools.mirror_dir(root, "lsp"), "unused"
    proxy.sync()
    walks, asked = [], []
    real_mirror = tools.mirror
    monkeypatch.setattr(tools, "mirror", lambda *a, **k: walks.append(a[1]) or real_mirror(*a, **k))
    monkeypatch.setattr(tools, "probe", lambda root, out, cli, pyns: asked.append(sorted(p.name for p in pyns)))
    proxy.run_probe()  # the first run syncs whole
    assert walks == ["lsp-probe"] and asked == [["a.pyn", "b.pyn"]]

    (root / "a.pyn").write_text("x = (a=1, b=2)\n")
    saved = [{"uri": (root / "a.pyn").as_uri(), "type": 2}]
    assert proxy.watched(saved) == [{"uri": (proxy.mirror / "a.py").as_uri(), "type": 2}]
    assert "x = _rec_a__b(" in (proxy.mirror / "a.py").read_text()
    (root / "c.pyn").write_text("y = 1\n")
    proxy.probe_changed = {root / "a.pyn", root / "c.pyn"}
    proxy.run_probe()
    assert walks == ["lsp-probe"] and asked[-1] == ["a.pyn", "b.pyn", "c.pyn"]
    assert "x = _rec_a__b(" in (tools.mirror_dir(root, "lsp-probe") / "a.py").read_text()

    # made, removed, many at once (git, a script): synced whole, as anything may have changed
    (root / "b.pyn").unlink()
    proxy.watched([{"uri": (root / "b.pyn").as_uri(), "type": 3}])
    assert not (proxy.mirror / "b.py").exists() and walks == ["lsp-probe", "lsp"]
    proxy.probe_full = True
    proxy.run_probe()
    assert asked[-1] == ["a.pyn", "c.pyn"]
    proxy.proc.kill()


def test_a_probe_s_answer_is_a_publish_that_has_one(tmp_path):
    # Ctrl+hover opens the target for its preview and closes it, the click opens it again: the reopen's first
    # publish can be of the text before the probe (the mirror's). It isn't the answer; the probe's own publish
    # is, which answers its last line, reveal_type('_byname_probe'), even when the sources go unanswered (an
    # unanalysed function). Twice without (the module's end unreachable), the probe goes on
    path = tmp_path / "probe.pyn"
    text = "def f(users):\n    for u in users:\n        moved = (**u, zip=1)\n"
    proxy = Proxy(["cat"])  # stands in for the checker: what's sent to it goes nowhere
    doc = Doc(path.as_uri(), text, 1, probe=True)
    doc.sent = 1
    proxy.docs[path] = [doc]
    hidden = doc.tr.hidden.splitlines()
    line = next(k for k, h in enumerate(hidden) if "reveal_type(u)" in h)
    at = {"line": line, "character": hidden[line].index("reveal_type(u)") + len("reveal_type(")}
    stale = {"range": {"start": {"line": 0, "character": 0}}, "message": '"zip" is not accessed'}
    answer = {"range": {"start": at}, "message": 'Type of "u" is "_typ_name___0[str]"'}
    assert doc.tr.hidden.endswith("reveal_type('_byname_probe')\n")
    mark = {"range": {"start": {"line": len(hidden) - 1, "character": 12}}, "message": "Type of \"'_byname_probe'\" is"}
    proxy.probed(doc, path, [stale])
    assert proxy.docs[path] == [doc]  # still waiting
    proxy.probed(doc, path, [stale, answer, mark])
    final = proxy.docs[path][-1]
    assert final is not doc and not final.probing
    assert "moved = _rec_name__zip(" in final.tr.hidden and "reveal_type" not in final.tr.hidden
    unreachable = Doc(path.as_uri(), text, 1, probe=True)
    proxy.docs[path] = [unreachable]
    assert proxy.probed(unreachable, path, [stale]) and proxy.probed(unreachable, path, [stale])
    # the third publish without an answer: nothing changed from what the probe was built with (no answer),
    # so the probe is the file's text, and this publish its diagnostics
    assert not proxy.probed(unreachable, path, [stale]) and not unreachable.probing
    assert proxy.docs[path] == [unreachable]

    # the next edit's probe is built with the answers the file had: typed while it's in the checker
    # (completion lists name), and each asked again. Answered the same: no final, its diagnostics shown
    typing = Doc(path.as_uri(), text + "# typing\n", 2, probe=True, carry=final.tr.probed)
    assert typing.probing and "name=reveal_type(u).name" in typing.tr.hidden
    proxy.docs[path] = [typing]
    hidden = typing.tr.hidden.splitlines()
    line = next(k for k, h in enumerate(hidden) if "reveal_type(u)" in h)
    at = {"line": line, "character": hidden[line].index("reveal_type(u)") + len("reveal_type(")}
    same = {"range": {"start": at}, "message": 'Type of "u" is "_typ_name___0[str]"'}
    assert (at["line"], at["character"]) in typing.tr.reveals  # its note isn't shown
    assert not proxy.probed(typing, path, [same, mark]) and proxy.docs[path] == [typing]
    # answered otherwise (users' type changed): the final is built with the new answer
    retyped = Doc(path.as_uri(), text, 3, probe=True, carry=final.tr.probed)
    proxy.docs[path] = [retyped]
    other = {**same, "message": 'Type of "u" is "_typ_name__age___0[str, int]"'}
    assert proxy.probed(retyped, path, [other, mark])
    assert "moved = _rec_name__age__zip(" in proxy.docs[path][-1].tr.hidden
    proxy.proc.kill()


def test_a_spread_of_what_a_closed_file_s_spread_returns_is_exact(lsp):
    # first_moved() returns a spread the checker is asked about, in a file never opened: the editor's checker
    # reads its mirror copy, which a run in the background asks about (probe_project, on a save here), and
    # the open file that spreads what it returns is asked again
    c, root, _ = lsp
    (root / "closed_dep.pyn").write_text(
        "type User = (name: str, age: int)\n"
        "def all_users() -> list[User]: ...\n"
        "def first_moved():\n"
        "    for u in all_users():\n"
        "        return (**u, zip=1)\n"
        "    raise LookupError\n"
    )
    # rows has no type: never answered, so the file is asked again on top of the background run's answers,
    # which the final keeps
    text = "from closed_dep import first_moved\nx = (**first_moved(), k=1)\ndef g(rows):\n    y = (**rows, k=1)\n"
    (root / "uses_closed.pyn").write_text(text)
    made = [{"uri": (root / f).as_uri(), "type": 1} for f in ("closed_dep.pyn", "uses_closed.pyn")]
    c.send({"method": "workspace/didChangeWatchedFiles", "params": {"changes": made}})  # as the editor tells
    end = time.time() + 5  # the file events start a background run: both files answered while closed
    while time.time() < end:
        try:
            c.handle(c.q.get(timeout=0.1))
        except queue.Empty:
            pass
    uri = c.open(root / "uses_closed.pyn")
    want = "x: (name: str, age: int, zip: int, k: int)"
    for _ in range(100):
        if want in (got := hover_text(c, uri, pos(text, "x ="))):
            break
        time.sleep(0.1)
    assert want in got


def test_completion_on_a_probed_spread_lists_its_fields_while_typing(lsp):
    # each edit's probe is built with the answers the file had (Doc's carry), so `moved.` completes `name`
    # at once, not only after the final text is in
    c, root, _ = lsp
    head = "type User = (name: str, age: int)\ndef f(users: list[User]):\n    for u in users:\n        moved = (**u, zip=1)\n"
    uri = c.open(root / "completes.pyn", head)
    for _ in range(100):  # the final text is in: answered once
        if "moved: (name: str, age: int, zip: int)" in hover_text(c, uri, pos(head, "moved =")):
            break
        time.sleep(0.1)
    text = head + "        moved.\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": text}]},
        }
    )
    got = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": {"line": 4, "character": 14}})
    labels = [i["label"] for i in (got["items"] if isinstance(got, dict) else got)]
    assert {"name", "age", "zip"} <= set(labels), labels[:10]


def test_a_spread_of_this_file_s_asked_spread_is_answered_in_a_second_round(lsp):
    # first_moved() returns (**u, zip=1), itself asked: the first probe's answer for first_moved() is that
    # open spread (waiting), so the editor sends the next probe, built with u's answer, at once
    c, root, _ = lsp
    text = (
        "type User = (name: str, age: int)\n"
        "def all_users() -> list[User]: ...\n"
        "def first_moved():\n"
        "    for u in all_users():\n"
        "        return (**u, zip=1)\n"
        "    raise LookupError\n"
        "x = (**first_moved(), k=1)\n"
    )
    uri = c.open(root / "same_file_chain.pyn", text)
    want = "x: (name: str, age: int, zip: int, k: int)"
    for _ in range(50):
        if want in (got := hover_text(c, uri, pos(text, "x ="))):
            break
        time.sleep(0.1)
    assert want in got


def test_a_probed_spread_s_fields_are_coloured_once_its_final_text_is_in(lsp):
    # the editor asks for tokens right after an edit, and gets the probe translation's, where moved.name has
    # no colour: the final text's arrival tells it to ask again (refresh_tokens)
    c, root, _ = lsp
    text = (
        "type User = (name: str, age: int)\n"
        "def f(users: list[User]):\n"
        "    for u in users:\n"
        "        moved = (**u, zip=1)\n"
        "        print(moved.name)\n"
    )
    before = c.asked.count("workspace/semanticTokens/refresh")
    uri = c.open(root / "probed_tokens.pyn", text)
    end = time.time() + 30
    while c.asked.count("workspace/semanticTokens/refresh") == before and time.time() < end:
        try:
            c.handle(c.q.get(timeout=0.5))
        except queue.Empty:
            pass
    assert c.asked.count("workspace/semanticTokens/refresh") > before
    legend = c.init["capabilities"]["semanticTokensProvider"]["legend"]["tokenTypes"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    line = col = 0
    kinds = set()
    for i in range(0, len(data), 5):
        dl, dc, length, typ, _ = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        if line == 4 and text.split("\n")[4][col : col + length] == "name":
            kinds.add(legend[typ])
    assert kinds == {"property"}


def test_a_field_a_spread_brought_says_where_it_went_as_byname_tool_does(lsp):
    # the field came with `u`, whose fields byname can't see: the editor says so, word for word as the tool
    c, root, _ = lsp
    text = "def f(users):\n    for u in users:\n        moved = (**u, zip=1)\n        print(moved.name)\n"
    uri = c.open(root / "spread_unseen.pyn", text)
    want = (
        "\"name\" isn't on (..., zip: int): it's built from a spread of u, whose fields byname can't see. To check"
        ' it, give u a record type; or read it unchecked with ["name"]'
    )
    assert wait_errors(c, uri, [(3, want)]) == [(3, want)]
    d = next(d for d in c.diags[uri] if d.get("severity", 1) == 1)
    actions = c.request(
        "textDocument/codeAction", {"textDocument": {"uri": uri}, "range": d["range"], "context": {"diagnostics": [d]}}
    )
    assert actions[0]["title"] == 'Read it unchecked: moved["name"]'


def test_an_undeclared_field_on_an_open_type_offers_reading_it_unchecked(lsp):
    # brackets say "unchecked" in the text; pyright's own fix, an ignore comment, leaves `p.msg` looking checked
    c, root, _ = lsp
    text = "def log(p: (...)):\n    print(p.msg)\n"
    uri = c.open(root / "unchecked.pyn", text)
    want = '"msg" isn\'t declared on (...): declare it, (..., msg: <type>), or read it unchecked with ["msg"]'
    assert wait_errors(c, uri, [(1, want)]) == [(1, want)]
    d = next(d for d in c.diags[uri] if d.get("severity", 1) == 1)
    actions = c.request(
        "textDocument/codeAction", {"textDocument": {"uri": uri}, "range": d["range"], "context": {"diagnostics": [d]}}
    )
    assert actions[0]["title"] == 'Read it unchecked: p["msg"]'  # before the checker's ignore comment
    ours = [a for a in actions if a["title"].startswith("Read it unchecked")]
    assert len(ours) == 1
    edit = ours[0]["edit"]["changes"][uri][0]
    assert edit == {
        "range": {"start": {"line": 1, "character": 11}, "end": {"line": 1, "character": 15}},
        "newText": '["msg"]',
    }


def test_a_bracket_read_of_a_declared_field_offers_reading_it_checked(lsp):
    # brackets are the unchecked read: once the type declares the field, the dot read checks it
    c, root, _ = lsp
    text = "def log(p: (..., msg: str)):\n    print(p['msg'], p['other'])\n"
    uri = c.open(root / "declared.pyn", text)
    c.wait_diags(uri)
    infos = [d for d in c.diags[uri] if d.get("severity") == 3]
    assert [d["message"] for d in infos] == ['"msg" is declared on p\'s type: read it checked, p.msg']
    d = infos[0]
    actions = c.request(
        "textDocument/codeAction", {"textDocument": {"uri": uri}, "range": d["range"], "context": {"diagnostics": [d]}}
    )
    assert actions[0]["title"] == "Read it checked: p.msg"
    edit = actions[0]["edit"]["changes"][uri][0]
    assert edit == {
        "range": {"start": {"line": 1, "character": 11}, "end": {"line": 1, "character": 18}},
        "newText": ".msg",
    }


def test_fix_all_applies_byname_quick_fixes_with_one_right_answer(lsp):
    # read it checked goes in; read it unchecked doesn't (declaring the field is the other answer), nor read by
    # name (it pairs targets with fields in written order: `y, x = batch()` would bind them swapped)
    c, root, _ = lsp
    text = (
        "def batch():\n    return (x=1, y=2)\n\n\n"
        "def log(p: (..., msg: str), q: (...)):\n    print(p['msg'], q.msg)\n\n\n"
        "x, y = batch()\n"
    )
    uri = c.open(root / "fixall.pyn", text)
    end = time.time() + 60
    while time.time() < end and len([d for d in c.diags.get(uri, []) if d.get("data")]) < 3:
        c.handle(c.q.get(timeout=5))
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    actions = c.request(
        "textDocument/codeAction",
        {"textDocument": {"uri": uri}, "range": rng, "context": {"diagnostics": [], "only": ["source.fixAll"]}},
    )
    fixed = actions[0]["edit"]["changes"][uri][0]["newText"]
    assert "print(p.msg, q.msg)" in fixed
    assert "x, y = batch()" in fixed


def test_fix_all_right_after_an_edit_reads_declared_fields_checked(lsp):
    # a paste then a save: the checker hasn't answered for this text yet, and read it checked still applies
    c, root, _ = lsp
    uri = c.open(root / "quicksave.pyn", "def log(p: (..., msg: str)):\n    pass\n")
    c.wait_diags(uri)
    text = "def log(p: (..., msg: str)):\n    print(p['msg'])\n"
    c.send(
        {
            "method": "textDocument/didChange",
            "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": text}]},
        }
    )
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    actions = c.request(
        "textDocument/codeAction",
        {"textDocument": {"uri": uri}, "range": rng, "context": {"diagnostics": [], "only": ["source.fixAll"]}},
    )
    assert "print(p.msg)" in actions[0]["edit"]["changes"][uri][0]["newText"]


ONLY_ANY = """\
import ast


def only_any(*, path, source, tree, diagnostics):
    # reportAny's findings only, as warnings, naming the file; the AST has the file's own lines: `x` is
    # found by line in a .pyn as in a .py
    lines = {n.lineno for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "x"}
    return [
        {**d, "severity": 2, "message": f"{path.name}:{d['range']['start']['line'] + 1 in lines} {d['message']}"}
        for d in diagnostics
        if d.get("code") == "reportAny"
    ]
"""


def test_checker_rules_and_a_diagnostics_filter(tmp_path):
    # byname.checkerRules turns reportAny on in the editor's checker; byname.diagnosticsFilter, a function in
    # the project's environment, gets each document's diagnostics (its own positions) with its text and AST,
    # and returns what shows: here reportAny's alone, so the assignment error goes
    root = tmp_path / "ws"
    root.mkdir()
    (root / "flt.py").write_text(ONLY_ANY)  # importable: the server runs in the project (cwd on sys.path)
    text = 'import json\n\n(name=) = (name=1)\nx = json.loads("1")\ny: int = "a"\n'
    (root / "main.pyn").write_text(text)
    c = Client(root, tmp_path / "cache")
    caps = {"workspace": {"configuration": True, "workspaceFolders": True}}
    c.initialize(root, caps, checkerRules={"reportAny": "warning"}, diagnosticsFilter="flt:only_any")
    pyn = c.open(root / "main.pyn")
    got = c.wait_diags(pyn)
    assert [(d["message"], d["severity"]) for d in got] == [('main.pyn:True Type of "x" is Any', 2)]
    py = (root / "plain.py").as_uri()  # a .py the same way
    c.send(
        {
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": py,
                    "languageId": "python",
                    "version": 1,
                    "text": 'import json\n\nx = json.loads("1")\n',
                }
            },
        }
    )
    end = time.time() + 30
    while not c.diags.get(py) and time.time() < end:  # the first publish can be the empty one before analysis
        c.diags.pop(py, None)
        c.wait_diags(py, timeout=5)
    assert [d["message"] for d in c.diags[py]] == ['plain.py:True Type of "x" is Any']
    assert c.shown == []
    c.close()


def test_a_filter_that_cannot_load_is_said_and_lets_everything_through(tmp_path):
    c, root, uri = start(tmp_path, diagnosticsFilter="nowhere:fn")
    c.wait_diags(uri)
    assert len(c.shown) == 1 and "byname.diagnosticsFilter can't load nowhere:fn" in c.shown[0]
    c.close()


REVEALS = """\
import ast


def asks(*, path, source, tree):
    # for each `name: T = value`: the value's type, then T's, before the statement
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.AnnAssign) and n.value is not None:
            out += [(n.lineno, n.col_offset, ast.unparse(n.value), False), (n.lineno, n.col_offset, ast.unparse(n.annotation), True)]
    return out


def answers(*, path, source, tree, diagnostics, reveals):
    return [*diagnostics, {"range": diagnostics[0]["range"], "message": f"answers {reveals}"}]
"""


def test_reveals_are_asked_in_the_editors_own_check(tmp_path):
    # byname.diagnosticsReveals: a hook names expressions (and types) whose types the checker reveals, in the
    # same pass as everything else; the filter gets the answers. The probes' own notes and errors aren't shown,
    # and the user's code after a probe keeps its positions
    root = tmp_path / "ws"
    root.mkdir()
    (root / "rv.py").write_text(REVEALS)
    text = 'r = (name="a")\nx: float = len(r.name); y: int = "a"\n'
    (root / "main.pyn").write_text(text)
    c = Client(root, tmp_path / "cache")
    caps = {"workspace": {"configuration": True, "workspaceFolders": True}}
    c.initialize(root, caps, diagnosticsReveals="rv:asks", diagnosticsFilter="rv:answers")
    got = c.wait_diags(c.open(root / "main.pyn"))
    assert [d["message"] for d in got][-1] == "answers ['int', 'float', \"Literal['a']\", 'int']"
    (error,) = [d for d in got if d.get("code") == "reportAssignmentType"]
    assert snippet(text, error["range"]) == '"a"'  # after both probes on its line
    assert not any("reveal_type" in d["message"] or "Type of" in d["message"] for d in got)
    assert c.shown == []
    c.close()
