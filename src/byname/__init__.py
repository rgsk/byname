from .hook import install
from .runtime import record
from .transform import source_ast, to_ast, to_code, to_python, transform

install()


def load_ipython_extension(shell) -> None:
    """`%load_ext byname`: the cells that follow are .pyn (see notebook.py)."""
    from .notebook import load

    load(shell)


def unload_ipython_extension(shell) -> None:
    from .notebook import unload

    unload(shell)


__all__ = ["install", "record", "source_ast", "to_ast", "to_code", "to_python", "transform"]
