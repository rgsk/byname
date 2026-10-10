"""byname tool <cmd> [args...]: run a Python tool (ruff, basedpyright) on .pyn files, like nbqa.

The project is mirrored into a cache dir: .pyn translated to .py, .py and config files symlinked,
so imports and tool config work unchanged. Notebooks are mirrored too: one with `%load_ext byname`
gets each code cell translated (as the kernel runs it), a plain one is symlinked. The tool runs
inside the mirror on the translated files; `path:line[:col]` in its output is mapped back to the
.pyn, and a notebook's cell locations to the cell as written. Diagnostics on the generated record
header are dropped, as in the editor.
"""

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .lsp import LOAD_EXT_RE, explain_fields, pretty
from .srcmap import Translation, generated

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
CONFIGS = {"pyproject.toml", "setup.cfg", "ruff.toml", ".ruff.toml", "pyrightconfig.json"}
# tool caches survive between runs; so do the editor's .pyn from outside the project
KEEP_IN_MIRROR = {".ruff_cache", "_abs"}
LOCATION = re.compile(r"^(?P<pre>\s*)(?P<path>[^\s:]+\.py):(?P<line>\d+)(?::(?P<col>\d+))?(?P<rest>.*)$")
# a notebook cell: ruff `nb.ipynb:cell 3:1:5` (counting every cell), basedpyright `nb.ipynb:3:1:5` (code cells)
NB_LOCATION = re.compile(
    r"^(?P<pre>\s*)(?P<path>[^\s:]+\.ipynb):(?P<ruff>cell )?(?P<cell>\d+):(?P<line>\d+):(?P<col>\d+)(?P<rest>.*)$"
)
NB_HEADER = re.compile(r"^(?P<path>\S+\.ipynb)(?P<rest> - cell \d+)$")  # basedpyright's per-cell header
# Ruff findings about how byname wrote its code, not about the user's: statements a translation puts on
# one line (`(a=, b=) = r` is `_ds = r; a = _ds.a; b = _ds.b`), and anything naming byname's helpers
GENERATED_STYLE = re.compile(r"\bE70[123]\b")
HELPER = re.compile(r"`_byname_\w*`")
TOO_LONG = re.compile(r"\bE501 Line too long \(\d+ > (\d+)\)")  # measured on the translation's longer line
UNSORTED = re.compile(r"\bI001\b")  # import block un-sorted or un-formatted


def has_pyright_config(root: Path) -> bool:
    """The project configures the checker itself: pyrightconfig.json, or [tool.basedpyright] / [tool.pyright]."""
    if (root / "pyrightconfig.json").exists():
        return True
    pp = root / "pyproject.toml"
    return pp.exists() and re.search(r"^\[tool\.(based)?pyright", pp.read_text(), re.MULTILINE) is not None


def resolve(cmd: str) -> str:
    local = Path(sys.executable).parent / cmd
    return str(local) if local.exists() else cmd


def byname_cells(nb: dict) -> list[str] | None:
    """A notebook's cells' sources, or None unless a code cell loads byname."""
    cells = [
        "".join(c["source"]) if isinstance(c.get("source"), list) else c.get("source", "") for c in nb.get("cells", [])
    ]
    code = [src for c, src in zip(nb.get("cells", []), cells) if c.get("cell_type") == "code"]
    return cells if any(LOAD_EXT_RE.search(src) for src in code) else None


def translated_notebook(src: Path) -> str | None:
    """The notebook with each code cell translated, or None for a plain notebook (or one that isn't JSON)."""
    try:
        nb = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (cells := byname_cells(nb)) is None:
        return None
    for c, text in zip(nb["cells"], cells):
        if c.get("cell_type") == "code":
            c["source"] = Translation(text, src).hidden
    return json.dumps(nb, indent=1, ensure_ascii=False) + "\n"


def write_if_changed(dst: Path, text: str) -> bool:
    if dst.is_file() and not dst.is_symlink() and dst.read_text(encoding="utf-8") == text:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    dst.write_text(text, encoding="utf-8")
    return True


def mirror_dir(root: Path, kind: str) -> Path:
    key = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "byname" / f"{key}-{kind}"


