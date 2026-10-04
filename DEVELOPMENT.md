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
- **basedpyright infers a mixed list as `list[Unknown]`** (`[(age="90"), (age=23)]`, even `[1, "a"]`), unlike pyright's `list[int | str]`. Unknown switches off checking for everything read from it, so `rs[0]._replace(nme=...)` passed silently. byname turns on `strictListInference`, `strictDictionaryInference` and `strictSetInference` with a `# pyright:` comment at the top of the generated prelude (`CHECKER_DIRECTIVE` in `transform.py`), giving `list[A | B]`. A comment, not a setting: these aren't language-server settings (injecting them via `workspace/configuration` silently did nothing), and the checker runs in the user's project, where byname writes no config file. Only files with records get it, and the comment overrides a project config for those files. Output files don't get it. Records can't be told apart at runtime (one class per field set; field types exist only for the checker), so `isinstance(r.age, int)` narrows the field, not the record; to require one type, annotate: `rs: list[(name: str, age: int)] = [...]`.
- **basedpyright config:** it asks for the `python` and `basedpyright` sections, with `analysis` nested inside. It only asks when the client declares `workspace.configuration`; VS Code does.
- **Ruff formatter:** it rejects `__P(a=__p) = x` as an invalid assignment target, but accepts `__P[a:__p] = x`. It writes complex slices as `a : b`, and `decode` strips that spacing.
- **mypy and `__repr__`:** mypy rejects `__repr__ = helper` inside a NamedTuple body, so the generated classes define a real method.
- **Grids ride on a comment:** Ruff has no bin-packing option, but it keeps a comment that follows an opening bracket on that line. `grid` tags each grid with `# __grid:<k>` (k = items on its first line). After Ruff, `ungrid` measures the width of those k items as Ruff wrote them and repacks to it, so the result doesn't depend on how the first line was spaced, and a second format changes nothing.
- **pyright (basedpyright 1.40.1, pyright 1.1.414) loses the type arguments of a generic tuple subclass when it's star-unpacked into a call:** `fn(*R("a", 1))` with `class R[T0, T1](NamedTuple)` checks each argument as `object` (`T0@R`). Same with old-style `TypeVar` generics, a plain `class B[T0, T1](tuple[T0, T1])`, and a declared `__iter__`; a non-generic NamedTuple and a plain `tuple[X, Y]` are fine; mypy is fine. Records are generic, so `fn(*rec)` was always wrong. Workaround, in the checker's translation only (`transform(checker=True)`, used by `Translation`): every `*x` in a call becomes `*_byname_star(x)`, an identity function overloaded `tuple[*Ts] -> tuple[*Ts]` / `Iterable[T] -> Iterable[T]`. What runs and output files are untouched. Upstream bug, not reported yet.
- **Ruff's maximum `line-length` is 320.** It can't turn wrapping off, and it has no option to keep `a; b` on one line (only `# fmt: skip`).

## Design decisions and why

