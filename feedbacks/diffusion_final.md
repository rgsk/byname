# Feedback: diffusion-final

Written from the user's seat: Claude (Opus 5.5), building `/home/rahul/Documents/codes/projects/diffusion-final` in byname on 2026-10-08. It's flow matching on MNIST, in llm-final's layout: `data`, `flow`, `model` (UNet), `train`, `sample` and `main`, all `.pyn`, with 85 tests.

Overall, records worked well for this kind of code. The friction was almost all in the CLI tooling, not the language.

## What worked

- **Records between modules.** `FlowBatch = (x_t, t, target)` is built by `flow_batch` and destructured in `batch_loss`'s signature. Swapping `t` and `target` can't go unnoticed.
- **Destructuring in parameters** (`(train_data=, val_data=): DataSplit`) reads like documentation. When the `DataSplit` fields were renamed, 0 checker errors confirmed every use had moved.
- **Partial destructuring and renaming** on return records: `(step=) = load_checkpoint(p)`, `(model=loaded) = ...`.
- **`_replace` in tests** makes "the fixture, except one thing" explicit.
- **`record(UNetConfig, dict)`** in the checkpoint loader caught a missing field. A tuple field (`channels`) survived the save/load round trip.
- **`{**cfg}` for wandb config, and `**lr_cfg` into kwargs,** worked and were checked.
- **No `Any` leaked** through destructuring. Every `Any` flagged came from torch.

## Friction, with repros (run in diffusion-final)

1. **`byname fix` / `fix --check` doesn't recognise `src/` as local code.** It merges `from paths import ...` into the `torch` import group. `uv run byname fix --check src/data.pyn src/train.pyn tests/test_train.pyn` reports "would fix" on all three, even though they're sorted the way the editor's on-save fix leaves them. I had to put the blank line back by hand twice. My guess is that the CLI runs ruff on a translated file outside the project, so `[tool.ruff] src = ["src"]` doesn't apply.
2. **`byname tool ruff check` without `--output-format=concise`** prints diagnostics from the generated prelude: `I001` on the `_NT` imports, and `PYI042` on `_key_train_data__val_data`. It then prints "All checks passed!" and exits 0. So the full output format both leaks generated code and contradicts its own summary.
3. **`byname tool ruff format --check`** formats the translated Python and prints a huge, meaningless diff. You have to know to use `byname format --check` instead. Better to redirect it, or to refuse with a pointer.
4. **`conftest.py` has to be plain `.py`.** That's why fixtures live in `unet_fixtures.pyn` and get re-exported with `# noqa: F401`. It's the same workaround llm-final uses, and supporting `conftest.pyn` would remove it.

## Design observations

- **Shorthand pushes field names into local names, and that can shadow things.** In `main.pyn`, `(train=, val=) = split()` would have shadowed the `train` function imported from `train.pyn`. The rename form handled it, and renaming the fields to `train_data` removed the issue. But the pressure is real: a record field name tends to become a local name at every call site, so short field names like `train`, `model` or `step` collide more easily.
  - A checker warning for "destructuring shadows an imported name" would catch this.
- **Records holding tensors and `==`.** I nearly wrote `assert a == b` on two `FlowBatch` records. Comparing by value means comparing the tensors, which raises "ambiguous truth value". That's the same trap as plain tuples, but records make the comparison look safer than it is.
  - A checker hint, when `==` is used on records whose fields don't return a plain bool, would help.
- **Tuple vs record for `get_batch`.** The labeled tuple `(x := ..., y := ...)` was the right choice. `x0, _ = get_batch(...)` is what every call site wants, and the hover still shows the names. The README's guidance on when to use which held up in practice.

**Bottom line:** at the language level I'd change nothing. The two worth fixing first are issues 1 and 2: the CLI's import sorting and the generated-code diagnostics. Both made me second-guess output that turned out to be correct.
