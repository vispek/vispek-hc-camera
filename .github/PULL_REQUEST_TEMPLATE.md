## What and why

## Checklist

- [ ] `uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest` pass
- [ ] New behaviour has tests; no test needs hardware unless marked `hardware`
- [ ] Safety invariants in AGENTS.md are untouched (or a maintainer is asked to review)
- [ ] Docs and `CHANGELOG.md` updated; changed defaults have a "Defaults" entry
- [ ] No code or names from other camera software
- [ ] AI assistance used: yes / no
