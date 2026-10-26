import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from byname.fmt import FormatError, UnparsableError, decode, encode, format_pyn
from byname.transform import to_python

pytestmark = pytest.mark.skipif(
    not (Path(sys.executable).parent / "ruff").exists() and not shutil.which("ruff"), reason="ruff not installed"
)

ALL = Path(__file__).parent.parent / "examples" / "all.pyn"


def test_stand_ins_are_plain_python():
    # each byname form gets a stand-in ruff can parse; a pattern becomes a subscript target
    src = "r = fn(x=, y=1)\nq = (x=, y=2)\n(x=, y=t) = r\n"
    assert encode(src) == "r = fn(x=__p, y=1)\nq = __P(x=__p, y=2)\n__P[x:__p, y:t] = r\n"
    assert decode(encode(src)) == src


def test_for_target_stand_in(tmp_path):
    src = "for (x=, y=t) in pts:\n    pass\n"
    assert encode(src) == "for __P[x:__p, y:t] in pts:\n    pass\n"
    assert format_pyn("for (x= ,y = t) in pts :\n  pass\n", cwd=tmp_path) == src


def test_nested_pattern_stand_in(tmp_path):
    src = "(id=, user=(name=, age=a)) = r\n"
    assert encode(src) == "__P[id:__p, user:__P[name:__p, age:a]] = r\n"
    assert format_pyn("(id= ,user=( name=,age = a ))=r\n", cwd=tmp_path) == src


def test_final_pattern_stand_in(tmp_path):
    # an annotated subscript is a valid target, so the annotation needs no stand-in of its own
    src = "(name=, age=a): Final = r\n"
    assert encode(src) == "__P[name:__p, age:a]: Final = r\n"
    assert format_pyn("(name= ,age = a):Final=r\n", cwd=tmp_path) == src


def test_rest_stand_in(tmp_path):
    # `**rest` -> `*rest`: a starred item is valid in a subscript, so the pattern stays one target
    src = "(name=, **rest) = r\nfor (a=, b=(c=, **more), **top) in xs:\n    pass\n"
    assert encode(src) == "__P[name:__p, *rest] = r\nfor __P[a:__p, b:__P[c:__p, *more], *top] in xs:\n    pass\n"
    assert format_pyn("(name= ,** rest)=r\nfor (a=,b=(c=,**more),**top) in xs :\n  pass\n", cwd=tmp_path) == src


def test_spreads_format(tmp_path):
    src = "both = (**u,**r)\nx=(**u, age =27, name=)\nfn(**u, **r)\n"
    want = "both = (**u, **r)\nx = (**u, age=27, name=)\nfn(**u, **r)\n"
    assert format_pyn(src, cwd=tmp_path) == want
    assert format_pyn(want, cwd=tmp_path) == want


def test_record_defaults_format(tmp_path):
    src = "def f(*,user=( name='x' ,age=1)):\n    pass\n"
    want = 'def f(*, user=(name="x", age=1)):\n    pass\n'
    assert format_pyn(src, cwd=tmp_path) == want
    assert format_pyn(want, cwd=tmp_path) == want


def test_decode_eats_slice_spacing():
    # ruff writes complex slices as `a : b`; that must come back as `a=b`
    assert decode('__P[name:__p, age : d["age"]] = r\n') == '(name=, age=d["age"]) = r\n'


def test_format_normalises_like_ruff(tmp_path):
    src = "x=fn( name= ,age = 1 )\n(name= ,age=a)=r\nrec=(name= , age= )\n"
    assert format_pyn(src, cwd=tmp_path) == "x = fn(name=, age=1)\n(name=, age=a) = r\nrec = (name=, age=)\n"


def test_long_pattern_splits_one_item_per_line(tmp_path):
    # in an empty dir ruff uses its default 88 columns (byname's own pyproject sets 120)
    src = "(name=self.label, age=d['age'], greeting=self.greeting_value_that_is_long, extra=, more=) = make(name=)\n"
    assert format_pyn(src, cwd=tmp_path).splitlines()[:3] == ["(", "    name=self.label,", '    age=d["age"],']


