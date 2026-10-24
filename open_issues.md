# Open issues

Found while writing `tests/test_exhaustive.pyn`; to look at later.

## Ruff B008 on a record default

```python
def welcome_or_guest(person: Person = (name="guest", age=0)) -> str: ...
```

`byname tool ruff check` reports `B008 Do not perform function call `(name, age)` in argument defaults`.
The translation turns the record into a class call, `_rec_name__age(name="guest", age=0)`, and B008 flags
any call in a default. Records are immutable NamedTuples, so the default is as safe as a tuple one: B008's
concern (one mutable object shared across calls) doesn't apply. Every record default in user code hits
this. Likely fix: `byname tool` / the lint pass drops B008 when its range is a record literal (the same
mapping that already hides diagnostics on generated code). 

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
isn't "known" (bound in a branch, a parameter without a record type, ...). Likely fix:
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

## The same fields in another order aren't the same type inside a list

```python
type Item = (sku: str, qty: int)
def again(items: list[(qty: int, sku: str)]) -> int: ...
typed: list[Item] = []
again(typed)   # error: list is invariant, "Item" is not the same as "(qty: int, sku: str)"
```

The checker's classes are named by the fields in written order (`_typ_sku__qty`, `_typ_qty__sku`), so two
orders are two classes, and pyright never takes two classes as the same type argument of an invariant
`list`/`dict`/`set` (outside one they match by structure, so `f(x)` is fine). Older than site classes.
**Not fixing (2026-10-24, the user's call):** order already doesn't matter where records are matched by
structure; inside an invariant container it's a special case, and writing the type's order fixes it. Measured,
so don't re-try: one class per field set with a generic `type` alias per written order fixes the list, but
pyright drops a generic alias's name once specialized (`reveal_type` of `c: Item = ...` showed the shared
class), so hover would show one order per field set instead of each definition's. (A list of records built in
another file, `[(sku=, qty=)]` passed to `ma.total(items: list[Item])`, had the same cause and is fixed:
DEVELOPMENT.md, "One class per shape for the project".)

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
A `Literal` tag field does narrow, checked: `if s.kind == "square":` or `match s.kind:` (DEVELOPMENT.md, "Narrowing a
record by a field").

## A labelled tuple widens literal values

`return name, age` with `name = "rahul"` is `tuple[name: str, age: int]` to the checker, where plain Python
gives `tuple[Literal['rahul'], Literal[26]]`: the checker translation returns `_byname_tup_name__age(name, age)`,
and pyright widens literals when it solves a generic call's type parameters (a `(t: tuple[T0, T1])` helper and
a `*Ts` one widen too). Only a caller that needs the literal sees it, a false error such as passing the first
item to a `Literal["r", "w"]` parameter. Workaround: annotate the function (`-> tuple[Literal["r", "w"], int]`);
annotated functions aren't rewritten. Accepted: labels in hover are worth more than literals here. Showing the
labels in hover text only (the proxy) would keep literals but work only on the definition's own hover.

## A function in a record field doesn't match its type from another module

```python
# a.pyn
type Fn = (fn: Callable[[Tensor], Tensor], name: str)
def takes_fn(f: Fn) -> None: ...

# b.pyn
from a import takes_fn
def double(x: Tensor) -> Tensor: ...
takes_fn((fn=double, name="b"))   # error: "Could not bind method "double"", passes in a.pyn
```

Found in world-model-final (`../byname/feedbacks/world_model_final.md`). Within one module the literal is the
same Protocol class as the type and is matched by its type arguments; from another module it's a different
class, matched member by member. Record fields are `name: Final[T0]` in the Protocol's body, and `Final` in a
class body is a class variable, so a field holding a function is bound as a method (`self` passed as its first
argument) and the match fails. Only functions: an `int` into a `float` field and a subclass into an
`nn.Module` field pass across modules. Related to "A record doesn't widen" above (the `_replace` mismatch in
the message follows from the same comparison).

**Fix on branch `record-fields-as-properties` (8ddf084), not merged:** for checkers the fields are read-only
`@property`s (runtime keeps the class annotations, which `record()` reads), and record-literal completion
looks for property members of a type with `keys()` instead of constants. All 387 tests pass, and it was
measured to keep int → float, str-into-int errors, read-only fields, open types and exact field types.
**Blocked on colour:** pyright gives a Final class attribute `property readonly static`, which `remap_tokens`
turns into the record-field colour (`static`); a property is `property readonly`, the same token as any
user `@property` (`model.device`), so record fields would change colour, which isn't acceptable. Measured
and rejected: fields declared as `self.x: Final[T]` in a Protocol `__init__` aren't Protocol members, so
open types stopped checking their fields. To finish: the proxy must tell record-field reads from ordinary
properties and keep their `static` colour, then a test in `test_exhaustive.pyn` and the docs.
Workaround meanwhile: hold an object with a method in the field, typed by a Protocol (`(coder: Coder, ...)`
where `Coder` has `def codes(...)`); objects match across modules.

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
