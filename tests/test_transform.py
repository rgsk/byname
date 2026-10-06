import ast
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from byname import source_ast, to_code, to_python, transform
from byname.transform import pattern_slot


def compile_pyn(src):
    return to_code(src, "<test>")


# --- the three forms, each on its own ---------------------------------------


def test_call_shorthand_expands_to_keyword():
    # `name=` inside a call means `name=name`
    assert transform("fn(name=, age=)\n").body == "fn(name=name, age=age)\n"


def test_call_shorthand_mixes_with_normal_args():
    # positional and explicit keywords pass through; only bare `x=` expands
    src = "fn(1, name=, age=age + 1, **kw)\n"
    assert transform(src).body == "fn(1, name=name, age=age + 1, **kw)\n"


def test_method_and_chained_calls_count_as_calls():
    # `obj.m(` and `f()(` are calls, so `x=` there is a keyword, not a record
    assert transform("obj.m(a=)\n").body == "obj.m(a=a)\n"
    assert transform("f()(a=)\n").body == "f()(a=a)\n"


def test_record_literal_becomes_generic_namedtuple():
    # a bare paren group of keywords is a record; the class goes in the prelude
    r = transform("def f():\n    return (name=, age=)\n")
    assert r.body == "def f():\n    return _rec_name__age(name=name, age=age)\n"
    lines = r.prelude.splitlines()
    at = lines.index("else:")  # the runtime class; checkers get their own version above it
    assert lines[at + 1 : at + 4] == [
        "    class _rec_name__age[T0, T1](_NT):",
        "        age: T1; name: T0",  # stored sorted by name: equality and hashing ignore the written order
        "    _byname_setup(_rec_name__age, ('name', 'age'))",  # shown in the written order
    ]
    # checkers: a function returning the exact type, a Protocol, so field order doesn't matter there either
    assert lines[at - 1] == "    def _rec_name__age[T0, T1](*, name: T0, age: T1) -> _typ_name__age[T0, T1]: ..."


def test_record_with_explicit_values():
    assert transform("x = (a=1, b=y)\n").body == "x = _rec_a__b(a=1, b=y)\n"


def test_record_prints_like_its_literal():
    # repr mirrors the syntax that built it, nesting included
    ns = {}
    src = "name, age = 'Rahul', 26\nr = (name=, age=)\nn = (user=(name=), ok=True)\n"
    exec(compile_pyn(src), ns)
    assert repr(ns["r"]) == "(name='Rahul', age=26)"
    assert repr(ns["n"]) == "(user=(name='Rahul'), ok=True)"
    assert repr(ns["r"]._replace(age=27)) == "(name='Rahul', age=27)"  # still a record after _replace


def test_records_with_same_fields_share_one_class():
    r = transform("x = (a=, b=)\ny = (a=1, b=2)\n")
    assert r.prelude.count("class _rec_") == r.prelude.count("def _rec_") == 1  # the runtime class, the checkers' version


def test_destructure_binds_fields_by_name():
    # target order is free: fields are read by attribute name
    src = "(age=, name=) = fn()\n"
    assert transform(src).body == "_ds = fn(); age = _ds.age; name = _ds.name\n"


def test_destructure_keeps_trailing_comment_and_indent():
    src = "def g():\n    (a=) = f()  # note\n"
    assert transform(src).body == "def g():\n    _ds = f(); a = _ds.a  # note\n"


def test_destructure_rename():
    # field on the left, local on the right, same as fn(name=n) and (name=n)
    src = "(name=n, age=) = res\n"
    assert transform(src).body == "_ds = res; n = _ds.name; age = _ds.age\n"


def test_destructure_rename_into_attribute_and_subscript():
    src = "(name=self.name, age=d['age']) = res\n"
    assert transform(src).body == "_ds = res; self.name = _ds.name; d['age'] = _ds.age\n"


def test_destructure_same_field_twice():
    # unlike records, a pattern may read one field into two locals
    src = "(name=, name=n) = res\n"
    assert transform(src).body == "_ds = res; name = _ds.name; n = _ds.name\n"


def test_destructure_works_on_any_object():
    # it's plain attribute access, so complex numbers, dataclasses etc. all destructure
    ns = {}
    exec(compile_pyn("(real=re, imag=im) = 3 + 4j\n"), ns)
    assert (ns["re"], ns["im"]) == (3.0, 4.0)


def test_destructure_rhs_can_itself_use_shorthand():
    src = "(a=, b=) = fn(x=)\n"
    assert transform(src).body == "_ds = fn(x=x); a = _ds.a; b = _ds.b\n"


def test_destructure_rhs_spanning_lines():
    src = "(a=) = fn(\n    x=,\n)\nz = 1\n"
    assert transform(src).body == "_ds = fn(\n    x=x,\n); a = _ds.a\nz = 1\n"


