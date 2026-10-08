# Feedback: llm-final

Written from the user's seat: Claude (Opus 5.5), building `/home/rahul/Documents/codes/projects/llm-final` in byname over 2026-10-06/07.

## The project

A character-level GPT trained on tiny Shakespeare: seven modules and five test files, all `.pyn`, plus one `conftest.py`.

| File | What it does |
|---|---|
| `src/tokenizer.pyn` | `CharTokenizer` |
| `src/data.pyn` | `split()` and `get_batch()` |
| `src/model.pyn` | GPT: attention, MLP, block, model |
| `src/train.pyn` | losses, evaluation and the training loop |
| `src/generate.pyn` | sampling |
| `src/main.pyn` | the end-to-end run |
| `tests/test_*.pyn` | 50 tests, written to be read top to bottom as an explanation of the code |

It uses torch, einops, pytest, ruff and basedpyright, with Alt+L (`byname tool`) and `scripts/any_check.py` run on every file.

## Where byname is used

| Feature | Where |
|---|---|
| `name=` shorthand | every module: `get_batch(ids, batch_size=, device=)`, `torch.tensor(0.0, device=)`, `AdamW(params, lr=)`, `normal_(mean=0.0, std=)`, `Block(n_embed=, n_head=)` |
| Record types as configs | `type GPTConfig`, `type TrainConfig`, `type DataSplit` |
| Open record type | GPT's constructor accepts any record with at least its five fields |
| Destructuring a record in a parameter | `GPT.__init__(self, (vocab_size=, block_size=, ...): (..., vocab_size: int, ...))`, `train(model, (train_data=, val_data=): DataSplit, (batch_size=, ...): TrainConfig)` |
| Returning a record | `split()` returns `(train_data=, val_data=)`, `train()` returns `(step=, train_loss=, val_loss=)` history |
| Destructuring all or part of a record | `(train_data=, val_data=) = split(ids)`, `(train_data=) = split(ids)` |
| Inline record type in an annotation | `history: list[(step: int, train_loss: float, val_loss: float)] = []` |
| A record-building fixture with a declared return | `def cfg() -> GPTConfig:` in three test files |
| A walrus-labelled tuple return | `return (x := x.to(device), y := y.to(device))` in `get_batch` |
| Renaming while destructuring | `(train=train_ids, val=val_ids)` in `train()`'s signature, before the fields were renamed |
| Grid formatting | config records and the `get_batch(...)` calls |
| `.pyn` tests | every test file; fixtures, `parametrize`, `pytest.raises(match=)` |
| Records printed | `print(cfg)` → `(vocab_size=65, block_size=64, n_embed=128, n_head=4, n_layer=4)` |

## What worked best

### `name=` shorthand, everywhere

This is the feature I'd miss first. Most calls in ML code pass a local straight through, and with the
shorthand the call reads as just the list of settings that matter:

```python
total = torch.tensor(0.0, device=)
opt = torch.optim.AdamW(model.parameters(), lr=)
val_loss = full_val_loss(model, val_data, batch_size=)
```

It also works on third-party calls (torch, einops) with nothing to configure. And it made a convention
cheap: inputs are positional and settings are keyword-only (`def get_batch(ids, *, batch_size,
block_size, device)`). Keyword-only parameters normally cost typing at every call, and with
`batch_size=` that cost disappears, so the safer signature has no downside.

### Records as configs, destructured right in the signature

```python
type TrainConfig = (
    batch_size: int, max_steps: int, lr: float,
    eval_interval: int, eval_iters: int,
)

def train(
    model: GPT,
    (train_data=, val_data=): DataSplit,
    (batch_size=, max_steps=, lr=, eval_interval=, eval_iters=): TrainConfig,
):
```

Callers pass one value (`train(model, data, train_cfg)`), and the body gets plain locals with no
`cfg.` prefix. The same record type documents the parameter, checks every call, and lets the body use
the shorthand again (`AdamW(..., lr=)`). There's no dataclass boilerplate.

