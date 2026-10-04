# Contributing

## Setup

```bash
uv sync --group dev
uv run pre-commit install
uv run pytest
```

Before you open a pull request: `ruff format`, `ruff check`, `mypy` and `pytest` pass, and
new behaviour comes with tests. No test may need hardware unless it is marked
`@pytest.mark.hardware`.

## Rules specific to this project

- **Safety first.** Read the safety invariants in [AGENTS.md](AGENTS.md). Changes to
  `SAFETY.md`, `safety.py`, `illuminator.py` or `models.py` need a maintainer review.
- **Clean provenance.** Do not contribute code, names or documentation derived from
  proprietary camera software or from software under an incompatible licence.
- **Defaults are data.** A change to an acquisition default, or to how a scalar, mask or
  relative value is defined, needs a `CHANGELOG.md` "Defaults" entry and a record under
  `docs/acceptance/`.
- **AI-assisted contributions are welcome.** You are responsible for what you submit;
  say in the pull request that an assistant was used, and include tests.

## Sign-off

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead
of a contributor agreement. Sign each commit with `git commit -s`.

## Versioning

0.x semantic versioning: a `0.MINOR` release may break the Python API or CLI flags, and
says so under "Breaking" in the changelog after one minor release of deprecation warnings.
A `0.x.PATCH` release never breaks anything. File formats are more stable than code:
readers of schema `/1` accept every `/1` file forever.
