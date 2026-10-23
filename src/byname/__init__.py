import sys
import types

from .hook import install

TYPE_CHECKING = False  # as checkers read it, without importing typing (3 ms)

# byname.pth imports byname in every interpreter of the venv, to install the import hook: the rest loads on
# first use, so a Python that never touches a .pyn doesn't pay for the translator
if TYPE_CHECKING:
    from .runtime import deepdict, record
    from .transform import source_ast, to_ast, to_code, to_python, transform

_LAZY = {
    "deepdict": "runtime",
    "record": "runtime",
    "source_ast": "transform",
    "to_ast": "transform",
    "to_code": "transform",
    "to_python": "transform",
    "transform": "transform",
}


class _Package(types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        # importing the submodule byname.transform sets byname.transform to it; the name is the function
        if not (name in _LAZY and isinstance(value, types.ModuleType)):
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Package
install()


def __getattr__(name: str):
    if (module := _LAZY.get(name)) is None:
        raise AttributeError(f"module 'byname' has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{module}", __name__), name)


def load_ipython_extension(shell) -> None:
    """`%load_ext byname`: the cells that follow are .pyn (see notebook.py)."""
    from .notebook import load

    load(shell)


def unload_ipython_extension(shell) -> None:
    from .notebook import unload

    unload(shell)


__all__ = ["deepdict", "install", "record", "source_ast", "to_ast", "to_code", "to_python", "transform"]
