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
| record type | `def f() -> (name: str, age: int):` | a record with these fields, in any order; `(…, ...)` for at least these |

Every form is a syntax error in plain Python, so byname never changes the meaning of valid Python code.

**Records** are generic NamedTuples:
- **Access:** `.field` access, positional unpacking (`a, b = rec`), `rec._asdict()` and `rec._replace(age=27)`.
- **Copies are checked:** records are immutable, so change one with `rec = rec._replace(age=27)`. The editor
  completes and checks `_replace`'s field names and types, and checks `f(**rec._asdict())` against `f`'s
  parameters, so a field `f` doesn't take is flagged.
- **Equality** is by value.
- **They print the way you write them:** `(name='Rahul', age=26)`.
- **Form:** one field needs no trailing comma (`(name=)`). Records can nest, and can appear anywhere an expression can, including comprehensions and lambdas.
- **Field names** can't start with `_`, and can't repeat.

**Record types** are written the way hover shows them, `(name: str, age: int)`, anywhere an annotation
goes, including `type` aliases. A record type means exactly these fields, in any order:

```python
type User = (name: str, age: int)
u: User = (age=26, name="Rahul")             # fine: field order doesn't matter
x: User = (age=26, name="Rahul", po="x")     # error: extra field: po

def birthday(p: (age: int, ...)) -> int:     # `...`: at least these fields; others are fine
    return p.age + 1
```

Order only matters where you can see it: a record you build has its fields in the order you wrote, so
`a, b = (age=26, name="R")` gives `a: int`. Through a record type the order isn't known, so `a, b = u`
gives `str | int` for each. Destructure by name instead.

You rarely need a record type, since return types are inferred. Two cases where you do:
- **Recursive functions:** checkers can't infer through the recursive call, so its fields come out `Unknown`.
- **Functions that take part of a config:** `def get_batch(cfg: (batch_size: int, block_size: int, ...))`
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
With no type expected, like `x = (**u, **r)`, it works but isn't checked: the checker can't know its
fields, so it's `Any`. Overloads work: `f(x: int)` / `f(p: Person)` picks the `Person` one for
`f((**u, **r))` (with basedpyright; mypy doesn't check spread records passed to overloads).
A field can't be called `keys`: records have a `keys()` method, which is what lets `**` work.

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

def send(*, user=(name=, age=): User): ...     # named: `user` can be passed by keyword, send(user=…)
def send(user=(name=): User = (name="anon", age=0)): ...
```

`user=(name=)` reads like the nested pattern `(user=(name=)) = x`: the thing called `user`, destructured.
An unnamed pattern can't be passed by keyword; hovers show it as `(...)`. In a signature a group of
pattern items is always a pattern, so a default record needs values: `user=(name="x")`, not `(name=)`.

**Not planned:**
- **Dict keys:** destructuring reads attributes, not keys. Dict syntax would buy nothing over records,
  and dicts lose per-key types (`dict[str, str | int]`).
- **Positional items in a pattern,** like `(a, b=) = r`. Fields are read by name so field order never
  matters; positional items would bring that fragility back.
- **Shorthand in `def` signatures,** like `def f(name=)` or a default `user=(name=, age=30)`. There it
  would mean "default to the outer `name`", and a function should almost never take outer variables by
  the same name. A `user=(...)` made only of pattern items is a named parameter pattern (above).
- **Syntax that isn't binding by name,** like defaults with `(email= ?? "none") = user`. Python has no
  `??` ([PEP 505](https://peps.python.org/pep-0505/) is deferred), so byname leaves it to plain Python,
  `email = user.email if user.email is not None else "none"`, rather than invent an operator that a
  future Python might define differently.

[`examples/all.pyn`](examples/all.pyn) runs every feature, one assert per use.

## Install

byname isn't on PyPI yet. Add it to a project from a local checkout:

```
uv add --editable ../path/to/byname
uv add --dev basedpyright ruff mypy        # editor checker, formatter, linters (optional)
```

## Command line

| Command | What it does |
|---|---|
| `byname run file.pyn [args]` | run a `.pyn` script. Tracebacks point at `.pyn` lines |
| `byname show [--no-main] file.pyn` | print the plain-Python translation (`--no-main` drops the `if __name__ == "__main__":` block) |
| `byname format [--check] files…` | format `.pyn` files with Ruff |
| `byname fix [--check] files…` | apply Ruff's safe lint fixes to `.pyn` files (what `source.fixAll` does on save) |
| `byname tool <cmd> [args] file.pyn` | run Ruff, mypy or basedpyright on `.pyn` files, with positions mapped back |
| `byname lsp [-- checker cmd]` | language server (see below) |

`import byname` installs an import hook. After that, `import foo` finds `foo.pyn`, and `.py` and `.pyn`
modules can import each other.

## Editor support (VS Code)

```
editor <--LSP--> byname lsp <--LSP--> basedpyright (or another checker)
```

`byname lsp` translates each `.pyn` to Python in memory, hands it to a real type checker, and maps every
position back. Hidden translations live in `~/.cache/byname/`, never in your project.

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

The extension starts `<workspace>/.venv/bin/byname lsp`.

| Setting | Default | |
|---|---|---|
| `byname.serverCommand` | `[]` | command that starts the server, e.g. `["uv", "run", "byname", "lsp"]` |
| `byname.checker` | `[]` | type checker behind byname, e.g. `["pyrefly", "lsp"]` (default: basedpyright) |
| `byname.outputOnSave` | `false` | on save, write the translation next to the file as `<name>.pyn.py` |
| `byname.outputStripMain` | `false` | leave the `if __name__ == "__main__":` block out of `<name>.pyn.py` |
| `byname.diagnosticsOnSave` | `false` | new errors appear on save, not while typing; fixed ones disappear at once |

The command **"byname: Write Python Output"** writes `<name>.pyn.py` on demand.

**Choosing a checker:** it must infer return types of functions without annotations, because that's
where record types come from. pyright, basedpyright and Pyrefly do. ty doesn't yet (everything shows as
`Unknown`).

## Lint, type-check, format

`byname tool` mirrors the project into `~/.cache`, with `.pyn` files translated and everything else
symlinked. It runs the tool there and maps `file:line:col` back. Your project's tool config applies.

```
byname tool ruff check --output-format=concise file.pyn
byname tool mypy file.pyn
byname tool basedpyright file.pyn
```

mypy doesn't infer return types, so records look like `Any` to it. It won't catch `(nope=) = res`;
basedpyright will.

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

`byname fix` runs `ruff check` on the translation, so `f(os=)` counts as a use of `os`, and applies
the safe fixes whose edits fall on code you wrote. Fixes that would touch generated code are skipped.

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
PyPy 3.10). The checker's hidden translation uses Python 3.12 generic classes instead, for field types.

## How it works

```python
def make(*, name: str, age: int):          from typing import NamedTuple as _NT
    return (name=, age=)          ──▶      class _rec_name__age[T0, T1](_NT):
                                               name: T0; age: T1
res = make(name=, age=)                    def make(*, name: str, age: int):
(name=, age=) = res                            return _rec_name__age(name=name, age=age)
                                           res = make(name=name, age=age)
                                           _ds = res; name = _ds.name; age = _ds.age
```

Each record shape becomes a generic NamedTuple, so the checker infers
`make() -> _rec_name__age[str, int]` without annotations. The translation keeps every line on the same
line number. The generated classes go in a header that's spliced in, which is why tracebacks and editor
positions line up with your `.pyn`.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for design decisions, status and the code map.

```
uv sync --all-extras
uv run pytest
```
