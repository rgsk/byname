"""byname tool <cmd> [args...]: run a Python tool (ruff, basedpyright) on .pyn files, like nbqa.

The project is mirrored into a cache dir: .pyn translated to .py, .py and config files symlinked,
so imports and tool config work unchanged. The tool runs inside the mirror on the translated
files; `path:line[:col]` in its output is mapped back to the .pyn. Diagnostics on the generated
record header are dropped, as in the editor.
"""

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

from .lsp import explain_fields, pretty
from .srcmap import Translation, generated

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
CONFIGS = {"pyproject.toml", "setup.cfg", "ruff.toml", ".ruff.toml", "pyrightconfig.json"}
KEEP_IN_MIRROR = {".ruff_cache"}  # tool caches survive between runs
LOCATION = re.compile(r"^(?P<pre>\s*)(?P<path>[^\s:]+\.py):(?P<line>\d+)(?::(?P<col>\d+))?(?P<rest>.*)$")
SUMMARY = re.compile(r"^(Found \d+ errors?|\d+ errors?, \d+ warnings?|All checks passed|Success: no issues|\[\*\] \d+ fixable)")


def resolve(cmd: str) -> str:
    local = Path(sys.executable).parent / cmd
    return str(local) if local.exists() else cmd


def mirror(root: Path) -> Path:
    """Sync root into its mirror; returns the mirror root. Unchanged files keep their mtime (tool caches)."""
    key = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    out = cache / "byname" / f"{key}-tools"
    wanted: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = Path(dirpath).relative_to(root)
        for f in filenames:
            src = Path(dirpath) / f
            if f.endswith(".pyn"):
                dst = out / rel / (f[:-4] + ".py")
                text = Translation(src.read_text(encoding="utf-8")).hidden
                if not dst.is_file() or dst.is_symlink() or dst.read_text(encoding="utf-8") != text:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.unlink(missing_ok=True)
                    dst.write_text(text, encoding="utf-8")
            elif f.endswith((".py", ".pyi")) or (f in CONFIGS and rel == Path(".")):
                dst = out / rel / f
                if not (dst.is_symlink() and dst.readlink() == src):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.unlink(missing_ok=True)
                    dst.symlink_to(src)
            else:
                continue
            wanted.add(dst)
    if not has_pyright_config(root):  # same default as the editor; "recommended" is noisy
        cfg = out / "pyrightconfig.json"
        if not cfg.is_file():
            cfg.write_text('{"typeCheckingMode": "standard"}\n')
        wanted.add(cfg)
    if out.exists():  # drop files whose source is gone
        for dirpath, dirnames, filenames in os.walk(out, topdown=True):
            dirnames[:] = [d for d in dirnames if d not in KEEP_IN_MIRROR]
            for f in filenames:
                p = Path(dirpath) / f
                if p not in wanted:
                    p.unlink()
    out.mkdir(parents=True, exist_ok=True)
    return out


def has_pyright_config(root: Path) -> bool:
    if (root / "pyrightconfig.json").exists():
        return True
    pp = root / "pyproject.toml"
    return pp.exists() and re.search(r"^\[tool\.(based)?pyright", pp.read_text(), re.MULTILINE) is not None


def remap(line: str, root: Path, out: Path, cache: dict) -> tuple[str | None, bool]:
    """(line to print or None to drop, whether it was a location line)."""
    line = pretty(line)
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
    if p.is_absolute() and not p.is_relative_to(out) or not pyn.exists():
        return line, True  # an ordinary .py file
    if pyn not in cache:
        cache[pyn] = Translation(pyn.read_text(encoding="utf-8"))
    tr = cache[pyn]
    col = int(m["col"]) - 1 if m["col"] else 0
    h = tr.hid_lines.offset(int(m["line"]) - 1, col)
    hit = tr._from_hidden(h, False)
    if hit is None:
        return None, True  # generated record header
    off, how = hit
    if span := generated(how):
        off = span[0]
    pos = tr.src_lines.position(off)
    loc = f"{rel.with_suffix('.pyn')}:{pos['line'] + 1}" + (f":{pos['character'] + 1}" if m["col"] else "")
    return f"{m['pre']}{loc}{m['rest']}", True


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
        args.append(a)
    if Path(cmd).name in ("basedpyright", "pyright") and "--pythonpath" not in args:
        args = ["--pythonpath", sys.executable, *args]
    proc = subprocess.run([resolve(cmd), *args], cwd=out, capture_output=True, text=True, check=False)
    cache: dict = {}
    dropped = kept = 0
    lines = []
    for line in condense((proc.stdout + proc.stderr).splitlines()):
        new, located = remap(line, root, out, cache)
        if new is None:
            dropped += 1
        else:
            kept += located
            lines.append(new)
    if dropped:  # the tool's own totals now overcount
        lines = [ln for ln in lines if not SUMMARY.match(ln.strip())]
        lines.append(f"byname: hid {dropped} diagnostic(s) on generated code")
    print("\n".join(lines))
    return 0 if dropped and not kept else proc.returncode
