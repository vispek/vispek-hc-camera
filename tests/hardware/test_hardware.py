# SPDX-License-Identifier: Apache-2.0
"""Acceptance on a real HC1500. Not run by default:

    VISPEK_HC_ACCEPTANCE_DIR=acceptance-run uv run pytest -m hardware tests/hardware -v

Set the device up as docs/acceptance/hardware-runbook.md says first. The white-board
tests need the white board in view and are skipped unless VISPEK_HC_WHITE_BOARD=1.
VISPEK_HC_ACCEPTANCE_SIMULATE=1 rehearses the whole run on the simulated device; the
first line pytest prints says which of the two it is.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import vispek_hc

# No test timeout here: the driver's own waits are bounded, and a timeout that kills the
# process (as pytest-timeout does on Windows) would leave an LED lit.
pytestmark = [pytest.mark.hardware, pytest.mark.timeout(0)]

needs_white_board = pytest.mark.skipif(
    os.environ.get("VISPEK_HC_WHITE_BOARD") != "1",
    reason="put the white board in view and set VISPEK_HC_WHITE_BOARD=1",
)


def test_pairing(session: vispek_hc.Session, acceptance: Any, records: dict[str, Any]) -> None:
    records["checks"]["pairing"] = acceptance.pairing(session)


def test_black_level(session: vispek_hc.Session, acceptance: Any, records: dict[str, Any]) -> None:
    session.lock_controls()  # the black level of 5 was observed with exposure and gain fixed
    records["checks"]["black_level"] = acceptance.black_level(session)


def test_visible_led(session: vispek_hc.Session, acceptance: Any, records: dict[str, Any]) -> None:
    records["checks"]["visible_led"] = acceptance.visible_led(session)


def test_camera_lock(session: vispek_hc.Session, acceptance: Any, records: dict[str, Any]) -> None:
    records["checks"]["camera_lock"] = acceptance.camera_lock(session)


def test_exposure_steps(
    session: vispek_hc.Session, acceptance: Any, records: dict[str, Any]
) -> None:
    records["checks"]["exposure_steps"] = acceptance.exposure_steps(session)


@pytest.mark.parametrize("gap_ms", [10, 20, 50, 100])
def test_reply_loss_against_the_pause_between_commands(
    gap_ms: int, session: vispek_hc.Session, acceptance: Any, records: dict[str, Any]
) -> None:
    record = acceptance.reply_loss(session, gap_ms)
    records["checks"].setdefault("reply_loss", []).append(record)  # kept even if it fails
    assert record["failed_pairs"] == 0, "commands went unanswered after three attempts"


@pytest.mark.parametrize("timeout_ms", [300, 600])
def test_reply_loss_with_a_longer_timeout(
    timeout_ms: int, acceptance: Any, records: dict[str, Any]
) -> None:
    with acceptance.open_light(timeout_ms / 1000) as light:
        light_only: Any = type("LightOnly", (), {"light": light, "clock": light.clock})()
        record = acceptance.reply_loss(light_only, 10)
    records["checks"].setdefault("reply_loss", []).append(record)
    assert record["ack_timeout_ms"] == timeout_ms
    assert record["failed_pairs"] == 0, "commands went unanswered after three attempts"


def test_scan(
    session: vispek_hc.Session, acceptance: Any, records: dict[str, Any], acceptance_dir: Path
) -> None:
    session.pair_probe()
    records["checks"]["scan"] = acceptance.scan(session, acceptance_dir / "scan")


@needs_white_board
def test_white_reference(
    session: vispek_hc.Session, acceptance: Any, records: dict[str, Any], acceptance_dir: Path
) -> None:
    session.pair_probe()
    records["checks"]["white_reference"] = acceptance.white_reference(session, acceptance_dir)


@needs_white_board
def test_scalar_noise(session: vispek_hc.Session, acceptance: Any, records: dict[str, Any]) -> None:
    session.lock_controls()
    records["checks"]["scalar_noise"] = [
        acceptance.scalar_noise(session, led_id) for led_id in (5, 7, 9, 13)
    ]


@needs_white_board
@pytest.mark.parametrize("source", ["uncompressed", "mjpeg"])
def test_pixel_path_gives_black_5_and_clips_at_255(
    source: str, acceptance: Any, records: dict[str, Any]
) -> None:
    with vispek_hc.open_session(**acceptance.device_options(source=source)) as opened:
        opened.lock_controls()
        record = {**acceptance.black_level(opened), **acceptance.clipping(opened)}
    records["checks"].setdefault("pixel_paths", {})[source] = record
