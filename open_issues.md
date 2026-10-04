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
