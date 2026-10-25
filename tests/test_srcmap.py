from byname.srcmap import Translation


def at(text: str, needle: str, nth: int = 0) -> dict:
    """LSP position of the nth occurrence of needle."""
    off = -1
    for _ in range(nth + 1):
        off = text.index(needle, off + 1)
    line = text.count("\n", 0, off)
    return {"line": line, "character": off - (text.rfind("\n", 0, off) + 1)}


def word(text: str, needle: str, nth: int = 0) -> dict:
    s = at(text, needle, nth)
    return {"start": s, "end": {"line": s["line"], "character": s["character"] + len(needle)}}


def hidden_text(t: Translation, r: dict) -> str:
    lines = t.hidden.splitlines(keepends=True)
    assert r["start"]["line"] == r["end"]["line"]
    return lines[r["start"]["line"]][r["start"]["character"] : r["end"]["character"]]


def source_text(t: Translation, r: dict) -> str:
    lines = t.source.splitlines(keepends=True)
    assert r["start"]["line"] == r["end"]["line"]
    return lines[r["start"]["line"]][r["start"]["character"] : r["end"]["character"]]


SRC = """\
def make(*, name: str, age: int):
    return (name=, age=)

(name=who, age=) = make(name="R", age=1)
print(who)
"""


def test_hidden_has_prelude_and_translation():
    t = Translation(SRC)
    lines = t.hidden.splitlines()
    assert "from typing import NamedTuple as _NT" in lines
    assert "    class _rec_name__age[T0, T1](_NT):" in lines
    assert (
        '_ds = make(name="R", age=1); '
        "who = _byname_field(_ds.name, lambda _byname_m: _byname_no_method(name=_byname_method(_byname_m))); "
        "age = _byname_field(_ds.age, lambda _byname_m: _byname_no_method(age=_byname_method(_byname_m)))"
    ) in t.hidden


# --- source -> hidden: where requests (hover, completion) land ----------------


def test_plain_code_maps_past_prelude():
    # `print` is untouched source; it moves down by however many lines the prelude has
    t = Translation(SRC)
    h = t.position_to_hidden(at(SRC, "print"))
    assert h["line"] == at(SRC, "print")["line"] + t.hidden[: t.at + t.plen].count("\n")
    assert t.hidden.splitlines()[h["line"]][h["character"] :].startswith("print")


def test_hover_on_renamed_target_lands_on_bound_variable():
    # cursor on `who` inside the pattern -> `who` in `; who = _ds.name`, so hover shows its type
    t = Translation(SRC)
    r = t.range_to_hidden(word(SRC, "who"))
    assert hidden_text(t, r) == "who"
    assert t.hidden.splitlines()[r["start"]["line"]].startswith("_ds = make(")


def test_hover_on_pattern_field_lands_on_attribute():
    # cursor on `age` in `(name=who, age=)` -> `age` in `_ds.age`, the field's type
    t = Translation(SRC)
    src_age = word(SRC, "age", nth=2)  # 0: signature, 1: record, 2: pattern
    line = SRC.splitlines()[src_age["start"]["line"]]
    assert line.startswith("(name=who, age=)")
    r = t.range_to_hidden(src_age)
    hl = t.hidden.splitlines()[r["start"]["line"]]
    assert hl[r["start"]["character"] - 4 : r["end"]["character"]] == "_ds.age"


# --- hidden -> source: where results (diagnostics, edits) land ---------------


def test_diagnostic_on_expanded_shorthand_shows_on_field_name():
    # an error on the generated `nam` in `fn(nam=nam)` is shown on the `nam` the user wrote
    src = "fn(nam=)\n"
    t = Translation(src)
    gen = {"start": {"line": 0, "character": 7}, "end": {"line": 0, "character": 10}}
    assert t.hidden[7:10] == "nam"
    assert source_text(t, t.range_from_hidden(gen, display=True)) == "nam"


