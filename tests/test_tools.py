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
    # inferred records keep their written order; explicit types match any order, but exactly these fields
    (project / "types.pyn").write_text(
        "type User = (name: str, age: int)\n"
        "u: User = (age=26, name='R')\n"            # order differs: fine
        "x: User = (age=26, name='R', po='d')\n"    # extra field
        "def f() -> User:\n"
        "    return (age=1, name='R')\n"
        "a, b = f()\n"
        "reveal_type(a)\n"                          # order not guaranteed through an explicit type
        "c, d = (age=1, name='R')\n"
        "reveal_type(c)\n"                          # inferred: as written
        "def g(p: (age: int, ...)) -> int:\n"
        "    return p.age\n"
        "g(u); g((name='R',))\n"                    # open type: at least `age`
    )
    out, rc = tool(project, "basedpyright", "types.pyn")
    assert "types.pyn:2" not in out
    assert 'types.pyn:3:11 - error: Type "(age: int, name: str, po: str)" is not assignable to declared type "User"' in out
    assert "extra field: po (reportAssignmentType)" in out and "_byname_fieldset" not in out
    assert 'types.pyn:7:13 - information: Type of "a" is "str | int"' in out
    assert 'types.pyn:9:13 - information: Type of "c" is "int"' in out
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


def test_star_unpacking_a_record_into_a_call_is_typed(project):
    (project / "star.pyn").write_text(
        "def fn(name: str, age: int) -> None: ...\n"
        "fn(*(name='r', age=1))\n"   # fine
        "fn(*(name=1, age=1))\n"     # name is an int
    )
    out, rc = tool(project, "basedpyright", "star.pyn")
    assert "star.pyn:2" not in out
    assert 'star.pyn:3:1 - error: Argument of type "int" cannot be assigned to parameter "name"' in out
    assert "_byname_star" not in out


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
    )
    out, rc = tool(project, "basedpyright", "spread.pyn")
    assert 'spread.pyn:5:1 - error: Argument missing for parameter "surname"' in out
    assert out.count("missing field: surname") == 2 and "extra field: x" in out
    assert "_byname" not in out and "_dct_" not in out


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
