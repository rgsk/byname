# Findings

Bugs and gaps found while exercising byname in `projects/pyn-modules-template` (src/ of loose modules,
hatchling `dev-mode-dirs`) and `projects/pyn-package-template` (a package, editable install). Each case
is compared four ways: what Python does at runtime (the truth), plain basedpyright/ruff on the project,
Alt+L (`byname tool`) and the editor (`byname lsp`).

Status: **open** until fixed; a fix names its commit.

## 1. Runtime errors name the record's generated class — open

A record's attribute error at runtime shows byname's internal class, not the record:

```
AttributeError: '_rec_x__y' object has no attribute 'nope'
```

The editor says `Cannot access attribute "nope" for class "(x: int, y: int)"` for the same line.
Seen with `print(make().nope)` where `make()` returns `(x=1, y=2)`, run with `byname run`.

## 2. Only byname's own entry points can import a .pyn module — open

The import hook (`byname.hook.install()`) is installed by `byname run`, the pytest plugin and
`%load_ext byname`. Anything else can't import a .pyn module, while the editor resolves it fine:

```
$ uv run python -c "from pyn_package_template.boxes import make_box"
ModuleNotFoundError: No module named 'pyn_package_template.boxes'
```

Affects plain `python`, `python -m`, a plain notebook's kernel, the debugger. Options: a `.pth` shipped
with byname that installs the hook in every interpreter of the venv, or the project's `__init__.py`
calling `byname.hook.install()`.

The editor and Alt+L don't know which launcher will run a file, so they resolve the .pyn module and
show nothing wrong where the run fails (both templates):

| case | editor / Alt+L | runtime |
|---|---|---|
| plain notebook, `from geometry.solids import cube` (a .pyn) | clean | kernel: `ModuleNotFoundError` |
| `use_solids_py.py` imports the .pyn module | clean | `python`: `ModuleNotFoundError`; `byname run`: works |
| `both.py` and `both.pyn` side by side, a .py imports `both` | `both.pyn` | `python`: `both.py`; `byname run`: `both.pyn` |
| a package whose `__init__` is a .pyn (`pynpkg/__init__.pyn`) | resolved | `python`: `ImportError: cannot import name 'WHO' from 'pynpkg' (unknown location)`: without the hook the folder is an empty namespace package |

With the hook in every interpreter, the .pyn would win everywhere and the editor would be right.
pytest is fine: the plugin installs the hook, so a .py test importing a .pyn module passes, even alone.

## 3. `byname tool <ruff> --fix` / `ruff format` report fixes they don't make — open

The tool runs in the mirror, so writes land on the mirror's copy. A .py is a symlink there, so its fix
reaches the real file; a .pyn and a byname notebook are translations, so theirs are lost:

```
$ byname tool ruff check --fix src/walkthroughs/use_dup.pyn
Found 1 error (1 fixed, 0 remaining).        <- use_dup.pyn unchanged
$ byname tool ruff format src/walkthroughs/byname.ipynb
1 file reformatted                           <- notebook unchanged
```

`byname fix` / `byname format` (and save in the editor) do change .pyn files. The tool could refuse
writing flags (`--fix`, `format` without `--check`/`--diff`) on .pyn and byname notebooks and point there,
or run those through `byname fix` / `byname format`.

## 4. `byname fix` / `byname format` run Ruff in the project, not the mirror — open

The editor's fix on save runs Ruff on the mirror's copy (since this session's `ruff_at`), as Alt+L does;
the CLI commands still run it on the project (`fix_pyn(src, str(path))`, no mirror). A module that
exists only as a .pyn is third-party there and first-party in the mirror, so they disagree:

```
src/user.pyn:   from only_pyn import X      (src/only_pyn.pyn)
                                            
                from plain_mod import Y     (src/plain_mod.py)
$ byname fix src/user.pyn                   -> no change
$ byname tool ruff check src/user.pyn       -> I001 Import block is un-sorted or un-formatted
```

Same cause as the editor's I001 on `llm/src/records/tc.pyn` fixed earlier; the CLI was left out.

## Notes (not byname bugs)

- A module named like a stdlib one (`src/calendar.pyn`, `pyn_package_template/calendar.pyn`) shadows it
  when the script runs from that folder (`byname run src/use_calendar_here.pyn`:
  `AttributeError: module 'calendar' has no attribute 'month_name'`), while the editor and Alt+L say
  clean: basedpyright resolves the stdlib first. Same with a plain `calendar.py` and plain basedpyright,
  even with `reportShadowedImports` on, so it's basedpyright's, not the mirror's.
- The same module name in two folders (`src/dup.py`, `src/walkthroughs/dup.py`): at runtime the script's
  (or notebook's) own folder wins, basedpyright tries it last, after `extraPaths`. Plain basedpyright
  agrees with byname. `executionEnvironments = [{ root = "src/walkthroughs" }]` fixes it for .pyn, .py and
  both notebook kinds (verified); the modules template has it now. The package template doesn't need
  it: no `extraPaths` competes with a file's own folder there.
- Plain basedpyright and Ruff can't read a byname notebook (`"(" was not closed`): expected, it's
  byname syntax.

- Destructuring with a shorthand at module level rebinds the name: `(area=) = box` after
  `from shapes import area` replaces the imported function, as `area = box.area` would in Python. The
  checker reports the earlier call (`Object of type "float" is not callable`). Both templates' first
  demos had it; they use a label now, `(area=box_area) = box`.
