"""`byname tool` runs real tools on a throwaway two-file project and maps their output back to .pyn."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PEOPLE = """\
def make(*, name: str, age: int):
    greeting = f"hi {name}"
    return (name=, age=, greeting=)
"""

MAIN = """\
from people import make

res = make(name="R", age=1)
(nope=, age=) = res
x: int = "s"
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "people.pyn").write_text(PEOPLE)
    (tmp_path / "main.pyn").write_text(MAIN)
    return tmp_path


def tool(project: Path, *args: str) -> tuple[str, int]:
    bindir = Path(sys.executable).parent
    if not (bindir / args[0]).exists():
        pytest.skip(f"{args[0]} not installed (uv sync)")
    env = {**os.environ, "XDG_CACHE_HOME": str(project / ".cache")}
    p = subprocess.run(
        [sys.executable, "-m", "byname", "tool", *args],
        cwd=project, env=env, capture_output=True, text=True, check=False,
    )
    return p.stdout, p.returncode


def test_basedpyright_errors_land_on_pyn_lines(project):
    # line 4 col 2 is `nope` in `(nope=, age=)`; the type shows as fields, not _rec_...
    out, rc = tool(project, "basedpyright", "main.pyn")
    assert rc == 1
    assert 'main.pyn:4:2 - error: Cannot access attribute "nope" for class "(name: str, age: int, greeting: str)"' in out
    assert "main.pyn:5:10 - error" in out
    assert ".cache" not in out and "_rec_" not in out  # no mirror paths, no generated names


def test_mixed_record_list_stays_typed(project):
    # without strict list inference a mixed list is list[Unknown] and the typo below passes silently
    (project / "mixed.pyn").write_text('rs = [(name="a", age="90"), (name="b", age=23)]\nrs[0]._replace(nme="x")\n')
    out, rc = tool(project, "basedpyright", "mixed.pyn")
    assert 'mixed.pyn:2:16 - error: No parameter named "nme"' in out


def test_explicit_record_types_ignore_order_but_not_field_set(project):
    # explicit types match any order, but exactly these fields; a record is read by name, never by position
    (project / "types.pyn").write_text(
        "type User = (name: str, age: int)\n"
        "u: User = (age=26, name='R')\n"            # order differs: fine
        "x: User = (age=26, name='R', po='d')\n"    # extra field
        "def f() -> User:\n"
        "    return (age=1, name='R')\n"
        "a, b = f()\n"                              # positional: an error, typed or not
        "(name=) = f()\n"
        "c, d = (age=1, name='R')\n"
        "reveal_type(name)\n"
        "def g(p: (age: int, ...)) -> int:\n"
        "    return p.age\n"
        "g(u); g((name='R',))\n"                    # open type: at least `age`
    )
    out, rc = tool(project, "basedpyright", "types.pyn")
    assert "types.pyn:2" not in out
    assert 'types.pyn:3:11 - error: Type "(age: int, name: str, po: str)" is not assignable to declared type "User"' in out
    assert "extra field: po (reportAssignmentType)" in out and "_byname_fieldset" not in out
    assert 'types.pyn:6:8 - error: "User" is not iterable' in out or 'types.pyn:6:8 - error: "(name: str, age: int)" is not iterable' in out
    assert "types.pyn:7" not in out
    assert 'types.pyn:8:8 - error: "(age: int, name: str)" is not iterable' in out
    assert 'types.pyn:9:13 - information: Type of "name" is "str"' in out
    assert "types.pyn:12:9 - error" in out and "types.pyn:12:1 " not in out


def test_field_set_errors_between_record_types_point_the_right_way(project):
    # between two record types pyright also compares them in reverse; extra vs missing must not flip
    (project / "dirs.pyn").write_text(
        "type User = (name: str, age: int)\n"
        "def fuu() -> User:\n"
        "    return (name='r', age=1)\n"
        "v = fuu()\n"
        "k: (name: str) = v\n"
        "q: (name: str, age: int, x: int) = v\n"
    )
    out, rc = tool(project, "basedpyright", "dirs.pyn")
    k, q = out.split("dirs.pyn:5:")[1], out.split("dirs.pyn:6:")[1]
    assert "extra field: age" in k.split("dirs.pyn:")[0] and "missing" not in k.split("dirs.pyn:")[0]
    assert "missing field: x" in q and "extra" not in q


