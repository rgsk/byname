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

from byname.lsp import pretty, read_message

pytestmark = pytest.mark.skipif(
    not (Path(sys.executable).parent / "basedpyright-langserver").exists() and not shutil.which("basedpyright-langserver"),
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
            [sys.executable, "-m", "byname", "lsp"], cwd=root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.q: queue.Queue = queue.Queue()
        self.next_id = 0
        self.diags: dict[str, list] = {}
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
            if msg["method"] == "workspace/configuration":
                self.send({"id": msg["id"], "result": [self.settings.get(i.get("section")) for i in msg["params"]["items"]]})
            else:
                self.send({"id": msg["id"], "result": None})
        elif msg.get("method") == "textDocument/publishDiagnostics":
            self.diags[msg["params"]["uri"]] = msg["params"]["diagnostics"]
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
        params = {"processId": None, "rootPath": str(root), "rootUri": root.as_uri(), "workspaceFolders": folders, "capabilities": caps}
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
        "workspace": {"configuration": True, "workspaceFolders": True},
        "notebookDocument": {"synchronization": {}},
        "textDocument": {"semanticTokens": {"requests": {"full": {"delta": True}}, "tokenTypes": [], "tokenModifiers": [], "formats": ["relative"]}},
    }
    c.init = c.initialize(root, caps)
    uri = (root / "main.pyn").as_uri()
    c.send({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "languageId": "pyn", "version": 1, "text": MAIN}}})
    yield c, root, uri
    c.close()


def hover_text(c, uri, p) -> str:
    h = c.request("textDocument/hover", {"textDocument": {"uri": uri}, "position": p})
    return h["contents"]["value"] if h else ""


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
    assert "Config = (n_layer: int, dropout: float)" in hover_text(c, rec_uri, pos(RECORD_CALL, "Config,"))
    assert "def record(" in hover_text(c, rec_uri, pos(RECORD_CALL, "record(C"))
    assert "cfg: Config" in hover_text(c, rec_uri, pos(RECORD_CALL, "cfg ="))  # named by its alias, as with cast


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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": text}]}})
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": pos(text, "res.\n", delta=4)})
    labels = {i["label"] for i in (res["items"] if isinstance(res, dict) else res)}
    assert {"name", "age", "greeting"} <= labels


def test_completion_hides_generated_names(lsp):
    c, root, uri = lsp
    text = MAIN + "res.\n"
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": {"line": text.count("\n"), "character": 0}})
    labels = {i["label"] for i in (res["items"] if isinstance(res, dict) else res)}
    assert "greeting" in labels  # module-level names are offered...
    assert "_ds" not in labels and not any(l.startswith("_rec_") for l in labels)  # ...generated ones aren't


def test_definition_of_destructured_local_covers_the_word(lsp):
    # `greeting` was bound by `(greeting=, ...)`; the jump lands on that word, not a zero-width point
    c, root, uri = lsp
    res = c.request("textDocument/definition", {"textDocument": {"uri": uri}, "position": pos(MAIN, "shout(greeting", delta=7)})
    rng = (res[0].get("targetSelectionRange") or res[0]["range"])
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
        want = [(0, "Type \"Literal['a']\" is not assignable to declared type \"int\"")]
        assert wait_errors(c, uri, want) == want, name
    c.close()


def test_checker_commands_are_advertised_with_ours(lsp):
    # byname is the only Python server in the editor, so basedpyright's commands come through too
    c, root, uri = lsp
    commands = c.init["capabilities"]["executeCommandProvider"]["commands"]
    assert "byname.server.writeOutput" in commands
    assert any(cmd.startswith("basedpyright.") for cmd in commands)


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
    assert "property" in seen["greeting"]  # record field names, which the checker leaves uncoloured


def test_spread_names_and_fields_keep_their_colours(lsp):
    # a spread of a known record is rebuilt field by field for the checker; the user's `d` must stay real
    # text (variable colour, not dropped), and field names written next to it are record fields (property)
    c, root, uri = lsp
    text = "d = (a=1, b=2)\nx = (**d, c=3)\ny = (**load(), c=3)\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 992}, "contentChanges": [{"text": text}]}})
    legend = c.init["capabilities"]["semanticTokensProvider"]["legend"]["tokenTypes"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    lines, line, col, got = text.splitlines(), 0, 0, {}
    for i in range(0, len(data), 5):
        dl, dc, length, typ, _ = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        got[(line, lines[line][col : col + length])] = legend[typ]
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 993}, "contentChanges": [{"text": MAIN}]}})
    assert got.get((1, "d")) == "variable" and got.get((1, "c")) == "property", got
    assert got.get((2, "c")) == "property", got  # the generic path, for comparison


