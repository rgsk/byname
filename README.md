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

Every form is a syntax error in plain Python, so byname never changes the meaning of valid Python code.

**Records** are generic NamedTuples:
- **Access:** `.field` access, positional unpacking (`a, b = rec`), `rec._asdict()` and `rec._replace(age=27)`.
- **Equality** is by value.
- **They print the way you write them:** `(name='Rahul', age=26)`.
- **Form:** one field needs no trailing comma (`(name=)`). Records can nest, and can appear anywhere an expression can, including comprehensions and lambdas.
- **Field names** can't start with `_`, and can't repeat.

**Destructuring** is plain attribute access, so it works on any object, not only records:

```python
(real=re, imag=im) = 3 + 4j            # builtins
(email=) = user                        # dataclasses, any object with attributes
(sep=, curdir=) = os                   # even modules
(x=self.x, y=self.y) = (x=, y=)        # targets can be attributes or subscripts
(name=, name=alias) = r                # one field into two locals
(a=b, b=a) = (a=, b=)                  # swap
```

**Not supported (yet):**
- **Nested patterns:** `(user=(name=)) = r`.
- **Destructuring in `for` targets.**
- **Dicts:** destructuring reads attributes, not keys.
- **Positional items in a pattern:** `(a, b=) = r`.
- **Shorthand in `def` signatures.**

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

See [DEVELOPMENT.md](DEVELOPMENT.md) for design decisions, parked designs, status and the code map.

```
uv sync --all-extras
uv run pytest
```
