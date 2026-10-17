# High priority issues

## Nested records survive `_asdict()`, so they break plain-data consumers

```python
type Schedule = (warmup_steps: int, max_lr: float)
type TrainConfig = (batch_size: int, schedule: Schedule)

cfg: TrainConfig = (batch_size=64, schedule=(warmup_steps=200, max_lr=5e-4))
d = cfg._asdict()   # {'batch_size': 64, 'schedule': (warmup_steps=200, max_lr=0.0005)}
json.dumps(d)       # TypeError: _iterencode_list needs a sequence
torch.save(d, p); torch.load(p, weights_only=True)   # UnpicklingError: the record class isn't allowlisted
```

`_asdict()` and `dict(rec)` convert only the outer record, for the checker too (`d["schedule"]` is the record
type). Python's `NamedTuple._asdict()` is shallow as well, but configs nest in practice (world-model-final's
`TrainConfig`), and a nested config fails only when it is saved or loaded, far from where the nesting was added.
Repro: `examples/scratch.pyn`.

### Found in the scratchpad
- A prototype `plain()` that recurses into records, exact `list`, `tuple` and `dict` gives data that
  `json.dumps` and `torch.save` / `torch.load(weights_only=True)` accept (tuples come back as tuples).
- `record(T, plain(cfg)) == cfg` is **False** for `history: list[EvalRow]`: `record()` checks `list[R]`
  shallowly (the list only), so its elements stay dicts.
- A precise deep type can't be written generically: `_asdict()` is `_dct_<fields>[T0, T1, ...]`, and there is
  no type-level "plain form of T1". One overload per record type the file names works in basedpyright, each
  returning a TypedDict built from that type's written fields (`_pln_TrainConfig` with
  `schedule: _pln_Schedule`, `history: list[_pln_EvalRow]`). `plain(cfg)["schedule"]["max_lr"]` is `float`, and
  an inferred record that fits `TrainConfig` matches too. A record with no named type falls back to
  `dict[str, Any]`. The overloads overlap the fallback (reportOverlappingOverload), so they need an ignore.

### Options weighed
1. `rec._asdict(deep=True)`: the same typing problem, plus it adds to the `_asdict` on every record type.
2. A separate `from byname import plain; plain(rec)`: deep by definition, recurses into containers, leaves
   `_asdict()` and `**rec` checking alone, and is the inverse of `record(T, d)`.
3. `_asdict()` deep by default: breaks NamedTuple parity, and `**rec` is checked through the shallow TypedDict.
4. Documentation only: `json.dumps` still fails.

### Proposed plan (option 2, not yet agreed)
- `plain(x)` in `runtime.py`, exported from `byname`: records become dicts in written field order, and it
  recurses into exact `list`, `tuple` (kept a tuple) and `dict` (values only). Anything else (sets, tensors,
  NamedTuples) is returned unchanged.
- `record()` builds records inside `list[R]`, `tuple[R, ...]` and `dict[K, R]` when the element type is a
  record type, with errors at paths like `history[0].loss`. Element types that aren't records stay shallow.
  Without this the round trip fails for any list of records.
- Typing, tier 1 (now): `plain(rec)` is `dict[str, Any]` for any record (overload on a protocol with
  `_asdict`), `Any` otherwise. No transform changes. This covers json, torch and wandb, and `record(T, d)`
  already returns a typed `T`.
- Typing, tier 2 (only if reading the plain dict by key turns out common): precise `_pln_` TypedDicts plus
  overloads, generated only in files that import `plain`. This needs the transform to rewrite each type's field
  annotations, handle inline nested record types and follow imported types through the lookup. Roughly
  100–150 lines plus tests.
- README records section: `_asdict()` / `dict(rec)` are shallow, `plain(rec)` is deep, and
  `record(T, plain(rec)) == rec`, next to the `record(T, d)` docs.

### Done when
- A nested config goes through `json.dumps` and `torch.save` / `torch.load(weights_only=True)`, and
  `record(T, ...)` gives back a record equal to the original.
- Tests cover nested records, records in a list, the round trip with `record()`, and the checker's type for the
  result.
- `uv run pytest -q` passes.