def test_pattern_labels_reading_methods_are_coloured_as_methods(lsp):
    # a pattern label reads an attribute, so it's coloured like one: `decode=` as `tok.decode` (method),
    # `n=` as `tok.n` (property); a shorthand is also the local
    c, root, uri = lsp
    text = (
        "class Tok:\n    def __init__(self): self.n = 65\n    def encode(self, s: str) -> int: return 1\n"
        "    def decode(self, i: int) -> str: return ''\n(encode=, decode=renamed, n=) = Tok()\nr = (encode=)\n"
    )
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 994}, "contentChanges": [{"text": text}]}})
    legend = c.init["capabilities"]["semanticTokensProvider"]["legend"]["tokenTypes"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    lines, line, col, got = text.splitlines(), 0, 0, {}
    for i in range(0, len(data), 5):
        dl, dc, length, typ, _ = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        got[(line, lines[line][col : col + length])] = legend[typ]
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 995}, "contentChanges": [{"text": MAIN}]}})
    assert got[(4, "encode")] == got[(4, "decode")] == "method" and got[(4, "renamed")] == "function", got
    assert got[(4, "n")] == "property", got
    assert got[(5, "encode")] == "function", got  # a record built from the local `encode`


def test_fields_read_through_record_types_look_like_record_fields(lsp):
    # `p.age` through a record type reads a Final Protocol attribute (readonly static); a record's own
    # field is static. Both must get the same token, or themes colour them differently
    c, root, uri = lsp
    text = "def f(p: (..., age: int), q: (age: int, name: str)) -> int:\n    return p.age + q.age\nr = (age=1, name='x')\nprint(r.age)\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 990}, "contentChanges": [{"text": text}]}})
    legend = c.init["capabilities"]["semanticTokensProvider"]["legend"]
    data = c.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})["data"]
    lines, line, col, got = text.splitlines(), 0, 0, []
    for i in range(0, len(data), 5):
        dl, dc, length, typ, mods = data[i : i + 5]
        line, col = line + dl, (col + dc if dl == 0 else dc)
        if lines[line][col : col + length] == "age" and line in (1, 3):
            got.append((legend["tokenTypes"][typ], mods))
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 991}, "contentChanges": [{"text": MAIN}]}})
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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 10}, "contentChanges": [{"text": text}]}})
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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 100 + at}, "contentChanges": [{"text": text}]}})
    p = {"line": text.count("\n", 0, at), "character": at - (text.rfind("\n", 0, at) + 1)}
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": p})
    return text, (res["items"] if isinstance(res, dict) else res)


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


def test_completion_on_a_class_instance_offers_methods(lsp):
    # destructuring reads attributes, so methods and properties are slots too
    c, root, uri = lsp
    cls = "class Tok:\n    def __init__(self): self.vocab_size = 65\n    def encode(self, s: str) -> list[int]: return []\n    @property\n    def n(self) -> int: return 1\n"
    _, items = complete(c, uri, MAIN + cls + "(|) = Tok()\n")
    assert {i["label"] for i in items} == {"encode", "n", "vocab_size"}


def test_completion_in_parameter_patterns(lsp):
    # `def f((name=, |): T)` offers T's other fields, annotated inline or with an alias
    c, root, uri = lsp
    _, items = complete(c, uri, MAIN + "def f((name=, |): (name: str, age: int, city: str)): pass\n")
    assert {i["label"] for i in items} == {"age", "city"}
    _, items = complete(c, uri, MAIN + "type U = (name: str, age: int)\ndef f(n: int, (ag|): U): pass\n")
    assert [i["label"] for i in items] == ["age"]


def test_parameter_pattern_shows_as_a_pattern(lsp):
    # the generated parameter name doesn't leak into hovers: `(...)`
    c, root, uri = lsp
    text = MAIN + "type U = (name: str, age: int)\ndef greet((name=, age=): U) -> str:\n    return name\ngreet\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 300}, "contentChanges": [{"text": text}]}})
    res = c.request("textDocument/hover", {"textDocument": {"uri": uri}, "position": pos(text, "greet\n")})
    value = res["contents"]["value"]
    assert "def greet((...): U) -> str" in value


