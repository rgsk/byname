# byname

Bind-by-name for Python: pass locals by name, return them by name, and destructure them by name, with full
type inference in the editor. You write `.pyn` files; byname turns them into plain Python.

```python
def make(*, name: str, age: int):
    greeting = f"hi {name}"
    return (name=, age=, greeting=)    # record: return locals by name

res = make(name=, age=)                # call: pass locals by name
(greeting=, age=years) = res           # destructure by name, any order, with rename
print(res)                             # (name='Rahul', age=26, greeting='hi Rahul')
```

Hover `years` in the editor and you get `int`; hover `make` and you get
`-> (name: str, age: int, greeting: str)`. A misspelled field is a type error.

Status: experimental. Requires Python 3.12+.

## The syntax

Everything is built on one piece of syntax, `name=`, from [PEP 736](https://peps.python.org/pep-0736/)
(rejected). In `field=value`, the left side is always the field or parameter name and the right side is
always your local. A bare `field=` means `field=field`.

| Form | Example | Means |
|---|---|---|
| call | `fn(name=, age=age + 1)` | `fn(name=name, age=age + 1)` |
| record | `(name=, score=99.5)` | an immutable record with fields `name`, `score` |
| destructure | `(name=, age=) = r` | `name = r.name; age = r.age` |
| rename | `(name=who) = r` | `who = r.name` |
| loop | `for (name=, age=) in rs:` | each item destructured, also in comprehensions |
| record type | `def f() -> (name: str, age: int):` | a record with these fields, in any order; `(..., …)` for at least these |

Every form is a syntax error in plain Python, so byname never changes the meaning of valid Python code.

**Records** are read by name, never by position, so field order never matters: reorder the fields where a
record is built and nothing that reads it changes meaning.
- **Access:** `.field` access, destructuring by name, `rec._asdict()` and `rec._replace(age=27)`. Reading
  by position (`a, b = rec`, `rec[0]`, `for v in rec`, `f(*rec)`, `sorted(recs)`) is a checker error and
  raises `TypeError` at runtime, and the editor offers a quick fix: `x, y = get_batch()` becomes
  `(x=, y=) = get_batch()`. Want positions? Return a tuple. Like a dict, `"name" in rec` asks for a field
  name and `{**rec}` gives a dict (`json.dumps({**rec})`; `json.dumps(rec)` raises).
- **`rec.name` is checked, `rec["name"]` isn't:** brackets take any string key and give `Any`, on any record
  type, open types and unions of records included. It's the way around the checker when you know better
  (`if "title" in p: p["title"]`); a field that isn't there is a `KeyError` at runtime.
- **Copies are checked:** records are immutable, so change one with `rec = rec._replace(age=27)`. The editor
  completes and checks `_replace`'s field names and types, and checks `f(**rec._asdict())` against `f`'s
  parameters, so a field `f` doesn't take is flagged.
- **Equality** is by name and value: the same fields with the same values, in any written order, are equal
  and hash alike, so `(x=1, y=2) == (y=2, x=1)`. A record never equals a plain tuple.
- **They print the way you write them:** `(name='Rahul', age=26)`.
- **Records or tuples:** a tuple fits values unpacked right where they're returned (`x, y = get_batch()`),
  and hover labels each position with the name returned there, a bare name or a walrus:
  `return x, y.to(device)` shows `-> tuple[x: Tensor, Tensor]`, and
  `return (x := x.to(device), y := y.to(device))` shows `-> tuple[x: Tensor, y: Tensor]` (a walrus name
  there isn't reported unused). Any other
  expression has no label, and a written `-> tuple[...]` wins. Display only: at runtime it's a plain tuple,
  and `.x` is an error. A record fits a value that's kept, passed around, has many fields or may grow:
  adding a field breaks no reader.
- **Form:** one field needs no trailing comma (`(name=)`). Records can nest, and can appear anywhere an expression can, including comprehensions and lambdas.
- **Field names** can't start with `_`, and can't repeat.

**Record types** are written the way hover shows them, `(name: str, age: int)`, anywhere an annotation
goes, including `type` aliases. A record type means exactly these fields, in any order:

```python
type User = (name: str, age: int)
u: User = (age=26, name="Rahul")             # fine: field order doesn't matter
x: User = (age=26, name="Rahul", po="x")     # error: extra field: po

def birthday(p: (..., age: int)) -> int:     # `...`: at least these fields; others are fine
    return p.age + 1

def log(p: (...)) -> None:                   # any record
    print(p["msg"])                          # brackets: unchecked, see Access above
```

`...` goes first, so a long type says it's open before listing its fields; anywhere else it's an error.

The written order is only for display: hover, printing, `_asdict()` and `**rec` show the fields as you
wrote them. To a checker, records with the same fields are the same type whatever the order, so
`rs.append((age=1, name="a"))` on a list of `(name=, age=)` records is fine.

You rarely need a record type, since return types are inferred. Two cases where you do:
- **Recursive functions:** checkers can't infer through the recursive call, so its fields come out `Unknown`.
- **Functions that take part of a config:** `def get_batch(cfg: (..., batch_size: int, block_size: int))`
  accepts any config with those fields, while `f(**cfg._asdict())` requires an exact match.

Annotating a recursive function's return type:

```python
def dfs(node: TreeNode | None) -> (height: int, diameter: int):
    if node is None:
        return (height=0, diameter=0)
    (height=lh, diameter=ld) = dfs(node.left)
    ...
```

**Spreads:** records work with `**`, by field name, like dicts:

```python
u = (name="rahul", age=26)
r = (sex="male", surname="gupta")

fn(**u, **r)                     # into a call: checked against fn's parameters
{**u, **r}                       # into a dict
(**u, age=27)                    # build a record: a later field wins, like {**a, **b}

type Person = (name: str, age: int, sex: str, surname: str)
def greet(p: Person): ...
greet((**u, **r))                # checked against greet's parameter: missing / extra / wrong-typed fields are errors
greet(p=(**u, **r))              # by keyword too
p: Person = (**u, **r)           # checked against Person
def make() -> Person:
    return (**u, **r)            # checked against `-> Person`
(name: str, age: int, sex: str, surname: str)(**u, **r)   # or name the type inline
```

A spread record is checked against the type expected where it stands, so you never have to name one.
With no type expected, like `x = (**u, **r)`, it still gets its exact type when every spread is a name
the file shows the fields of: bound once, to a record or a spread of such names, or annotated with a
record type (a parameter's too). Then `x` is `(name: str, age: int, sex: str, surname: str)` and a typo
like `x.surnme` is an error. Otherwise (a name bound twice, from a call, an import) it's `Any`. Overloads work: `f(x: int)` / `f(p: Person)` picks the `Person` one for
`f((**u, **r))`.
A field can't be called `keys`: records have a `keys()` method, which is what lets `**` work.

**From a dict:** `record(T, d)` turns a mapping into a record of type `T`, checked at runtime. Data loaded
from a file arrives as a dict, and `(**d)` can't be checked: the checker rejects it, and at runtime it builds
whatever keys `d` has.

```python
from byname import record

type ModelConfig = (n_layer: int, dropout: float)

cfg = record(ModelConfig, json.loads(text))      # typed as ModelConfig
record(ModelConfig, {"n_layer": "4", "dropuot": 0.2})
# TypeError: ModelConfig: field 'n_layer': expected int, got str ('4'); missing field 'dropout'; unexpected field 'dropuot'
record(ModelConfig, {"dropout": 0.0, **saved})    # a file older than the dropout field: the left is the default
record(ModelConfig, {**saved, "dropout": 0.0})    # one field changed: the right wins
```

- **Names:** every field of `T` must be there, and no others. An open type, `(..., n_layer: int)`, keeps extra keys.
- **Types,** by the checker's rules: `isinstance` for classes (an `int` is a `float`, a `bool` is an `int`), unions,
  `None`, `Literal`. A nested record type takes a dict too, and is built into a record. Generics are checked
  shallowly: `list[int]` checks for a `list`, not its elements.
- **Every problem in one `TypeError`,** with dotted paths into nested records (`field 'cfg.lr'`).
- **Not in output files:** it needs byname at runtime, which output files don't have.

**Destructuring** is plain attribute access, so it works on any object, not only records:

```python
(real=re, imag=im) = 3 + 4j            # builtins
(email=) = user                        # dataclasses, any object with attributes
(sep=, curdir=) = os                   # even modules
(x=self.x, y=self.y) = (x=, y=)        # targets can be attributes or subscripts
(name=, name=alias) = r                # one field into two locals
(a=b, b=a) = (a=, b=)                  # swap
for (x=, y=) in pts: ...               # for-loop targets
[x * y for (x=, y=) in pts]            # comprehension targets
(id=, user=(name=, age=a)) = r         # nested: name = r.user.name
```

**In parameters,** like TypeScript's `function f({ name, age }: User)`:

```python
type User = (name: str, age: int)

def greet((name=, age=): User, loud: bool = False) -> str:
    ...                                # name: str, age: int; a typo like (nme=) is an error on the pattern
def f((id=, user=(name=n))): ...       # nested, renamed, or with no type
def f((name=, age=): User = (name="anon", age=0)): ...   # a default goes after the type
```

A pattern parameter has no name, so it can't be passed by keyword; hovers show it as `(...)`. To take a
record by keyword, name the parameter and destructure it on the body's first line:

```python
def send(*, user: User):
    (name=, age=) = user
```

`user=(name="x")` in a signature is a default record, as in Python, never a pattern.

**Not planned:**
- **Dict keys:** destructuring reads attributes, not keys. Dict syntax would buy nothing over records,
  and dicts lose per-key types (`dict[str, str | int]`).
- **Positional items in a pattern,** like `(a, b=) = r`. Fields are read by name so field order never
  matters; positional items would bring that fragility back.
- **Shorthand in a parameter's default,** like `def f(name=)`, `def f(user=(name=))` or
  `lambda x=g(name=): x`, at any depth. A default is evaluated where the function is defined, so `name=`
  there would quietly take the outer `name`, though in a signature it reads as the parameter. Write
  `name=name` when that's what you mean: `def f(user=(name=name))` is a record holding the outer `name`
  as it was when the `def` ran.
- **Syntax that isn't binding by name,** like defaults with `(email= ?? "none") = user`. Python has no
  `??` ([PEP 505](https://peps.python.org/pep-0505/) is deferred), so byname leaves it to plain Python,
  `email = user.email if user.email is not None else "none"`, rather than invent an operator that a
  future Python might define differently.
- **Flagging a swapped unpack,** like `y, x = get_batch()` when it returns `x, y`. It could only catch a
  swap under the returned names, never `yb, xb = get_batch()`, the same mistake under names of your own, so
  it would suggest unpacks are checked when most aren't. Hover shows the order where you write the unpack.
- **Labels written in a tuple type,** like `-> tuple[x: Tensor, y: Tensor]`. It's valid Python already
  (two slices, `x:Tensor` and `y:Tensor`), and up to Python 3.13 it raises `NameError` when the `def`
  runs. Nothing would check the labels against what's returned either. Leave the function unannotated and
  hover shows `tuple[x: Tensor, y: Tensor]` from `return x, y`; annotate a record for names that are checked.
- **Mutable records,** like `{name="rahul", age=26}` with `m.age += 1`. Immutability is what lets a record
  be passed without the caller wondering whether it changed, and what makes it hashable (a set member,
  a dict key). Code that changes values mostly changes keyed collections (`cnt[x] += 1`), not named
  fields; for the rest there's `rec = rec._replace(age=rec.age + 1)` or a `@dataclass`. Braces couldn't
  carry it cleanly either: `{**a, **b}` is a dict and `{name: str}` parses as one, so neither a spread-only
  value nor the type could be written that way.

[`examples/all.pyn`](examples/all.pyn) runs every feature, one assert per use.

## Install

byname isn't on PyPI yet. Add it to a project from a local checkout:

```
uv add --editable ../path/to/byname
uv add --dev basedpyright ruff            # editor checker, formatter and linter (optional)
```

## Command line

| Command | What it does |
|---|---|
| `byname run file.pyn [args]` | run a `.pyn` script. Tracebacks point at `.pyn` lines |
| `byname show [--no-main] file.pyn` | print the plain-Python translation (`--no-main` drops the `if __name__ == "__main__":` block) |
| `byname format [--check] files…` | format `.pyn` files with Ruff |
| `byname fix [--check] files…` | apply Ruff's safe lint fixes to `.pyn` files (what `source.fixAll` does on save) |
| `byname tool <cmd> [args] file.pyn` | run Ruff or basedpyright on `.pyn` files, with positions mapped back |
| `byname lsp [-- checker cmd]` | language server (see below) |

`import byname` installs an import hook. After that, `import foo` finds `foo.pyn`, and `.py` and `.pyn`
modules can import each other.

## Editor support (VS Code)

```
editor <--LSP--> byname lsp <--LSP--> basedpyright
```

`byname lsp` translates each `.pyn` to Python in memory, hands it to basedpyright, and maps every
position back. Hidden translations live in `~/.cache/byname/`, never in your project.

**byname is the workspace's Python language server:** it serves `.pyn`, `.py` and Jupyter notebooks,
so turn the BasedPyright (or Pylance) extension off in a workspace where byname is on. `.py` files and
notebooks without `%load_ext byname` go to basedpyright as they are. In a notebook with
`%load_ext byname`, every cell is checked as `.pyn` (see [Notebooks](#notebooks)). In a plain Python
project, use any language server you like.

**Tool config lives in `pyproject.toml`, editor behaviour in `.vscode/settings.json`.** Ruff and
basedpyright read their own config, `[tool.ruff]` and `[tool.basedpyright]` (or `ruff.toml`,
`pyrightconfig.json`), the same for the editor, `byname tool` and CI. VS Code `ruff.*` settings do
nothing (byname runs the project's `ruff`), and `basedpyright.analysis.*` ones are ignored once the project
has a basedpyright config (without one they apply, in the editor only, `standard` mode if unset), so keep
both in `pyproject.toml`:

```toml
[tool.basedpyright]
typeCheckingMode = "standard"

[tool.ruff]
line-length = 100
``` Formatting and Ruff's fixes on save are served for `.pyn` and `.py` (the project's
Ruff and `[tool.ruff]` config), and for notebooks (byname or plain) as notebook-wide actions: Ruff sees
the whole notebook, so an import used in a later cell isn't removed. The Ruff extension isn't needed;
disable it in the workspace so its fixes don't run alongside. In VS Code:

```jsonc
"[python]": {
  "editor.defaultFormatter": "rgsk.byname",
  "editor.formatOnSave": true,
  "editor.codeActionsOnSave": { "source.fixAll": "explicit", "source.organizeImports": "explicit" }
},
"notebook.defaultFormatter": "rgsk.byname",
"notebook.formatOnSave.enabled": true,
"notebook.codeActionsOnSave": { "notebook.source.fixAll": "explicit", "notebook.source.organizeImports": "explicit" }
```

**What you get in `.pyn` files:**
- **Navigation and editing:** hover, completion, go to definition, rename, outline, and colours from semantic highlighting.
- **Errors** shown where you wrote the code.
- **Formatting:** Format Document and format-on-save.
- **Ruff fixes on save:** `source.fixAll` and `source.organizeImports` in `editor.codeActionsOnSave` work as in `.py` files, e.g. `List[int]` becomes `list[int]` and the unused `typing` import goes.
- **Readable record types:** hover and error messages show `(name: str, age: int)`, not the generated class name.
- **Field suggestions inside a pattern,** like TS's `const { | } = fn()`. `(name=, |) = make(...)` offers the remaining fields, and typing `,` inside a pattern opens the list.
- **Ctrl+click on a shorthand name** (`name` in `fn(name=)`) goes to the **local variable**. *Go to Declaration* goes to the parameter.

**Install the extension:**

```
uv sync --all-extras                       # installs basedpyright
cd editors/vscode && npm install
vsce package --allow-missing-repository --skip-license -o byname.vsix
code --install-extension byname.vsix
```

The extension starts `<workspace>/.venv/bin/byname lsp`, else `byname lsp` from `PATH`; with neither (a project
without byname) it stays off.

| Setting | Default | |
|---|---|---|
| `byname.serverCommand` | `[]` | command that starts the server, e.g. `["uv", "run", "byname", "lsp"]` |
| `byname.checker` | `[]` | checker command behind byname, e.g. `["pyright-langserver", "--stdio"]` (default: basedpyright) |
| `byname.outputOnSave` | `false` | on save, write the translation next to the file as `<name>.pyn.py` |
| `byname.outputStripMain` | `false` | leave the `if __name__ == "__main__":` block out of `<name>.pyn.py` |
| `byname.diagnosticsOnSave` | `false` | new errors appear on save, not while typing; fixed ones disappear at once |
| `byname.pythonDiagnostics` | `true` | `false`: `.py` files and plain notebooks show no errors or warnings, only syntax errors and faded unused code (check them with `byname tool basedpyright`); `.pyn` always shows all |

The command **"byname: Write Python Output"** writes `<name>.pyn.py` on demand.

**Type checker:** basedpyright only, see [Type checkers](#type-checkers).

## Lint, type-check, format

`byname tool` mirrors the project into `~/.cache`, with `.pyn` files translated and everything else
symlinked. It runs the tool there and maps `file:line:col` back. Your project's tool config applies.
Notebooks work too: one with `%load_ext byname` has each code cell translated, and errors point at
the cell as written (`nb.ipynb:cell 4:2:5` from Ruff, `nb.ipynb:3:2:5` from basedpyright, which counts
code cells only). `.py` files and plain notebooks are checked as they are, with basedpyright in
`standard` mode unless the project configures it.

```
byname tool ruff check --output-format=concise file.pyn   # or .py, .ipynb
byname tool basedpyright file.pyn
```

### Type checkers

**basedpyright (or pyright) is the only supported checker.** Every feature is designed and tested against
it, messages are rewritten in byname's terms (`extra field: po`, `records are read by name: (x=, y=)`),
and the editor's quick fixes rely on it. Other checkers (mypy, Pyrefly, ty, ...) are not supported.
What runs never depends on the checker, so your code runs the same whichever one you use.

`byname format` runs `ruff format`. It swaps byname syntax for short plain-Python stand-ins, formats,
then swaps them back:
- **Width:** the stand-ins add a few characters per shorthand, so a line right at your length limit can wrap one step early.
- **Reserved names:** files that use the names `__p` or `__P` are refused.
- **Keeping a line as written:** `# fmt: skip` works the same as in `.py` files.
- **Grids:** if the first line inside a split bracket holds several items, later lines are packed up
  to its width instead of one item per line. Put one item on the first line to get Ruff's layout back.

  ```python
  s = [
      1, 2, 3,
      4, 5, 6,
  ]
  bfs(
      n, m, k,
      start=(sr, sc),
      blocked=walls,
  )
  ```

  A `from m import (` grid survives organize imports: Ruff sorts the names, and they're packed again
  with as many on the first line. Ruff's I001 calls a grid "un-formatted"; `byname tool ruff check` hides
  that finding when organizing the file would change nothing.

`byname fix` runs `ruff check` on the translation, so `f(os=)` counts as a use of `os`, and applies
the safe fixes whose edits fall on code you wrote. Fixes that would touch generated code are skipped.

## Tests in `.pyn`

pytest collects `test_*.pyn` (any `python_files` pattern, with `.pyn` for `.py`) wherever byname is
installed: the plugin is registered through pytest's `pytest11` entry point, nothing to configure.
Asserts are rewritten as in `.py` tests (`assert 26 == 27`), failures point at `.pyn` lines, and a test
imports its `.pyn` and `.py` neighbours. VS Code's Testing panel and the run buttons in the gutter work
as for `.py` files once pytest is enabled (`"python.testing.pytestEnabled": true`). byname's own
`tests/test_records.pyn` is an example.

## Notebooks

In Jupyter (VS Code, JupyterLab, Colab), run `%load_ext byname` in a cell; every cell after it is `.pyn`:

```python
%load_ext byname
```
```python
def stats(xs: list[float]):
    return (mean=sum(xs) / len(xs), n=len(xs))

(mean=, n=) = stats([1.0, 2.0, 3.0])
```

Magics (`%time x = (a=1)`), `!shell` lines and top-level `await` work as in Python cells, records
pass between cells (equal by name and value whichever cell built them), and tracebacks quote the cell
as written, at its own line numbers. The kernel's `byname` must be the one installed in the notebook's
environment, and `import foo` finds `foo.pyn` there too. `%unload_ext byname` turns it off.

In VS Code, the byname extension checks the cells as `.pyn` (hover, errors, completion, formatting), each
cell the way the kernel runs it, with names flowing from cell to cell. Typing `%load_ext byname` into
a cell switches the notebook over at once. See [Editor support](#editor-support-vs-code).

## Output files

`<name>.pyn.py` is plain Python with no dependency on byname. It's useful for handing code to something
that doesn't have byname, such as an online judge:

```python
# pyn output of sol.pyn (generated by byname: edit the .pyn, not this file)
from typing import NamedTuple as _NT
...                                    # generated header, one class per record shape
# ---- sol.pyn ----
...                                    # your code, translated
```

The generated header uses plain `NamedTuple` classes, so output files run on Python 3.6+ (judges often run
PyPy 3.10). The checker's hidden translation types records as Python 3.12 generic Protocols instead, for field types.

## How it works

```python
def make(*, name: str, age: int):          from typing import NamedTuple as _NT
    return (name=, age=)          ──▶      class _rec_name__age[T0, T1](_NT):
                                               age: T1; name: T0
res = make(name=, age=)                    _byname_setup(_rec_name__age, ('name', 'age'))
                                           def make(*, name: str, age: int):
(name=, age=) = res                            return _rec_name__age(name=name, age=age)
                                           res = make(name=name, age=age)
                                           _ds = res; name = _ds.name; age = _ds.age
```

At runtime each record shape is a NamedTuple that stores its fields sorted by name (so equality and hashing
ignore the written order) and shows them in the order written. The checker sees a generic Protocol instead,
the same type `(name: str, age: int)` is, so it infers `make() -> (name: str, age: int)` without annotations
and has no positional access to offer. The translation keeps every line on the same
line number. The generated classes go in a header that's spliced in, which is why tracebacks and editor
positions line up with your `.pyn`.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for design decisions, status and the code map.

```
uv sync --all-extras
uv run pytest
```

`BYNAME_DEBUG=1 byname tool ...` also prints what byname hid from the tool's output: findings on its
generated code, and unused-variable warnings on walrus labels. Without it the output reads like the
tool's own, totals recounted.
