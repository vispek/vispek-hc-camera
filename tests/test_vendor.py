# SPDX-License-Identifier: Apache-2.0
"""Third-party source kept in this repository stays byte-identical to its upstream."""

from __future__ import annotations

import hashlib
from pathlib import Path

UVC_UTIL = Path(__file__).parent.parent / "tools" / "uvc-util"


def test_vendored_uvc_util_files_match_their_recorded_hashes() -> None:
    recorded = dict(
        reversed(line.split(maxsplit=1))
        for line in (UVC_UTIL / "SHA256SUMS").read_text("utf-8").splitlines()
    )
    assert sorted(recorded) == [
        "LICENSE",
        "src/UVCController.h",
        "src/UVCController.m",
        "src/UVCType.h",
        "src/UVCType.m",
        "src/UVCValue.h",
        "src/UVCValue.m",
        "src/uvc-util.m",
    ]
    actual = {name: hashlib.sha256((UVC_UTIL / name).read_bytes()).hexdigest() for name in recorded}
    assert actual == recorded


def test_only_the_upstream_sources_are_vendored() -> None:
    assert sorted(path.name for path in (UVC_UTIL / "src").iterdir()) == [
        "UVCController.h",
        "UVCController.m",
        "UVCType.h",
        "UVCType.m",
        "UVCValue.h",
        "UVCValue.m",
        "uvc-util.m",
    ]


def test_licence_notice_of_the_vendored_code_is_kept() -> None:
    assert "Copyright (c) 2016 Jeffrey Frey" in (UVC_UTIL / "LICENSE").read_text("utf-8")
    notices = (UVC_UTIL.parent.parent / "THIRD-PARTY-NOTICES.md").read_text("utf-8")
    assert "uvc-util" in notices
    assert "8110da7025c95eea3096a7181af9a46c0cc7ac37" in notices
