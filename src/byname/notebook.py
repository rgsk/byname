"""`%load_ext byname` in Jupyter/IPython: every cell after it is .pyn.

Cells are translated after IPython has turned magics into calls (`%time x = (a=1)` becomes
`get_ipython().run_line_magic('time', 'x = (a=1)')`, and %time translates its own argument), so
magics, `!shell` lines and top-level `await` work as in plain Python cells.

A cell's prelude (record classes, helpers) runs into the notebook's namespace at translation time,
and the cell itself runs as its body alone, so traceback line numbers are the cell's own. (Handing
the prelude to an AST transformer instead pairs it with the wrong cell whenever IPython transforms
a cell without compiling it.) Records from different cells are different classes, which equality
and hashing don't look at: `(a=1) == (a=1)` across cells is True.

Tracebacks quote lines from the text IPython caches for the cell, which is the translation; the
wrapped `shell.compile.cache` puts the cell as written there instead."""

import linecache

from .transform import transform


def _translate(shell, written: dict[str, str]):
    def translate(lines: list[str]) -> list[str]:
        src = "".join(lines)
        r = transform(src, f"<cell In[{shell.execution_count}]>")
        if r.prelude:
            exec(compile(r.prelude, "<byname prelude>", "exec"), shell.user_ns)  # noqa: S102 -- defines the cell's record classes
        if r.body != src:
            written[r.body] = src
        return r.body.splitlines(keepends=True)

    translate.byname = True
    return translate


def _cache(original, written: dict[str, str]):
    def cache(transformed_code: str, number: int = 0, raw_code: str | None = None) -> str:
        name = original(transformed_code, number, raw_code)
        if (src := written.pop(transformed_code, None)) is not None:
            linecache.cache[name] = (len(src), None, [line + "\n" for line in src.splitlines()], name)
        written.clear()  # transformed but never compiled (e.g. %time's argument): nothing to keep
        return name

    cache.byname_original = original
    return cache


def load(shell) -> None:
    if any(getattr(t, "byname", False) for t in shell.input_transformers_post):
        return
    written: dict[str, str] = {}
    shell.input_transformers_post.append(_translate(shell, written))
    shell.compile.cache = _cache(shell.compile.cache, written)


def unload(shell) -> None:
    shell.input_transformers_post[:] = [t for t in shell.input_transformers_post if not getattr(t, "byname", False)]
    if original := getattr(shell.compile.cache, "byname_original", None):
        shell.compile.cache = original
