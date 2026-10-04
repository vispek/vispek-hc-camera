# SPDX-License-Identifier: Apache-2.0
"""Checks a release tag against the package version and cuts the release notes.

Used by ``.github/workflows/release.yml``:

    python tools/release.py v0.1.0-rc1 > notes.md

prints the changelog section for that release (for a release candidate: the section of
the final version, or "Unreleased") and exits non-zero if the tag is not the version in
``pyproject.toml``.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:-?rc(\d+))?")


def version_of_tag(tag: str) -> str:
    """``v0.1.0`` is version ``0.1.0``; ``v0.1.0-rc1`` is ``0.1.0rc1``."""
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"{tag!r} is not a release tag like v0.1.0 or v0.1.0-rc1")
    major, minor, patch, candidate = match.groups()
    return f"{major}.{minor}.{patch}" + (f"rc{candidate}" if candidate else "")


def is_candidate(tag: str) -> bool:
    return "rc" in tag


def changelog_section(changelog: str, version: str) -> str:
    """The text under ``## [version]``.

    A candidate may take the section of its final version or, failing that, "Unreleased".
    A final release needs its own section: it is never published with the notes of
    whatever happens to be unreleased.
    """
    final = version.split("rc")[0]
    sections = re.split(r"^## \[", changelog, flags=re.M)[1:]
    by_name = {section.split("]", 1)[0]: section.split("\n", 1)[1].strip() for section in sections}
    names = (version, final, "Unreleased") if "rc" in version else (version,)
    for name in names:
        if by_name.get(name):
            return by_name[name]
    raise ValueError(f"CHANGELOG.md has no section for {version}")


def main(argv: list[str]) -> int:
    tag = argv[1]
    wanted = version_of_tag(tag)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    if project["version"] != wanted:
        message = f"tag {tag} is version {wanted}, but pyproject.toml says {project['version']}"
        print(message, file=sys.stderr)  # noqa: T201
        return 1
    print(changelog_section((ROOT / "CHANGELOG.md").read_text("utf-8"), wanted))  # noqa: T201
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (ValueError, IndexError) as error:
        print(error, file=sys.stderr)  # noqa: T201
        sys.exit(2)