- **`field=value`: the left side is always the field, the right side always the local,** in calls, records and patterns. Renaming is `(name=n) = r`, matching `{ name: n }` in TS.
- **Patterns use parens `(…) = r`, not braces,** matching record literals.
- **No dict syntax.** A dict design was drafted: in braces, `=` for a literal key and `:` keeping its Python meaning (`{name=, age=} = d`, `{key: v} = d`). Dropped because it buys nothing over records, and dict literals lose per-key types (`dict[str, str | int]`).
- **No shorthand in `def` signatures.** `def f(name=)` would mean "default to the outer `name`", and a function should almost never take outer variables by the same name.
- **Record types are `(name: type, ...)` in annotations,** matching hover's display. Added because recursive functions can't be inferred (pyright treats the recursive call as `Unknown` while inferring), and a record's generated class can't be named in a `.pyn`. Here `:` really is a type annotation, unlike in the rejected `(email: e)` pattern form below. Never valid Python, so no clash: lambdas start with a keyword, walrus is its own `:=` token, and def parameter lists are skipped. Translated by small edits (`(` → `R[`, drop `name:`, `)` → `]`), so nested record types work. Formatter stand-in: `__T[name: type]`.
- **Inferred records keep their order; record types don't.** A record literal's class follows the written order (hover, printing and `a, b = rec` all match what you wrote). A record type, written inline or as a `type` alias, is a generated `Protocol` (`_typ_<fields>`) whose fields are `Final` (read-only, so immutable records match; a plain attribute would require them writable), so a record with the same fields in any order matches it. It also requires the same field set: every record has a `_byname_fieldset` property returning its sorted field names as a `Literal` (`'age,name'`), and so does the Protocol, so an extra or missing field is an error, on any value, not only fresh literals as in TypeScript. Positional access through a record type is `T0 | T1 | ...`, since the order isn't known. The type also declares `_replace(...) -> Self` and a typed `_asdict`, which is accurate because the field set is exact. byname rewrites pyright's page about `_byname_fieldset` into "extra field: po" / "missing field: name" (`explain_fields`). Rejected: sorting fields into one class per field set (hover would read `(age: int, name: str)` whatever you wrote), and keeping order-sensitive types with a better error (the same alias and an inline type annotation would still disagree with the literals you write).
- **Open record types, `(name: str, age: int, ...)`:** any object with at least these fields, with these types. A Protocol (`_opn_<fields>`) with just the `Final` fields: no field set, no positional access, no `_asdict` (its real fields are unknown). Matches records, dataclasses, any object. The trailing `...` follows Flow (`{ name: string, ... }`), Rust's `{ x, .. }` and Python's own `tuple[int, ...]`. For functions that read part of a config: `def __init__(self, model: (vocab_size: int, n_layer: int, ...), train: (dropout: float, ...))` called as `GPT(gpt_cfg, train_cfg)`. Pick per function: `**rec._asdict()` when it must handle every field (it catches "added `dropout`, `GPT` ignores it"); an open type when it reads a subset. One parameter per source config: merging configs isn't typeable (`{**a._asdict(), **b._asdict()}` is `dict[str, int | object | float]`, a merged type is an intersection). Nesting works: `(model=gpt_cfg, train=train_cfg)` against `(model: (...), train: (...), ...)`.
- **Fields read through a record type are coloured like record fields.** pyright gives a NamedTuple field `static`, a `@property` `readonly`, a `Final` attribute `readonly static`; themes colour `readonly` differently. `Final` (not `@property`) gives a pair that's otherwise rare, and the proxy drops `readonly` from `property` tokens carrying both, so `p.age` and `u.age` match. Measured: a descriptor-typed Protocol member (`age: _F[int]`) doesn't match NamedTuple fields at all.
- **Spreads: `f(**rec)`, `{**rec}`, and records built from spreads `(**u, **r, age=27)`.** `**` means "by name" in Python, `*` "by position"; `(*u, *r)` stays a plain tuple. At runtime records are mappings by field name: every record class has `keys()` and `rec["name"]` (`_byname_keys` / `_byname_item`), so Python's own `**` works on them, and valid Python is never rewritten for what runs. `keys` is a reserved field name for it. For the checker only, `**x` becomes `**_byname_kw(x)`, overloaded to give a record's typed `_asdict()` TypedDict (and pass dicts through), so `fn(**u, **r)` is checked field by field (missing, extra, wrong type; a field in both is "multiple values"). A spread record is `_byname_rec({**u, **r, 'age': 27})`, built from a dict display: a later field wins and keeps the first one's position, like `{**a, **b}` and JS `{...a, ...b}`; repeating a field you wrote yourself is still an error. Where a record type is in reach, it's checked: inline `(name: str, …)(**u, **r)`, `p: T = (**u, **r)`, and `return (**u, **r)` under `-> T` become `_cast(T, _byname_rec(_byname_check(lambda: _cast(T, _byname_anyv))({…})))`: `cast` makes checkers read `T` as a type (aliases and imported names included, no lookup by byname), `_byname_check` takes T's `_asdict` TypedDict and checks the dict display against it; the lambda never runs. Open types have no `_asdict`, so they fall to an unchecked overload. A bare spread record elsewhere (`fn((**u, **r))`) is `Any`. Measured: the type can't come from where the value goes (`fn`'s parameter): expected-type inference returned `Unknown` / `Never` and checked nothing; `type` aliases can't be called; a Protocol with a metaclass `__call__` can't be instantiated by either checker; passing an alias as a value to a generic helper fails (`TypeAliasType`). The rejected first version wrapped every `**x` in calls at runtime too, which changed valid Python (the stdlib round-trip test caught it).
- **Record types in output files** are plain stand-in classes with `__class_getitem__` returning the class, so `R[int, str]` evaluates on PyPy 3.10, where annotations are evaluated eagerly; no Protocol.
- **Scope: byname adds binding by name and nothing else.** Every form is `name=` (in calls, records and patterns), or the record type that annotates the result. General-purpose syntax that happens to be convenient is out of scope, even next to a pattern.
- **No defaults in patterns, `??` included.** `(email= ?? "none") = user` was designed and dropped. `??` is a general null-coalescing operator, not a binding form. [PEP 505](https://peps.python.org/pep-0505/) is deferred, not rejected, so if Python ever adds `??` with different meaning or precedence, byname would clash with real Python. (`name=` can't clash that way, since PEP 736 was rejected.) Records always have their fields, so a default only matters for an `Optional` field, and the plain-Python line is short: `email = user.email if user.email is not None else "none"`.
- **TS-style `(email: e = "x")` was rejected for defaults and renaming.** `:` reads as a type annotation in Python (the TS `{ name: string }` trap), and it would make building use `=` while taking apart used `:`.
- **No trailing comma required on a single-field record.** Unlike `(x)`, `(x=)` can't be read as grouping, so a comma would only be noise.
- **Positional items in patterns are left an error.** Reading A, where a bare name means by-name, conflicts with Python's `(a, b) = r`, which is positional. Reading B, positional first and then keywords like a call, is consistent, but it brings back the field-order fragility that by-name avoids, and only records support it. Not planned.
- **`for (a=, b=t) in xs:` becomes `for (a, t) in ((_ds.a, _ds.b) for _ds in xs):`**, on one line so line numbers hold. A generator, not binds at the top of the body: the body's first statement may be compound (`a = …; if …:` is invalid), and the generator keeps exact field types and scopes `_ds`. The same rewrite covers comprehension `for` clauses. The iterable ends at the statement's `:`, or for a comprehension at the next `if`/`for`/`async` or the closing bracket. A bare tuple iterable (`in a, b`) is parenthesised; `async for` gets an async generator. Costs a generator per loop, which is fine for judges' time limits but not free.
- **Nested patterns read through chains:** `(user=(name=)) = r` binds `name = _ds.user.name`, with no temporary per level. A `field=(...)` value is a nested pattern only if the group holds a `field=` item; `(x=(a)) = r` stays a parenthesised target. In a `for` target the nested parens stay in the tuple target and the generator's element mirrors them.
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
- **Output files use portable record classes** (`class R(_NT):` with `object` fields, Python 3.6+), because judges' PyPy is 3.10 and rejects `class R[T0](...)`. The checker's translation keeps the 3.12 generic classes, since that's where field types come from.
- **`_replace` and `_asdict` are typed for the checkers only.** NamedTuple types them `(**kwargs: Any)` and `dict[str, Any]`, so `r._replace(n=90)` and a stale `GPT(**cfg._asdict())` passed silently. Each record class has a checkers' version (`if _t.TYPE_CHECKING:` ... `else:` the runtime class) that declares `_replace(self, *, name: T0 = ..., ...)` and `_asdict() -> _dct_<fields>[...]` (a generic TypedDict, shown as `{name: str, ...}` in hover). basedpyright and mypy both complete and check field names, and check `**rec._asdict()` against the callee's parameters. They can't exist at runtime: NamedTuple refuses to let a class override them. The checkers' complaint about overriding final methods is silenced in the generated code. Measured: `if TYPE_CHECKING` must be spelled so both checkers recognise it; `from typing import TYPE_CHECKING as _TC` isn't (pyright then reads both branches, mypy reports a redefinition), `import typing as _t` + `_t.TYPE_CHECKING` is, and keeps `TYPE_CHECKING` out of the user's namespace. An `if` inside a NamedTuple body is "Invalid statement" to mypy, so the earlier in-class block was basedpyright-only. New public names like `replace`/`asdict` were rejected: they'd take names from fields and add API beyond binding by name. Output files don't get any of this.

## Not yet built

**Destructuring in parameters:** `def f((name=, age=): User)` with `type User = (name: str, age: int)`, TypeScript's `function f({ name, age }: User)`. The fields are written twice, as in TypeScript; an alias keeps it short.
- **Translation:** `def f(_p0: User):` with `name = _p0.name; age = _p0.age; ` prefixed to the body's first line, to keep line numbers. Fine before a simple statement or a docstring (`"""doc"""; name = ...` keeps the docstring). It breaks when the body starts with a compound statement (`if`, `for`, `try`), since `a = 1; if x:` is invalid. Options: an error asking for a simple first statement, or a cleaner trick if one turns up. (Unlike `for` targets, Python has no destructuring slot here, since Python 3 dropped tuple parameters, PEP 3113, so it can't stay on the `def` line.)
- **The parameter has no name,** so it can't be passed by keyword.

**Ruled out along the way:**
- **Partial cast `cast((age: int, ...), r)`**, meaning "same record, `age` now `int`": needs an intersection type. Use a full `cast((name: str, age: int), r)`; it's checker-only, like TypeScript's `as`.
- **A generic merge, `a.merge(b)` or `def merge[A, B](a: A, b: B) -> A & B`**: the merged type of two unknown types is an intersection. Spreads (below) work instead because the target type is written out, or the result is `Any`.
- **Narrowing a record by a field:** `isinstance(r.age, int)` narrows `r.age`, not `r`, so `r._replace(age=99)` on a mixed union still fails. Keep record lists to one type, or annotate them (`rs: list[(name: str, age: int)] = [...]`).
- **`**` of a union of records:** with a mixed list, `fn(**rs[0]._asdict())` is flagged even when `fn` accepts both types, because basedpyright unpacks a union of TypedDicts as `object` values. Same remedy: one type per list.

**Smaller items:**
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
