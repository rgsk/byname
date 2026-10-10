import pytest

pytest.importorskip("IPython")
from IPython.core.interactiveshell import InteractiveShell


@pytest.fixture
def shell():
    sh = InteractiveShell.instance(colors="nocolor")
    sh.run_cell("%load_ext byname")
    yield sh
    sh.run_cell("%unload_ext byname")
    sh.reset()


def run(shell, cell: str):
    result = shell.run_cell(cell)
    if result.error_in_exec or result.error_before_exec:
        raise result.error_in_exec or result.error_before_exec
    return result.result


def test_cells_after_load_ext_are_pyn_and_share_records(shell):
    run(
        shell, "def make(*, name: str, age: int):\n    return (name=, age=)\nname, age = 'r', 26\nr = make(name=, age=)"
    )
    run(shell, "(age=years) = r")
    assert run(shell, "years") == 26
    # records built in different cells are different classes, but equal by name and value
    assert run(shell, "r == (age=26, name='r'), hash(r) == hash((name='r', age=26))") == (True, True)
    assert repr(run(shell, "r._replace(age=27)")) == "(name='r', age=27)"


def test_magics_shell_lines_and_await_still_work(shell, capsys):
    run(shell, "%time x = (a=1, b=2)")  # %time translates its own argument
    assert repr(run(shell, "x")) == "(a=1, b=2)"
    run(shell, "!echo from-shell")
    assert "from-shell" in capsys.readouterr().out
    assert repr(run(shell, "import asyncio\nawait asyncio.sleep(0)\n(v=1)")) == "(v=1)"


def test_tracebacks_point_at_the_cell_as_written(shell, capsys):
    # line 3 of the cell, quoted as typed: the record classes aren't lines of the cell
    with pytest.raises(ZeroDivisionError):
        run(shell, "x = (a=1, b=2)\ny = 1\nz = y / 0")
    out = capsys.readouterr().out
    assert "----> 3 z = y / 0" in out
    assert "x = (a=1, b=2)" in out


def test_byname_errors_are_syntax_errors_of_the_cell(shell):
    with pytest.raises(SyntaxError, match="duplicate record field"):
        run(shell, "(a=1, a=2)")


def test_unload_ext_makes_cells_plain_python_again(shell):
    shell.run_cell("%unload_ext byname")
    with pytest.raises(SyntaxError):
        run(shell, "(k=1)")
    shell.run_cell("%load_ext byname")
    assert repr(run(shell, "(k=1)")) == "(k=1)"