def test_rename_edit_on_expanded_shorthand_inserts_after_equals():
    # renaming local `nam` to `x` edits the generated half -> becomes fn(nam=x)
    src = "fn(nam=)\n"
    t = Translation(src)
    gen = {"start": {"line": 0, "character": 7}, "end": {"line": 0, "character": 10}}
    r = t.range_from_hidden(gen, display=False)
    assert r["start"] == r["end"] == {"line": 0, "character": 7}


def pattern_line(t: Translation) -> tuple[int, str]:
    """The hidden line a one-line pattern statement went to, past the prelude."""
    return next((k, line) for k, line in enumerate(t.hidden.splitlines()) if line.startswith("_ds = "))


def test_diagnostic_on_destructured_attribute_shows_on_field():
    # pyright flags `nope` in `_ds.nope`; the user sees it on `nope` in their pattern
    src = "(nope=) = obj\n"
    t = Translation(src)
    k, line = pattern_line(t)
    assert (
        line
        == "_ds = obj; nope = _byname_field(_ds.nope, lambda _byname_m: _byname_no_method(nope=_byname_method(_byname_m)))"
    )
    c = line.index("_ds.nope") + 4
    gen = {"start": {"line": k, "character": c}, "end": {"line": k, "character": c + 4}}
    assert t.range_from_hidden(gen, display=True) == word(src, "nope")
    # a method destructured: the checker flags the argument of the generated check, shown on the field too
    c = line.index("_byname_method(")
    gen = {"start": {"line": k, "character": c}, "end": {"line": k, "character": line.rindex(")") - 1}}
    assert t.range_from_hidden(gen, display=True) == word(src, "nope")


def test_shorthand_target_rename_inserts_explicit_target():
    # renaming the local bound by `(name=)` must give `(name=new)`, not `(new=)`
    src = "(name=) = obj\n"
    t = Translation(src)
    k, line = pattern_line(t)
    assert line.startswith("_ds = obj; name = _byname_field(_ds.name, ")
    c = line.index("; name") + 2
    gen = {"start": {"line": k, "character": c}, "end": {"line": k, "character": c + 4}}
    r = t.range_from_hidden(gen, display=False)
    assert r["start"] == r["end"] == at(src, ")")


def test_prelude_locations_are_dropped():
    t = Translation(SRC)
    prelude = {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 5}}
    assert t.range_from_hidden(prelude) is None


def test_untranslatable_source_maps_identity():
    # unbalanced bracket mid-typing: sent raw, positions unchanged
    src = "x = fn(a=,\n"
    t = Translation(src)
    assert t.error is not None and t.hidden == src
    p = {"line": 0, "character": 5}
    assert t.position_to_hidden(p) == p


def test_utf16_columns():
    # emoji is 2 UTF-16 units; columns after it must account for that
    src = 's = "😀"; fn(a=)\n'
    t = Translation(src)
    p = at(src, "fn")
    p16 = {"line": 0, "character": p["character"] + 1}
    h = t.position_to_hidden(p16)
    assert h == p16


def test_value_position_points_at_expanded_shorthand():
    # `x` in `fn(x=)` -> the generated value `x` in `fn(x=x)`, so go-to-definition finds the local
    src = "fn(x=, y=1)\n"
    t = Translation(src)
    vp = t.value_position(at(src, "x"))
    assert t.hidden.splitlines()[vp["line"]][vp["character"] - 2 : vp["character"] + 1] == "x=x"
    assert t.value_position(at(src, "y")) is None  # explicit value: nothing implicit to jump to
    assert t.value_position(at(src, "fn")) is None


def test_cursor_after_half_typed_item_maps_after_attribute():
    # completion at `ag|` must land right after `_ds.ag`, not at the start of the replaced pattern
    src = "(name=, ag) = r\n"
    t = Translation(src)
    p = at(src, "ag")
    p = {"line": 0, "character": p["character"] + 2}
    h = t.position_to_hidden(p)
    assert t.hidden.splitlines()[h["line"]][: h["character"]].endswith("_ds.ag")
    assert t.problems[0]["message"] == "pattern item 'ag' needs '='"


def _edit(src: str, s: int, e: int, text: str) -> dict:
    t = Translation(src)
    return {"range": {"start": t.src_lines.position(s), "end": t.src_lines.position(e)}, "newText": text}