# --- things that must NOT change --------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "x = (1, 2)\n",
        "x = (a == b)\n",
        "x = (a := 1)\n",
        "f = (lambda a=1: a)\n",
        "f = (lambda a=1, b=2: a)\n",
        "def f(a=1, b=2): pass\n",
        "class C(Base, metaclass=M): pass\n",
        "x = (y for y in z)\n",
    ],
)
def test_ordinary_python_untouched(src):
    r = transform(src)
    assert r.body == src and r.prelude == ""


def test_stdlib_is_untouched():
    # every form byname adds is a SyntaxError in Python, so real code must round-trip
    lib = Path(sysconfig.get_paths()["stdlib"])
    files = sorted(lib.glob("*.py"))[:150]
    assert len(files) > 100
    for f in files:
        src = f.read_text(encoding="utf-8")
        r = transform(src, str(f))
        assert r.body == src, f
        assert r.prelude == "", f


def test_body_keeps_line_count():
    # tracebacks and editor positions rely on line N of .pyn being line N of the body
    src = "x = 1\n(a=, b=) = (a=1, b=2)\ndef f():\n    return (q=)\n"
    r = transform(src)
    assert r.body.count("\n") == src.count("\n")




# --- nested patterns ----------------------------------------------------------


def test_nested_pattern_reads_through_the_outer_field():
    src = "(id=, user=(name=n, age=)) = r\n"
    assert transform(src).body == "_ds = r; id = _ds.id; n = _ds.user.name; age = _ds.user.age\n"
    assert transform(src).prelude == ""  # the inner group is a pattern, not a record literal


def test_nested_pattern_runs_at_any_depth():
    src = (
        "r = (id=1, user=(name='Rahul', home=(city='Pune',)))\n"
        "(id=, user=(name=, home=(city=c))) = r\n"
    )
    ns = {}
    exec(compile_pyn(src), ns)
    assert (ns["id"], ns["name"], ns["c"]) == (1, "Rahul", "Pune")


def test_parenthesised_name_is_still_a_target():
    # `(a)` holds no `field=`, so it's just a parenthesised local
    assert transform("(x=(a)) = r\n").body == "_ds = r; (a) = _ds.x\n"


def test_nested_pattern_in_for_target():
    src = "for (id=, user=(name=, age=a)) in rows: pass\n"
    body = "for (id, (name, a)) in ((_ds.id, (_ds.user.name, _ds.user.age)) for _ds in rows): pass\n"
    assert transform(src).body == body
    ns = {}
    exec(compile_pyn("rows = [(id=1, user=(name='x', age=2))]\nfor (user=(age=)) in rows: pass\n"), ns)
    assert ns["age"] == 2


def test_nested_half_typed_item_reads_through_the_chain():
    r = transform("(user=(name=, ag)) = r\n", tolerant=True)
    assert r.body == "_ds = r; name = _ds.user.name; _ds.user.ag\n"


def test_nested_field_spans_cover_inner_names():
    src = "(user=(name=)) = r\n"
    assert [src[a:b] for a, b in transform(src).fields] == ["user", "name"]

# --- for-loop targets ---------------------------------------------------------


def test_for_target_destructures_each_item():
    # one line, so line numbers hold; the generator keeps field types
    src = "for (name=, age=a) in rows:\n    pass\n"
    assert transform(src).body == "for (name, a) in ((_ds.name, _ds.age) for _ds in rows):\n    pass\n"


def test_for_target_single_field_needs_no_tuple():
    assert transform("for (x=) in pts: print(x)\n").body == "for (x) in (_ds.x for _ds in pts): print(x)\n"


def test_for_target_trailing_comma_keeps_the_tuple_shape():
    # `(age=,)` leaves the target `(age,)`, a 1-tuple, so each element must be one too
    src = "for (age=,) in rows: pass\nfor (u=(age=,)) in rows: pass\n"
    body = "for (age,) in ((_ds.age,) for _ds in rows): pass\nfor ((age,)) in ((_ds.u.age,) for _ds in rows): pass\n"
    assert transform(src).body == body
    ns = {}
    exec(compile_pyn("out = []\nfor (age=,) in [(age=1), (age=2)]:\n    out.append(age)\n"), ns)
    assert ns["out"] == [1, 2]


def test_for_target_runs():
    src = (
        "rows = [(name='a', age=1), (name='b', age=2)]\n"
        "out = []\n"
        "for (age=, name=n) in rows:\n"
        "    out.append((n, age))\n"
        "pairs = [(n, a) for (name=n, age=a) in rows if a > 1]\n"
        "d = {}\n"
        "for (name=d['k']) in rows: pass\n"
    )
    ns = {}
    exec(compile_pyn(src), ns)
    assert ns["out"] == [("a", 1), ("b", 2)]
    assert ns["pairs"] == [("b", 2)]
    assert ns["d"] == {"k": "b"}


