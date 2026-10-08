# Open issues

Found while writing `tests/test_exhaustive.pyn`; to look at later.

## Ruff B008 on a record default

```python
def welcome_or_guest(user=(name=): Person = (name="guest", age=0)) -> str: ...
```

`byname tool ruff check` reports `B008 Do not perform function call `(name, age)` in argument defaults`.
The translation turns the record into a class call, `_rec_name__age(name="guest", age=0)`, and B008 flags
any call in a default. Records are immutable NamedTuples, so the default is as safe as a tuple one: B008's
concern (one mutable object shared across calls) doesn't apply. Every record default in user code hits
this. Likely fix: `byname tool` / the lint pass drops B008 when its range is a record literal (the same
mapping that already hides diagnostics on generated code). Suppressed with `# noqa: B008` in the test for now.

## Ruff B008 on spreads that take the generic path (regression from dfb09f7)

```python
person, place = (name="ann", age=30), (surname="lee", city="oslo")   # `person` bound elsewhere too
c: Contact = (**person, **place)
```

`byname tool ruff check` reports `B008 Do not perform function call `_byname_kw` in argument defaults`,
twice per line. dfb09f7 reads spread names through lambda defaults so a rebound name keeps its narrowing:
`lambda _byname_t, _byname_s0=_byname_kw(person): ...`. Ruff lints the checker translation and sees a call in
a default; `byname tool` only drops diagnostics in the generated header, and ones inside a user's line are
moved onto it (on purpose, so errors in generated reads still show). It hits any spread of a name that
isn't "known" (bound more than once in the file, a parameter without a record type, ...). Likely fix:
make the default the bare name, `_byname_s0=person` (no call, nothing for B008), and call `_byname_kw` in the
lambda body: `{**_byname_pick(person, _byname_kw(_byname_s0))}`; the default still carries the narrowed
type. Suppressed with `# noqa: B008` on the two lines in `test_exhaustive.pyn` for now.

## Checker messages quote the generated code of a spread record

```python
reveal_type(lookup((**person, **place)))
```

gives `Type of "lookup(_byname_arg(lambda _byname_t, _byname_s0 = _byname_kw(person), ...))" is "str"`.
The verdict is right; the quoted expression is byname's checker translation of `(**person, **place)`.
Any message that quotes an expression containing a spread record shows it (hover over such a call too,
probably). `pretty()` in `lsp.py` already rewrites `_rec_…[...]` class names into `(name: str, ...)`; it
would need to turn the whole `_byname_arg(lambda …)` / `_byname_ctx(lambda …)` call back into
`(**person, **place)`, e.g. by mapping the quoted span back to source text. Seen in
`test_exhaustive.pyn` section 10, which asserts only the end of the message for now.

## A record doesn't widen to a wider field type

```python
def f(p: (a: int | str)) -> None: ...
x = (a=1)
f(x)        # error: "(a: int)" is not assignable to "(a: int | str)"
f((a=1))    # fine: the literal is inferred against the expected type
```

Records are generic Protocols in the checker, and `_replace(self, *, a: T0 = ...)` takes the field types as
parameters, so pyright infers the type parameters invariant. It was the same before records became
Protocols (the NamedTuple version failed the same way), so not a regression. Likely fix: covariant type
parameters (old-style `TypeVar(..., covariant=True)`, silencing the "covariant in a parameter" complaint
on `_replace`), since records are immutable; check that `_replace`'s field checking survives it.

## A function returning records in two orders reports positional reads twice

```python
def branches(flag: bool):
    if flag:
        return (x=1, y="s")
    return (y="t", x=2)
c, d = branches(True)   # two "not iterable" errors, one per order
```

The inferred return type is a union of the two exact types, which are the same type structurally but print
in their written orders, so the union isn't merged. One error (and one quick fix) would be enough; the
proxy could drop a positional-read diagnostic at the same range as one it already kept.

## `"name" in rec` doesn't narrow a union of records