### Record types catch real bugs, including where pytest hides them

The biggest catch of the session. A `cfg` fixture that built a `GPTConfig` without `n_layer`:

| fixture | basedpyright |
|---|---|
| `def cfg() -> GPTConfig:` | `missing field: n_layer` |
| `def cfg():` | 0 errors |

pytest passes fixtures by name, so the checker can't see the connection, and the test's own
`cfg: GPTConfig` annotation is simply trusted. Declaring the fixture's return type puts the check back
where the record is built. That became a project rule: every fixture that builds a record declares its
return type.

### Open record types keep the model separate from its config

`GPT.__init__` first took `GPTConfig` itself. That tied the two together: a config field the model
doesn't use, such as a run `name` for checkpoints, would have changed what GPT accepts. An open record
type lets GPT declare only the fields it reads:

```python
def __init__(
    self,
    (vocab_size=, block_size=, n_embed=, n_head=, n_layer=): (
        ..., n_embed: int, n_head: int, n_layer: int,
        vocab_size: int, block_size: int,
    ),
):
```

Now any record with at least those fields builds a model, extra fields included
(`test_gpt_ignores_extra_config_fields` passes one with `name="small"`), while a record missing
`n_layer` is still rejected: `"n_layer" is not present`. `GPTConfig` stays a closed type for configs
and can grow without GPT changing.

### One alias for both sides of a hand-off

```python
type DataSplit = (train_data: Tensor, val_data: Tensor)

def split(ids: Tensor, *, frac: float = 0.9) -> DataSplit: ...
def train(model: GPT, (train_data=, val_data=): DataSplit, ...): ...
```

`split()`'s `return` and `train()`'s parameter are checked against the same definition, so renaming a
field flags every reader. The rename that motivated it (`train`/`val` → `train_data`/`val_data`, after
`train` had come to mean the function, the data and a loss value) went through all files with the
checker confirming each site.

### Partial destructuring

`(train_data=) = split(ids)` takes only the field you need. A tuple would need `train_data, _ = ...`,
and the reader would have to know the field order. Field order never comes up with records.

### Records vs tuples: a clear rule that byname makes cheap to follow

- **Record:** the value is kept, passed around, read in parts, or may grow. That's `split()`'s
  `DataSplit` and `train()`'s history; `h.val_loss` reads clearly in the tests:
  `assert last.val_loss < first.val_loss / 2`.
- **Tuple:** the value is unpacked immediately in a fixed, conventional order. That's `get_batch()`'s
  `x, y`, which also keeps `for x, y in batches` working.

The walrus return closes the one gap tuples have. `return (x := x.to(device), y := y.to(device))`
shows up in hover as `-> tuple[x: Tensor, y: Tensor]`, which labels each position while the call site
stays a plain `x, y = get_batch(...)`. And the walrus names aren't reported as unused.

### Grid formatting

When the first line of a split bracket holds several items, later lines are packed to that width.
That gives a third layout between "all on one line" and "one per line", and the author picks it by
writing the first line:

```python
x, y = get_batch(
    train_data, batch_size=,
    block_size=model.block_size,
    device=model.device,
)
```

Combined with records being order-free, it groups related config fields by row:

```python
type GPTConfig = (
    n_embed: int, n_head: int, n_layer: int,
    vocab_size: int, block_size: int,
)
```

The rows were reordered here (wider row first) so the grid would hold. That's only possible because
field order doesn't matter in a record.

### Records print the way they're written

`print(cfg)` gives `(vocab_size=65, block_size=64, n_embed=128, n_head=4, n_layer=4)`, which is
exactly the source syntax, so run logs can be pasted back into code.

### `.pyn` tests just work

`test_*.pyn` is collected with nothing to configure:
- asserts are rewritten
- failures point at `.pyn` lines
- fixtures, `parametrize` and `pytest.raises(match=...)` behave as in `.py`
- VS Code's Testing panel and gutter run buttons work on `.pyn` tests (with `python.testing.pytestEnabled` and `pytestArgs: ["tests"]`): each test gets a run button and a pass/fail mark on its own `.pyn` line