def test_field_set_errors_on_long_records_name_the_fields(project):
    # pyright cuts literals over 50 chars ('...vocab_si…'), so the field sets come from the class names
    (project / "long.pyn").write_text(
        "type Cfg = (vocab_size: int, block_size: int, n_embed: int, n_head: int, n_layer: int)\n"
        "a: Cfg = (vocab_size=65, block_size=32, n_embed=64, n_head=4, n_layer=3, dropout=0.1)\n"
        "b: Cfg = (vocab_size=65, block_size=32, n_embed=64, n_head=4, dropout_rate=0.1)\n"
    )
    out, rc = tool(project, "basedpyright", "long.pyn")
    a, b = out.split("long.pyn:2:")[1].split("long.pyn:3:")[0], out.split("long.pyn:3:")[1]
    assert "extra field: dropout (reportAssignmentType)" in a and "missing" not in a
    assert "extra field: dropout_rate\n" in b and "missing field: n_layer (reportAssignmentType)" in b
    assert "_byname_fieldset" not in out


def test_a_record_spreads_into_a_call_by_name_only(project):
    (project / "star.pyn").write_text(
        "def fn(name: str, age: int) -> None: ...\n"
        "fn(*(age=1, name='r'))\n"   # by position: an error
        "fn(**(age=1, name='r'))\n"  # by name: fine
        "fn(**(name=1, age=1))\n"    # name is an int
    )
    out, rc = tool(project, "basedpyright", "star.pyn")
    assert 'star.pyn:2:5 - error: "(age: int, name: str)" is not iterable' in out
    assert "star.pyn:3" not in out
    assert 'star.pyn:4:6 - error: Argument of type "int" cannot be assigned to parameter "name"' in out


def test_spread_mistakes_are_named(project):
    (project / "spread.pyn").write_text(
        "type Person = (name: str, age: int, sex: str, surname: str)\n"
        "def fn(name: str, age: int, sex: str, surname: str): ...\n"
        "u = (age=26, name='rahul')\n"
        "r = (sex='male',)\n"
        "fn(**u, **r)\n"
        "p: Person = (**u, **r)\n"
        "q = (name: str, age: int, sex: str, surname: str)(**u, **r, surname='g', x=1)\n"
        "def mk() -> Person:\n"
        "    return (**u, **r)\n"
        "def take(p: Person, n: int = 0): ...\n"
        "take((**u, **r, surname='g'))\n"     # fine: no type written, the parameter's is used
        "take((**u, **r))\n"                  # missing surname
        "take(p=(**u, **r, surname=1))\n"     # by keyword: surname is an int
        "x = (**u, **r)\n"                    # u, r are known records: x is typed, nothing to check here
    )
    out, rc = tool(project, "basedpyright", "spread.pyn")
    assert 'spread.pyn:5:1 - error: Argument missing for parameter "surname"' in out
    assert out.count("missing field: surname") == 3 and "extra field: x" in out
    assert "spread.pyn:11" not in out and "spread.pyn:12:6" in out and "spread.pyn:14" not in out
    assert 'spread.pyn:13:8 - error: Argument of type "(age: int, name: str, sex: str, surname: int)"' in out
    assert "_byname" not in out and "_dct_" not in out


def test_spread_of_known_records_is_typed_without_an_annotation(project):
    # defaults spread into a config: no annotation, yet the result has its exact type, so a typo is caught
    (project / "defaults.pyn").write_text(
        "train_defaults = (eval_interval=100, lr=1e-2)\n"
        "cfg = (**train_defaults, batch_size=32, lr=3e-3)\n"
        "reveal_type(cfg)\n"
        "cfg.btach_size\n"
    )
    out, rc = tool(project, "basedpyright", "defaults.pyn")
    assert 'defaults.pyn:3:13 - information: Type of "cfg" is "(eval_interval: int, lr: float, batch_size: int)"' in out
    assert 'defaults.pyn:4:5 - error: Cannot access attribute "btach_size"' in out


def test_spread_of_a_rebound_name_keeps_its_narrowing(project):
    # `r` is bound twice, so its fields aren't known and the generic path checks the spread inside a
    # lambda, where pyright drops narrowing for a name reassigned later. Read through a lambda default,
    # `r` is the record it holds at that point, not the union of both bindings
    (project / "rebound.pyn").write_text(
        "type Args = (name: str, age: int)\n"
        "def fn(*, user: Args): ...\n"
        "u = (name='r',)\n"
        "r = (age=1,)\n"
        "fn(user=(**u, **r))\n"
        "fn(user=(**u, **r, age='x'))\n"   # still checked: wrong type
        "r = (tag='a', note=1)\n"
    )
    out, rc = tool(project, "basedpyright", "rebound.pyn")
    assert "rebound.pyn:5" not in out and "rebound.pyn:6:9 - error" in out and "1 error," in out


