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
