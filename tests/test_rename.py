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


def test_renaming_a_field_of_a_record_type_renames_the_type_written(client):
    # the literal passed is written where the type is expected, so it's that type's (transform's `typed`)
    src = "def show(q: (name: str)) -> str:\n    return q.name\n\n\nshow((name='rahul'))\n"
    got = rename(client, "typed.pyn", src, pos(src, "q.name", delta=2), "who")
    assert got == "def show(q: (who: str)) -> str:\n    return q.who\n\n\nshow((who='rahul'))\n"


def test_a_record_built_on_its_own_and_passed_later_stays_its_own(client):
    # `u` was built where nothing was expected: it fits the type now, but nothing ties it to it; the
    # checker flags `show(u)` after the rename
    src = "def show(q: (name: str)) -> str:\n    return q.name\n\n\nu = (name='rahul')\nshow(u)\n"
    got = rename(client, "own.pyn", src, pos(src, "q.name", delta=2), "who")
    assert got == src.replace("(name: str)", "(who: str)").replace("q.name", "q.who")


def test_a_record_of_the_same_shape_elsewhere_is_left_alone(client):
    # make's record and show's type both have a `label`; renaming make's leaves show's
    src = (
        "def make():\n    return (label='x')\n\n\n"
        "def show(q: (label: str)) -> str:\n    return q.label\n\n\n"
        "r = make()\nprint(r.label)\n"
    )
    got = rename(client, "shape.pyn", src, pos(src, "r.label", delta=2), "who")
    assert got == src.replace("(label='x')", "(who='x')").replace("r.label", "r.who")


def test_a_record_typed_by_a_written_type_renames_the_type_and_the_literal_returned(client):
    src = "def make() -> (label: str):\n    return (label='x')\n\n\nr = make()\nprint(r.label)\n"
    got = rename(client, "annotated.pyn", src, pos(src, "r.label", delta=2), "who")
    assert got == src.replace("label", "who")


def test_renaming_a_literals_field_renames_the_type_it_is_written_against(client):
    # as in TypeScript: from the literal or from the type, it's one field; a shorthand keeps its local
    src = "type User = (name: str, age: int)\nname = 'r'\nuser: User = (name=, age=26)\nxs: list[User] = [(name='a', age=1)]\nprint(user.name)\n"
    got = rename(client, "declared.pyn", src, pos(src, "name=,"), "named")
    assert got == (
        "type User = (named: str, age: int)\nname = 'r'\nuser: User = (named=name, age=26)\n"
        "xs: list[User] = [(named='a', age=1)]\nprint(user.named)\n"
    )


def test_renaming_a_field_of_an_open_spread_record_reaches_where_it_is_written(client):
    # `(**rest, name=...)` with no type expected is its own open record (transform.open_build)
    src = "def greet(u: (..., name: str)):\n    (name=, **rest) = u\n    return (**rest, name=name.upper())\n\n\nprint(greet((name='a')).name)\n"
    got = rename(client, "open.pyn", src, pos(src, ").name", delta=2), "who")
    assert got.splitlines()[2] == "    return (**rest, who=name.upper())"
    assert got.splitlines()[-1] == "print(greet((name='a')).who)"


def test_renaming_a_field_of_a_spread_record_of_known_names_reaches_where_it_is_written(client):
    src = "u = (a=1)\nr = (**u, b=2)\nprint(r.b)\n"
    got = rename(client, "known.pyn", src, pos(src, "r.b", delta=2), "c")
    assert got == "u = (a=1)\nr = (**u, c=2)\nprint(r.c)\n"


def test_a_rename_gives_the_checker_back_the_real_translation(client):
    # the rename runs on a translation with a class per site (Proxy.start_rename), where a list of literals
    # isn't a list[Item] (list is invariant); after it, the file's diagnostics are the real translation's
    c, root = client
    src = "type Item = (sku: str)\n\n\ndef f(xs: list[Item]) -> int:\n    return len(xs)\n\n\nxs = [(sku='a')]\nprint(f(xs), xs[0].sku)\n"
    path = root / "invariant.pyn"
    path.write_text(src)
    uri = c.open(path)
    assert c.wait_diags(uri) == []
    c.diags.pop(uri)
    edit = c.request(
        "textDocument/rename",
        {"textDocument": {"uri": uri}, "position": pos(src, "xs[0].sku", delta=6), "newName": "code"},
    )
    assert apply(src, uri, edit) == src.replace("(sku='a')", "(code='a')").replace(".sku", ".code")
    assert c.wait_diags(uri) == []