def test_for_target_iterable_ends_at_the_right_place():
    cases = {
        # ternary in a for statement belongs to the iterable
        "for (a=) in f(n=) if c else xs:\n    pass\n": "for (a) in (_ds.a for _ds in f(n=n) if c else xs):\n    pass\n",
        # a bare tuple gets parenthesised
        "for (a=) in xs, ys:\n    pass\n": "for (a) in (_ds.a for _ds in (xs, ys)):\n    pass\n",
        # comprehension: stops at the next clause / closing bracket
        "s = [a for (a=) in xs if a for q in r]\n": "s = [a for (a) in (_ds.a for _ds in xs) if a for q in r]\n",
        "d = {k: v for (k=, v=) in items}\n": "d = {k: v for (k, v) in ((_ds.k, _ds.v) for _ds in items)}\n",
    }
    for src, expected in cases.items():
        assert transform(src).body == expected


def test_async_for_target_uses_an_async_generator():
    src = "async def f():\n    async for (a=) in g(): pass\n"
    assert transform(src).body == "async def f():\n    async for (a) in (_ds.a async for _ds in g()): pass\n"


def test_for_target_half_typed_item_reads_the_field():
    r = transform("for (name=, ag) in rows: pass\n", tolerant=True)
    assert r.body == "for (name, _) in ((_ds.name, _ds.ag) for _ds in rows): pass\n"
    with pytest.raises(SyntaxError, match="'ag' needs '='"):
        transform("for (name=, ag) in rows: pass\n")


def test_plain_for_tuple_untouched():
    assert transform("for (a, b) in xs: pass\n").body == "for (a, b) in xs: pass\n"

# --- errors ------------------------------------------------------------------


def test_destructure_target_must_be_assignable():
    # `1` and `f()` can't be assigned to, at any depth
    for src in ["(a=1) = f()\n", "(a=g()) = f()\n", "(x=(a=1)) = f()\n"]:
        with pytest.raises(SyntaxError, match="cannot bind field 'a'"):
            transform(src)


def test_bare_name_in_pattern_is_an_error_when_running():
    # `(name=, age) = r` is a half-written pattern; running it says what's missing
    with pytest.raises(SyntaxError, match="'age' needs '='"):
        transform("(name=, age) = r\n")


def test_bare_names_without_any_field_stay_tuple_unpacking():
    # `(a, b) = r` is ordinary Python; only patterns with at least one `x=` are byname's
    assert transform("(a, b) = r\n").body == "(a, b) = r\n"


def test_tolerant_mode_turns_half_typed_item_into_attribute_access():
    # editor-only: `ag` becomes `_ds.ag`, so the checker completes field names there
    r = transform("(name=, ag) = r\n", tolerant=True)
    assert r.body == "_ds = r; name = _ds.name; _ds.ag\n"
    assert r.problems == [(8, 10, "pattern item 'ag' needs '='")]


@pytest.mark.parametrize(
    "src, expected",
    [
        ("(greeting=greet, |) = r\n", ("", ["greeting"])),          # empty slot after a comma
        ("(gree|) = r\n", ("gree", [])),                           # first item: plain Python so far
        ("(age=, gr|) = r\n", ("gr", ["age"])),                     # bare name being typed
        ("(gre|eting=g) = r\n", ("greeting", [])),                  # renaming the field of an item
        ("for (age=, |) in rows:\n", ("", ["age"])),                # for-loop target
        ("(id=, user=(name=, |)) = r\n", ("", ["name"])),           # nested group
    ],
)
def test_pattern_slot_finds_field_positions(src, expected):
    at = src.index("|")
    src = src.replace("|", "")
    ws, we, close, listed = pattern_slot(src, at)
    assert (src[ws:we], listed) == expected
    assert src[close] == ")"


@pytest.mark.parametrize(
    "src",
    [
        "(a=, b=x|) = r\n",  # on the target side of `=`
        "fn(a=, |)\n",       # a call, not a pattern
        "(a=, |)\n",         # a record literal, not a pattern
        "x = (a=, |) = r\n", # not at statement start
        "for (a=, b=x|) in r:\n",  # target side, in a for
        "(a=d[(|)]) = r\n",          # a bracket in a target, not a nested pattern
    ],
)
def test_pattern_slot_ignores_non_field_positions(src):
    at = src.index("|")
    assert pattern_slot(src.replace("|", ""), at) is None


