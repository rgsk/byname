# byname

Bind-by-name shorthand for Python, in `.pyn` files. All three forms use the `name=` shorthand
from PEP 736 (rejected):

```python
def make(*, name: str, age: int):
    return (name=, age=)          # record: return locals by name

res = make(name=, age=)           # call: pass locals by name
(name=, age=) = res               # destructure: bind fields by name, any order
(name=n) = res                    # rename: n = res.name
```

The left of `=` is always the field, the right is always the local, in all forms.
Destructuring is plain attribute access, so it works on any object: `(real=re, imag=im) = 3 + 4j`.

Each form is a SyntaxError in plain Python, so valid Python is never changed by the transform.

## How it works

`.pyn` → plain Python (`byname show file.pyn`):

```python
from typing import NamedTuple as _NT
class _rec_name__age[T0, T1](_NT): name: T0; age: T1

def make(*, name: str, age: int):
    return _rec_name__age(name=name, age=age)

res = make(name=name, age=age)
_ds = res; name = _ds.name; age = _ds.age
```

Records are generic NamedTuples, so pyright infers `make() -> _rec_name__age[str, int]` and
`name: str` with no annotations, and a misspelled field is a type error. Records also unpack
positionally and support `.name` access.

## Run

```
uv run byname run examples/main.pyn
```

`import byname` installs an import hook, after which `.pyn` modules import like `.py` ones.
Line numbers in tracebacks match the `.pyn` file.

## Parked designs

Decided in discussion, not built. Editor support comes first.

**Dict literals and dict destructuring.** In braces, `=` means a key written literally and `:` keeps its
normal Python meaning (a computed key):

```python
{name=, age=26, 'first-name'=f}   # → {'name': name, 'age': 26, 'first-name': f}
{name=, age=} = d                 # name = d['name']; age = d['age']
{key: v} = d                      # v = d[key]
```

Parked because dicts lose per-key types (`dict[str, str | int]`), and records `(name=, age=)` are the
form that keeps exact types.

**Defaults, `??` (PEP 505).** `x ?? d` inside a `field=…` entry means "x, or d if x is None". It
reaches to the edges of its entry, so no operator-precedence handling is needed.

```python
(email= ?? "none") = user     # email = user.email if user.email is not None else "none"
(email=e ?? "none") = user    # with rename
(age= ?? 18)                  # building a record; narrows int | None → int
fn(age= ?? 18)                # in a call
```

- Triggers on `None` only, not on a missing field. `getattr(obj, f, d)` types as `Any | T` and hides typos.
- Rejected: TS-style `(email: e = "none")`. `:` reads as a type annotation in Python (the TS
  `{ name: string }` trap), and it would make renaming use `:` while building uses `=`.
- Rejected: `(email?="none")`. It leaves no place for a rename.

## Editor support

```
editor <--LSP--> byname lsp <--LSP--> basedpyright (or any checker)
```

`byname lsp` translates each `.pyn` into a shadow `.py`, hands it to a type checker, and maps
positions both ways: hover, completion, go-to-definition, rename and diagnostics all work in `.pyn`.
Shadows live in `~/.cache/byname/`, so nothing is written to your project.

```
uv sync --all-extras                                   # installs basedpyright
cd editors/vscode && npm install && vsce package --allow-missing-repository --skip-license -o byname.vsix
code --install-extension byname.vsix
```

The extension starts `<workspace>/.venv/bin/byname lsp` (override with `byname.serverCommand`).
Swap the checker with `"byname.checker": ["pyrefly", "lsp"]`. The checker must infer return types
of unannotated functions: pyright, basedpyright and Pyrefly do; ty doesn't yet.

## Status

- [x] transform, import hook, CLI
- [x] language server (proxy) + VS Code extension
- [ ] semantic highlighting (dropped in the proxy for now; TextMate Python grammar is used)