`.py` and `.pyn` live side by side (`conftest.py` next to `test_*.pyn`).

### Interop with typed libraries

torch, einops and (while it was in the project) jaxtyping all worked unchanged. jaxtyping's shape
strings (`Float[Tensor, "b t 3*e"]`) passed both ruff and basedpyright through `byname tool`, and
beartype's runtime shape checks fired correctly in `.pyn` code.

## Aha moments

1. **A fixture's return type is where a record gets checked.** The missing `n_layer` was invisible
   until `-> GPTConfig` was added; after that it was a precise one-line error.
2. **Grid formatting is the author's choice.** Two short items on the first line, and the rest of the
   call lays itself out around them.
3. **Order-free records make layout free too.** Reordering `GPTConfig`'s fields to fix a grid row costs
   nothing, since no reader depends on the order.
4. **The walrus as a label.** A tuple gets named positions in hover without becoming a record.
5. **Shorthand makes keyword-only parameters free.** The safe signature (`*, batch_size, iters`) is
   no longer the verbose one.
6. **Destructuring with a rename in a parameter** (`(train=train_ids, val=val_ids): ...`) worked on the
   first try, both running and type-checked, and solved a shadowing problem without touching callers.

7. **Open types separate a constructor from its config.** `(..., field: T)` in GPT's signature means
   "at least these fields". The config can grow, and the model's inputs are still checked.

## Still to be handled in byname

Each was worked around in llm-final. They're listed so byname can absorb them.

1. **`conftest.pyn` isn't loaded.** Its fixtures silently never run, with no error or warning.
   Workaround: keep `conftest.py`. (Detailed in `open_issues.md`.)
2. **`byname tool ruff check` without `--output-format=concise`** prints I001/PYI042 on the generated
   record header, then "All checks passed!" with exit 0. Workaround: always pass `--output-format=concise`
   (the Alt+L task does). (Detailed in `open_issues.md`.)
3. **`byname tool ruff format --check` checks the generated Python, not the `.pyn`**, so it reports
   files as unformatted that `byname format --check` accepts. It's an easy wrong command to reach for.
   It could redirect to `byname format`, or refuse.
4. **Fixture parameters are `Unknown`.** basedpyright doesn't follow pytest's fixture wiring, so
   `def test_x(tok):` leaves `tok` and every `tok.attr` uncoloured in the editor. Workaround: annotate
   test parameters (`tok: CharTokenizer`). byname could infer a fixture parameter's type from the
   fixture function of the same name, as Pylance does for `.py`.
5. **`Any` leaking from untyped calls.** Every `nn.Module` call (`self.qkv(x)`, `self.ln1(x)`) returns
   `Any`, and the names it lands in show white. Workaround: `scripts/any_check.py`, copied from `llm`,
   plus `: Tensor` on the first assignment. A built-in "untyped names" check in `byname tool` would
   replace the script.
6. **Spreading a parameter whose record type is imported gives `Unknown`.** `(**cfg, name="small")`
   with `cfg: GPTConfig` imported from `model.pyn` has no type, so nothing after it is checked. The
   same spread with a same-file alias is typed correctly. Workaround: write the record out
   literally. (Detailed in `open_issues.md`.)
7. **Strict rules report errors in byname's spread helpers.** With `reportAny` /
   `reportUnknownVariableType` on (what `scripts/any_check.py` does), that same spread prints errors
   about `pop`, `r`, `c` and a lambda, all byname's generated code. (Detailed in `open_issues.md`.)
8. **Tooling outside the byname path:**
   - basedpyright is the only supported checker, and Pylance has to stay off
   - notebooks need `%load_ext byname`
   - tools that read `.py` directly (GitHub highlighting, coverage, other linters) don't understand `.pyn`
