# Feedback: llm-final

Claude (Opus 5.5) building `projects/llm-final` in byname, 2026-10-06/07: a character GPT on tiny
Shakespeare, 7 modules and 50 tests, all `.pyn`.

## What worked

**`name=` shorthand.** Most ML calls pass a local straight through, so the call reads as just the
settings, third-party calls included. It also makes keyword-only settings free to call:

```python
total = torch.tensor(0.0, device=)
opt = torch.optim.AdamW(model.parameters(), lr=)
val_loss = full_val_loss(model, val_data, batch_size=)

def get_batch(ids, *, batch_size, block_size, device): ...   # safe signature, no cost at call sites
```

**Records as configs.** They document the parameter, check every call, and need no dataclass.
Destructuring gives plain locals for the shorthand:

```python
type TrainConfig = (
    batch_size: int, max_steps: int, lr: float,
    eval_interval: int, eval_iters: int,
)
(batch_size=, max_steps=, lr=) = train_cfg
opt = torch.optim.AdamW(model.parameters(), lr=)
```

**One alias for both sides of a hand-off.** A field rename (`train` → `train_data`) flagged every
reader:

```python
type DataSplit = (train_data: Tensor, val_data: Tensor)

def split(ids: Tensor, *, frac: float = 0.9) -> DataSplit: ...
def train(model: GPT, *, data: DataSplit, ...): ...
```

**A fixture's return type catches what pytest hides.** Rule: annotate every fixture that builds a
record.

| fixture missing `n_layer` | basedpyright |
|---|---|
| `def cfg() -> GPTConfig:` | `missing field: n_layer` |
| `def cfg():` | 0 errors |

**Partial destructuring:** `(train_data=) = split(ids)`, with no field order to remember.

**Records vs tuples.** A record when the value is kept, passed around or may grow. A tuple when it's
unpacked on the spot. A walrus return labels the tuple in hover:

```python
return (x := x.to(device), y := y.to(device))   # hover: -> tuple[x: Tensor, y: Tensor]
x, y = get_batch(...)
```

**Grid formatting.** The first line sets the width, so related fields share a row. Records are
order-free, so fields can be reordered to make the grid fit:

```python
type GPTConfig = (
    n_embed: int, n_head: int, n_layer: int,
    vocab_size: int, block_size: int,
)
```

**Records print as written:** `(vocab_size=65, block_size=64, n_embed=128, ...)`, pasteable into code.

**`.pyn` tests just work:** assert rewriting, `.pyn` line numbers, fixtures, `parametrize`,
`pytest.raises(match=)`, and VS Code's gutter buttons.

**Saving records.** `_asdict()` for checkpoints, which load with `weights_only=True` because it's a
plain dict. `dict(rec)` for `dict[str, Any]` APIs, because `_asdict()` is a TypedDict and isn't
assignable to `dict`:

```python
torch.save({"model": model.state_dict(), "gpt_cfg": gpt_cfg._asdict()}, path)
Run("test", dict(train_cfg))
```

**An optional record groups settings that only make sense together.** Both are present or both
absent, with no assert:

```python
def train(model, *, data, train_cfg, save_best: (path: Path, gpt_cfg: GPTConfig) | None = None): ...
    if save_best is not None:
        (path=, gpt_cfg=) = save_best
```

**`_replace` vs spread** (the user's rule). One record with some fields changed is `_replace`, which
is checked with no annotation. A spread is for combining records or adding fields, into an annotated
type. An unannotated spread can change types silently:

```python
no_decay = lr_cfg._replace(warmup_steps=10, max_steps=11)
cfg: RunConfig = (**gpt_cfg, **train_cfg, name="small")
```

**A dict spread into a record is rejected on purpose.** Use `record(T, d)` or build field by field,
so a field added later is flagged:

```python
gpt_cfg = record(GPTConfig, ckpt["gpt_cfg"])   # TypeError naming a missing or wrong field
```

## When to use an open record (revised over three projects)

**Open:** a part of a model reads a slice of the model's own config. The parent doesn't forward
fields one by one, and tests can build a Block from a small record:

```python
class Block(nn.Module):
    def __init__(self, cfg: (..., n_embed: int, n_head: int, dropout: float)): ...

self.blocks = [Block(cfg) for _ in range(n_layer)]
```

**Closed sub-record:** a separate concern with one consumer that uses all of it. An open `schedule`
read as if `lr_at` depended on the whole config, so world-model-final groups it:

```python
# before: lr_at(step, schedule=train_cfg), with schedule: (..., warmup_steps: int, ...)
type Schedule = (warmup_steps: int, max_steps: int, max_lr: float, min_lr: float)
type TrainConfig = (batch_size: int, schedule: Schedule, ...)

def lr_at(step: int, *, schedule: Schedule) -> float: ...
lr = lr_at(step, schedule=)
```

**Keyword arguments:** a few settings with no natural group.

## Records vs dataclasses for configs

Neither checks types at runtime. Destructuring by name and the `name=` shorthand work on both.
- **Left for dataclasses:** the runtime missing-key `TypeError` on `D(**d)`, and a class for
  `__post_init__`, properties and `isinstance`.
- **Left for records:** no declaration, structural/open types, and checked partial values
  (`(**defaults, block_size=32)`).

## Still to fix in byname

1. **`conftest.pyn` isn't loaded,** and its fixtures silently never run. Workaround: put fixtures in a
   `.pyn` module and re-export them:
   ```python
   # conftest.py
   from gpt_fixtures import cfg, ids, model  # noqa: F401
   ```
2. **`byname tool ruff check` without `--output-format=concise`** prints I001/PYI042 on the generated
   header, then "All checks passed!".
3. **`byname tool ruff format --check`** checks the generated Python, not the `.pyn`. It should
   redirect to `byname format`, or refuse.
4. **`byname tool ruff check --fix`** reports "N fixed" but doesn't write the `.pyn`. Repro:
   `ruff check --select I --fix exercises/test_round_1.pyn` printed "4 errors (4 fixed)" and left the
   file unchanged. The editor's format and organize-imports on save do work.
5. **Fixture parameters are `Unknown`,** so `def test_x(tok):` shows white. byname could infer the type
   from the fixture function of the same name.
6. **`Any` from untyped calls:** every `nn.Module` call, e.g. `h = self.ln1(x)`. `any-check` finds
   them. It lives in `../ml-tools`, shared by the ML projects, and not in byname on purpose, because
   byname also serves projects without torch.
7. **Spreading a parameter whose record type is imported gives `Unknown`:** `(**cfg, name="small")`
   with `cfg: GPTConfig` from another module. A same-file alias works.
8. **Editor completion.** Fixed: after a comma in `_replace(x=1, |)`, and record literals in
   `user: User = (|)`. Still open: a record literal as a call argument, `**u` not excluding the
   fields it already provides, and signature help in spread records showing `T0@(fields)`.
9. **Outside byname's path:** basedpyright is the only checker (Pylance off), notebooks need
   `%load_ext byname`, and tools that read `.py` directly don't understand `.pyn`.
