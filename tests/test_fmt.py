import ast
import shutil
import sys
from pathlib import Path

import pytest

from byname.fmt import FormatError, decode, encode, format_pyn
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


def test_decode_eats_slice_spacing():
    # ruff writes complex slices as `a : b`; that must come back as `a=b`
    assert decode('__P[name:__p, age : d["age"]] = r\n') == '(name=, age=d["age"]) = r\n'


def test_format_normalises_like_ruff(tmp_path):
    src = "x=fn( name= ,age = 1 )\n(name= ,age=a)=r\nrec=(name= , age= )\n"
    assert format_pyn(src, cwd=tmp_path) == "x = fn(name=, age=1)\n(name=, age=a) = r\nrec = (name=, age=)\n"


def test_long_pattern_splits_one_item_per_line(tmp_path):
    # in an empty dir ruff uses its default 88 columns (byname's own pyproject sets 320)
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
