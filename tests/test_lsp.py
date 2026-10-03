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
