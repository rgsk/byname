# Enhancements

Touch-ups: things that work, but could match TypeScript more closely. Not bugs.

## "Read it checked" for an inferred record type (2026-10-27, not built)

```python
type User = (name: str, age: int)
user: User = (name="rahul", age=26)
print(user["name"])   # hint: "name" is declared on user's type: read it checked, user.name

user = (name="rahul", age=26)
print(user["name"])   # no hint

def fn() -> User: ...
user = fn()
print(user["name"])   # no hint
```

A bare name may be rebound (`user = json.load(f)`) before the read, and fix all applies the hint on save,
so only written types are hinted today.

**Plan:** ask the checker. Each `name["key"]` read gets a `reveal_type(name); ` probe via
`Translation.add_reveals` (the `diagnosticsReveals` mechanism), answered in the checker's normal pass. One
record type (`REVEAL`, `one_type`, `class_fields`; skip `SPREAD` classes) with the key: hint. Flow-sensitive
for free. Rejected: text-only rules (every binding a record literal, or a call to a local `def -> Record`),
which still miss imports and methods.

**Setting:** flat `byname.checkedReadHints`, default `true`, gating the whole hint; reveals run when it or
`diagnosticsReveals` needs them. Rejected: a parent `byname.addReveals` object (plumbing as a feature, no
Settings UI toggles, children silently dead under a disabled parent).

**Not shared with spread/rest probes:** their answer changes the translation (second round needed), and
they wrap in place, inside generated text, also outside the editor; `add_reveals` can do none of that.

**Cost:** only in files with a `name["string"]` read: AST parse ~1.5 ms + ~0.2 ms per probe (one per name
per statement), ~20 ms per edit at 100 reads. Editor only.

**Plumbing:** call `add_reveals` once (hook's asks first; the filter gets only those answers). Probe before
simple statements and the headers of `if` (not `elif`), `for`, `with`, `match`; not `while`. Skip reads
under a lambda/comprehension binding the name, statements with `name :=`, and stores. Fix all uses checker
hints only once this text's answers are in (a save right after an edit waits one save).

**Open: drop `declared_reads`?** The probes cover written types too. Keep: instant hints (fix all right
after a paste), hints `p["msg"] if p else ""` with `p: P | None`, fewer probes, costs 1-3 ms. Drop: one
path, the checker's flow analysis, ~90 fewer lines of token scoping.
