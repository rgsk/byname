# byname: development notes

Tracking for whoever works on byname next, including a fresh Claude session with no memory of the
original discussions. `README.md` is the user-facing doc; this file holds the why, the state and the plan.

## Where byname is used

**`/home/rahul/Documents/codes/dsa/cp`**, a competitive-programming repo that's mostly C++. Python
solutions there are written as `.pyn`.

| File | Purpose |
|---|---|
| `pyproject.toml` | byname as an editable path dependency (`../../projects/byname`), so byname changes apply straight away. Dev dependencies: ruff, mypy, basedpyright |
| `python_main.pyn` | **LeetCode template**: `class Solution` plus an `if __name__ == "__main__":` block of local examples |
| `ip_main.pyn` | **CSES / stdin template**: `set_io()` reads `input.txt` next to the file when run locally, then module-level I/O |
| `cf_main.pyn` | **Codeforces template**: same shape as `ip_main.pyn`, shares `input.txt` |
| `.vscode/settings.json` | `byname.outputOnSave` and `byname.outputStripMain` both on, so each save writes `<name>.pyn.py` without `__main__` |
| `.vscode/tasks.json` | `check: current file` (Alt+L): runs ruff, mypy and basedpyright on `.pyn` through `byname tool` |
| `.gitignore` | `*.pyn.py` (generated) and `.venv/` |

**Workflow there:** write the `.pyn`, press Alt+R to run it, save (which writes `<name>.pyn.py`), then
paste `<name>.pyn.py` into the judge.
- **`set_io()`:** `__file__` ends in `.pyn` only when run from the source through byname, so that check stands in for C++'s `#ifdef LOCAL`. It reads `input.txt`, and the `output.txt` line is commented out.
- **CSES I/O stays at module level,** because `outputStripMain` would delete a `__main__` block, leaving a program that does nothing.
- **Solution style:** plain, like `cses/*/sol.cpp`. Logic inline, short comments at each step, no helper functions, and no byname features forced in where they don't fall out naturally.

**Global VS Code config** (`~/.config/Code/User/`):
- `keybindings.json` has **Alt+R** for `.pyn`: save, then `uv run byname run "${file}"` in the terminal, with `VIRTUAL_ENV` pinned.
- **Alt+C** (write output) was added and then removed; output-on-save replaced it.
- `settings.json` has global `editor.formatOnSave: true`, but `false` for `[python]`. `.pyn` isn't `[python]`, so `.pyn` files format on save.
- `"ruff.lint.enable": false`, so the Ruff extension doesn't lint in the editor; only the Alt+L task does.

## Verified on real judges

| Judge | Result |
|---|---|
| LeetCode | Two Sum and 3Sum **accepted**. The output **with records** works, so LeetCode's Python handles the 3.12 generic-class syntax |
| LeetCode | Runs your file as `__main__`: prints from the `if __name__` block showed up in Stdout. That's why `outputStripMain` exists |
| CSES | Weird Algorithm and Chessboard and Queens **accepted** (no records) |
| CSES (PyPy3) | Nested Ranges Check with a 3.12 generic record header: **runtime error on every test**, a SyntaxError. That led to portable output headers |
| Codeforces | 2269A with records: **accepted** on Python 3.13; **compilation error** on PyPy 3.10 (`class R[T0](_NT)`), before portable headers |
| CSES (PyPy3), Codeforces (PyPy 3.10) | With portable headers, Nested Ranges Check and 2269A, both with records: **accepted** |

## Measured facts (don't re-measure)

- **Return-type inference through the translation:** pyright, basedpyright and Pyrefly give identical results (`-> _rec_…[str, int]`, field types, and an error on unknown fields). ty returns `Unknown` everywhere.
- **mypy:** records are `Any`, because mypy doesn't infer return types. It runs clean but can't catch field typos.
- **basedpyright's default mode is noisy:** "recommended" flags `reportImplicitRelativeImport` and similar. Both the language server and `byname tool` default it to `standard`, unless the project has its own pyright config.
- **basedpyright config:** it asks for the `python` and `basedpyright` sections, with `analysis` nested inside. It only asks when the client declares `workspace.configuration`; VS Code does.
- **Ruff formatter:** it rejects `__P(a=__p) = x` as an invalid assignment target, but accepts `__P[a:__p] = x`. It writes complex slices as `a : b`, and `decode` strips that spacing.
- **mypy and `__repr__`:** mypy rejects `__repr__ = helper` inside a NamedTuple body, so the generated classes define a real method.
- **Grids ride on a comment:** Ruff has no bin-packing option, but it keeps a comment that follows an opening bracket on that line. `grid` tags each grid with `# __grid:<k>` (k = items on its first line). After Ruff, `ungrid` measures the width of those k items as Ruff wrote them and repacks to it, so the result doesn't depend on how the first line was spaced, and a second format changes nothing.
- **Ruff's maximum `line-length` is 320.** It can't turn wrapping off, and it has no option to keep `a; b` on one line (only `# fmt: skip`).