def test_renaming_a_field_from_the_type_written_reaches_its_reads(client):
    # the checker has no text at `label` in `(label: str)` (it's the type's class); the proxy answers
    # prepareRename and starts the rename at the class's field (Proxy.written_field). Another record of
    # that shape (make's) is left alone; the literal passed is written against the type, so it follows
    c, root = client
    src = (
        "def make():\n    return (label='x')\n\n\n"
        "def show(q: (label: str)) -> str:\n    return q.label\n\n\n"
        "show((label='y'))\n"
    )
    at = pos(src, "label: str")
    path = root / "fromtype.pyn"
    path.write_text(src)
    uri = c.open(path)
    c.wait_diags(uri)
    got = c.request("textDocument/prepareRename", {"textDocument": {"uri": uri}, "position": at})
    assert got == {"range": {"start": at, "end": {**at, "character": at["character"] + 5}}, "placeholder": "label"}
    assert rename(client, "fromtype.pyn", src, at, "who") == src.replace("(label: str)", "(who: str)").replace(
        "q.label", "q.who"
    ).replace("(label='y')", "(who='y')")


def test_renaming_a_field_of_a_type_alias_reaches_everything_typed_by_it(client):
    src = "type Item = (sku: str, qty: int)\n\n\ndef code(it: Item) -> str:\n    (sku=) = it\n    return sku + it.sku\n"
    got = rename(client, "alias.pyn", src, pos(src, "sku: str"), "code")
    assert got == src.replace("(sku: str", "(code: str").replace("(sku=)", "(code=sku)").replace("it.sku", "it.code")


def test_a_spread_of_a_renamed_record_stays_as_written(client):
    # `(**user, sex=...)` is `name=(_byname_v0 := user).name, ...` to the checker: the field it reads is
    # generated, so the rename has nothing to write there
    src = "type User = (name: str, age: int)\nuser: User = (name='r', age=1)\nfor u in [user]:\n    moved = (**u, sex='m')\n    print(moved.name)\n"
    got = rename(client, "spread.pyn", src, pos(src, "name: str"), "who")
    assert got.splitlines()[3] == "    moved = (**u, sex='m')"
    assert got.splitlines()[0] == "type User = (who: str, age: int)"


def test_a_literal_nested_in_a_typed_literal_renames_with_its_type(client):
    # inside `o: Order = (...)` the nested literals are Order's fields' types: `_byname_t.c.name_op` and
    # `_byname_t.items[0].sku` read them (transform's field_reads)
    src = (
        "type Customer = (name_op: str)\ntype Item = (sku: str)\n"
        "type Order = (id: int, c: Customer, items: list[Item])\n"
        "o: Order = (id=1, c=(name_op='s'), items=[(sku='a'), (sku='b')])\n"
    )
    got = rename(client, "nested.pyn", src, pos(src, "name_op: str"), "name")
    assert got == src.replace("name_op", "name")
    got = rename(client, "nested.pyn", src, pos(src, "name_op='"), "name")
    assert got == src.replace("name_op", "name")
    got = rename(client, "nested.pyn", src, pos(src, "sku='b'"), "code")
    assert got == src.replace("sku", "code")


def test_a_literal_nested_in_any_display_renames_with_its_type(client):
    # in a dict (read with its key as written), a set (`next(iter(...))`), a tuple, and nested displays
    src = (
        "type Item = (sku: str)\n"
        "type Order = (by_code: dict[str, Item], tags: set[Item], pair: tuple[Item, Item], deep: list[dict[str, Item]])\n"
        "o: Order = (by_code={'pen': (sku='a')}, tags={(sku='b')}, pair=((sku='c'), (sku='d')), deep=[{'x': (sku='e')}])\n"
    )
    assert rename(client, "displays.pyn", src, pos(src, "sku: str"), "code") == src.replace("sku", "code")
    assert rename(client, "displays.pyn", src, pos(src, "sku='a'"), "code") == src.replace("sku", "code")


def test_a_closed_file_s_literals_rename_with_another_file_s_type(client):
    # only types.pyn is open: uses.pyn is opened in the rename translation for the rename (start_rename),
    # so its literal written against Item follows, not just its read
    c, root = client
    (root / "uses.pyn").write_text(
        "from types_mod import Item\n\nit: Item = (sku='a')\nlater = [(sku='b')]\nprint(it.sku)\n"
    )
    src = "type Item = (sku: str)\n"
    path = root / "types_mod.pyn"
    path.write_text(src)
    # as the editor does: the checker hears of files made since it started from these events
    made = [{"uri": (root / n).as_uri(), "type": 1} for n in ("uses.pyn", "types_mod.pyn")]
    c.send({"method": "workspace/didChangeWatchedFiles", "params": {"changes": made}})
    uri = c.open(path)
    c.wait_diags(uri)
    edit = c.request(
        "textDocument/rename", {"textDocument": {"uri": uri}, "position": pos(src, "sku"), "newName": "code"}
    )
    assert apply(src, uri, edit) == "type Item = (code: str)\n"
    uses = (root / "uses.pyn").as_uri()
    got = apply((root / "uses.pyn").read_text(), uses, edit)
    assert got == "from types_mod import Item\n\nit: Item = (code='a')\nlater = [(sku='b')]\nprint(it.code)\n"