def test_all_pyn_keeps_its_meaning_and_is_idempotent():
    # ruff only changes layout: the translated Python has the same AST before and after
    src = ALL.read_text()
    out = format_pyn(src, str(ALL))
    assert ast.dump(ast.parse(to_python(src))) == ast.dump(ast.parse(to_python(out)))
    assert format_pyn(out, str(ALL)) == out


def test_refuses_sources_using_stand_in_names():
    with pytest.raises(FormatError):
        format_pyn("__p = 1\n")


def test_code_ruff_cannot_parse_is_unparsable_not_a_failure(tmp_path):
    # a half-typed line: the editor offers no edits (as the Ruff extension does); `byname format` still says so
    with pytest.raises(UnparsableError, match="Failed to parse"):
        format_pyn("r = (a=1)\nprint(r.)\n", cwd=tmp_path)
    with pytest.raises(UnparsableError, match="Failed to parse"):  # an unclosed bracket: tokenizing fails first
        format_pyn("print(r.\n", cwd=tmp_path)


def test_format_record_types(tmp_path):
    # stand-in __T[x: int] is a subscript ruff can format; it comes back as (x: int)
    src = "def f(n:int)->(h:int,d:   (a: int, b: list[int])):\n    return (h=n, d=(a=1, b=[n]))\n"
    out = format_pyn(src, str(tmp_path / "f.pyn"))
    assert out == "def f(n: int) -> (h: int, d: (a: int, b: list[int])):\n    return (h=n, d=(a=1, b=[n]))\n"
    assert format_pyn(out, str(tmp_path / "f.pyn")) == out


def test_one_item_record_type_and_pattern_keep_the_magic_trailing_comma(tmp_path):
    # Ruff reads `__T[x: int,]` as a one-tuple subscript, whose comma isn't magic: a hidden second item keeps it so
    src = "def f(x: (a: int,)) -> (b: int):\n    (c=,) = x\n"
    out = format_pyn(src, cwd=tmp_path)
    assert out == "def f(\n    x: (\n        a: int,\n    ),\n) -> (b: int):\n    (\n        c=,\n    ) = x\n"
    assert format_pyn(out, cwd=tmp_path) == out
    with pytest.raises(FormatError, match="stand-in"):
        format_pyn("__1 = 1\n", cwd=tmp_path)


def test_grid_packs_to_the_first_lines_width(tmp_path):
    # the first line sets the most a line may hold; ruff alone would put one item per line
    src = (
        "s = [\n    1,2, 3,\n    4, 5, 6, 7, 8, 9,\n]\nf(\n    a, b, c,\n    d=jfljdskfsd, e=fdsljfsdklfd, g=, h=,\n)\n"
    )
    out = format_pyn(src, cwd=tmp_path)
    assert (
        out
        == "s = [\n    1, 2, 3,\n    4, 5, 6,\n    7, 8, 9,\n]\nf(\n    a, b, c,\n    d=jfljdskfsd,\n    e=fdsljfsdklfd,\n    g=, h=,\n)\n"
    )
    assert format_pyn(out, cwd=tmp_path) == out


def test_grid_falls_back_to_one_per_line(tmp_path):
    # one item on the first line, a comment inside, or an item spanning lines: plain ruff layout
    one = "s = [\n    1,\n    2, 3,\n]\n"
    comment = "s = [\n    1, 2,  # first\n    3,\n]\n"
    assert format_pyn(one, cwd=tmp_path) == "s = [\n    1,\n    2,\n    3,\n]\n"
    assert format_pyn(comment, cwd=tmp_path) == "s = [\n    1,\n    2,  # first\n    3,\n]\n"
    nested = "s = [\n    [1], [\n        2, 3,\n        4,\n    ],\n]\n"
    assert format_pyn(nested, cwd=tmp_path) == "s = [\n    [1],\n    [\n        2, 3,\n        4,\n    ],\n]\n"


