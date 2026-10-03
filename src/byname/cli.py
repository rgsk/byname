"""byname run file.pyn [args...]   run a .pyn script
byname show file.pyn             print the plain-Python translation
byname lsp [-- checker cmd...]   language server (default checker: basedpyright-langserver --stdio)
byname tool <cmd> [args...]      run ruff / mypy / basedpyright on .pyn files (positions mapped back)"""

import sys
import types
from pathlib import Path

from .hook import install
from .transform import to_code, to_python


def run(path: Path, argv: list[str]) -> None:
    install()
    src = path.read_text()
    mod = types.ModuleType("__main__")
    mod.__file__ = str(path)
    sys.modules["__main__"] = mod
    sys.argv = [str(path), *argv]
    sys.path.insert(0, str(path.resolve().parent))
    exec(to_code(src, str(path)), mod.__dict__)


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["tool"]:
        from .tools import main as tool_main

        sys.exit(tool_main(args[1:]))
    if args[:1] == ["lsp"]:
        from .lsp import main as lsp_main

        lsp_main(args[1:])
        return
    if len(args) < 2 or args[0] not in ("run", "show"):
        sys.exit(__doc__)
    cmd, path = args[0], Path(args[1])
    if cmd == "show":
        print(to_python(path.read_text(), str(path)), end="")
    else:
        run(path, args[2:])