## Design decisions and why

- **`field=value`: the left side is always the field, the right side always the local,** in calls, records and patterns. Renaming is `(name=n) = r`, matching `{ name: n }` in TS.
- **Patterns use parens `(…) = r`, not braces.** Braces are reserved for the dict design (parked).
- **Record types are `(name: type, ...)` in annotations,** matching hover's display. Added because recursive functions can't be inferred (pyright treats the recursive call as `Unknown` while inferring), and a record's generated class can't be named in a `.pyn`. Here `:` really is a type annotation, unlike in the rejected `(email: e)` pattern form below. Never valid Python, so no clash: lambdas start with a keyword, walrus is its own `:=` token, and def parameter lists are skipped. Translated by small edits (`(` → `R[`, drop `name:`, `)` → `]`), so nested record types work. Output files' plain classes get `__class_getitem__` returning the class, so `R[int, str]` evaluates on PyPy 3.10, where annotations are evaluated eagerly. Formatter stand-in: `__T[name: type]`.
- **TS-style `(email: e = "x")` was rejected for defaults and renaming.** `:` reads as a type annotation in Python (the TS `{ name: string }` trap), and it would make building use `=` while taking apart used `:`.
- **No trailing comma required on a single-field record.** Unlike `(x)`, `(x=)` can't be read as grouping, so a comma would only be noise.
- **Positional items in patterns are left an error.** Reading A, where a bare name means by-name, conflicts with Python's `(a, b) = r`, which is positional. Reading B, positional first and then keywords like a call, is consistent, but it brings back the field-order fragility that by-name avoids, and only records support it. If ever built, use B.
- **`for (a=, b=t) in xs:` becomes `for (a, t) in ((_ds.a, _ds.b) for _ds in xs):`**, on one line so line numbers hold. A generator, not binds at the top of the body: the body's first statement may be compound (`a = …; if …:` is invalid), and the generator keeps exact field types and scopes `_ds`. The same rewrite covers comprehension `for` clauses. The iterable ends at the statement's `:`, or for a comprehension at the next `if`/`for`/`async` or the closing bracket. A bare tuple iterable (`in a, b`) is parenthesised; `async for` gets an async generator. Costs a generator per loop, which is fine for judges' time limits but not free.
- **Records are generic NamedTuples,** so checkers infer exact field types without annotations. Each file generates its own classes, so records from two files are different types; equality still works.
- **Ctrl+click on shorthand `x=` goes to the local** (the implicit value); Go to Declaration goes to the parameter. A shortcut for Declaration was skipped as rarely needed.
- **The editor uses a language-server proxy,** not a piggyback on Pylance, so the checker can be swapped (Pyrefly works) and it works outside VS Code. Pylance itself can't sit behind a proxy because it's closed-source.
- **Hidden translations go in `~/.cache/byname/<hash>`,** not the project, so there's nothing to gitignore.
- **Completion in an empty pattern slot** uses a temporary text: the slot is filled with `_ds.<prefix>` (plus a dummy `x=` item so `(x) = y` counts), the checker is asked, then the real text is restored. The proxy numbers document versions itself, so diagnostics computed on the temporary text are dropped.
- **Output files are named `<name>.pyn.py`,** following the `foo.min.js` convention. They sort right after their source, and the gitignore pattern `*.pyn.py` can't hide a hand-written file.
- **Every output file gets the marker line and the `# ---- <name>.pyn ----` divider,** even with no generated header, so every output file has the same shape.
- **Output-on-save lives in the server's didSave handler,** not the extension, so it works in any editor. It's off by default: projects that run `.pyn` through byname (like llm would) don't need output files. A `byname build --out-dir` (like `tsc --outDir`) is only for publishing to places without byname, and isn't built.
- **`outputStripMain` is off by default** and on in `cp` only.
- **`diagnosticsOnSave` holds the checker's diagnostics in the proxy** until the editor version they were computed for is the saved one, then sends them (on save, or on arrival if the save came first). While the text is unsaved it still sends the shown ones that survive (matched by message, code and severity, since positions move), so a fixed error disappears at once and only new ones wait. On in `cp`, where half-typed code covered the file in red. VS Code moves the squiggles already shown along with edits, so they stay roughly in place while typing. The proxy removes the client's pull-diagnostics capability (`textDocument.diagnostic`) in `initialize`: basedpyright registers pull diagnostics when VS Code offers them, and pulled results would skip `diagnostics()` (the first version of this setting failed in the editor because of that). There's no debounce option: the user wanted "nothing until I save", and holding diagnostics in the proxy works with any checker.
- **Lint fixes run on the translation, not the formatter's stand-ins.** Stand-ins turn `f(os=)` into `f(os=__p)`, so `import os` would look unused and F401 would delete it. On the translation every use is real. A fix is applied only if each of its edits maps back to source text that matches the hidden text exactly; anything touching generated code or the prelude is dropped whole. It loops until nothing changes, because fixes unlock others (`List[int]` → `list[int]` leaves `from typing import List` unused). Only `safe` fixes, like Ruff's own on-save.
- **The server answers `textDocument/codeAction` itself only when `only` is all `source.fixAll*` or all `source.organizeImports*`** (what `codeActionsOnSave` sends); other code-action requests still go to the checker. Organize imports is `--select I001`, as in the Ruff extension.
- **Output files use portable record classes** (`class R(_NT):` with `object` fields, Python 3.6+), because judges' PyPy is 3.10 and rejects `class R[T0](...)`. The checker's translation keeps the 3.12 generic classes, since that's where field types come from. Both are 3 lines per record, so line numbers match.