def test_replace_completes_and_checks_fields(lsp):
    # `_replace(` offers the record's fields; a stale `**cfg._asdict()` is an error in the editor
    c, root, uri = lsp
    _, items = complete(c, uri, MAIN + "res._replace(|)\n")
    assert {"name=", "age=", "greeting="} <= {i["label"] for i in items}
    text = MAIN + "res._replace(nme='x')\ndef h(*, name: str): ...\nh(**res._asdict())\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 900}, "contentChanges": [{"text": text}]}})
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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 950}, "contentChanges": [{"text": text}]}})
    end, msgs = time.time() + 60, []
    while time.time() < end and not any("nme" in m for m in msgs):  # skip diagnostics for the previous text
        c.diags.pop(uri, None)
        msgs = [d["message"] for d in c.wait_diags(uri)]
    assert any('No parameter named "nme"' in m for m in msgs)

def test_a_walrus_labelling_a_returned_tuple_is_not_called_unused(lsp):
    # `(x := ...)` in a return names a position (hover shows tuple[x: ..., y: ...]); nothing reads it after
    c, root, uri = lsp
    text = MAIN + "def pair(a: int):\n    return (lo := a, hi := a + 1)\ndef other(a: int):\n    unused = a\n    return a\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 960}, "contentChanges": [{"text": text}]}})
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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 970}, "contentChanges": [{"text": text}]}})
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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 500}, "contentChanges": [{"text": text}]}})
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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 980}, "contentChanges": [{"text": text}]}})
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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 900}, "contentChanges": [{"text": text}]}})
    edits = c.request("textDocument/formatting", {"textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": True}})
    assert len(edits) == 1
    assert "res = make(name=, age=)\n" in edits[0]["newText"]


def test_formatting_code_that_does_not_translate_offers_nothing(lsp):
    # rather than failing the save: the file's diagnostic says what's wrong
    c, root, uri = lsp
    text = "name = 1\ndef f(g=lambda v: (v=, name=name)): return g\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 902}, "contentChanges": [{"text": text}]}})
    edits = c.request("textDocument/formatting", {"textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": True}})
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 903}, "contentChanges": [{"text": MAIN}]}})
    assert edits == []


def test_a_broken_byname_rule_shows_only_its_own_error(lsp):
    # the checker gets the raw .pyn then, so its `"(" was not closed` is noise
    c, root, uri = lsp
    text = "name = 1\ndef f(g=lambda v: (v=, name=name)): return g\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 904}, "contentChanges": [{"text": text}]}})
    want = [(1, "v= in a parameter's default would take the outer 'v': write v=v to mean that")]
    got = wait_errors(c, uri, want)
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 905}, "contentChanges": [{"text": MAIN}]}})
    assert got == want



def test_fix_all_on_save(lsp):
    # codeActionsOnSave: source.fixAll is answered by byname with ruff's safe fixes, as for a .py
    c, root, uri = lsp
    assert "source.fixAll" in c.init["capabilities"]["codeActionProvider"]["codeActionKinds"]
    text = "from typing import List\n\n\ndef f(xs: List[int]):\n    n = len(xs)\n    return (n=, xs=)\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 901}, "contentChanges": [{"text": text}]}})
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    actions = c.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": {"diagnostics": [], "only": ["source.fixAll"]}})
    assert [a["kind"] for a in actions] == ["source.fixAll.byname"]
    new = actions[0]["edit"]["changes"][uri][0]["newText"]
    assert new == "\n\ndef f(xs: list[int]):\n    n = len(xs)\n    return (n=, xs=)\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 902}, "contentChanges": [{"text": MAIN}]}})


def test_reading_a_record_by_position_offers_reading_it_by_name(lsp):
    # the error says how, and a quick fix rewrites the targets by field, matched in the order written
    c, root, uri = lsp
    text = MAIN + "def batch():\n    return (x=1, y=2)\nx, y = batch()\nfor a, _ in [batch()]: pass\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 990}, "contentChanges": [{"text": text}]}})
    last = text.count("\n") - 1
    end, mine = time.time() + 60, []
    while time.time() < end and len(mine) < 2:  # skip diagnostics for the previous text
        c.diags.pop(uri, None)
        mine = [d for d in c.wait_diags(uri) if d["range"]["start"]["line"] in (last - 1, last)]
    assert all("records are read by name: (x=, y=)" in d["message"] for d in mine)
    fixes = []
    for d in sorted(mine, key=lambda d: d["range"]["start"]["line"]):
        actions = c.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": d["range"], "context": {"diagnostics": [d]}})
        fixes += [(a["title"], a["edit"]["changes"][uri][0]) for a in actions if a.get("kind") == "quickfix" and a["title"].startswith("Read by name")]
    assert [t for t, _ in fixes] == ["Read by name: (x=, y=)", "Read by name: (x=a)"]
    assert [snippet(text, e["range"]) for _, e in fixes] == ["x, y", "a, _"]
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 991}, "contentChanges": [{"text": MAIN}]}})


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
    c.send({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "languageId": "pyn", "version": 1, "text": SOLUTION}}})
    return c, root, uri