def test_grid_leaves_fmt_skip_alone(tmp_path):
    src = "s = [\n    1, 2,\n    3, 4, 5,\n]  # fmt: skip\n"
    assert format_pyn(src, cwd=tmp_path) == src


def test_grid_from_items_right_after_the_bracket(tmp_path):
    # `[1, 2, 3,` : the items on the bracket line are the first line
    src = "s = [1, 2, 3,\n4, 5, 6, 7, 8, 9,]\nr = bfs(n, m, k,\n  start=(sr, sc), blocked=walls,)\n"
    out = format_pyn(src, cwd=tmp_path)
    assert (
        out
        == "s = [\n    1, 2, 3,\n    4, 5, 6,\n    7, 8, 9,\n]\nr = bfs(\n    n, m, k,\n    start=(sr, sc),\n    blocked=walls,\n)\n"
    )
    assert format_pyn(out, cwd=tmp_path) == out


def test_grid_needs_a_trailing_comma(tmp_path):
    # no trailing comma: plain ruff, which joins what fits on one line
    assert format_pyn("s = [1, 2, 3,\n4, 5, 6]\n", cwd=tmp_path) == "s = [1, 2, 3, 4, 5, 6]\n"


def notebook(*cells: tuple[str, str | list[str]]) -> str:
    return json.dumps(
        {
            "cells": [
                {"cell_type": kind, "metadata": {}, "source": src}
                | ({"outputs": [], "execution_count": None} if kind == "code" else {})
                for kind, src in cells
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        },
        indent=1,
    )


def byname_cli(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "XDG_CACHE_HOME": str(cwd / ".cache")}
    return subprocess.run([sys.executable, "-m", "byname", *args], cwd=cwd, env=env, capture_output=True, text=True)


def test_the_cli_formats_and_fixes_notebooks_as_saving_in_the_editor_does(tmp_path):
    # byname format / fix take a .ipynb: each code cell as the editor formats it on save (a byname notebook's
    # through the translation), a magic's cell left as it is, a cell's last line still without a newline;
    # fix sees the whole notebook, so the import a later cell uses stays
    nb = tmp_path / "nb.ipynb"
    nb.write_text(
        notebook(
            ("code", "%load_ext byname"),
            ("markdown", "# messy   markdown  stays"),
            ("code", ["import os\n", "import json\n", "u=( name='r',age = 1 )"]),
            ("code", "%time x=1"),
            ("code", "(name=,age=)=u\nprint( os.sep,name )\n"),
        )
    )
    assert byname_cli(tmp_path, "format", "--check", "nb.ipynb").returncode == 1
    done = byname_cli(tmp_path, "format", "nb.ipynb")
    assert (done.returncode, done.stdout, done.stderr) == (0, "reformatted nb.ipynb\n", "")
    cells = [c["source"] for c in json.loads(nb.read_text())["cells"]]
    assert cells == [
        "%load_ext byname",
        "# messy   markdown  stays",
        ["import os\n", "import json\n", "\n", 'u = (name="r", age=1)'],  # a list of lines stays one
        "%time x=1",
        "(name=, age=) = u\nprint(os.sep, name)\n",
    ]
    assert byname_cli(tmp_path, "format", "--check", "nb.ipynb").returncode == 0
    assert byname_cli(tmp_path, "fix", "nb.ipynb").stdout == "fixed nb.ipynb\n"
    assert json.loads(nb.read_text())["cells"][2]["source"] == ["import os\n", "\n", 'u = (name="r", age=1)']

    # a plain notebook, and a cell that doesn't parse: said, and left as it is
    plain = tmp_path / "plain.ipynb"
    plain.write_text(notebook(("code", "x=[1,2]"), ("code", "print(x.")))
    done = byname_cli(tmp_path, "format", "plain.ipynb")
    assert "plain.ipynb: cell 2 left as it is: error: Failed to parse" in done.stderr
    assert [c["source"] for c in json.loads(plain.read_text())["cells"]] == ["x = [1, 2]", "print(x."]
