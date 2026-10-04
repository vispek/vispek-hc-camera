# Releasing

How a version of `vispek-hc` reaches PyPI. This page is for maintainers.

## Where the code lives

Development happens in a private repository. This public repository receives one commit
per release: the released tree as it is, made by a maintainer. Contributions sent here
are carried over to the private repository and come back with the next release.

## Release settings

- PyPI project `vispek-hc` trusts this repository's `release.yml` in the GitHub
  environment `pypi` (trusted publishing: no token is stored anywhere). TestPyPI trusts
  the same workflow in the environment `testpypi`.
- Environment `pypi` accepts tags `v*` and requires a maintainer's approval; environment
  `testpypi` accepts tags `v*-rc*`.
- Secret scanning with push protection, private vulnerability reporting and Dependabot
  alerts are on. `main` and tags `v*` cannot be force-pushed or deleted.

## Release

After **every** change of `version` in `pyproject.toml`, run `uv lock` and commit it: the
lock file records the version, and the workflow installs with `--locked`.

1. A candidate, when wanted. Version `0.1.0rc1` (and `uv lock`), tag `v0.1.0-rc1`.
   `release.yml` checks that tag and version agree, runs the tests, builds, installs the
   wheel in a clean environment and publishes to TestPyPI. Install from TestPyPI and run
   `vispek-hc check --simulate`.
2. The release. `[Unreleased]` in `CHANGELOG.md` becomes `[0.1.0] - <date>` with a new
   empty `[Unreleased]` above it; version `0.1.0` (and `uv lock`); annotated tag
   `v0.1.0`. The workflow waits for a maintainer to approve the `pypi` environment, then
   publishes to PyPI and creates the GitHub release with that changelog section and the
   built files. A final release without its own changelog section is refused.

If publishing succeeded and the GitHub release failed, rerun only the `github-release`
job; a version cannot be published twice.

What can be checked without publishing is checked in CI (`package` job) and by
`tests/test_release.py`.

## By hand

If the workflow cannot run, a maintainer builds with `uv build` from a clean checkout of
the tagged commit, checks with `twine check --strict dist/*`, installs the wheel in a
clean environment, uploads with `twine upload` and a token scoped to the project, deletes
the token, and compares the SHA-256 of the files on the index with the local ones.
`0.1.0rc1` went to TestPyPI this way.

## Before a release

- `uv run pytest tests/test_provenance.py`: no name of other camera software in what is
  shipped ([provenance.md](provenance.md)).
- No real recording in any file: sample captures are not distributed
  (`sample-captures/` is ignored by git).
- The acceptance records in [acceptance/](acceptance/README.md) exist and the defaults
  they decide are in the changelog under "Defaults".
