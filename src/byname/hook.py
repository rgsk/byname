"""Import hook: lets `import foo` find and run foo.pyn (and packages with __init__.pyn)."""

import sys
from importlib import _bootstrap_external
from importlib.machinery import FileFinder, SourceFileLoader

from .transform import to_code

SUFFIX = ".pyn"


class PynLoader(SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        return to_code(data.decode("utf-8"), path)

    def set_data(self, path, data, *, _mode=0o666):
        pass  # no .pyc: a cached transform would go stale when byname changes


def install() -> None:
    if any(getattr(h, "_byname", False) for h in sys.path_hooks):
        return
    loaders = [(PynLoader, [SUFFIX]), *_bootstrap_external._get_supported_file_loaders()]
    hook = FileFinder.path_hook(*loaders)
    hook._byname = True
    sys.path_hooks.insert(0, hook)
    sys.path_importer_cache.clear()
