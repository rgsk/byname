# High priority issues

Nested records surviving `_asdict()` was solved by `deepdict(rec)` (see DEVELOPMENT.md).

## What else can go now that byname asks the checker (branch `checker-spread-types`)

Since fcde25f the checker answers what byname can't read off the file (imported types, a call's result, a
loop's item, any expression), and `imports.Modules` and `of_def` are gone. Three more pieces may no longer
earn their keep. Measured on fcde25f.

**A. The rest of the in-file text reading, ~260 lines: leave it.** `known_fields` (228) with `_Scope` and
`_Bind` (31): scoping, which bindings reach a read, declared types, literals, `_asdict()` dicts. Asking for
every spread and rest instead would need `_dct_` answers read too (`_asdict()` dicts). Cost: every file with
any spread or rest probes, the common `(**train_defaults, batch_size=32)` with literal defaults included:
0.95 s on its first edit instead of 0.45 s, and `byname tool` runs the checker once more whenever any .pyn
spreads. It reads exactly the cases people write most for free, and never disagreed with the checker in what
was measured (`rough.pyn`, `scratch.pyn`, llm-final).

**B. The generic path for spreads with a type expected, ~80-120 lines: prototype first.** `_byname_ctx`,
`_byname_arg`, `_byname_check`, the `_byname_pick` lambda defaults (narrowing), `_byname_AnyRec` and the
overload-selection trick. Every source the checker can type now gets an exact build (known_build), so the
generic path only takes sources nobody can type, and a written field overridden by a later spread. Building
those as open records too (open_build) would remove most of it. Behaviour changes: `take((**untyped_row,
zip=1))` into `User` becomes an error (missing fields, with 1859a38's "can't see" message) where it passes
unchecked today; overloads pick differently for such arguments; known_build must handle an overridden field.
Possibly the biggest simplification; needs the diff and the new errors seen before deciding.

**C. The separate hint for unknown rests, ~60 lines: do it.** `unknown_rests`, `srcmap.unknown_rest`,
`tools.add_rest_hints` and their part of `Translation.hints`. A rest nobody can type gets an information
hint on the rest; a spread nobody can type gets 1859a38's message where a field is read. Building such a rest
as an open record carrying the same `SPREAD` marker gives both one message in one place. Behaviour change: the
hint on the rest goes; a field read off it says where it came from.

Stays: `untyped_builds` (where no type is expected is a question about the text), the `SPREAD` marker and its
messages, the probe machinery.

## A grid row isn't held to the line length

A grid's first row sets its packing width (`ungrid`), whatever the line length: a single-row grid (kept on
purpose: arguments on the line after a long name, all on one line, which Ruff can't write) or a grid's first
row stays as written when it's too long. Measured 2026-10-24, line-length 88:

```python
fn_def_big_name(
    argument_number_0, argument_number_1, argument_number_2, argument_number_3, argument_number_4, argument_number_5,
)
```

is left at 118 characters. Fix: cap the packing width at the line length minus the indent, so a row that fits
packs as today and a too-long one wraps into as many rows as it needs. The line length is Ruff's
`formatter.line_width`: `ruff check --show-settings <file>` prints it (about 10 ms), but printed nothing for a
path that doesn't exist, and the CLI and editor pass a mirror path, so check that first, or read it some other way.
Also state in README's grid bullet and DEVELOPMENT.md that a single row is a grid on purpose.
