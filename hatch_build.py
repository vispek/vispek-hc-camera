# SPDX-License-Identifier: Apache-2.0
"""Build hook: the package description is README.md with its links made absolute."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from hatchling.metadata.plugin.interface import MetadataHookInterface


class ReadmeHook(MetadataHookInterface):
    def update(self, metadata: dict[str, Any]) -> None:
        root = Path(self.root)
        spec = importlib.util.spec_from_file_location(
            "pypi_readme", root / "tools" / "pypi_readme.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = (root / "README.md").read_text("utf-8")
        ref = module.ref_of_version(metadata["version"])
        metadata["readme"] = {
            "content-type": "text/markdown",
            "text": module.absolute_links(text, ref),
        }
