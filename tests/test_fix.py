import shutil
import sys
from pathlib import Path

import pytest

from byname.fix import ORGANIZE, FixError, fix_notebook, fix_pyn

pytestmark = pytest.mark.skipif(not (Path(sys.executable).parent / "ruff").exists() and not shutil.which("ruff"), reason="ruff not installed")


def test_fix_all_upgrades_typing_then_drops_the_import():
    # what source.fixAll does to a .py on save: List -> list, then the import is unused, so it goes
    src = "from typing import List\n\n\ndef f(xs: List[int]) -> List[int]:\n    return xs\n"
    assert fix_pyn(src) == "\n\ndef f(xs: list[int]) -> list[int]:\n    return xs\n"


def test_import_used_only_through_shorthand_stays():
    # ruff sees the translation, where f(os=) is f(os=os); the formatter's stand-in f(os=__p) would hide that use
    src = "import os\n\n\ndef f(*, os):\n    return os\n\n\nprint(f(os=))\n"
    assert fix_pyn(src) == src


def test_records_and_patterns_come_back_unchanged():
    src = "from typing import List\n\n\ndef g(xs: List[int]):\n    n = len(xs)\n    return (n=, xs=)\n\n\n(n=, xs=ys) = g([1])\n"
    out = fix_pyn(src)
    assert "def g(xs: list[int]):" in out
    assert "return (n=, xs=)\n" in out and "(n=, xs=ys) = g([1])\n" in out


def test_organize_imports_sorts_only():
    src = "import sys\nimport os\nfrom typing import List\n\nprint(os.sep, sys.argv, List)\n"
    assert fix_pyn(src, select=ORGANIZE) == "import os\nimport sys\nfrom typing import List\n\nprint(os.sep, sys.argv, List)\n"


def test_unfinished_code_is_an_error():
    with pytest.raises(FixError):
        fix_pyn("(a=, = r\n")


def test_notebook_imports_used_in_a_later_cell_stay():
    # Ruff sees the whole notebook: `os` is used two cells down, `sys` nowhere. Each cell keeps its own
    # ending (no newline after the last line)
    cells = ["import sys\nimport os", "x = 1", "print(os.sep, x)"]
    assert fix_notebook(cells, byname=False) == ["import os", "x = 1", "print(os.sep, x)"]
    assert fix_notebook(["import sys\nimport os", "print(os.sep, sys.argv)"], byname=False, select=ORGANIZE) == ["import os\nimport sys", "print(os.sep, sys.argv)"]


def test_byname_notebook_cells_are_fixed_through_their_translation():
    # `%load_ext byname`: Ruff checks each cell's translation; `os` is used only through a record field's
    # value two cells down, and the record cell itself comes back as written apart from List -> list
    cells = ["%load_ext byname", "import sys\nimport os\nfrom typing import List", "x: List[int] = [1]\nr = (a=os.sep, b=x)"]
    assert fix_notebook(cells, byname=True) == ["%load_ext byname", "import os", "x: list[int] = [1]\nr = (a=os.sep, b=x)"]
