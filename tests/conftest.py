# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures.

No test may open a real serial port or spawn a process unless it is marked ``hardware``.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import namedtuple
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest
import serial

from vispek_hc import cli, controls, illuminator, safety, video


@pytest.fixture(autouse=True)
def _no_real_devices(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("hardware"):
        yield
        return
    violations: list[str] = []

    def refuse(what: str) -> Any:
        def refused(*args: Any, **kwargs: Any) -> NoReturn:
            violations.append(what)
            raise AssertionError(f"unit tests must not use {what}; mark the test 'hardware'")

        return refused

    monkeypatch.setattr(serial, "Serial", refuse("serial.Serial"))
    monkeypatch.setattr(subprocess, "Popen", refuse("subprocess.Popen"))
    # Python may invoke `cmd /c ver` to discover this on restricted Windows hosts.
    monkeypatch.setattr(cli.platform, "machine", lambda: "AMD64")
    yield
    # Checked here as well: code under test may catch the AssertionError above.
    assert violations == []


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test reads or writes the real configuration directory."""
    path = tmp_path / "home"
    monkeypatch.setenv("VISPEK_HC_HOME", str(path))
    return path


@pytest.fixture(autouse=True)
def _forget_lights() -> Iterator[None]:
    """Light sources a test leaves open do not pile up in the process-wide registry."""
    before = set(safety._registry.lights)
    yield
    safety._registry.lights.intersection_update(before)


@pytest.fixture
def quiet_host(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A computer with ffmpeg and a webcam, but no HC1500 and no uvc-util. Returns its home."""
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**12, 10**11, 9 * 10**11))
    monkeypatch.setattr(video, "ffmpeg_version", lambda *args: (9, 0))
    monkeypatch.setattr(video, "list_cameras", lambda *args: ["FaceTime HD Camera"])
    monkeypatch.setattr(cli, "list_cameras_without_ffmpeg", lambda: [])
    monkeypatch.setattr(illuminator, "find_ports", lambda: [])
    monkeypatch.setattr(controls, "find_uvc_util", lambda: None)
    monkeypatch.setattr(cli, "has_xcode_tools", lambda: False)
    monkeypatch.setattr(cli, "PLATFORM", "linux")
    monkeypatch.setattr(cli, "find_v4l2_ctl", lambda: "/usr/bin/v4l2-ctl")
    monkeypatch.setattr(cli, "linux_host", lambda: {"dialout": True, "brltty": False})
    return home
