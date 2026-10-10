"""Record types imported from other .pyn modules, for the checker translation's spreads (known_fields).

`from train import TrainConfig` then `(**cfg)` with `cfg: TrainConfig`: the spread is typed only if its
fields are known, and they're in train.pyn. The module is found as the checker would find it: relative to
the importing file for `from .m import X`; otherwise in the importing file's folder, the project root
(the nearest folder with a pyproject.toml or pyrightconfig.json), its `src`, the checker's `extraPaths`,
then this interpreter's sys.path (an editable install's .pth folders). Only .pyn modules are read: a .py
can't write a record type. A module's names are read from its file on disk, cached by mtime.
"""

import json
import sys
import tomllib
from pathlib import Path

from .transform import Known, known_fields, transform

CONFIGS = ("pyproject.toml", "pyrightconfig.json")
# module file -> (its names' fields, the stat of every file they were read from: it and its imports')
_exports: dict[Path, tuple[dict[str, Known], dict[Path, tuple[int, int] | None]]] = {}
_roots: dict[Path, list[Path]] = {}  # project root -> its search folders


def project_root(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        if any((d / c).is_file() for c in CONFIGS):
            return d
    return None


def extra_paths(root: Path) -> list[Path]:
    """The checker's `extraPaths`, from pyrightconfig.json or pyproject.toml's [tool.basedpyright] / [tool.pyright]."""
    found: list[str] = []
    try:
        if (root / "pyrightconfig.json").is_file():
            found = json.loads((root / "pyrightconfig.json").read_text(encoding="utf-8")).get("extraPaths", [])
        elif (root / "pyproject.toml").is_file():
            tool = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("tool", {})
            found = (tool.get("basedpyright") or tool.get("pyright") or {}).get("extraPaths", [])
    except (OSError, ValueError):
        return []
    return [root / p for p in found if isinstance(p, str)]


def search_folders(importer: Path) -> list[Path]:
    root = project_root(importer.parent)
    if root is not None and root not in _roots:
        _roots[root] = [root, root / "src", *extra_paths(root)]
    folders = [importer.parent, *(_roots[root] if root is not None else [])]
    folders += [Path(p) for p in sys.path if p and Path(p).is_dir()]
    return list(dict.fromkeys(folders))


def find_module(importer: Path, module: str, level: int) -> Path | None:
    parts = module.split(".") if module else []
    if level:
        base = importer.parent
        for _ in range(level - 1):
            base = base.parent
        folders = [base]
    else:
        folders = search_folders(importer)
    for d in folders:
        for f in (d.joinpath(*parts).with_suffix(".pyn") if parts else None, d.joinpath(*parts, "__init__.pyn")):
            if f is not None and f.is_file():
                return f
    return None


class Modules:
    """The lookup for one importing file (transform's `lookup`). used: the module files it read, so the
    editor can translate the importer again when one of them is saved. cut: an import led back to a file
    being read (a cycle), which then read as showing nothing."""

    def __init__(self, importer: Path, seen: frozenset[Path] = frozenset()):
        self.importer = importer
        self.seen = seen | {importer}
        self.used: set[Path] = set()
        self.cut = False

    def __call__(self, module: str, level: int, name: str) -> Known | None:
        f = find_module(self.importer, module, level)
        if f is None:
            return None
        if f in self.seen:
            self.cut = True
            return None
        names, inner, cut = exports(f, self.seen)
        self.used |= {f, *inner}
        self.cut |= cut
        return names.get(name)


def stamp(f: Path) -> tuple[int, int] | None:
    try:
        st = f.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def exports(f: Path, seen: frozenset[Path]) -> tuple[dict[str, Known], set[Path], bool]:
    """The fields of f's module-level names, the module files they were read from (f and its imports), and
    whether a cycle cut the reading short (then it isn't cached: read from elsewhere, it may show more)."""
    if (hit := _exports.get(f)) and all(stamp(d) == s for d, s in hit[1].items()):
        return hit[0], set(hit[1]), False
    before = stamp(f)
    try:
        src = f.read_text(encoding="utf-8")
        body = transform(src, str(f), tolerant=True, checker=True, known={}).body
    except (OSError, SyntaxError, ValueError):
        return {}, {f}, False
    lookup = Modules(f, seen)
    names = {name: k for (name, _), k in known_fields(body, lookup, exports=True).items()}
    deps = {f, *lookup.used}
    if not lookup.cut:
        _exports[f] = (names, {d: before if d == f else stamp(d) for d in deps})
    return names, deps, lookup.cut
