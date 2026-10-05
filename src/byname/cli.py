"""byname run file.pyn [args...]   run a .pyn script
byname show [--no-main] FILE     print the plain-Python translation (--no-main: drop the
                                 `if __name__ == "__main__":` block, e.g. for LeetCode)
byname lsp [-- checker cmd...]   language server (default checker: basedpyright-langserver --stdio)
byname tool <cmd> [args...]      run ruff / basedpyright on .pyn files (positions mapped back)
byname format [--check] FILE...  ruff format .pyn files in place
byname fix [--check] FILE...     apply ruff's safe lint fixes to .pyn files in place (as source.fixAll on save)"""

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


def format_files(args: list[str]) -> int:
    from .fmt import FormatError, format_pyn

    check = "--check" in args
    changed = 0
    for name in (a for a in args if a != "--check"):
        path = Path(name)
        src = path.read_text()
        try:
            out = format_pyn(src, str(path))
        except (FormatError, SyntaxError) as e:
            print(f"{name}: {e}", file=sys.stderr)
            return 2
        if out != src:
            changed += 1
            print(f"{'would reformat' if check else 'reformatted'} {name}")
            if not check:
                path.write_text(out)
    return 1 if check and changed else 0


def fix_files(args: list[str]) -> int:
    from .fix import FixError, fix_pyn

    check = "--check" in args
    changed = 0
    for name in (a for a in args if a != "--check"):
        path = Path(name)
        src = path.read_text()
        try:
            out = fix_pyn(src, str(path))
        except FixError as e:
            print(f"{name}: {e}", file=sys.stderr)
            return 2
        if out != src:
            changed += 1
            print(f"{'would fix' if check else 'fixed'} {name}")
            if not check:
                path.write_text(out)
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