def test_underscore_field_rejected():
    # NamedTuple forbids fields starting with '_'
    with pytest.raises(SyntaxError, match="_x"):
        transform("x = (_x=1)\n")


def test_duplicate_field_rejected():
    with pytest.raises(SyntaxError, match="duplicate"):
        transform("x = (a=1, a=2)\n")


# --- end to end --------------------------------------------------------------


def test_to_python_puts_prelude_after_future_imports():
    src = '"""doc"""\nfrom __future__ import annotations\nx = (a=1)\n'
    out = to_python(src)
    assert out.splitlines()[:3] == ['"""doc"""', "from __future__ import annotations", "from typing import NamedTuple as _NT"]


def test_output_files_read_records_by_name_only_too():
    # what judges run: the same runtime rules as `byname run`
    ns = {}
    exec(to_python("r = (name='a', age=1)\nk = 'name' in r\n", portable=True), ns)
    assert ns["k"] is True
    with pytest.raises(TypeError, match="read by name only"):
        list(ns["r"])
    with pytest.raises(TypeError, match="'<' not supported"):
        sorted([ns["r"], ns["r"]])


def test_a_returned_tuple_is_labelled_for_the_checker_only():
    src = "def f(n):\n    lo = n\n    return lo, n + 1\ndef g() -> tuple[int, int]:\n    a = 1\n    return a, a\n"
    r = transform(src, checker=True)
    assert r.body.splitlines()[2] == "    return _byname_tup_lo__(lo, n + 1)"
    assert r.body.splitlines()[5] == "    return a, a"  # annotated: left alone
    assert "class _tup_lo__[T0, T1](tuple[T0, T1]): ..." in r.prelude
    assert transform(src).body == src and transform(src).prelude == ""  # what runs: untouched