def mirrored(root: Path, src: Path) -> Path | None:
    """Where src goes in the mirror, relative to its root (a .pyn as its .py); None if it isn't mirrored."""
    try:
        rel = src.relative_to(root)
    except ValueError:
        return None
    if not rel.parts or any(p in SKIP_DIRS for p in rel.parts[:-1]):
        return None
    if src.suffix == ".pyn":
        return rel.with_suffix(".py")
    if src.suffix in (".py", ".pyi", ".ipynb") or (src.name in CONFIGS and len(rel.parts) == 1):
        return rel
    return None


def place(root: Path, out: Path, src: Path) -> tuple[Path, int | None] | None:
    """Bring src's copy in the mirror up to date, removing it if src is gone. A .pyn wins over a .py of the
    same name. Returns (the copy, what happened as a FileChangeType: 1 created, 2 changed, 3 deleted, None
    unchanged), or None if src isn't mirrored."""
    rel = mirrored(root, src)
    if rel is None:
        return None
    dst = out / rel
    existed = dst.is_symlink() or dst.exists()
    if src.suffix == ".py" and src.with_suffix(".pyn").is_file():
        return dst, None  # the .pyn's translation is there
    if src.suffix == ".pyn" and not src.is_file() and src.with_suffix(".py").is_file():
        src = src.with_suffix(".py")  # a .pyn gone uncovers its .py
    if not src.is_file():
        if not existed:
            return dst, None
        dst.unlink()
        return dst, 3
    if src.suffix == ".pyn":
        changed = write_if_changed(dst, Translation(src.read_text(encoding="utf-8"), src).hidden)
    elif src.suffix == ".ipynb" and (text := translated_notebook(src)) is not None:
        changed = write_if_changed(dst, text)
    elif dst.is_symlink() and dst.readlink() == src:
        changed = False
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.unlink(missing_ok=True)
        dst.symlink_to(src)
        changed = True
    return dst, (2 if existed else 1) if changed else None


def mirror(root: Path, kind: str = "tools", default_config: bool = True, changes: list | None = None) -> Path:
    """Sync root into its mirror; returns the mirror root. Unchanged files keep their mtime (tool caches).
    default_config: a project that doesn't configure the checker gets the editor's default, "standard".
    changes: collects (mirror path, FileChangeType) for what this sync changed."""
    out = mirror_dir(root, kind)
    wanted: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and Path(dirpath, d) != out]
        for f in filenames:
            if (placed := place(root, out, Path(dirpath) / f)) is not None:
                wanted.add(placed[0])
                if placed[1] is not None and changes is not None:
                    changes.append(placed)
    if default_config and not has_pyright_config(root):  # same default as the editor; "recommended" is noisy
        cfg = out / "pyrightconfig.json"
        if not cfg.is_file():
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text('{"typeCheckingMode": "standard"}\n')
        wanted.add(cfg)
    if out.exists():  # drop files whose source is gone
        for dirpath, dirnames, filenames in os.walk(out, topdown=True):
            dirnames[:] = [d for d in dirnames if d not in KEEP_IN_MIRROR]
            for f in filenames:
                p = Path(dirpath) / f
                if p not in wanted:
                    p.unlink()
                    if changes is not None:
                        changes.append((p, 3))
    out.mkdir(parents=True, exist_ok=True)
    return out


def remap(line: str, root: Path, out: Path, cache: dict) -> tuple[str | None, bool]:
    """(line to print, None to drop it as generated code or "" as a quiet walrus label, whether it was a
    location line)."""
    line = pretty(line)
    if NB_LOCATION.match(line) or NB_HEADER.match(line):
        return remap_notebook(line, root, out, cache)
    m = LOCATION.match(line)
    if not m:
        bare = Path(line.strip())
        if bare.is_absolute() and bare.is_relative_to(out):  # pyright's per-file header
            rel = bare.relative_to(out)
            if (root / rel.with_suffix(".pyn")).exists():
                rel = rel.with_suffix(".pyn")
            return str(rel), False
        return line, False
    p = Path(m["path"])
    rel = p.relative_to(out) if p.is_absolute() and p.is_relative_to(out) else p
    pyn = root / rel.with_suffix(".pyn")
    if p.is_absolute() and not p.is_relative_to(out):
        return line, True  # outside the project (site-packages, ...)
    if UNSORTED.search(line) and only_grids(pyn if pyn.exists() else root / rel, out / rel, out, cache):
        return None, True
    if not pyn.exists():  # an ordinary .py file: the project's path, not the mirror's
        return f"{m['pre']}{rel}:{m['line']}" + (f":{m['col']}" if m["col"] else "") + m["rest"], True
    if pyn not in cache:
        cache[pyn] = Translation(pyn.read_text(encoding="utf-8"), pyn)
    pos = source_position(cache[pyn], int(m["line"]) - 1, int(m["col"]) - 1 if m["col"] else 0, line)
    if not isinstance(pos, dict):
        return pos, True
    loc = f"{rel.with_suffix('.pyn')}:{pos['line'] + 1}" + (f":{pos['character'] + 1}" if m["col"] else "")
    return f"{m['pre']}{loc}{m['rest']}", True


