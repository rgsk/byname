"""Rename through the language server on shorthand `x=`.

A bare `x=` is one piece of text that stands for two symbols: the label (a parameter, a record field, an
attribute read by a pattern) and the implicit value (a local). Renaming either one must leave the other
alone, so the shorthand splits into `label=value` when only one of them changes:

    renaming the parameter `name` -> `who`:   fn(name=)  becomes  fn(who=name)
    renaming the local `name` -> `who`:       fn(name=)  becomes  fn(name=who)

Before this, renaming the parameter edited the shared text to `fn(who=)`, which silently passed whatever
local happened to be called `who`.
"""

import shutil

import pytest
from test_lsp import Client, pos

pytestmark = pytest.mark.skipif(shutil.which("basedpyright-langserver") is None, reason="needs basedpyright")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("ws")
    c = Client(root, tmp_path_factory.mktemp("cache"))
    caps = {  # like VS Code: edits come back as documentChanges
        "workspace": {"configuration": True, "workspaceFolders": True, "workspaceEdit": {"documentChanges": True}},
    }
    c.initialize(root, caps)
    yield c, root
    c.close()


def offset(text: str, p: dict) -> int:
    lines = text.split("\n")
    return sum(len(line) + 1 for line in lines[: p["line"]]) + p["character"]


def apply(text: str, uri: str, edit: dict) -> str:
    """Apply a WorkspaceEdit's edits for `uri`, whichever shape the server sent."""
    edits = list(edit.get("changes", {}).get(uri, []))
    for dc in edit.get("documentChanges", []):
        if dc.get("textDocument", {}).get("uri") == uri:
            edits += dc["edits"]
    spans = sorted(
        ((offset(text, e["range"]["start"]), offset(text, e["range"]["end"]), e["newText"]) for e in edits),
        reverse=True,
    )
    for s, e, new in spans:
        text = text[:s] + new + text[e:]
    return text


def rename(client, name: str, text: str, at: dict, new: str) -> str:
    c, root = client
    path = root / name
    path.write_text(text)
    uri = c.open(path)
    c.wait_diags(uri)  # the checker has the file
    edit = c.request("textDocument/rename", {"textDocument": {"uri": uri}, "position": at, "newName": new})
    assert edit is not None
    return apply(text, uri, edit)


def test_renaming_a_parameter_keeps_the_local_a_shorthand_passed(client):
    # the reported case: another local already has the parameter's new name
    src = 'def fn(name: str):\n    print(name)\n\n\nname_renamed = "new name"\n\nname = "rahul"\nfn(name=)\n'
    got = rename(client, "param.pyn", src, pos(src, "name", nth=0), "name_renamed")
    assert got.splitlines()[0] == "def fn(name_renamed: str):"
    assert got.splitlines()[-1] == "fn(name_renamed=name)"  # still passes "rahul"


def test_renaming_the_local_keeps_the_parameter(client):
    src = "def fn(name: str):\n    print(name)\n\n\nname = 'rahul'\nfn(name=)\n"
    got = rename(client, "local.pyn", src, pos(src, "name = "), "who")
    assert got.splitlines()[-2:] == ["who = 'rahul'", "fn(name=who)"]


def test_renaming_a_symbol_that_is_both_halves_keeps_the_shorthand(client):
    # inside fn, `n` is the parameter and the local passed: one symbol, so `n=` stays a shorthand
    src = "def fn(n: int) -> int:\n    return fn(n=) if n else 0\n"
    got = rename(client, "both.pyn", src, pos(src, "n:"), "m")
    assert got == "def fn(m: int) -> int:\n    return fn(m=) if m else 0\n"


def test_renaming_an_attribute_keeps_the_local_a_pattern_binds(client):
    src = "class P:\n    def __init__(self) -> None:\n        self.age = 1\n\n\n(age=) = P()\nprint(age)\n"
    got = rename(client, "pattern.pyn", src, pos(src, "age = 1"), "years")
    assert got.splitlines()[-2:] == ["(years=age) = P()", "print(age)"]


def test_renaming_a_record_field_from_a_read_reaches_the_literal(client):
    # `r.name` and `(name=)` are two symbols to the checker; the proxy renames both, and the literal's
    # shorthand keeps its local: `(who=name)`
    src = "name = 'rahul'\nr = (name=)\nprint(r.name)\n"
    got = rename(client, "record.pyn", src, pos(src, "r.name", delta=2), "who")
    assert got == "name = 'rahul'\nr = (who=name)\nprint(r.who)\n"


def test_renaming_a_record_field_from_the_literal_reaches_its_reads(client):
    src = "r = (name='rahul')\nprint(r.name)\n(name=n) = r\ns = r._replace(name='x')\n"
    got = rename(client, "literal.pyn", src, pos(src, "name="), "who")
    assert got == "r = (who='rahul')\nprint(r.who)\n(who=n) = r\ns = r._replace(who='x')\n"


@pytest.mark.xfail(
    strict=True,
    reason="a written record type's field name maps to no symbol: `(name: str)` is `_typ_name[str]` to the "
    "checker, so it stays; the call passing `(who=...)` is then an error, not silent",
)
def test_renaming_a_field_of_a_record_type_reaches_the_records_passed(client):
    src = "def show(q: (name: str)) -> str:\n    return q.name\n\n\nshow((name='rahul'))\n"
    got = rename(client, "typed.pyn", src, pos(src, "q.name", delta=2), "who")
    assert got == "def show(q: (who: str)) -> str:\n    return q.who\n\n\nshow((who='rahul'))\n"
