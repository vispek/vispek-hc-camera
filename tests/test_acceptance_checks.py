# SPDX-License-Identifier: Apache-2.0
"""The hardware acceptance checks, run on the simulated device.

They cannot prove anything about a real HC1500 here. This proves that the checks run,
measure what they say, and fail when the thing they check is wrong.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from vispek_hc.session import Session
from vispek_hc.sim import SimulatedRig

HARDWARE = Path(__file__).parent / "hardware"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HARDWARE / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


acceptance = load("acceptance")


def opened(**options: Any) -> tuple[Session, SimulatedRig]:
    rig = SimulatedRig(**options)
    session = Session.from_rig(rig)
    session.__enter__()
    return session, rig


def test_pairing_reports_latency_and_frame_rate() -> None:
    session, rig = opened()
    record = acceptance.pairing(session)
    assert record == {"rise_dn": 182.0, "latency_ms": 185.0, "measured_fps": 10.0}
    assert rig.device.lit == {}
    dim, _ = opened(reflectance=0.0)
    with pytest.raises(AssertionError, match="did not react"):
        acceptance.pairing(dim)


def test_black_level_must_be_the_clamp() -> None:
    session, rig = opened()
    assert acceptance.black_level(session) == {"median_rgb": [5, 5, 5]}
    import dataclasses

    rig.frames._model = dataclasses.replace(rig.frames._model, black_level=0)
    with pytest.raises(AssertionError, match="black level is"):
        acceptance.black_level(session)


def test_visible_led_is_seen_on_and_off_again() -> None:
    session, rig = opened()
    record = acceptance.visible_led(session)
    assert (record["led_id"], record["pwm"]) == (6, 224), "the PWM the unit scans LED 6 with"
    assert record["lit_dn"] - record["dark_dn"] == pytest.approx(182, abs=1)
    assert record["after_dn"] == record["dark_dn"] == 5.0
    assert rig.device.lit == {}
    rig.device.dead_leds = {6}
    with pytest.raises(AssertionError, match="raised its channel by"):
        acceptance.visible_led(session)
    assert rig.device.lit == {}


def test_visible_led_is_seen_on_a_dark_sample() -> None:
    # On the first device a dark sample rose by 3.5 code values at PWM 50: too faint to
    # tell from nothing. At the PWM of a scan the same sample is unmistakable.
    session, rig = opened(reflectance=0.1)
    record = acceptance.visible_led(session)
    assert record["lit_dn"] - record["dark_dn"] > 15
    session.unit.pwm_by_led[6] = 448  # what tune or --auto-pwm stored for this unit
    assert acceptance.visible_led(session)["pwm"] == 448
    assert acceptance.visible_led(session, pwm=300)["pwm"] == 300
    assert rig.device.lit == {}


def test_camera_lock_reports_what_the_camera_settled_on() -> None:
    session, rig = opened()
    rig.controls.quantise["exposure-time-abs"] = lambda value: value - value % 4
    record = acceptance.camera_lock(session)
    assert record["differs_from_request"] == {
        "exposure-time-abs": {"requested": 157, "effective": 156}
    }
    rig.controls.quantise["auto-white-balance-temp"] = lambda value: 1
    with pytest.raises(AssertionError, match="white balance"):
        acceptance.camera_lock(session)


def test_exposure_steps_tabulates_and_restores() -> None:
    session, rig = opened()
    rig.controls.quantise["exposure-time-abs"] = lambda value: value - value % 50
    record = acceptance.exposure_steps(session, values=(63, 157, 333))
    assert record == {"asked_to_got": {"63": 50, "157": 150, "333": 300}}
    assert rig.controls.read_all()["exposure-time-abs"].current == 150, "back to the profile"


def test_reply_loss_counts_resends_and_leaves_nothing_lit() -> None:
    from vispek_hc.sim import SimReply

    session, rig = opened()
    clean = acceptance.reply_loss(session, gap_ms=20, pairs=5)
    assert (clean["commands"], clean["retried"], clean["loss_rate"]) == (10, 0, 0.0)
    assert (clean["gap_ms"], clean["failed_pairs"]) == (20, 0)
    assert clean["ack_timeout_ms"] == 200
    assert rig.device.lit == {}
    for _ in range(17):  # the all-off that starts the check
        rig.device.script(SimReply())
    rig.device.script(SimReply(lost=True))  # the first OFF of the first pair needs a resend
    lossy = acceptance.reply_loss(session, gap_ms=0, pairs=5)
    assert lossy["retried"] == 1
    assert lossy["loss_rate"] == 0.1
    assert lossy["gap_ms"] == 10, "the driver never leaves less than 10 ms"


def test_reply_loss_counts_pairs_that_were_never_answered_and_goes_on() -> None:
    from vispek_hc.sim import SimReply

    session, rig = opened()
    for _ in range(17 + 2):  # the all-off that starts the check, then the first pair
        rig.device.script(SimReply())
    for _ in range(3):  # the OFF of the second pair: all three attempts lost
        rig.device.script(SimReply(lost=True))
    record = acceptance.reply_loss(session, gap_ms=10, pairs=4)
    assert record["failed_pairs"] == 1
    assert rig.device.lit == {}


@pytest.mark.parametrize("check", ["visible_led", "scalar_noise", "clipping"])
@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_led_goes_off_when_a_check_is_interrupted_while_it_is_lit(
    check: str, failure: type[BaseException]
) -> None:
    session, rig = opened()
    original = rig.frames.wait_new

    def fail_while_lit(count: int = 1, timeout: float | None = None) -> Any:
        if rig.device.lit:
            raise failure("while lit")
        return original(count, timeout)

    rig.frames.wait_new = fail_while_lit  # type: ignore[method-assign]
    with pytest.raises(failure):
        getattr(acceptance, check)(session)
    assert rig.device.lit == {}


@pytest.mark.parametrize(("value", "expected"), [(None, False), ("0", False), ("1", True)])
def test_rehearsal_switch_takes_only_0_or_1(
    value: str | None, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(acceptance.REHEARSAL, raising=False)
    if value is not None:
        monkeypatch.setenv(acceptance.REHEARSAL, value)
    assert acceptance.simulated() is expected
    assert acceptance.device_options()["simulate"] is expected


@pytest.mark.parametrize("value", ["true", "yes", " 1", "", "01"])
def test_mistyped_rehearsal_switch_opens_nothing(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(acceptance.REHEARSAL, value)
    with pytest.raises(ValueError, match="use 1 for a rehearsal"):
        acceptance.device_options()
    with pytest.raises(ValueError, match="use 1 for a rehearsal"):
        acceptance.open_light(0.2)


def test_no_check_can_reach_an_ultraviolet_led() -> None:
    assert min(acceptance.NO_UV) == 5
    session, rig = opened()
    session.pair_probe()
    from vispek_hc.errors import UVNotAllowedError

    for check in (acceptance.visible_led, acceptance.scalar_noise, acceptance.clipping):
        with pytest.raises(UVNotAllowedError):
            check(session, 1)
    assert rig.device.lit == {}


def test_scan_reports_every_channel_and_fails_on_a_dark_one(tmp_path: Path) -> None:
    session, rig = opened()
    session.pair_probe()
    session.lock_controls()
    record = acceptance.scan(session, tmp_path / "a", channels=(5, 9, 13))
    assert [channel["led_id"] for channel in record["channels"]] == [5, 9, 13]
    assert record["dark_median_rgb"] == [5, 5, 5]
    assert all(channel["response_dn"] > 100 for channel in record["channels"])
    assert 1.0 < record["seconds"] < 5.0
    rig.device.dead_leds = {9}
    with pytest.raises(AssertionError, match=r"no light seen under LED \[9\]"):
        acceptance.scan(session, tmp_path / "b", channels=(5, 9, 13))


def test_white_reference_divides_the_board_by_itself(tmp_path: Path) -> None:
    session, _ = opened()
    session.pair_probe()
    record = acceptance.white_reference(session, tmp_path, channels=(5, 7, 9))
    assert set(record["pwm_by_led"]) == {"5", "7", "9"}
    assert record["median_of_board_over_itself"] == [1.0, 1.0, 1.0]
    assert record["differs_from_model"] == {}
    assert record["dominant_channel_by_led"] == {"5": "B", "7": "G", "9": "R"}


def test_white_reference_fails_when_the_board_changed_between_the_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, rig = opened()
    session.pair_probe()
    original = acceptance.sweep.record
    calls = iter(range(10))

    def darker_second_time(*args: Any, **options: Any) -> Any:
        if next(calls) == 1:
            rig.frames._reflectance = np.full((36, 64), 0.8)
        return original(*args, **options)

    monkeypatch.setattr(acceptance.sweep, "record", darker_second_time)
    with pytest.raises(AssertionError, match="not 1 within 5 %"):
        acceptance.white_reference(session, tmp_path, channels=(9,))


def test_scalar_noise_compares_dominant_channel_and_luma() -> None:
    session, rig = opened()
    record = acceptance.scalar_noise(session, led_id=9, frames=4)
    assert record["dominant"]["signal_dn"] == 175.0
    assert record["luma"]["signal_dn"] < 80, "luma of a red LED is much smaller"
    assert record["dominant"]["temporal_std_dn"] == 0.0, "the simulated camera has no noise"
    assert record["dominant"]["one_code_value_relative"] < record["luma"]["one_code_value_relative"]
    assert rig.device.lit == {}


def test_clipping_raises_the_pwm_only_as_far_as_needed() -> None:
    session, rig = opened()
    record = acceptance.clipping(session)
    assert record["steps"] == [{"pwm": 72, "peak_dn": 255}], "twice the usual PWM already clips"
    assert rig.device.lit == {}
    half, half_rig = opened(reflectance=0.25)
    steps = acceptance.clipping(half)["steps"]
    assert [step["pwm"] for step in steps] == [72, 144, 288]
    assert [step["peak_dn"] == 255 for step in steps] == [False, False, True]
    assert half_rig.device.lit == {}
    dim, dim_rig = opened(reflectance=0.001)
    with pytest.raises(AssertionError, match="never reached 255"):
        acceptance.clipping(dim)
    assert dim_rig.device.lit == {}
    assert max(c.pwm or 0 for _, c in dim_rig.device.log) == 1023


def test_hardware_tests_are_collected_and_all_marked() -> None:
    text = (HARDWARE / "test_hardware.py").read_text("utf-8")
    assert "pytestmark = [pytest.mark.hardware, pytest.mark.timeout(0)]" in text
    for check in ("pairing", "black_level", "visible_led", "camera_lock", "exposure_steps",
                  "reply_loss", "scan", "white_reference", "scalar_noise", "clipping"):  # fmt: skip
        assert f"acceptance.{check}(" in text, check
        assert callable(getattr(acceptance, check))