def source_position(tr: Translation, line: int, col: int, message: str) -> dict | str | None:
    """A 0-based position in tr's translation as one in its source. None hides the finding: it's on the
    generated record header, or a Ruff finding about how byname laid its code out (see GENERATED_STYLE,
    HELPER, imports_lead_to, TOO_LONG). "" for a walrus labelling a returned tuple, called unused."""
    if HELPER.search(message):
        return None
    hit = tr._from_hidden(tr.hid_lines.offset(line, col), False)
    if hit is None:
        return None
    off, how = hit
    if span := generated(how):
        if GENERATED_STYLE.search(message):
            return None
        off = span[0]
    if tr.quiet(off, message):
        return ""
    if "E402" in message and imports_lead_to(tr, line):
        return None
    pos = tr.src_lines.position(off)
    if (long := TOO_LONG.search(message)) and len(tr.source.split("\n")[pos["line"]]) <= int(long[1]):
        return None  # the line as written fits
    return pos


def imports_lead_to(tr: Translation, line: int) -> bool:
    """Ruff's E402 (import not at top) on hidden `line`, caused only by the record prelude byname puts
    above the user's imports: without it, nothing but imports and a docstring comes first."""
    prelude_lines = tr.hidden.count("\n", tr.at, tr.at + tr.plen)
    if line < tr.hidden.count("\n", 0, tr.at) + prelude_lines:
        return True  # a line of the prelude itself
    try:
        tree = ast.parse(tr.hidden[: tr.at] + tr.hidden[tr.at + tr.plen :])
    except SyntaxError:
        return False
    for st in tree.body:
        if st.lineno - 1 >= line - prelude_lines:
            return True
        doc = isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)
        if not (isinstance(st, (ast.Import, ast.ImportFrom)) or doc):
            return False
    return True


def only_grids(src: Path, mirrored: Path, out: Path, cache: dict) -> bool:
    """Organizing src's imports changes nothing: Ruff's I001 there is only about names packed several to
    a line (a grid, see fmt.py), which byname's organize imports keeps."""
    from .fix import ORGANIZE, FixError, fix_notebook, fix_pyn

    if ("only_grids", src) not in cache:
        same = False
        try:
            text = src.read_text(encoding="utf-8")
            if src.suffix == ".ipynb":
                nb = json.loads(text)
                byname = byname_cells(nb) is not None
                cells = [
                    "".join(c["source"]) if isinstance(c.get("source"), list) else c.get("source", "")
                    for c in nb.get("cells", [])
                    if c.get("cell_type") == "code"
                ]
                same = fix_notebook(cells, byname, str(mirrored), out, select=ORGANIZE) == cells
            else:
                same = fix_pyn(text, str(mirrored), out, select=ORGANIZE) == text
        except (OSError, ValueError, FixError):
            pass
        cache["only_grids", src] = same
    return cache["only_grids", src]


