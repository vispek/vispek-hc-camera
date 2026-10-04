# SPDX-License-Identifier: Apache-2.0
"""The README as the package index shows it: relative links made absolute.

On the package index the README stands alone, so ``[docs/cli.md](docs/cli.md)`` leads
nowhere. The build hook (``hatch_build.py``) passes the README through
:func:`absolute_links`; the file in the repository keeps its relative links, which work
in a checkout and on the repository's own pages.
"""

from __future__ import annotations

import re

BASE = "https://github.com/vispek/vispek-hc-camera"
_RELATIVE = re.compile(r"\]\((?![a-z][a-z0-9+.-]*:|#)([^)\s]+)\)")
_RELEASE = re.compile(r"(\d+\.\d+\.\d+)(?:rc(\d+))?")


def ref_of_version(version: str) -> str:
    """The git ref the links of this version point to: its tag, or ``main`` for a
    development version, which has none."""
    match = _RELEASE.fullmatch(version)
    if not match:
        return "main"
    return f"v{match[1]}" + (f"-rc{match[2]}" if match[2] else "")


def absolute_links(text: str, ref: str) -> str:
    """Every relative Markdown link, turned into a link to that file at ``ref``."""

    def absolute(match: re.Match[str]) -> str:
        target = match[1]
        kind = "tree" if target.split("#")[0].endswith("/") else "blob"
        return f"]({BASE}/{kind}/{ref}/{target})"

    return _RELATIVE.sub(absolute, text)