def _apply(src: str, edits: list[dict]) -> str:
    t = Translation(src)
    spans = sorted(
        (
            (
                t.src_lines.offset(*e["range"]["start"].values()),
                t.src_lines.offset(*e["range"]["end"].values()),
                e["newText"],
            )
            for e in edits
        ),
        reverse=True,
    )
    for s, e, text in spans:
        src = src[:s] + text + src[e:]
    return src


def test_renaming_a_shorthands_label_keeps_its_local():
    # the parameter `name` renamed: the edit replaces the `name` written, so the old local goes after `=`
    src = "fn(name=)\n"
    edits = Translation(src).split_shorthands([_edit(src, 3, 7, "who")])
    assert _apply(src, edits) == "fn(who=name)\n"


def test_renaming_a_shorthands_label_in_a_pattern_keeps_its_local():
    src = "(age=) = p\n"
    edits = Translation(src).split_shorthands([_edit(src, 1, 4, "years")])
    assert _apply(src, edits) == "(years=age) = p\n"


def test_renaming_both_halves_of_a_shorthand_keeps_it_a_shorthand():
    # one symbol is the label and the local: the label edit plus the insertion after `=` -> just `m=`
    src = "fn(n=)\n"
    edits = Translation(src).split_shorthands([_edit(src, 3, 4, "m"), _edit(src, 5, 5, "m")])
    assert _apply(src, edits) == "fn(m=)\n"


def test_renaming_only_the_local_is_left_as_it_is():
    src = "fn(name=)\n"
    edits = [_edit(src, 8, 8, "who")]
    assert Translation(src).split_shorthands(edits) == edits


def _hinted(src: str) -> list[int]:
    return [d["range"]["start"]["line"] + 1 for d in Translation(src).hints]


def test_a_bracket_read_is_hinted_only_where_the_name_is_the_typed_one():
    # fix all turns a hinted p["msg"] into p.msg: on a dict that's an AttributeError, so a name bound again
    # by a nested def, lambda or comprehension is another variable there, and gets no hint
    src = (
        "def outer(p: (..., msg: str)):\n"
        "    def inner(p):\n"  # its own p, untyped
        "        return p['msg']\n"
        "    def typed(p: (..., msg: int)):\n"  # its own p, typed
        "        return p['msg']\n"
        "    fix = lambda p: p['msg']\n"
        "    rows = [p['msg'] for p in [{'msg': 1}]]\n"
        "    other = lambda q: p['msg']\n"  # the outer p
        "    p = (msg='again')\n"  # the parameter rebound: still its declared type
        "    p['msg'] = 'x'\n"  # a store
        "    return p['msg'], p['other']\n"
    )
    assert _hinted(src) == [5, 8, 11]


def test_a_module_name_assigned_in_a_function_is_its_local_there():
    src = (
        "x: (name: str) = (name='a')\n"
        "def f():\n"
        "    x = {'name': 1}\n"
        "    return x['name']\n"
        "def g():\n"
        "    for x in [{'name': 1}]:\n"
        "        print(x['name'])\n"
        "def h():\n"
        "    global x\n"
        "    x = (name='b')\n"
        "    return x['name']\n"
        "def k():\n"
        "    return x['name']\n"
        "type Cfg = (..., lr: float)\n"
        "def one(c: Cfg): return c['lr']\n"
        "print(x['name'], x [ 'name' ])\n"
    )
    assert _hinted(src) == [11, 13, 15, 16, 16]


def test_a_rest_whose_record_byname_cannot_tell_is_hinted():
    # its rest is (...), unchecked: the hint says how to give the record a type (transform.rest_of)
    src = (
        "type User = (name: str, age: int)\n"
        "def f(u: User, users: list[User]):\n"
        "    (name=, **known) = u\n"
        "    (name=, **unknown) = users[0]\n"
    )
    (hint,) = Translation(src).hints
    assert hint["range"]["start"] == {"line": 3, "character": 12}
    assert hint["message"].startswith("unknown is (...), any record, unchecked:")