def remap_notebook(line: str, root: Path, out: Path, cache: dict) -> tuple[str | None, bool]:
    """A notebook location: the mirror's path as the project's, and in a byname notebook the cell's
    translated position as its source one. Cells are numbered as the tool numbers them."""
    m = NB_LOCATION.match(line) or NB_HEADER.match(line)
    assert m is not None
    p = Path(m["path"])
    rel = p.relative_to(out) if p.is_absolute() and p.is_relative_to(out) else p
    if "line" not in m.groupdict():  # header
        return f"{rel}{m['rest']}", False
    nb_path = root / rel
    if UNSORTED.search(line) and only_grids(nb_path, out / rel, out, cache):
        return None, True
    if nb_path not in cache:
        try:
            nb = json.loads(nb_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            nb = {}
        cells = byname_cells(nb)
        if cells is None:
            cache[nb_path] = None
        else:
            kinds = [c.get("cell_type") for c in nb["cells"]]
            every = [Translation(src, nb_path) if k == "code" else None for k, src in zip(kinds, cells)]
            cache[nb_path] = (every, [t for t in every if t is not None])
    loc = f"{rel}:{m['ruff'] or ''}{m['cell']}"
    if cache[nb_path] is None:  # a plain notebook: positions are the cell's own
        return f"{m['pre']}{loc}:{m['line']}:{m['col']}{m['rest']}", True
    every, code = cache[nb_path]
    cells = every if m["ruff"] else code
    k = int(m["cell"]) - 1
    tr = cells[k] if 0 <= k < len(cells) else None
    if tr is None:
        return line, True
    pos = source_position(tr, int(m["line"]) - 1, int(m["col"]) - 1, line)
    if not isinstance(pos, dict):
        return pos, True
    return f"{m['pre']}{loc}:{pos['line'] + 1}:{pos['character'] + 1}{m['rest']}", True


def condense(lines: list[str]) -> list[str]:
    """Shorten record field-set errors (see explain_fields); each message is its location line plus the
    more-indented lines under it."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not LOCATION.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        j, pre = i + 1, len(lines[i]) - len(lines[i].lstrip())
        while j < len(lines) and lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) > pre:
            j += 1
        out.extend(explain_fields("\n".join(lines[i:j])).split("\n"))
        i = j
    return out


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit("usage: byname tool <ruff|basedpyright|...> [args...] FILE.pyn")
    root = Path.cwd()
    out = mirror(root)
    cmd, args = argv[0], []
    for a in argv[1:]:
        p = Path(a)
        if a.endswith(".pyn"):
            p = p.resolve()
            if not p.is_relative_to(root):
                sys.exit(f"byname tool: {a} is outside {root}")
            a = str(p.relative_to(root).with_suffix(".py"))
        elif a.endswith((".py", ".ipynb")) and p.is_absolute() and p.resolve().is_relative_to(root):
            a = str(p.resolve().relative_to(root))  # its copy in the mirror (a byname notebook is translated there)
        args.append(a)
    if Path(cmd).name in ("basedpyright", "pyright") and "--pythonpath" not in args:
        args = ["--pythonpath", sys.executable, *args]
    proc = subprocess.run([resolve(cmd), *args], cwd=out, capture_output=True, text=True, check=False)
    cache: dict = {}
    dropped = quiet = 0
    lines, located_lines = [], []
    for line in condense((proc.stdout + proc.stderr).splitlines()):
        new, located = remap(line, root, out, cache)
        if new is None:
            dropped += 1
        elif new == "":
            quiet += 1
        else:
            if located:
                located_lines.append(new)
            lines.append(new)
    if dropped or quiet:  # the tool's own totals count what byname hid: recount what's shown
        lines = recount(lines, located_lines)
    if os.environ.get("BYNAME_DEBUG"):
        if dropped:
            lines.append(f"byname: hid {dropped} diagnostic(s) on generated code or gridded imports")
        if quiet:
            lines.append(f"byname: hid {quiet} unused-variable warning(s) on walrus labels in returns")
    print("\n".join(lines))
    return 0 if (dropped or quiet) and not located_lines else proc.returncode


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def recount(lines: list[str], located: list[str]) -> list[str]:
    """The tool's summary lines, with totals for the diagnostics shown: Ruff's `Found N errors.` (or
    `All checks passed!`) and `[*] N fixable ...`, basedpyright's `N errors, N warnings, N notes`."""
    out = []
    for ln in lines:
        text = ln.strip()
        if text.startswith(("Found ", "All checks passed")):
            out.append(f"Found {plural(len(located), 'error')}." if located else "All checks passed!")
        elif text.startswith("[*] "):
            if fixable := sum("[*]" in loc for loc in located):
                out.append(f"[*] {fixable} fixable with the `--fix` option.")
        elif re.match(r"\d+ errors?, \d+ warnings?, \d+ notes?", text):
            n = {k: sum(f" - {k}:" in loc for loc in located) for k in ("error", "warning", "information")}
            out.append(
                f"{plural(n['error'], 'error')}, {plural(n['warning'], 'warning')}, {plural(n['information'], 'note')}"
            )
        else:
            out.append(ln)
    return out
