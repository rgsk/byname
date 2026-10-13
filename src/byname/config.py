"""Project settings, from `[tool.byname]` in the nearest pyproject.toml above the file being translated.

    [tool.byname]
    iter-callers = ["mylib.collate"]   # modules that may read a record by position, on top of ITER_CALLERS

A file with no path (a notebook cell, `<pyn>`) looks up from the working directory. Read once per
pyproject.toml and again when it changes, as the editor and a notebook kernel outlive an edit to it.
"""

import tomllib
from pathlib import Path

# modules allowed to read a record by position at runtime (iterate it, or index it with an int): each one
# treats any NamedTuple as a container and rebuilds it as `type(t)(*(f(v) for v in t))`, which gives the same
# record back, as iterating and the constructor both use the storage order. A name covers its submodules.
# Audited, so a package's other code, which may iterate a record and keep the values, still raises:
# - torch: DataLoader's pin_memory and default_collate, pytree, scatter/gather
# - accelerate.utils.operations: send_to_device and recursively_apply (honor_type), concatenate, and
#   find_batch_size (`data[0]`)
# - lightning_utilities.core.apply_func: apply_to_collection(s), which Lightning moves batches with
# optree (and JAX's pytrees) need no entry: their C++ reads the tuple without calling __iter__
ITER_CALLERS = ("torch", "accelerate.utils.operations", "lightning_utilities.core.apply_func")
_read: dict[Path, tuple[tuple[int, int], tuple[str, ...]]] = {}  # pyproject.toml -> (its stat, iter-callers)


def pyproject(path: str) -> Path | None:
    start = Path(path).resolve().parent if Path(path).is_file() else Path.cwd()
    for d in [start, *start.parents]:
        if (d / "pyproject.toml").is_file():
            return d / "pyproject.toml"
    return None


def iter_callers(path: str) -> tuple[str, ...]:
    """ITER_CALLERS and the project's `iter-callers`, for the file at path."""
    pp = pyproject(path)
    if pp is None:
        return ITER_CALLERS
    st = pp.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    if (hit := _read.get(pp)) is not None and hit[0] == stamp:
        return hit[1]
    extra = tomllib.loads(pp.read_text(encoding="utf-8")).get("tool", {}).get("byname", {}).get("iter-callers", [])
    if not isinstance(extra, list) or not all(isinstance(m, str) and m for m in extra):
        raise ValueError(f"{pp}: [tool.byname] iter-callers must be a list of module names, got {extra!r}")
    callers = tuple(dict.fromkeys([*ITER_CALLERS, *extra]))
    _read[pp] = (stamp, callers)
    return callers
