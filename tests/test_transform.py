import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from byname import to_code, to_python, transform


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
    assert r.prelude == (
        "from typing import NamedTuple as _NT\n"
        "class _rec_name__age[T0, T1](_NT): name: T0; age: T1\n"
    )


def test_record_with_explicit_values():
    assert transform("x = (a=1, b=y)\n").body == "x = _rec_a__b(a=1, b=y)\n"


def test_records_with_same_fields_share_one_class():
    r = transform("x = (a=, b=)\ny = (a=1, b=2)\n")
    assert r.prelude.count("class ") == 1


def test_destructure_binds_fields_by_name():
    # target order is free: fields are read by attribute name
    src = "(age=, name=) = fn()\n"
    assert transform(src).body == "_ds = fn(); age = _ds.age; name = _ds.name\n"


def test_destructure_keeps_trailing_comment_and_indent():
    src = "def g():\n    (a=,) = f()  # note\n"
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
    src = "(a=,) = fn(\n    x=,\n)\nz = 1\n"
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
    src = "x = 1\n(a=, b=) = (a=1, b=2)\ndef f():\n    return (q=,)\n"
    r = transform(src)
    assert r.body.count("\n") == src.count("\n")


# --- errors ------------------------------------------------------------------


def test_destructure_target_must_be_assignable():
    # `1` and `f()` can't be assigned to; nested patterns aren't supported yet
    for src in ["(a=1) = f()\n", "(a=g()) = f()\n", "(a=(b=,)) = f()\n"]:
        with pytest.raises(SyntaxError, match="cannot bind field 'a'"):
            transform(src)


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
        "print(greeting, age, tuple(res))\n"
    )
    out = subprocess.run([sys.executable, "main.py"], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert out.stdout == "hi Rahul 26 ('Rahul', 26, 'hi Rahul')\n"


def test_traceback_points_at_pyn_line(tmp_path):
    # prelude is spliced in at the AST level, so body line numbers are unchanged
    (tmp_path / "bad.pyn").write_text("x = (a=1)\n\n\nraise ValueError(x.a)\n")
    out = subprocess.run(
        [sys.executable, "-m", "byname", "run", "bad.pyn"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert 'bad.pyn", line 4' in out.stderr
