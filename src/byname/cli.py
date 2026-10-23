"""byname run file.pyn [args...]   run a .pyn script
byname show [--no-main] FILE     print the plain-Python translation (--no-main: drop the
                                 `if __name__ == "__main__":` block, e.g. for LeetCode)
byname lsp [-- checker cmd...]   language server (default checker: basedpyright-langserver --stdio)
byname tool <cmd> [args...]      run ruff / basedpyright on .pyn files (positions mapped back)
byname format [--check] FILE...  ruff format .pyn files and notebooks in place
byname fix [--check] FILE...     apply ruff's safe lint fixes to .pyn files and notebooks in place (as
                                 source.fixAll on save)"""

import json
import sys
import types
from pathlib import Path

from .hook import install
from .output import render
from .transform import to_code


def run(path: Path, argv: list[str]) -> None:
    install()
    src = path.read_text()
    mod = types.ModuleType("__main__")
    mod.__file__ = str(path)
    sys.modules["__main__"] = mod
    sys.argv = [str(path), *argv]
    sys.path.insert(0, str(path.resolve().parent))
    exec(to_code(src, str(path)), mod.__dict__)


def ruff_at(path: Path, mirrors: dict[Path, Path]) -> tuple[str, Path | None]:
    """(filename, cwd) for Ruff on a .pyn: its copy in the mirror of the current directory, as `byname tool`
    and the editor see it (lsp.ruff_at). Ruff tells first-party imports by the files it finds, and a .pyn is a
    module only in the mirror, so without it `from helper import f` sorts as third-party.
    mirrors: the mirror once synced, by root, so a run of many files syncs it once."""
    from .tools import mirror, mirrored

    root = Path.cwd()
    rel = mirrored(root, path.resolve())
    if rel is None:  # outside the project: Ruff on the file where it is
        return str(path), None
    if root not in mirrors:
        mirrors[root] = mirror(root)
    return str(rel), mirrors[root]


def code_cells(src: str) -> tuple[dict, list[dict], bool]:
    """A notebook's JSON, its code cells, and whether it's a byname notebook (`%load_ext byname`)."""
    from .tools import byname_cells

    nb = json.loads(src)
    return nb, [c for c in nb.get("cells", []) if c.get("cell_type") == "code"], byname_cells(nb) is not None


def cell_text(cell: dict) -> str:
    return "".join(cell["source"]) if isinstance(cell.get("source"), list) else cell.get("source", "")


def set_cells(src: str, nb: dict, cells: list[dict], texts: list[str]) -> str:
    """The notebook with these cells' texts, each kept as a list of lines or a string as it was; src itself
    if nothing changed, so a notebook left alone keeps its bytes."""
    if [cell_text(c) for c in cells] == texts:
        return src
    for c, text in zip(cells, texts):
        if cell_text(c) != text:
            c["source"] = text.splitlines(keepends=True) if isinstance(c.get("source"), list) else text
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"  # as Jupyter and VS Code write it


def format_notebook(path: Path, src: str, mirrors: dict[Path, Path]) -> str:
    """Each code cell formatted as the editor does it on save (Proxy.cell_action), byname's or plain: a cell's
    last line keeps having no newline, and a cell Ruff can't parse alone is left as it is, said unless it's
    a magic's (`%time x = 1`, `!pip ...`)."""
    from .fmt import FormatError, format_pyn

    nb, cells, _ = code_cells(src)
    texts = []
    for c in cells:
        text = cell_text(c)
        try:
            new = format_pyn(text, *ruff_at(path, mirrors))
        except (FormatError, SyntaxError) as e:
            if not any(ln.lstrip().startswith(("%", "!")) for ln in text.splitlines()):
                where = nb["cells"].index(c) + 1  # counting every cell, as Ruff does
                print(f"{path}: cell {where} left as it is: {str(e).splitlines()[0]}", file=sys.stderr)
            new = text
        texts.append(new if text.endswith("\n") else new.removesuffix("\n"))
    return set_cells(src, nb, cells, texts)


def fix_notebook_file(path: Path, src: str, mirrors: dict[Path, Path]) -> str:
    """Ruff's safe fixes over the whole notebook (fix.fix_notebook): an import used in a later cell is used."""
    from .fix import fix_notebook

    nb, cells, byname = code_cells(src)
    return set_cells(src, nb, cells, fix_notebook([cell_text(c) for c in cells], byname, *ruff_at(path, mirrors)))


def format_files(args: list[str]) -> int:
    from .fmt import FormatError, format_pyn

    check = "--check" in args
    changed = 0
    mirrors: dict[Path, Path] = {}
    for name in (a for a in args if a != "--check"):
        path = Path(name)
        src = path.read_text(encoding="utf-8")
        try:
            if path.suffix == ".ipynb":
                out = format_notebook(path, src, mirrors)
            else:
                out = format_pyn(src, *ruff_at(path, mirrors))
        except (FormatError, SyntaxError, ValueError) as e:
            print(f"{name}: {e}", file=sys.stderr)
            return 2
        if out != src:
            changed += 1
            print(f"{'would reformat' if check else 'reformatted'} {name}")
            if not check:
                path.write_text(out, encoding="utf-8")
    return 1 if check and changed else 0


def fix_files(args: list[str]) -> int:
    from .fix import FixError, fix_pyn

    check = "--check" in args
    changed = 0
    mirrors: dict[Path, Path] = {}
    for name in (a for a in args if a != "--check"):
        path = Path(name)
        src = path.read_text(encoding="utf-8")
        try:
            out = (
                fix_notebook_file(path, src, mirrors)
                if path.suffix == ".ipynb"
                else fix_pyn(src, *ruff_at(path, mirrors))
            )
        except (FixError, ValueError) as e:
            print(f"{name}: {e}", file=sys.stderr)
            return 2
        if out != src:
            changed += 1
            print(f"{'would fix' if check else 'fixed'} {name}")
            if not check:
                path.write_text(out, encoding="utf-8")
    return 1 if check and changed else 0


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["format"]:
        sys.exit(format_files(args[1:]))
    if args[:1] == ["fix"]:
        sys.exit(fix_files(args[1:]))
    if args[:1] == ["tool"]:
        from .tools import main as tool_main

        sys.exit(tool_main(args[1:]))
    if args[:1] == ["lsp"]:
        from .lsp import main as lsp_main

        lsp_main(args[1:])
        return
    no_main = args[:1] == ["show"] and "--no-main" in args
    if no_main:
        args.remove("--no-main")
    if len(args) < 2 or args[0] not in ("run", "show"):
        sys.exit(__doc__)
    cmd, path = args[0], Path(args[1])
    if cmd == "show":
        print(render(path.read_text(), path, strip_main=no_main), end="")
    else:
        run(path, args[2:])
