"""pytest collects `.pyn` test files like `.py` ones: any `python_files` pattern with `.pyn` for `.py`
(by default `test_*.pyn` and `*_test.pyn`). Registered through the `pytest11` entry point, so it's on
wherever byname is installed. Asserts are rewritten as in `.py` tests, and line numbers match the
`.pyn`, so failures point at it and editors (VS Code's test gutter) put run buttons on the right lines."""

import sys
import types
from pathlib import Path

import pytest
from _pytest.assertion.rewrite import rewrite_asserts
from _pytest.pathlib import fnmatch_ex

from .hook import install
from .transform import to_ast


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Module | None:
    if file_path.suffix != ".pyn":
        return None
    patterns = parent.config.getini("python_files")
    if not any(fnmatch_ex(p.removesuffix(".py") + ".pyn" if p.endswith(".py") else p, file_path) for p in patterns):
        return None
    return PynModule.from_parent(parent, path=file_path)


class PynModule(pytest.Module):
    def _getobj(self) -> types.ModuleType:
        # like pytest's default "prepend" import mode: the test's folder on sys.path, so it imports its
        # neighbours (.py, or .pyn through byname's import hook)
        install()
        folder = str(self.path.parent)
        if folder not in sys.path:
            sys.path.insert(0, folder)
        src = self.path.read_text()
        tree = to_ast(src, str(self.path))
        rewrite_asserts(tree, src.encode(), str(self.path), self.config)
        mod = types.ModuleType(self.path.stem)
        mod.__file__ = str(self.path)
        sys.modules[mod.__name__] = mod  # dataclasses and pickle look a module up by name
        exec(compile(tree, str(self.path), "exec", dont_inherit=True), mod.__dict__)  # noqa: S102 -- importing the test module is the point
        return mod
