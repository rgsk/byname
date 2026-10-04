"""Drive `byname lsp` + basedpyright like an editor would, on a small two-file workspace."""

import json
import queue
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
    assert pretty("-> _dct_name__age[str, int]") == "-> {name: str, age: int}"  # what _asdict() returns


class Client:
    def __init__(self, root: Path, cache: Path):
        env = {"XDG_CACHE_HOME": str(cache), "PATH": str(Path(sys.executable).parent)}
        self.p = subprocess.Popen(
            [sys.executable, "-m", "byname", "lsp"], cwd=root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.q: queue.Queue = queue.Queue()
        self.next_id = 0
        self.diags: dict[str, list] = {}
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
                self.send({"id": msg["id"], "result": [None] * len(msg["params"]["items"])})
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
        "workspace": {"configuration": True},
        "textDocument": {"semanticTokens": {"requests": {"full": {"delta": True}}, "tokenTypes": [], "tokenModifiers": [], "formats": ["relative"]}},
    }
    c.init = c.request("initialize", {"processId": None, "rootUri": root.as_uri(), "capabilities": caps})
    c.send({"method": "initialized", "params": {}})
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


def test_fields_read_through_record_types_look_like_record_fields(lsp):
    # `p.age` through a record type reads a Final Protocol attribute (readonly static); a record's own
    # field is static. Both must get the same token, or themes colour them differently
    c, root, uri = lsp
    text = "def f(p: (age: int, ...), q: (age: int, name: str)) -> int:\n    return p.age + q.age\nr = (age=1, name='x')\nprint(r.age)\n"
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


def test_completion_in_parameter_patterns(lsp):
    # `def f((name=, |): T)` offers T's other fields, annotated inline or with an alias
    c, root, uri = lsp
    _, items = complete(c, uri, MAIN + "def f((name=, |): (name: str, age: int, city: str)): pass\n")
    assert {i["label"] for i in items} == {"age", "city"}
    _, items = complete(c, uri, MAIN + "type U = (name: str, age: int)\ndef f(n: int, (ag|): U): pass\n")
    assert [i["label"] for i in items] == ["age"]
    _, items = complete(c, uri, MAIN + "type U = (name: str, age: int)\ndef f(*, user=(name=, |): U): pass\n")
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


def test_comma_trigger_only_inside_patterns(lsp):
    # `,` pops completion in a pattern; in an ordinary call it returns nothing instead of noise
    c, root, uri = lsp
    assert "," in c.init["capabilities"]["completionProvider"]["triggerCharacters"]
    text = MAIN + "print(1,)\n"
    c.send({"method": "textDocument/didChange", "params": {"textDocument": {"uri": uri, "version": 500}, "contentChanges": [{"text": text}]}})
    p = pos(text, "print(1,", delta=8)
    ctx = {"triggerKind": 2, "triggerCharacter": ","}
    res = c.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": p, "context": ctx})
    assert res["items"] == []


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
    caps = caps or {"workspace": {"configuration": True}}
    c.request("initialize", {"processId": None, "rootUri": root.as_uri(), "capabilities": caps, "initializationOptions": opts})
    c.send({"method": "initialized", "params": {}})
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
    assert pretty("p: _opn_age[int]") == "p: (age: int, ...)"


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