basedpyright narrows `"name" in td` on a union of TypedDicts (both branches, and for a local whose type is a
literal), but only for TypedDicts: records are Protocols for the checker, so after `if "name" in user:` a
`user: (name: str) | (age: int)` stays the union and `user.name` is an error. A `TypeIs` helper with one
overload per field name narrows records in both branches (measured), but the translation can't tell whether
the right side of `in` is a record, so it would rewrite every `in` (or every `"literal" in x`), and those lose
basedpyright's own checks on `in` and TypedDict narrowing. Dropped for now; `user["name"]` (unchecked, `Any`) reads it meanwhile.

## A labelled tuple widens literal values

`return name, age` with `name = "rahul"` is `tuple[name: str, age: int]` to the checker, where plain Python
gives `tuple[Literal['rahul'], Literal[26]]`: the checker translation returns `_byname_tup_name__age(name, age)`,
and pyright widens literals when it solves a generic call's type parameters (a `(t: tuple[T0, T1])` helper and
a `*Ts` one widen too). Only a caller that needs the literal sees it, a false error such as passing the first
item to a `Literal["r", "w"]` parameter. Workaround: annotate the function (`-> tuple[Literal["r", "w"], int]`);
annotated functions aren't rewritten. Accepted: labels in hover are worth more than literals here. Showing the
labels in hover text only (the proxy) would keep literals but work only on the definition's own hover.

## No syntax for function types; keyword-only callbacks can't be typed

```python
def fn(*, name: str, age: int) -> None: ...

a: Callable[[str, int], None] = fn   # error: fn takes no positional arguments
b: Callable[..., None] = fn          # accepted, but b(nme="x") isn't checked

type Greet = (*, name: str, age: int) -> None   # wanted; today a syntax error
```

byname favours keyword-only functions, and `Callable` can only describe positional parameters, so a keyword-only
function passed as a value, or held in a record field, has no type short of a hand-written Protocol with
`def __call__(self, *, name: str, age: int) -> None: ...` (measured: that checks calls, missing `age` included).
Plan in DEVELOPMENT.md, "Designed, not built": translate `(params) -> T` to that Protocol for the checker.

## `conftest.pyn` is never loaded

```python
# src/conftest.pyn
@pytest.fixture(autouse=True)
def _seed():
    torch.manual_seed(0)
```

The fixture silently never runs: no error, no warning, and tests that don't depend on it still pass. Found in
llm-final, where a probe test saw `torch.initial_seed() == 2669112705319434512` instead of 0. pytest finds
conftest files by the literal name `conftest.py` (`_getconftestmodules` / `_try_load_conftest`), and
`pytest_plugin.py` only hooks `pytest_collect_file`, which sees test modules, not conftests. Likely fix: in the
plugin, on `pytest_collect_directory` (or `pytest_sessionstart` for the rootdir), translate a `conftest.pyn` the
way `PynModule._getobj` does and hand the module to `config.pluginmanager._importconftest`-equivalent
registration (`config.pluginmanager.consider_conftest(mod, registration_name=...)`), scoped to its folder so
its fixtures only reach tests below it. At the least, warn when a `conftest.pyn` is present. Workaround: keep
conftests as `conftest.py` (they rarely need byname syntax).

## `byname tool ruff check` without `--output-format=concise` shows findings on the generated header

```
$ byname tool ruff check src            # ruff's default (full) output format
I001 [*] Import block is un-sorted or un-formatted
 --> src/data.py:1:1
...
PYI042 Type alias `_key_train__val` should be CamelCase
 --> src/data.py:37:6
...
All checks passed!                      # and exit 0
```

