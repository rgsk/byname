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
    assert t.hidden.splitlines()[:2] == [
        "from typing import NamedTuple as _NT",
        "class _rec_name__age[T0, T1](_NT): name: T0; age: T1",
    ]
    assert "_ds = make(name=\"R\", age=1); who = _ds.name; age = _ds.age" in t.hidden


# --- source -> hidden: where requests (hover, completion) land ----------------


def test_plain_code_maps_past_prelude():
    # `print` is untouched source; it moves down by the 2 prelude lines
    t = Translation(SRC)
    h = t.position_to_hidden(at(SRC, "print"))
    assert h["line"] == at(SRC, "print")["line"] + 2
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


def test_diagnostic_on_destructured_attribute_shows_on_field():
    # pyright flags `nope` in `_ds.nope`; the user sees it on `nope` in their pattern
    src = "(nope=) = obj\n"
    t = Translation(src)
    line = t.hidden.splitlines()[0]
    assert line == "_ds = obj; nope = _ds.nope"
    c = line.rindex("nope")
    gen = {"start": {"line": 0, "character": c}, "end": {"line": 0, "character": c + 4}}
    r = t.range_from_hidden(gen, display=True)
    assert r == word(src, "nope")


def test_shorthand_target_rename_inserts_explicit_target():
    # renaming the local bound by `(name=)` must give `(name=new)`, not `(new=)`
    src = "(name=) = obj\n"
    t = Translation(src)
    line = t.hidden.splitlines()[0]
    assert line == "_ds = obj; name = _ds.name"
    c = line.index("; name") + 2
    gen = {"start": {"line": 0, "character": c}, "end": {"line": 0, "character": c + 4}}
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