def save(c, uri, text, version):
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]}})
    c.send({"method": "textDocument/didSave", "params": {"textDocument": {"uri": uri}}})
    c.request("textDocument/hover", {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 0}})  # round trip: save handled


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
    caps = {"workspace": {"configuration": True, "diagnostics": {"refreshSupport": True}}, "textDocument": {"diagnostic": {"dynamicRegistration": True}}}
    c, root, uri = start(tmp_path, caps, diagnosticsOnSave=True)
    assert c.wait_diags(uri) == []  # the opened text counts as saved
    broken = SOLUTION + "print(nope)\n"
    c.diags.pop(uri)
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": broken}]}})
    drain(c, 3)
    assert c.diags.pop(uri, []) == []
    c.send({"method": "textDocument/didSave", "params": {"textDocument": {"uri": uri}}})
    assert any("nope" in d["message"] for d in c.wait_diags(uri))
    c.close()


def test_diagnostics_while_typing_by_default(tmp_path):
    c, root, uri = start(tmp_path)
    c.wait_diags(uri)
    c.diags.pop(uri)
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": SOLUTION + "print(nope)\n"}]}})
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
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 3}, "contentChanges": [{"text": SOLUTION + "print(other)\n"}]}})
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
    assert explain_fields(raw) == raw.removesuffix(" (reportOperatorIssue)") + "\n" + hint.replace("r.name", "r.age") + " (reportOperatorIssue)"


# --- .py files and notebooks ---------------------------------------------------


def test_py_files_go_to_the_checker_as_they_are(lsp):
    # byname syntax can't run in a .py file, so it isn't translated: hover is basedpyright's own
    c, root, uri = lsp
    text = "from helpers import shout\n\nn: int = shout('a')\n"
    py = (root / "script.py").as_uri()
    c.send({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": py, "languageId": "python", "version": 1, "text": text}}})
    assert "(s: str) -> str" in hover_text(c, py, pos(text, "shout", 1))
    assert errors(c.wait_diags(py)) == [(2, 'Type "str" is not assignable to declared type "int"')]


CELLS = [
    "%load_ext byname\n",
    "from people import make\n\nname, age = 'Rahul', 26\nres = make(name=, age=)\n",
    "(greeting=, age=years) = res\nres.nme\n",
]


def open_notebook(c, root, name, cells):
    nb = (root / name).as_uri()
    uris = [f"vscode-notebook-cell:{nb[len('file:'):]}#C{i}" for i in range(len(cells))]
    c.send({"method": "notebookDocument/didOpen", "params": {
        "notebookDocument": {"uri": nb, "notebookType": "jupyter-notebook", "version": 1, "cells": [{"kind": 2, "document": u} for u in uris]},
        "cellTextDocuments": [{"uri": u, "languageId": "python", "version": 1, "text": t} for u, t in zip(uris, cells)],
    }})
    return nb, uris


def edit_cell(c, nb, uri, version, start, end, text):
    rng = {"start": {"line": start[0], "character": start[1]}, "end": {"line": end[0], "character": end[1]}}
    c.diags.pop(uri, None)
    c.send({"method": "notebookDocument/didChange", "params": {
        "notebookDocument": {"uri": nb, "version": version},
        "change": {"cells": {"textContent": [{"document": {"uri": uri, "version": version}, "changes": [{"range": rng, "text": text}]}]}},
    }})


def wait_errors(c, uri, want, timeout=30) -> list[tuple[int, str]]:
    """The cell's errors once they match `want`: after an edit the checker may republish the old ones first."""
    end = time.time() + timeout
    while True:
        got = errors(c.diags.get(uri, []))
        if got == want or time.time() > end:
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
    return c.request("textDocument/formatting", {"textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": True}})


def organize_cell(c, uri, text) -> list:
    rng = {"start": {"line": 0, "character": 0}, "end": pos(text, text[-1])}
    ctx = {"diagnostics": [], "only": ["source.organizeImports"], "triggerKind": 2}
    actions = c.request("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": rng, "context": ctx})
    return [e for a in actions for es in a["edit"]["changes"].values() for e in es]