def test_spread_record_picks_the_overload_that_takes_a_record(project):
    # a spread record isn't whatever type an overload wants: `int` rejects it, so the Person overload is used
    (project / "ov.pyn").write_text(
        "from typing import overload, Any\n"
        "type Person = (name: str, age: int)\n"
        "@overload\n"
        "def lookup(key: int) -> int: ...\n"
        "@overload\n"
        "def lookup(key: Person) -> str: ...\n"
        "def lookup(key: Any) -> Any: ...\n"
        "u = (name='r',)\n"
        "reveal_type(lookup((**u, age=1)))\n"
        "lookup((**u,))\n"                     # missing age
        "x = (**u, age=1)\n"
        "def fn(p: Person) -> None: ...\n"
        "fn(x)\n"                               # x = (**u, age=1) is (name: str, age: int): fine
        "reveal_type(print((**u, age=1)))\n"
    )
    out, rc = tool(project, "basedpyright", "ov.pyn")
    line9 = next(ln for ln in out.splitlines() if "ov.pyn:9:13" in ln)
    assert line9.endswith('is "str"')
    assert "ov.pyn:10:8 - error" in out and "missing field: age" in out
    # the missing age is reported on the argument, plus pyright's "no overload matches" for the call
    assert "ov.pyn:13" not in out and "2 errors," in out and "ov.pyn:10:1 - error: No overloads" in out


def test_parameter_patterns_are_typed(project):
    # each local gets its field's type; an unknown field is reported on the pattern; calls are checked
    (project / "params.pyn").write_text(
        "type User = (name: str, age: int)\n"
        "def f((name=, age=): User):\n"
        "    if age > 1:\n"
        "        return name + 1\n"          # str + int
        "    return name\n"
        "def g((nme=): User): return nme\n"  # no such field
        "f((name='r', age=5))\n"
        "f((name='r',))\n"                   # missing age
    )
    out, rc = tool(project, "basedpyright", "params.pyn")
    assert 'params.pyn:4:16 - error: Operator "+" not supported for types "str" and "Literal[1]"' in out
    assert 'params.pyn:6:8 - error: Cannot access attribute "nme" for class "User"' in out
    assert "params.pyn:7" not in out and "params.pyn:8:3 - error" in out and "missing field: age" in out
    assert "_byname" not in out and out.count(" - error") == 3
    out, rc = tool(project, "mypy", "params.pyn")
    assert "params.pyn:4: error" in out and 'params.pyn:6: error: "(name: str, age: int)" has no attribute "nme"' in out


def test_named_parameter_patterns_are_typed(project):
    # `user=(...): User` is a real parameter: keyword calls are checked against User
    (project / "named.pyn").write_text(
        "type User = (name: str, age: int)\n"
        "def fn(*, user=(name=, age=): User):\n"
        "    return name + age\n"                 # str + int
        "fn(user=(name='r', age=5))\n"
        "fn(user=(name='r',))\n"                  # missing age
    )
    out, rc = tool(project, "basedpyright", "named.pyn")
    assert 'named.pyn:3:12 - error: Operator "+" not supported for types "str" and "int"' in out
    assert "named.pyn:4" not in out and "named.pyn:5:9 - error" in out and "missing field: age" in out


def test_mypy_line_numbers_map_back(project):
    # mypy reports lines only; records are Any to it, so only the plain type error shows
    out, rc = tool(project, "mypy", "main.pyn")
    assert rc == 1
    assert "main.pyn:5: error: Incompatible types in assignment" in out
    assert "nope" not in out


def test_ruff_clean_file_passes_despite_generated_header(project):
    # people.pyn is clean; complaints about the record header are hidden and don't fail the run
    out, rc = tool(project, "ruff", "check", "--output-format=concise", "people.pyn")
    assert rc == 0, out
    assert "people.pyn" not in out or "error" not in out


def test_ruff_real_issue_reported_at_pyn_position(project):
    (project / "bad.pyn").write_text("import os\n(a=) = (a=1)\n")
    out, rc = tool(project, "ruff", "check", "--output-format=concise", "--select", "F401", "bad.pyn")
    assert rc == 1
    assert "bad.pyn:1:8: F401" in out