Any file with a record shows I001 twice and PYI042 once on the record header (seen on llm-final's `data.pyn`
and llm's `records/second.pyn`). The Alt+L task passes `--output-format=concise` and is clean. Typed by hand,
ruff's default output gives:
- **Generated-code findings shown:** `LOCATION` matches a line that starts with `path.py:line:col`.
  In the full format, the location is on its own line, ` --> src/data.py:1:1`, below the `CODE message` line.
  So nothing is remapped, nothing is dropped, and the path shown is the mirror's `.py` rather than the `.pyn`.
- **A clean summary under them:** the snippet lines pass through too. `located_lines` stays empty, so when anything
  else was hidden (here 3 walrus labels), `recount` turns the summary into "All checks passed!" and
  `main` returns 0, right under the findings.

Likely fix: in `main`, add `--output-format=concise` to `ruff check` when the user didn't pass an output
format, as it already adds `--pythonpath` for basedpyright. Or parse the full format: a `CODE message` line,
then a ` --> path:line:col` line, then the snippet, all mapped or dropped together.

## Spreading a parameter whose record type is imported gives `Unknown`

```python
# tests/test_x.pyn                         # model.pyn: type GPTConfig = (vocab_size: int, ..., n_layer: int)
from model import GPTConfig

def test_x(cfg: GPTConfig):
    named = (**cfg, name="small")
    reveal_type(named)                     # Unknown
```

The same code with the alias defined in the same file reveals `(a: int, b: int, name: str)`, for a
parameter and for an annotated local alike. So the spread seems to find a name's fields only through
aliases defined in the file being translated; an imported alias sends it down the generic path (the same path as the B008 entry above), which types the result
`Unknown`. Nothing errors: `named` just has no type, so `named.name` and `GPT(named)` go unchecked
and the editor shows the names white. Found in llm-final (`tests/test_model.pyn`) by
`scripts/any_check.py`. Other uses of the imported alias (`cfg.n_layer`, destructuring, passing `cfg`
to a function typed with it) are checked fine; only the spread loses it. Workaround: write the record
out literally. Likely fix: resolve the parameter's annotation through the import (the checker already
knows the alias) before falling back to the generic path.

## With `reportAny` / `reportUnknownVariableType` on, a generic-path spread reports errors in generated code

Turn the two rules on (`# pyright: reportAny=true, reportUnknownVariableType=true`, which is what
llm-final's `scripts/any_check.py` does) on the file above, and `byname tool basedpyright` prints:

```
    Argument corresponds to parameter "default" in function "pop" (reportAny)
    Type of "r" is "bool | Unknown | NotImplementedType" (reportUnknownVariableType)
    Type of "c" is "Unknown | None" (reportUnknownVariableType)
  tests/test_x.pyn:6:13 - error: Return type of lambda is Any (reportAny)
```

`pop`, `r`, `c` and the lambda are byname's helpers (the record header and the spread's
`lambda _byname_t, ...`), not user code. The first three print as indented detail lines with no
location line above them: their parent, on the generated header, looks dropped while its detail lines
are kept; the lambda one is moved onto the user's line,
on purpose (see the B008 entry), but here it describes byname's code, not the user's. Likely fix: type
the helpers so the strict rules have nothing to say (`_byname_kw` and friends returning concrete
types), and drop a diagnostic's detail lines when their parent was dropped.

## Hover shows a destructured parameter as `(...)`, which is open-record syntax

```python
class GPT(nn.Module):
    def __init__(self, (vocab_size=, block_size=, n_embed=, n_head=, n_layer=, dropout=): GPTConfig): ...

model = GPT(cfg)    # hover on GPT: class GPT((...): GPTConfig)
```

Nothing in this code is open. `(...)` is how byname writes an open record type with no fields
(`_opn_ -> (...)`, and `(..., name: str)` with fields), so the hover reads as "GPT takes an open
record" when the parameter is really a closed `GPTConfig` destructured into names. `pretty()` in
`lsp.py` prints `(...)` for three different things: the empty open type, a spread record with unknown
fields (`_byname_AnyRec`), and a parameter pattern's parameter (`PARAM_RE`, `_byname_p0`). `fmt.py`
writes the same placeholder for a pattern (`__D: (__P[...], T)` -> `(...): T`). Found in llm-final, hovering
`GPT(cfg)` in `src/generate.pyn`. Likely fix: show the pattern itself, `(vocab_size=, block_size=, ...): GPTConfig`,
by mapping `_byname_pN` back to the Nth pattern's source text in the def. Or, if that's too much, use a
placeholder that isn't record-type syntax (say `(=…): GPTConfig`, which reads as a pattern). The unknown-fields spread
has its own problem: `(...)` there claims "open" when the truth is "unknown".