## Parked designs (agreed, not built)

**Dicts:** in braces, `=` means a key written literally and `:` keeps its normal Python meaning (a computed key).

```python
{name=, age=26, 'first-name'=f}   # → {'name': name, 'age': 26, 'first-name': f}
{name=, age=} = d                 # name = d['name']; age = d['age']
{key: v} = d                      # v = d[key]
```

Parked because dict literals lose per-key types (`dict[str, str | int]`). Records are the form that
keeps exact types.

**Defaults with `??` ([PEP 505](https://peps.python.org/pep-0505/)):** inside a `field=…` entry, `x ?? d`
means "x, or d if x is None". It reaches to the edges of its entry, so no operator-precedence handling is
needed.

```python
(email= ?? "none") = user     # email = user.email if user.email is not None else "none"
(email=e ?? "none") = user    # with rename
(age= ?? 18)                  # building; narrows int | None → int
fn(age= ?? 18)                # in a call
```

- **Triggers on `None` only, not a missing field.** `getattr(obj, f, d)` types as `Any | T` and hides typos.
- **`(email?="none")` was rejected:** it leaves no place for a rename.

**Other not-yet items:**
- Nested patterns.
- Ctrl+click on a field inside a pattern. It goes nowhere now; the natural target is where the record was built.
- `byname build --out-dir`.

## Open issues

- **Lint debt in byname's own `.py` files:** about 25 Ruff findings (mostly Ruff 0.16's strict defaults, like BLE001 and RUF059) and about 22 mypy errors (mostly `Optional` handling in `lsp.py`).
- **byname's `pyproject.toml` sets Ruff `line-length = 320`,** but the existing `.py` files haven't been reformatted under it. They change as they're saved.
- **A global Ruff setup** across projects (the 320 line length and so on) is still to be decided. There's no `~/.config/ruff`.
- **Display of field names containing `__`** is wrong, because `pretty()` splits the class name at `__`.
- **VS Code was launched with llm's `VIRTUAL_ENV`** in its environment, which made uv warn in tasks. The byname and `cp` tasks pin `VIRTUAL_ENV` to the workspace venv.

## Code map

| File | Role |
|---|---|
| `src/byname/transform.py` | token-level `.pyn` → Python. Records `Edit`/`Mark` provenance for position mapping, `standins` for formatting, and `problems` (tolerant mode, editor only). Also `pattern_slot` for completion |
| `src/byname/srcmap.py` | `Translation`: source ↔ hidden position mapping (UTF-16 columns, prelude offset). Display vs edit vs exact range modes |
| `src/byname/lsp.py` | the proxy: rewrites URIs and positions, shadow files, config injection, semantic tokens, slot completion, formatting, output writing, the `byname.server.writeOutput` command |
| `src/byname/tools.py` | `byname tool`: project mirror plus output remapping |
| `src/byname/fmt.py` | Ruff formatting through stand-ins (`__p`, `__P`) |
| `src/byname/fix.py` | Ruff's safe lint fixes (`source.fixAll`, `source.organizeImports`, `byname fix`) via the translation |
| `src/byname/output.py` | `<name>.pyn.py` rendering: marker, divider, `drop_main` |
| `src/byname/hook.py` | import hook for `.pyn` |
| `src/byname/cli.py` | `run`, `show`, `format`, `tool`, `lsp` |
| `editors/vscode/` | thin extension: launches `byname lsp`, settings, the "Write Python Output" command |

## Conventions

- **Commits:** one-line subjects, no co-author trailer.
- **Tests:** `uv run pytest`. They're written to explain the behaviour, with real values in the asserts. `test_lsp.py`, `test_tools.py` and `test_fmt.py` drive the real basedpyright, Ruff and mypy, and skip if those aren't installed.
- **Extension changes:** bump `version` in `editors/vscode/package.json`, rebuild with `vsce package …`, then `code --install-extension byname.vsix --force` and Reload Window. Server-only changes just need Reload Window, since `.venv` is an editable install.
- **Measure before proposing:** several plausible mechanisms turned out false in practice, such as Ruff accepting call stand-ins and mypy accepting `__repr__ =`.
