# Feedback: asr

Claude (Opus 5.5) building `projects/asr` in byname, 2026-10-12/13: character CTC speech recognition
on LibriSpeech, 20% dev WER, 64 tests, all `.pyn`.

## What worked

**`**rest`** builds "the config without one field", typed as exactly `GPTConfig` minus `dropout`:

```python
(dropout=_, **old_cfg) = cfg                 # old_cfg: (vocab_size: int, block_size: int, ...)
ckpt["gpt_cfg"] = old_cfg._asdict()          # as if saved before dropout existed
```

**Records through a training stack.** Batches, model outputs and eval results are records. `_replace`
changes one field, and `**` passes a record to a function by name, checked against its signature:

```python
batch = batch._replace(features=spec_augment(features, feature_lengths, fill=FEATURE_MEAN))
(logits=, lengths=) = model(features, feature_lengths)

type Inputs = (x_t: Tensor, t: Tensor, context: Tensor, action: Tensor)   # world-model-final
model(**batch)
model(**batch._replace(action=other_action))
```

**Locals stay exact.** An annotated local narrows back to the exact type, while parameters and return
types stay open. Use those, not `cast`, when something must stay open:

```python
o: (...,) = f()                      # o is still (name: str, age: int, sex: str)
def g() -> (..., age: int): ...      # callers see only age
```

## Fixed in byname during the project

**Records broke torch's DataLoader `pin_memory`** (and pytree, `default_collate`), which rebuild any
NamedTuple by iterating it:

```python
type(data)(*(pin_memory(sample) for sample in data))   # torch/utils/data/_utils/pin_memory.py
```

Fixed in cc7eba3: `ITER_CALLERS = ("torch.",)` lets code in those packages iterate a record, while user
code and `json` still raise. Allowing iteration everywhere was rejected:

```python
json.dumps((name="rahul", age=26))   # would silently write [26, "rahul"]: sorted values, no names
```

Pinning was worth ~3% per step. Rule: add a package to `ITER_CALLERS` when it raises "read by name
only" from inside its own code.

## Still to fix in byname

1. **`source_ast` line numbers drift after a multi-line destructure pattern,** 7 lines too low after an
   8-line one. any-check (../ml-tools) then looks in the wrong function and flags a name that is
   annotated. Workaround: write the destructure as a grid.
2. **`byname format` packs a grid's later rows by the first row's width in characters,** so this
   becomes 3 uneven rows (repro in `asr/src/scratch.pyn`):
   ```python
   (
       n_mels=, n_embed=, n_head=,
       n_layer=, vocab_size=, dropout=,
   ) = cfg
   ```