def test_import_hook_runs_pyn_modules(tmp_path):
    (tmp_path / "people.pyn").write_text(
        "def make(*, name: str, age: int):\n"
        "    greeting = f'hi {name}'\n"
        "    return (name=, age=, greeting=)\n"
    )
    (tmp_path / "main.py").write_text(
        "import byname\n"
        "exec(byname.to_code(open('app.pyn').read(), 'app.pyn'))\n"
    )
    (tmp_path / "app.pyn").write_text(
        "from people import make\n"
        "name, age = 'Rahul', 26\n"
        "res = make(name=, age=)\n"
        "(greeting=, age=) = res\n"
        "print(greeting, age, res)\n"
    )
    out = subprocess.run([sys.executable, "main.py"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert out.stdout == "hi Rahul 26 (name='Rahul', age=26, greeting='hi Rahul')\n"


def test_traceback_points_at_pyn_line(tmp_path):
    # prelude is spliced in at the AST level, so body line numbers are unchanged
    (tmp_path / "bad.pyn").write_text("x = (a=1)\n\n\nraise ValueError(x.a)\n")
    out = subprocess.run(
        [sys.executable, "-m", "byname", "run", "bad.pyn"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert 'bad.pyn", line 4' in out.stderr


def test_show_no_main_drops_local_test_block():
    # what Alt+C writes for LeetCode: the solution only, no `if __name__ == "__main__":` runner
    from byname.output import drop_main

    code = 'def f():\n    return 1\n\n\nif __name__ == "__main__":\n    print(f())\n    print(2)\n'
    assert drop_main(code) == "def f():\n    return 1\n"
    assert drop_main("x = 1\n") == "x = 1\n"  # nothing to drop


def test_divider_marks_where_the_users_code_starts():
    # `byname show` output: generated header, then a divider naming the source, then the .pyn
    out = to_python("# my note\nx = (a=1)\n", divider="# ---- f.pyn ----")
    lines = out.splitlines()
    assert lines[lines.index("# ---- f.pyn ----") + 1] == "# my note"
    assert to_python("x = 1\n", divider="# ---- f.pyn ----") == "# ---- f.pyn ----\nx = 1\n"  # no header: divider anyway


def test_output_files_run_on_old_pythons():
    from byname.output import render

    # judges run older Pythons (Codeforces/CSES PyPy is 3.10), which can't parse `class R[T0](...)`.
    # Output files get plain NamedTuple classes; the checker's translation keeps the generic ones.
    src = "def f(*, n: int):\n    return (n=, sq=n * n)\n(sq=) = f(n=3)\nprint(sq)\n"
    out = render(src, Path("f.pyn"))
    assert "class _rec_n__sq(_NT):\n    n: object; sq: object; __class_getitem__ = classmethod(lambda cls, _: cls)\n" in out
    ast.parse(out, feature_version=(3, 8))
    assert "class _rec_n__sq[T0, T1](_NT):" in to_python(src)
    with pytest.raises(SyntaxError):
        ast.parse(to_python(src), feature_version=(3, 8))
    assert "TYPE_CHECKING" not in out and "_dct_" not in out  # the checker-only typed methods stay out of output files


def test_replace_and_asdict_are_typed_for_the_checker_only():
    # NamedTuple types them `(**kwargs: Any)` / `dict[str, Any]`; the checker gets per-field versions,
    # on the record's exact type (see record_def)
    r = transform("x = (name=, age=)\n")
    assert "    def _replace(self, *, name: T0 = ..., age: T1 = ...) -> _S: ..." in r.prelude
    assert "    def _asdict(self) -> _dct_name__age[T0, T1]: ..." in r.prelude
    assert "class _dct_name__age[T0, T1](_TD):" in r.prelude
    ns = {}
    exec(compile_pyn("r = (name='a', age=1)\nr2 = r._replace(age=2)\nd = r._asdict()\n"), ns)
    assert repr(ns["r2"]) == "(name='a', age=2)" and ns["d"] == {"name": "a", "age": 1}


SPREADS = """\
def fn(name: str, age: int, sex: str, surname: str):
    return (name, age, sex, surname)


type Person = (name: str, age: int, sex: str, surname: str)
u = (age=26, name="rahul")
r = (sex="male", surname="gupta", name="mehak")
called = fn(**u, **(sex="m", surname="g"))
merged = {**u, **r}
both = (**u, **r)
override, first = (**u, age=27), (age=27, **u)
p: Person = (**u, **r)
q = (name: str, age: int, sex: str, surname: str)(**u, **r)


def mk() -> Person:
    return (**u, **r)
"""


def test_records_spread_with_double_star():
    # records are mappings by field name at runtime: f(**rec), {**rec}; (**u, **r) builds a record, later wins
    ns = {}
    exec(compile_pyn(SPREADS), ns)
    assert ns["called"] == ("rahul", 26, "m", "g")
    assert ns["merged"] == {"age": 26, "name": "mehak", "sex": "male", "surname": "gupta"}
    assert repr(ns["both"]) == "(age=26, name='mehak', sex='male', surname='gupta')"
    assert (ns["override"].age, ns["first"].age) == (27, 26)  # a later field wins, wherever it is
    assert ns["p"] == ns["q"] == ns["mk"]() == ns["both"]
    assert ns["p"].surname == "gupta" and ns["u"]["name"] == "rahul" and list(ns["u"].keys()) == ["age", "name"]


def test_spreads_leave_valid_python_alone_at_runtime():
    # `**kw` is only wrapped for the checker; what runs is the source as written
    src = "f(**kw)\nd = {**a, **b}\n"
    assert transform(src).body == src and transform(src).prelude == ""
    assert transform(src, checker=True).body == "f(**_byname_kw(kw))\nd = {**_byname_kw(a), **_byname_kw(b)}\n"


def test_spread_records_are_checked_against_the_expected_type():
    # when a spread's fields can't be read off the file (here `u` comes from a call), the checker's build
    # takes the type expected where it stands (argument, annotation, return), see CTX_PRELUDE; an inline
    # type `(name: str)(**u)` is a cast. What runs is just _byname_rec
    r = transform(SPREADS.replace('u = (age=26, name="rahul")', "u = load()"), checker=True)
    # a spread name is read through a lambda default, so it keeps its narrowing (pyright drops it inside a
    # lambda for a name reassigned later); the user's `u` stays in place, picked over by the narrowed copy
    ctx = (
        "_byname_ctx(lambda _byname_t, _byname_s0=_byname_kw(u), _byname_s1=_byname_kw(r): "
        "_byname_check(lambda: _byname_t)({**_byname_pick(u, _byname_s0), **_byname_pick(r, _byname_s1)}))"
    )
    assert f"p: Person = {ctx}" in r.body and f"    return {ctx}" in r.body
    assert "q = _cast(_typ_name__age__sex__surname[str, int, str, str], _byname_rec(_byname_check(" in r.body
    assert "def _byname_ctx[T](" in r.prelude
    r = transform(SPREADS)
    assert "p: Person = _byname_rec({**u, **r})" in r.body and "both = _byname_rec({**u, **r})" in r.body
    assert "_byname_ctx" not in r.prelude + r.body


def test_spread_records_of_known_names_are_plain_records():
    # for the checker, a spread of names bound once to a record (or annotated with a record type) is built
    # field by field, so it has its exact type with no annotation; a later field wins and keeps the first
    # one's position, as at runtime. What runs doesn't change
    body = transform(SPREADS, checker=True).body
    assert "both = _rec_age__name__sex__surname(age=u.age, name=r.name, sex=r.sex, surname=r.surname)" in body
    assert "override, first = _rec_age__name(name=u.name, age=27), _byname_ctx(" in body  # `(age=27, **u)`: generic
    assert "p: Person = _rec_age__name__sex__surname(age=u.age" in body
    assert "    return _rec_age__name__sex__surname(" in body
    src = (
        "type T = (a: int, b: str)\n"
        "def f(c: T, d):\n"
        "    x = (**c, z=1)\n"            # a parameter's record type
        "    y = (**d, z=1)\n"            # no type: unknown
        "t: T = (a=1, b='x')\n"
        "n = (**t,)\n"
        "again = (**n, b='y')\n"          # a spread of a spread
        "w = (a=1,)\n"
        "w = (a=1, b=2)\n"
        "rebound = (**w,)\n"              # `w` is bound twice: unknown
    )
    lines = transform(src, checker=True).body.splitlines()
    assert lines[2] == "    x = _rec_a__b__z(a=c.a, b=c.b, z=1)"
    assert "_byname_ctx(" in lines[3] and "_byname_ctx(" in lines[9]
    assert lines[6] == "again = _rec_a__b(a=n.a, b='y')"
    assert "_rec_" not in transform(SPREADS).body.split("both = ")[1].split("\n")[0]  # runtime: _byname_rec


def test_spread_records_as_call_arguments_fall_back_to_a_record():
    # a whole call argument gets _byname_arg (unknown type: a record, so a non-record overload rejects it);
    # anywhere else _byname_ctx (unknown type: Any)
    src = "f((**u))\nf(1, k=(**u))\no.m(a, (**u), b)\nx = (**u)\nf([(**u)])\nf((**u).a)\nt = ((**u), 1)\n"
    lines = transform(src, checker=True).body.splitlines()
    assert [ln.count("_byname_arg(") for ln in lines] == [1, 1, 1, 0, 0, 0, 0]
    assert [ln.count("_byname_ctx(") for ln in lines] == [0, 0, 0, 1, 1, 1, 1]


def test_spread_record_errors():
    with pytest.raises(SyntaxError, match="duplicate record field"):
        transform("x = (**u, a=1, a=2)\n")
    with pytest.raises(SyntaxError, match="takes `\\*\\*record` and `name=value`"):
        transform("x = (**u, 3)\n")
    with pytest.raises(SyntaxError, match="'keys' is reserved"):
        transform("x = (keys=1)\n")


def test_spread_records_run_on_old_pythons():
    from byname.output import render

    src = SPREADS.replace("type Person = ", "Person = ")  # `type` statements need 3.12
    out = render(src + "print(both, p.surname, fn(**p))\n", Path("s.pyn"))
    ast.parse(out, feature_version=(3, 8))
    p = subprocess.run([sys.executable, "-c", out], capture_output=True, text=True, check=True)
    assert p.stdout == "(age=26, name='mehak', sex='male', surname='gupta') gupta ('mehak', 26, 'male', 'gupta')\n"

def test_record_type_annotation():
    # `(name: type, ...)` in an annotation is the record's type, written the way hover shows it.
    # It lets a recursive function declare what it returns: checkers can't infer through recursion
    src = "def dfs(n: int) -> (h: int, d: int):\n    if n == 0:\n        return (h=0, d=0)\n    (h=, d=) = dfs(n - 1)\n    return (h=h + 1, d=d)\n"
    out = to_python(src)
    # a Protocol, not the record class: explicit types match records whatever their field order
    assert "def dfs(n: int) -> _typ_h__d[int, int]:" in out
    assert "class _typ_h__d[T0, T1](_PR):" in out
    # nested, and as a parameter / variable annotation
    out = to_python("def f(p: (x: (a: int, b: str))) -> None:\n    q: (y: int) = (y=1)\n")
    assert "def f(p: _typ_x[_typ_a__b[int, str]]) -> None:" in out
    assert "q: _typ_y[int] = _rec_y(y=1)" in out


def test_open_record_type():
    # a leading `...`: any record with at least these fields; no field-set marker, no tuple access
    out = to_python("def f(p: (..., age: int)) -> int:\n    return p.age\n")
    assert "def f(p: _opn_age[int]) -> int:" in out
    assert "class _opn_age[T0](_PR):" in out
    assert "_byname_fieldset" not in out.split("class _opn_age")[1]
    # read unchecked by any str key, never a position
    assert "    def __getitem__(self, k: _key_age, /) -> _Ay: ..." in out.split("class _opn_age")[1]
    assert "type _key_age = str" in out
    assert "    def keys(self) -> tuple[str, ...]: ..." in out.split("class _opn_age")[1]  # `**p`, `dict(p)`
    # split over lines, as ruff leaves a long one
    out = to_python("def f(\n    p: (\n        ...,\n        age: int,\n    ),\n): ...\n")
    assert "p: _opn_age[\n" in out and "..." not in out.split("p: _opn_age[")[1].split("]")[0]


def test_open_record_type_with_no_fields():
    # `(...)`: a record with any fields, where a type stands; `(...,)` too
    for src, want in [
        ("x: (...) = (a=1)\n", "x: _opn_ = "),
        ("x: (...,) = (a=1)\n", "x: _opn_ = "),
        ("self.x: (...) = (a=1)\n", "self.x: _opn_ = "),
        ("def f(p: (...)) -> (...): ...\n", "def f(p: _opn_) -> _opn_: ..."),
        ("def f(p: (q: (...))): ...\n", "def f(p: _typ_q[_opn_]): ..."),
        ("type R = (...)\n", "type R = _opn_"),
    ]:
        out = to_python(src)
        assert want in out, src
        assert "class _opn_(_PR):" in out
    # elsewhere it's Python's Ellipsis, left as written
    for src in ["x = (...)\n", "y = (..., 1)\n", "a[(...)]\n", "f(x=(...))\n", "if x: (...)\n", "d = {1: (...)}\n"]:
        assert to_python(src) == src


def test_open_record_type_dots_go_first():
    for src in ["def f(p: (age: int, ...)): ...\n", "def f(p: (..., a: int, ...)): ...\n", "x: (a: int, ..., b: int)\n"]:
        with pytest.raises(SyntaxError, match="`...` goes first in an open record type"):
            to_python(src)


def test_records_carry_their_sorted_field_set():
    # what an exact record type matches on: same names, any order
    r = transform("x = (b=1, a=2)\n")
    assert "class _typ_b__a[T0, T1](_PR):" in r.prelude  # in the checker a record is its exact type (see record_def)
    assert "    def _byname_fieldset(self) -> _L['a,b']: ..." in r.prelude


def test_record_type_runs_on_old_pythons():
    # output files: the plain class ignores the subscript, so the annotation evaluates on PyPy 3.10 too
    from byname.output import render

    src = (
        "def f() -> (a: int):\n    return (a=1)\n\n\ndef g(p: (..., a: int)) -> int:\n    return p.a\n\n\n"
        "print(f(), f.__annotations__['return'].__name__, g(f()))\n"
    )
    out = render(src, Path("f.pyn"))
    ast.parse(out, feature_version=(3, 8))
    assert "Protocol" not in out  # the plain stand-in classes only
    p = subprocess.run([sys.executable, "-c", out], capture_output=True, text=True, check=True)
    assert p.stdout == "(a=1) _typ_a 1\n"


def test_record_type_errors():
    with pytest.raises(SyntaxError, match="cannot start with '_'"):
        to_python("def f() -> (_a: int): ...\n")
    with pytest.raises(SyntaxError, match="duplicate"):
        to_python("def f() -> (a: int, a: str): ...\n")
    # not record types: lambdas, def parameter lists, walrus
    assert to_python("f = (lambda x: x)\n") == "f = (lambda x: x)\n"
    assert to_python("def g(a: int, b: str): ...\n") == "def g(a: int, b: str): ...\n"
    assert to_python("if (n := 3): ...\n") == "if (n := 3): ...\n"


def test_field_spans_cover_every_field_name():
    # the editor colours these as properties, in all three forms; shorthand call arguments aren't fields
    src = "def f(*, n: int) -> (h: int, d: int):\n    return (h=n, d=n)\n\n\n(h=, d=x) = f(n=)\n"
    r = transform(src)
    assert [src[s:e] for s, e in r.fields] == ["h", "d", "h", "d", "h", "d"]
    assert all(src[s - 1] == "(" or src[s - 2] == "," for s, _ in r.fields)


PARAMS = '''type User = (name: str, age: int)


def f((name=, age=): User):
    if age > 1:
        return name + "!"
    return name


def g(self, (name=n, addr=(city=)): (name: str, addr: (city: str)), k: int = 0) -> str:
    """doc"""
    return f"{n} {city} {k}"


def h((name=, age=)): return name


def two((name=): User, (age=) = (age=3)):
    return (name, age)


def bad((nme=): User):
    return nme
'''


def test_parameter_patterns_destructure_arguments():
    ns = {}
    calls = "u = (name='r', age=5)\nout = f(u), h(u), g(0, (name='a', addr=(city='b'))), two(u), two(u, (age=9))\n"
    exec(compile_pyn(PARAMS + calls), ns)
    assert ns["out"] == ("r!", "r", "a b 0", ("r", 3), ("r", 9))
    assert ns["g"].__doc__ == "doc"  # the unpacking goes after a docstring


def test_parameter_patterns_translation():
    body = transform(PARAMS).body
    # a line of its own before the body, so it may start with `if`/`for`/`try`; before a one-line body
    assert "def f(_byname_p0: User):\n    _ds = _byname_p0; name = _ds.name; age = _ds.age\n    if age > 1:" in body
    assert '    """doc"""\n    _ds = _byname_p1; n = _ds.name; city = _ds.addr.city\n' in body
    assert "def h(_byname_p2): _ds = _byname_p2; name = _ds.name; age = _ds.age; return name" in body
    assert "def two(_byname_p3: User, _byname_p4 = _rec_age(age=3)):\n    _ds = _byname_p3; name = _ds.name; _ds = _byname_p4; age = _ds.age\n" in body
    assert transform("def f[T]((a=): T): return a\n").body == "def f[T](_byname_p0: T): _ds = _byname_p0; a = _ds.a; return a\n"


def test_parameter_patterns_keep_line_numbers():
    # the generated lines count as the `def` line: tracebacks point into the .pyn as written
    ns = {}
    src = PARAMS + "bad((name='r', age=1))\n"
    with pytest.raises(AttributeError) as e:
        exec(to_code(src, "p.pyn"), ns)
    lines = [fr.lineno for fr in e.traceback if fr.frame.code.path == "p.pyn"]
    assert [ln + 1 for ln in lines] == [src.splitlines().index("bad((name='r', age=1))") + 1, src.splitlines().index("def bad((nme=): User):") + 1]
    code = to_code("def f((a=)):\n    x = 1\n    raise ValueError(a)\n\n\nf((a=1))\n", "q.pyn")
    with pytest.raises(ValueError) as e:
        exec(code, {})
    assert [fr.lineno + 1 for fr in e.traceback if fr.frame.code.path == "q.pyn"] == [6, 3]


def test_source_ast_has_the_pyn_line_numbers():
    # for tools matching checker output to source: the parameter pattern's generated line counts as
    # the `def`, so the function spans its .pyn lines and the statements after it keep theirs
    src = "x = 1\ndef f((a=, b=)):\n    y = a\n    return y\n\n\nz: int = f((a=1, b=2))\n"
    tree = source_ast(src)
    f = tree.body[1]
    assert isinstance(f, ast.FunctionDef) and (f.lineno, f.end_lineno) == (2, 4)
    assert [s.lineno for s in f.body][-2:] == [3, 4]
    assert tree.body[2].lineno == 7 and isinstance(tree.body[2], ast.AnnAssign)
    assert not any(isinstance(s, ast.ImportFrom) for s in tree.body)  # no prelude


def test_parameter_pattern_runs_on_old_pythons():
    from byname.output import render

    out = render(PARAMS.replace("type User = ", "User = ") + "print(f((name='r', age=5)), two((name='t', age=1)))\n", Path("p.pyn"))
    ast.parse(out, feature_version=(3, 8))
    p = subprocess.run([sys.executable, "-c", out], capture_output=True, text=True, check=True)
    assert p.stdout == "r! ('t', 3)\n"


NAMED = """type User = (name: str, age: int)


def fn(*, user=(name=, age=): User):
    return (name, age, user.age)


def h(user=(name=): User = (name="rahul", age=1)): return name


def k(user=(name=n) = (name="untyped")): return n


def rec(user=(name="x", age=30)): return user.age
"""


def test_named_parameter_patterns():
    # `user=(...)`: a parameter called `user`, destructured; it can be passed by keyword
    ns = {}
    exec(compile_pyn(NAMED + "out = fn(user=(name='r', age=5)), h(), h(user=(name='z', age=2)), k(), rec()\n"), ns)
    assert ns["out"] == (("r", 5, 5), "rahul", "z", "untyped", 30)
    body = transform(NAMED).body
    assert "def fn(*, user: User):\n    _ds = user; name = _ds.name; age = _ds.age\n" in body
    assert 'def h(user: User = _rec_name__age(name="rahul", age=1)): _ds = user; name = _ds.name; return name' in body
    assert 'def k(user = _rec_name(name="untyped")): _ds = user; n = _ds.name; return n' in body
    # values that can't be pattern targets: a default record, as in Python
    assert 'def rec(user=_rec_name__age(name="x", age=30)): return user.age' in body


def test_no_shorthand_in_def_signatures():
    # in a signature `name=` would mean the outer `name`: a group of pattern items is a pattern, and a
    # record default mixing `name=` with values is an error
    with pytest.raises(SyntaxError, match=r"name= in a def signature: give the value"):
        transform("def f(user=(name=, age=30)): pass\n")
    with pytest.raises(SyntaxError, match=r"city= in a def signature"):
        transform("def f(user=(id=1, addr=(city=))): pass\n")
