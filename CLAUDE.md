# byname

Read `DEVELOPMENT.md` before changing byname: it holds the design decisions, the code map and the conventions.
`README.md` is the user-facing doc.

- **Every feature goes in `tests/test_exhaustive.pyn`**, in the same change as the code. It's the walkthrough of
  everything byname enables; a feature missing there is unfinished, even when `test_transform.py` covers it.
- **Document it too:** the README for users, a `DEVELOPMENT.md` entry for why it's built that way.
- **Checks:** `uv run pytest`, `uv run ruff format --check`. Not `ruff check`: its default rules (0.16) flag
  intended code (`exec` in tests, unused unpacked fixtures), and fixing them is churn.