def test_notebook_cells_are_formatted(lsp):
    # byname formats notebooks (notebook.defaultFormatter): Ruff on each cell, through the translation in a
    # byname notebook, as it is in a plain one. A cell's last line keeps having no newline
    c, root, uri = lsp
    nb, cells = open_notebook(c, root, "fmt.ipynb", ["%load_ext byname", "import sys\nimport os\nr=(a=os.sep,b= sys.argv)"])
    assert [e["newText"] for e in format_cell(c, cells[1])] == ["import sys\nimport os\n\nr = (a=os.sep, b=sys.argv)"]
    assert organize_cell(c, cells[1], "import sys\nimport os\nr=(a=os.sep,b= sys.argv)") == []  # the notebook-wide action does it
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
    c.send({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": py, "languageId": "python", "version": 1, "text": text}}})
    assert [e["newText"] for e in format_cell(c, py)] == [text.replace("List\nx: List[int]=[1]", "List\n\nx: List[int] = [1]")]
    rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    for kind, want in (
        ("source.fixAll", "import os\n\nx: list[int]=[1]\nprint(os.sep, x)\n"),
        ("source.organizeImports", "import os\nimport sys\nfrom typing import List\n\nx: List[int]=[1]\nprint(os.sep, x)\n"),
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
    c.send({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": py, "languageId": "python", "version": 1, "text": text}}})
    end = time.time() + 30
    while not c.diags.get(py) and time.time() < end:  # the first publish can be the empty one before analysis
        c.diags.pop(py, None)
        c.wait_diags(py, timeout=5)
    diags = c.diags[py]
    assert {d.get("code") for d in diags} == {None, "reportUnusedImport"}
    assert not any(d.get("code") == "reportAssignmentType" for d in diags)
    bad = (root / "bad.pyn").as_uri()  # the same error in a .pyn is shown
    c.send({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": bad, "languageId": "pyn", "version": 1, "text": "x: int = 'a'\n"}}})
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
    c.send({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": late, "languageId": "pyn", "version": 1, "text": text}}})
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
    c.send({"method": "textDocument/didOpen", "params": {"textDocument": {"uri": uri, "languageId": "pyn", "version": 1, "text": text}}})
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
        res = c.request("textDocument/definition", {"textDocument": {"uri": uri}, "position": pos(PARITY, "to_be_imported", 1)})
        assert [r.get("targetUri") or r["uri"] for r in res] == [(root / "src" / "records" / "fn_import_test.py").as_uri()], name
    assert got["tc.pyn"] == got["tc_py.py"]
    c.close()


def test_the_editor_and_byname_tool_agree(tmp_path):
    # Alt+L (`byname tool basedpyright`) and the editor check the same mirror: the same findings
    root = parity_project(tmp_path)
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache"), "PATH": str(Path(sys.executable).parent)}
    out = subprocess.run([sys.executable, "-m", "byname", "tool", "basedpyright", "src/records/tc.pyn"], cwd=root, env=env, capture_output=True, text=True, check=False).stdout
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
    c.send({"method": "workspace/didChangeWatchedFiles", "params": {"changes": [{"uri": (root / "lib.pyn").as_uri(), "type": 1}]}})
    user = c.open(root / "user.pyn", "from lib import f\n\nprint(f().x)\n")
    c.wait_diags(user)
    assert wait_errors(c, user, []) == []
    (root / "lib.pyn").unlink()
    c.send({"method": "workspace/didChangeWatchedFiles", "params": {"changes": [{"uri": (root / "lib.pyn").as_uri(), "type": 3}]}})
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": user, "version": 2}, "contentChanges": [{"text": "from lib import f\n\nprint(f().x)\n\n"}]}})
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
        c.send({"method": "workspace/didChangeWatchedFiles", "params": {"changes": [{"uri": (root / f).as_uri(), "type": 1}]}})
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
    out = subprocess.run([sys.executable, "-m", "byname", "tool", "ruff", "check", "--select", "I", "user.pyn"], cwd=root, env=env, capture_output=True, text=True, check=False).stdout
    assert "All checks passed" in out, out
    c.close()
