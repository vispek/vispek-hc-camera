# SPDX-License-Identifier: Apache-2.0
"""Scanning on the simulated device: timing, retries, safety, what lands on disk."""

from __future__ import annotations

import dataclasses
import json
import math
import shutil
import threading
from collections import namedtuple
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vispek_hc import cube as cube_module
from vispek_hc import sweep
from vispek_hc.cube import CaptureWriter, load_capture
from vispek_hc.errors import (
    AWBNotConfirmedError,
    CameraChangedError,
    ConfigRejectedError,
    ControlsUnavailableError,
    FrameIncompleteError,
    GainNotConfirmedError,
    InvalidStateError,
    LedShutdownUnconfirmedError,
    OperationCancelledError,
    RecordingPartialError,
    StorageLowError,
    TimingUnstableError,
    UVNotAllowedError,
)
from vispek_hc.models import HC1500
from vispek_hc.protocol import Function
from vispek_hc.session import Session
from vispek_hc.sim import SimReply, SimulatedRig
from vispek_hc.sweep import ScanSettings, record

EXPOSURE, READOUT, LATENCY = 0.0157, 1 / 30, 0.150
HEIGHT = 36


def opened(probe: bool = True, **options: Any) -> tuple[Session, SimulatedRig]:
    rig = SimulatedRig(**options)
    session = Session.from_rig(rig)
    session.__enter__()
    if probe:
        session.pair_probe()
    return session, rig


def events(directory: Path, kind: str) -> list[dict[str, Any]]:
    lines = (directory / "events.jsonl").read_text("utf-8").splitlines()
    return [event for event in map(json.loads, lines) if event["type"] == kind]


def exposure_window(entry: dict[str, Any]) -> tuple[float, float]:
    """When the simulated sensor exposed a recorded frame, from its arrival time."""
    start = entry["received_mono_ns"] / 1e9 - (EXPOSURE + READOUT + LATENCY)
    return start, start + EXPOSURE + READOUT * (HEIGHT - 1) / HEIGHT


def lit_while_exposed(rig: SimulatedRig, entry: dict[str, Any]) -> bool:
    start, end = exposure_window(entry)
    return any(
        led_id == entry["led_id"] and lit_from <= start and end <= lit_until
        for led_id, _, lit_from, lit_until in rig.device.lit_intervals()
    )


# --- the default scan ------------------------------------------------------------------


def test_default_scan_records_a_dark_frame_and_one_frame_per_visible_led(tmp_path: Path) -> None:
    session, rig = opened()
    capture = record(session, tmp_path / "c")
    assert capture.status == "complete"
    (group,) = capture.groups
    assert group["status"] == "complete"
    assert [entry["led_id"] for entry in group["frames"]] == list(range(5, 18))
    assert group["dark"]["median_rgb"] == [5, 5, 5]
    assert sorted(path.name for path in (tmp_path / "c" / "g0001").iterdir())[:2] == [
        "dark.png",
        "led05_455nm.png",
    ]
    assert rig.device.lit == {}


def test_every_selected_frame_was_exposed_entirely_under_its_own_led(tmp_path: Path) -> None:
    session, rig = opened()
    (group,) = record(session, tmp_path / "c").groups
    assert all(lit_while_exposed(rig, entry) for entry in group["frames"])
    dark_start, dark_end = exposure_window(group["dark"])
    assert not any(
        lit_from < dark_end and dark_start < lit_until
        for _, _, lit_from, lit_until in rig.device.lit_intervals()
    ), "the dark frame saw no LED"


def test_recorded_cube_shows_the_white_board_values(tmp_path: Path) -> None:
    session, _ = opened()
    cube = record(session, tmp_path / "c").cube()
    assert cube.values.shape == (36, 64, 13)
    expected = [5 + max(HC1500.led(n).white_rgb) for n in range(5, 18)]
    assert cube.values[0, 0].tolist() == expected
    assert np.all(cube.dark_rgb == 5)


def test_only_one_led_is_lit_at_a_time_and_each_on_has_its_off(tmp_path: Path) -> None:
    session, rig = opened()
    record(session, tmp_path / "c")
    lit: set[int] = set()
    for _, command in rig.device.log:
        if command.function is Function.ON:
            assert lit == set(), "an LED was lit while another was still on"
            lit.add(command.led_id)
        else:
            lit.discard(command.led_id)
    assert lit == set()


def test_scan_takes_about_as_long_as_the_time_budget_says(tmp_path: Path) -> None:
    session, rig = opened()
    session.lock_controls()  # the first lock of a stream takes a second of darkness more
    started = rig.clock.now()
    record(session, tmp_path / "c")
    per_channel = (rig.clock.now() - started) / 14
    assert 0.60 < per_channel < 0.90, "13 channels and the dark frame, around 715 ms each"


def test_capture_records_the_configuration_it_was_made_with(tmp_path: Path) -> None:
    session, _ = opened()
    document = record(session, tmp_path / "c").document
    config = document["config"]
    assert config["channels"] == list(range(5, 18))
    assert config["pwm_by_led"]["5"] == 24
    assert config["min_on_ms"]["source"] == "auto"
    assert config["stable_frames"] == 2
    assert config["reduction"] == "dominant"
    assert config["controls_locked"] is True
    assert config["exposure"] == {"requested": 157, "effective": 157}
    assert config["camera_controls"]["auto-white-balance-temp"] == 0
    assert config["camera_controls_after"] == config["camera_controls"]
    assert config["actual_size"] == [64, 36]
    assert config["actual_fps"] == pytest.approx(10.0)
    assert config["pixel_format"] == "rgb24"
    assert config["pipeline_latency_ms"] == pytest.approx(185, abs=40)
    assert document["recording"] == {
        **document["recording"],
        "mode": "scan",
        "role": None,
        "cycles": 1,
        "average": 1,
        "status": "complete",
        "groups_done": 1,
    }
    assert document["device"]["simulated"] is True
    assert document["unit_id"] == "sim"


def test_capture_records_what_the_camera_and_the_link_were_set_to(tmp_path: Path) -> None:
    session, _ = opened()
    settings = ScanSettings(channels=(9,), white_roi=(0.25, 0.25, 0.5, 0.5), enclosure="darkbox")
    config = record(session, tmp_path / "c", settings).document["config"]
    assert config["ack_timeout_ms"] == 200
    assert (config["requested_size"], config["requested_fps"]) == ([64, 36], 10.0)
    assert config["gain"] == config["camera_controls"]["gain"]
    assert config["auto_white_balance"] is False
    assert config["exposure_locked"] is True
    assert config["white_roi_px"] == {"x0": 16, "y0": 9, "x1": 48, "y1": 27}
    assert config["acquisition"]["enclosure"] == "darkbox"


def test_capture_records_the_range_its_frames_were_converted_with(tmp_path: Path) -> None:
    session, rig = opened()
    assert (
        record(session, tmp_path / "a", ScanSettings(channels=(9,))).document["config"][
            "color_range"
        ]
        == "full"
    ), "simulated frames are RGB already"
    latest = rig.frames.latest

    def limited() -> Any:
        frame = latest()
        return dataclasses.replace(frame, color_range="limited") if frame else None

    rig.frames.latest = limited  # type: ignore[method-assign]
    config = record(session, tmp_path / "b", ScanSettings(channels=(9,))).document["config"]
    assert config["color_range"] == "limited"


def test_frame_entries_carry_the_frame_check(tmp_path: Path) -> None:
    session, _ = opened()
    entry = record(session, tmp_path / "c").groups[0]["frames"][4]
    assert (entry["led_id"], entry["nm"], entry["pwm"]) == (9, 630, 36)
    check = entry["frame_check"]
    assert check["attempts"] == 1
    assert check["observed_frames"] >= 2
    assert check["mean_difference"] <= 8
    assert check["response_dn"] == pytest.approx(175, abs=1)
    assert check["rgb_saturated_fraction"] == 0
    assert check["on_ack_to_selected_ms"] > 300
    assert check["suggested_min_on_ms"] % 50 == 0
    assert entry["on_ack_at_utc"].endswith("+00:00")
    assert entry["on_ack_at_utc"] < entry["received_at_utc"] <= entry["selected_at_utc"]


def test_events_tell_the_story_of_the_scan(tmp_path: Path) -> None:
    session, _ = opened()
    directory = tmp_path / "c"
    record(session, directory)
    commands = events(directory, "led_command")
    ons = [index for index, command in enumerate(commands) if command["pwm"] is not None]
    assert [commands[index]["led_id"] for index in ons] == list(range(5, 18))
    for index in ons:
        following = commands[index + 1]
        assert (following["led_id"], following["pwm"]) == (commands[index]["led_id"], None)
    assert commands[ons[0]]["sent"].startswith("3d 01 03 05")
    assert {"attempts", "duration_ms", "received", "accepted"} <= set(commands[0])
    assert len(commands) == 17 + 26, "all off before the dark frame, then ON and OFF per LED"
    assert len(events(directory, "frame_selected")) == 14
    assert len(events(directory, "channel_timing")) == 13
    history = events(directory, "timing_history")[0]["history"]
    assert {"mean_difference", "large_change_fraction", "max_line_difference"} <= set(history[0])


# --- timing ----------------------------------------------------------------------------


def test_minimum_on_time_comes_from_the_pairing_probe(tmp_path: Path) -> None:
    session, _ = opened()
    latency = session.unit.pipeline_latency_ms
    assert latency is not None
    document = record(session, tmp_path / "c").document
    expected = math.ceil((latency + 100 + 15.7) / 10) * 10
    assert set(document["config"]["min_on_ms"]["by_led"].values()) == {expected}, "up to 10 ms"


def test_minimum_on_time_falls_back_to_500_ms_without_a_probe(tmp_path: Path) -> None:
    # Measured on the device: light shows about 350 ms after the reply, and the first
    # whole lit frame 100 ms later. 300 ms, the earlier fallback, was inside that.
    session, rig = opened(probe=False)
    document = record(session, tmp_path / "c").document
    assert set(document["config"]["min_on_ms"]["by_led"].values()) == {500}
    assert all(lit_while_exposed(rig, entry) for entry in document["groups"][0]["frames"])


def test_the_camera_sees_one_whole_dark_frame_between_two_channels(tmp_path: Path) -> None:
    # The camera's own processing carries over from one picture to the next: on the
    # device, LED 9 read 118 straight after LED 8 and 141 after darkness. One frame
    # exposed entirely in the dark puts the camera in the same state before every channel.
    session, rig = opened()
    capture = record(session, tmp_path / "c")
    lit = sorted((start, end) for _, _, start, end in rig.device.lit_intervals())[-13:]
    # Whatever the phase of the frames against the scan: a frame that starts exposing the
    # moment the LED goes dark needs a period to come round, its exposure and its read-out.
    whole_frame = 1 / 10 + EXPOSURE + sweep.READOUT_ALLOWANCE_S
    for (_, went_off), (came_on, _) in pairwise(lit):
        assert came_on - went_off >= whole_frame, "a whole frame fits into the pause"
    assert capture.document["config"]["dark_pause_ms"] == 170
    longer = record(session, tmp_path / "longer", ScanSettings(inter_channel_ms=400))
    assert longer.document["config"]["dark_pause_ms"] == 400, "a longer pause may be asked for"
    shorter = record(session, tmp_path / "shorter", ScanSettings(inter_channel_ms=0))
    assert shorter.document["config"]["dark_pause_ms"] == 170, "but never a shorter one"


@pytest.mark.parametrize("fps", [60.0, 30.0, 10.04, 9.97, 5.6, 3.6, 2.6, 1.5, 0.7])
def test_the_dark_pause_holds_a_whole_frame_at_rates_that_are_not_whole_numbers(fps: float) -> None:
    # 5.6 fps is what the camera gives at 1080p. With the period of the rounded rate
    # (6 fps) the pause was shorter than a frame needs; at 2.6 fps nothing was left.
    session, _ = opened(probe=False, fps=fps)
    pause = sweep.plan(session, ScanSettings()).dark_pause_s
    whole_frame = 1 / fps + EXPOSURE + sweep.READOUT_ALLOWANCE_S
    assert pause >= whole_frame - 0.02 / fps, "a rate within 2 % of a whole number counts as it"
    assert pause < whole_frame + max(0.015, 1 / fps), "and not longer than one more frame"


def test_two_sessions_plan_the_same_dark_pause() -> None:
    # The pause is part of what binds a white reference to its samples, so the last
    # digits of the measured frame rate must not change it.
    pauses = set()
    for fps in (10.0, 9.97, 10.04, 9.85, 10.15):
        session, _ = opened(probe=False, fps=fps)
        pauses.add(sweep.plan(session, ScanSettings()).dark_pause_s)
    assert pauses == {0.17}


def test_time_floor_keeps_early_frames_out_even_with_no_waiting_at_all(tmp_path: Path) -> None:
    # With no minimum on-time, and a pause that puts each ON just before a frame arrives,
    # the first two frames after ON are both still dark and agree with each other. Only
    # the floor (latency + one period + exposure) keeps that pair from being accepted.
    session, rig = opened()
    capture = record(session, tmp_path / "c", ScanSettings(min_on_ms=0, inter_channel_ms=60))
    assert capture.document["config"]["min_on_ms"]["source"] == "manual"
    (group,) = capture.groups
    assert all(lit_while_exposed(rig, entry) for entry in group["frames"])
    assert all(entry["frame_check"]["response_dn"] > 100 for entry in group["frames"])


def test_a_longer_minimum_on_time_is_waited_out(tmp_path: Path) -> None:
    session, _ = opened()
    capture = record(session, tmp_path / "c", ScanSettings(channels=(5, 9), min_on_ms=600))
    for entry in capture.groups[0]["frames"]:
        assert entry["frame_check"]["on_ack_to_selected_ms"] >= 600
        assert entry["min_on_ms"] == 600


def test_plan_does_not_depend_on_the_last_digits_of_the_measured_frame_rate() -> None:
    plans = []
    for fps in (10.0, 9.97, 10.04):
        session, _ = opened(probe=False, fps=fps)
        session.unit.pipeline_latency_ms = 184.3  # one unit profile, three sessions
        plans.append(sweep.plan(session, ScanSettings()).min_on_s)
    # 184.3 + 100 + 15.7 = 300 ms exactly. With the period of 9.97 fps (100.3 ms) it
    # would be 300.3 and round up to 310: the frame rate is rounded first.
    assert plans[0] == plans[1] == plans[2]
    assert set(plans[0].values()) == {0.3}
    session.unit.pipeline_latency_ms = 181.3
    assert set(sweep.plan(session, ScanSettings()).min_on_s.values()) == {0.3}, "up to 10 ms"


def test_more_stable_frames_need_more_frames_that_agree(tmp_path: Path) -> None:
    session, _ = opened()
    observed = []
    for number, stable_frames in enumerate((2, 4)):
        settings = ScanSettings(channels=(9,), stable_frames=stable_frames)
        entry = record(session, tmp_path / str(number), settings).groups[0]["frames"][0]
        observed.append(entry["frame_check"]["observed_frames"])
    assert observed == [2, 4]
    history = events(tmp_path / "1", "timing_history")[0]["history"]
    assert len(history) == 3, "three comparisons in a row passed"


def restless(rig: SimulatedRig) -> None:
    """Make the scene change with every frame, so that no two frames agree."""
    flicker = iter(range(10**6))
    original = rig.frames.wait_new

    def changing(count: int = 1, timeout: float | None = None) -> Any:
        rig.frames._reflectance = np.full((36, 64), 0.2 + 0.8 * (next(flicker) % 2))
        return original(count, timeout)

    rig.frames.wait_new = changing  # type: ignore[method-assign]


def test_selection_gives_up_after_five_seconds_whatever_the_frame_budget(tmp_path: Path) -> None:
    session, rig = opened()
    restless(rig)
    with pytest.raises(TimingUnstableError) as caught:
        record(session, tmp_path / "c", ScanSettings(channels=(9,), max_extra_frames=60))
    assert 48 <= caught.value.details["observed_frames"] <= 53, "5 s at 10 fps, not 62 frames"


def test_frame_that_cannot_be_decoded_is_rejected_and_the_scan_goes_on(tmp_path: Path) -> None:
    session, rig = opened()
    original = rig.frames.wait_new
    under_led_9 = iter(range(10**6))

    def damaged_once(count: int = 1, timeout: float | None = None) -> Any:
        frame = original(count, timeout)
        if 9 in rig.device.lit and next(under_led_9) == 1:  # the second frame looked at
            return dataclasses.replace(frame, pixel_format="mjpeg", data=b"\xff\xd8 not a jpeg")
        return frame

    rig.frames.wait_new = damaged_once  # type: ignore[method-assign]
    directory = tmp_path / "c"
    capture = record(session, directory, ScanSettings(channels=(9, 10)))
    assert capture.status == "complete"
    first, second = capture.groups[0]["frames"]
    assert first["frame_check"]["observed_frames"] == second["frame_check"]["observed_frames"] + 2
    history = events(directory, "timing_history")[0]["history"]
    assert history[0] == {"frame": history[0]["frame"], "rejected": "undecodable"}
    assert lit_while_exposed(rig, first)


def test_dark_frame_of_a_later_group_waits_for_the_last_led_to_fade_from_the_pipeline(
    tmp_path: Path,
) -> None:
    # A slow pipeline at 60 fps: when the all-off between groups is done, several frames
    # exposed under the last LED are still on their way, and they agree with each other.
    session, _ = opened(latency_s=0.6, fps=60.0)
    capture = record(session, tmp_path / "c", ScanSettings(channels=(17,), average=3))
    assert [group["dark"]["median_rgb"] for group in capture.groups] == [[5, 5, 5]] * 3


def test_learned_timing_of_the_unit_is_used(tmp_path: Path) -> None:
    session, _ = opened()
    session.unit.min_on_ms_by_led = {5: 450}
    by_led = record(session, tmp_path / "c").document["config"]["min_on_ms"]["by_led"]
    assert by_led["5"] == 450
    assert by_led["6"] != 450


def test_suggested_on_time_is_when_the_agreeing_pair_of_frames_began(tmp_path: Path) -> None:
    session, _ = opened()
    document = record(session, tmp_path / "c", ScanSettings(channels=(9,))).document
    waited = document["config"]["min_on_ms"]["by_led"]["9"]
    check = document["groups"][0]["frames"][0]["frame_check"]
    assert check["observed_frames"] == 2, "the first pair agreed"
    assert check["suggested_min_on_ms"] == math.ceil(waited / 50) * 50
    assert check["suggested_min_on_ms"] < check["on_ack_to_selected_ms"]


def test_pwm_comes_from_the_settings_then_the_unit_then_the_model(tmp_path: Path) -> None:
    session, _ = opened()
    session.unit.pwm_by_led = {5: 30, 6: 200}
    settings = ScanSettings(channels=(5, 6, 7), pwm={5: 40})
    config = record(session, tmp_path / "a", settings).document["config"]
    assert config["pwm_by_led"] == {"5": 40, "6": 200, "7": 600}, "each LED on its own"
    config = record(session, tmp_path / "b", ScanSettings(channels=(5, 6))).document["config"]
    assert config["pwm_by_led"] == {"5": 30, "6": 200}
    config = record(session, tmp_path / "c", ScanSettings(channels=(5, 6), pwm=80)).document[
        "config"
    ]
    assert config["pwm_by_led"] == {"5": 80, "6": 80}


# --- refusing a bad plan before anything happens ---------------------------------------


def test_uv_channels_are_refused_while_planning(tmp_path: Path) -> None:
    session, rig = opened()
    sent = len(rig.device.log)
    with pytest.raises(UVNotAllowedError):
        record(session, tmp_path / "c", ScanSettings(channels=(5, 6, 7, 8, 1)))
    assert len(rig.device.log) == sent, "nothing was lit before the refusal"
    assert not (tmp_path / "c").exists()


def test_uv_channels_scan_with_permission(tmp_path: Path) -> None:
    session, rig = opened(allow_uv=True)
    capture = record(session, tmp_path / "c", ScanSettings(channels=(1, 5)))
    assert [entry["led_id"] for entry in capture.groups[0]["frames"]] == [1, 5]
    assert rig.device.lit == {}


@pytest.mark.parametrize(
    "bad",
    [
        {"stable_frames": 1},
        {"stable_frames": 9},
        {"max_extra_frames": 61},
        {"inter_channel_ms": -1},
        {"min_on_ms": 10001},
        {"stability_tolerance": 0},
        {"max_saturated_fraction": 0.06},
        {"average": 0},
        {"cycles": 10001},
    ],
)
def test_settings_outside_their_range_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ConfigRejectedError) as caught:
        ScanSettings(**bad)
    assert caught.value.details["setting"] in bad


@pytest.mark.parametrize(
    "settings", [{"pwm": 0}, {"pwm": 1024}, {"channels": (5, 5)}, {"channels": (18,)}]
)
def test_bad_pwm_or_channels_are_refused_while_planning(
    settings: dict[str, Any], tmp_path: Path
) -> None:
    session, rig = opened()
    sent = len(rig.device.log)
    with pytest.raises(ConfigRejectedError):
        record(session, tmp_path / "c", ScanSettings(**settings))
    assert len(rig.device.log) == sent


@pytest.mark.parametrize(
    "pwm", [True, 3.5, "600", {5: "600"}, {5: 1024}, {5: -1}, {18: 5}, {"5": 600}, {5: 0}]
)
def test_pwm_must_be_whole_numbers_for_leds_that_exist(pwm: Any) -> None:
    with pytest.raises(ConfigRejectedError):
        sweep.check_plan(ScanSettings(channels=(5, 6), pwm=pwm), allow_uv=False)


def test_plan_check_needs_no_device_and_resolves_every_pwm() -> None:
    settings = ScanSettings(channels=(5, 6, 7), pwm={5: 40, 4: 0})  # LED 4 is not scanned
    ids, pwm = sweep.check_plan(settings, allow_uv=False, profile_pwm={5: 30, 6: 200})
    assert ids == [5, 6, 7]
    assert pwm == {5: 40, 6: 200, 7: 600}
    with pytest.raises(UVNotAllowedError):
        sweep.check_plan(ScanSettings(channels=(1,)), allow_uv=False)
    with pytest.raises(ConfigRejectedError):
        sweep.check_plan(ScanSettings(channels=(5.0,)), allow_uv=False)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        {"average": 2.0},
        {"cycles": True},
        {"min_on_ms": 300.5},
        {"stability_tolerance": "8"},
        {"reduction": "mean"},
        {"role": "sample"},
        {"enclosure": "tent"},
        {"white_roi": (0.5, 0.5, 0.6, 0.2)},
    ],
)
def test_settings_of_the_wrong_kind_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ConfigRejectedError):
        ScanSettings(**bad)


def test_long_manual_on_time_is_refused_for_uv_leds() -> None:
    # A UV LED is switched off by its watchdog after 10 s; the scan must fit inside.
    with pytest.raises(ConfigRejectedError, match="too long for ultraviolet") as caught:
        sweep.check_plan(ScanSettings(channels=(1, 5), min_on_ms=5001), allow_uv=True)
    assert caught.value.details["led_ids"] == [1]
    sweep.check_plan(ScanSettings(channels=(1, 5), min_on_ms=5000), allow_uv=True)
    sweep.check_plan(ScanSettings(channels=(5,), min_on_ms=10000), allow_uv=True)


def test_bad_plan_is_refused_before_the_camera_is_touched(tmp_path: Path) -> None:
    session, rig = opened()
    with pytest.raises(UVNotAllowedError):
        record(session, tmp_path / "c", ScanSettings(channels=(1,)))
    with pytest.raises(ConfigRejectedError):
        record(session, tmp_path / "c", ScanSettings(pwm={5: 0}))
    assert rig.controls.writes == [], "camera parameters were not written"
    assert session.lock_report is None


def test_folder_in_use_is_refused_before_the_camera_is_touched(tmp_path: Path) -> None:
    session, rig = opened()
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(InvalidStateError):
        record(session, tmp_path, overwrite=True)
    assert rig.controls.writes == []
    assert (tmp_path / "notes.txt").read_text() == "mine"


def test_one_call_capture_checks_the_plan_before_it_opens_a_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(**options: Any) -> Any:
        raise AssertionError("the device was opened")

    monkeypatch.setattr(sweep, "open_session", never)
    with pytest.raises(UVNotAllowedError):
        sweep.capture(tmp_path / "c", ScanSettings(channels=(1, 5)), simulate=True)
    with pytest.raises(ConfigRejectedError):
        sweep.capture(tmp_path / "c", ScanSettings(pwm=2000), simulate=True)
    (tmp_path / "notes.txt").write_text("mine")
    with pytest.raises(InvalidStateError):
        sweep.capture(tmp_path, simulate=True, overwrite=True)


def test_one_call_capture_checks_the_stored_pwm_before_the_pairing_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = SimulatedRig()
    session = Session.from_rig(rig)
    session.unit.pwm_by_led = {9: 0}  # a profile edited by hand
    monkeypatch.setattr(sweep, "open_session", lambda **options: session)
    with pytest.raises(ConfigRejectedError):
        sweep.capture(tmp_path / "c", simulate=True)
    assert [c for _, c in rig.device.log if c.function is Function.ON] == []


def test_recording_is_refused_when_the_disk_is_nearly_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, rig = opened()
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(10**9, 10**9, 60_000))
    sent = len(rig.device.log)
    with pytest.raises(StorageLowError) as caught:
        record(session, tmp_path / "c")
    assert caught.value.details["needed_bytes"] > 60_000
    assert [c for _, c in rig.device.log[sent:] if c.function is Function.ON] == []


# --- camera parameters -----------------------------------------------------------------


def test_scan_is_refused_when_white_balance_stays_automatic(tmp_path: Path) -> None:
    session, rig = opened()
    rig.controls.quantise["auto-white-balance-temp"] = lambda value: 1
    with pytest.raises(AWBNotConfirmedError):
        record(session, tmp_path / "c")
    capture = record(session, tmp_path / "c", ScanSettings(require_awb_off=False))
    assert capture.document["config"]["controls_locked"] is False


def test_scan_without_a_control_backend_needs_the_explicit_override(tmp_path: Path) -> None:
    rig = SimulatedRig()
    session = Session.from_rig(rig)
    session.controls = None
    session.__enter__()
    with pytest.raises(ControlsUnavailableError):
        record(session, tmp_path / "c")
    capture = record(session, tmp_path / "c", ScanSettings(require_awb_off=False))
    assert capture.status == "complete"
    assert capture.document["config"]["controls_locked"] is False
    assert capture.document["config"]["camera_controls"] is None


def test_unlocked_scan_records_what_moved_instead_of_failing(tmp_path: Path) -> None:
    session, rig = opened()
    rig.controls.quantise["auto-white-balance-temp"] = lambda value: 1

    def gain_creeps() -> None:
        if len(rig.device.log) > 60:
            rig.controls.write("gain", 7)

    rig.device.on_write = gain_creeps
    directory = tmp_path / "c"
    capture = record(session, directory, ScanSettings(require_awb_off=False))
    assert capture.status == "complete"
    config = capture.document["config"]
    assert config["controls_locked"] is False
    assert config["auto_white_balance"] is True
    assert config["camera_controls_after"]["gain"] == 7
    (verification,) = events(directory, "camera_control_verification")
    assert verification["changed"] == ["gain"]


def test_locked_scan_logs_that_nothing_moved(tmp_path: Path) -> None:
    session, _ = opened()
    record(session, tmp_path / "c", ScanSettings(channels=(9,), average=2))
    checks = events(tmp_path / "c", "camera_control_verification")
    assert [(check["group_id"], check["changed"]) for check in checks] == [
        ("g0001", []),
        ("g0002", []),
    ]


def test_group_fails_when_camera_parameters_move_during_the_scan(tmp_path: Path) -> None:
    session, rig = opened()

    def gain_creeps() -> None:
        if len(rig.device.log) > 60:
            rig.controls.write("gain", 7)

    rig.device.on_write = gain_creeps
    with pytest.raises(CameraChangedError) as caught:
        record(session, tmp_path / "c")
    assert caught.value.details["changed"] == ["gain"]
    capture = load_capture(tmp_path / "c")
    assert capture.status == "partial"
    (group,) = capture.groups
    assert group["status"] == "failed"
    assert group["failure"]["code"] == "CAMERA_CHANGED"
    assert len(group["frames"]) == 13, "the source frames are kept"
    assert capture.outputs == 0, "but no result is made from them"
    assert rig.device.lit == {}


# --- no light, retries, failures -------------------------------------------------------


def test_sample_that_gives_no_response_is_recorded_with_a_warning(tmp_path: Path) -> None:
    session, rig = opened()
    rig.device.dead_leds = {7}
    directory = tmp_path / "c"
    capture = record(session, directory)
    assert capture.status == "complete"
    entry = next(e for e in capture.groups[0]["frames"] if e["led_id"] == 7)
    assert entry["frame_check"]["response_dn"] == 0
    assert entry["frame_check"]["attempts"] == 1
    (warning,) = events(directory, "no_led_response")
    assert warning["led_id"] == 7


def test_faint_response_counts_as_none_even_when_the_frame_is_not_black(tmp_path: Path) -> None:
    # Half the picture answers with 2 code values: the mean rises by exactly 1 (not above
    # 1), yet half the pixels are brighter than black + 1, so the frame is not "nearly black".
    reflectance = np.zeros((36, 64))
    reflectance[:, :32] = 2.2 / 175
    session, _ = opened(reflectance=reflectance)
    directory = tmp_path / "c"
    entry = record(session, directory, ScanSettings(channels=(9,))).groups[0]["frames"][0]
    assert entry["frame_check"]["response_dn"] == 1.0
    assert [warning["led_id"] for warning in events(directory, "no_led_response")] == [9]
    with pytest.raises(TimingUnstableError, match="no light"):
        record(session, tmp_path / "w", ScanSettings(channels=(9,), role="white"))


def test_nearly_black_frame_counts_as_no_response_whatever_its_mean(tmp_path: Path) -> None:
    # Ten clipped pixels lift the mean by more than 1, but 99.5 % of the frame is black.
    reflectance = np.zeros((36, 64))
    reflectance[0, :10] = 3.0
    session, _ = opened(reflectance=reflectance)
    directory = tmp_path / "c"
    entry = record(session, directory, ScanSettings(channels=(9,))).groups[0]["frames"][0]
    assert entry["frame_check"]["response_dn"] > 1
    assert [warning["led_id"] for warning in events(directory, "no_led_response")] == [9]
    with pytest.raises(TimingUnstableError, match="no light"):
        record(session, tmp_path / "w", ScanSettings(channels=(9,), role="white"))


def test_a_scan_records_that_the_camera_gain_was_settled_in_darkness(tmp_path: Path) -> None:
    session, _ = opened()
    config = record(session, tmp_path / "c", ScanSettings(channels=(9,))).document["config"]
    assert config["camera_gain_state"] == "dark"
    assert config["lock_dark_level"] == [5.0, 5.0, 5.0]
    assert config["controls_locked"] is True


def test_a_scan_is_refused_when_the_camera_sees_light_at_the_lock(tmp_path: Path) -> None:
    # The camera keeps a gain from the moment of the lock (a factor of 4.4 between a lock
    # in darkness and one under light); a scan locked under light is comparable with nothing.
    session, rig = opened()
    rig.frames.ambient = 40.0  # an open lid
    with pytest.raises(GainNotConfirmedError):
        record(session, tmp_path / "refused", ScanSettings(channels=(9,)))
    assert rig.device.lit == {}
    assert not (tmp_path / "refused" / "capture.json").exists(), "nothing was recorded"
    unlocked = ScanSettings(channels=(9,), require_awb_off=False)
    config = record(session, tmp_path / "open", unlocked).document["config"]
    assert config["camera_gain_state"] == "undetermined"
    assert config["controls_locked"] is False


def test_response_is_judged_in_the_leds_own_channel(tmp_path: Path) -> None:
    # Give every frame a green cast, the dark frame included. LED 5 answers in blue: its
    # response is its blue minus the dark frame's blue, and the cast must not enter.
    session, rig = opened()
    session.lock_controls()  # before the cast: a lock would take it for light
    original = rig.frames.wait_new

    def green_cast(count: int = 1, timeout: float | None = None) -> Any:
        frame = original(count, timeout)
        rgb = np.frombuffer(frame.data, np.uint8).reshape(36, 64, 3).copy()
        rgb[..., 1] += 20
        return dataclasses.replace(frame, data=rgb.tobytes())

    rig.frames.wait_new = green_cast  # type: ignore[method-assign]
    capture = record(session, tmp_path / "c", ScanSettings(channels=(5,), role="white"))
    assert capture.groups[0]["dark"]["median_rgb"] == [5, 25, 5]
    blue = max(HC1500.led(5).white_rgb)
    assert capture.groups[0]["frames"][0]["frame_check"]["response_dn"] == blue


@pytest.mark.parametrize(("columns", "warned"), [(2, True), (0, False)])
def test_saturation_is_warned_about_above_one_percent(
    columns: int, warned: bool, tmp_path: Path
) -> None:
    reflectance = np.ones((36, 64))
    reflectance[:, :columns] = 3.0  # two columns: 3.1 % of the picture clips
    reflectance[0, 60:] = 3.0  # four pixels: 0.2 %, below the threshold in both cases
    session, _ = opened(reflectance=reflectance)
    directory = tmp_path / "c"
    entry = record(session, directory, ScanSettings(channels=(9,))).groups[0]["frames"][0]
    warnings = events(directory, "saturation_warning")
    assert [warning["led_id"] for warning in warnings] == ([9] if warned else [])
    assert entry["frame_check"]["rgb_saturated_fraction"] > 0


def test_white_reference_retakes_a_channel_that_gave_no_light(tmp_path: Path) -> None:
    session, rig = opened()
    rig.device.dead_leds = {7}

    def revive_after_first_try() -> None:
        ons = [c for _, c in rig.device.log if c.function is Function.ON and c.led_id == 7]
        if len(ons) == 1:
            rig.device.dead_leds = set()

    rig.device.on_write = revive_after_first_try
    directory = tmp_path / "c"
    capture = record(session, directory, ScanSettings(role="white"))
    assert capture.status == "complete"
    entry = next(e for e in capture.groups[0]["frames"] if e["led_id"] == 7)
    assert entry["frame_check"]["attempts"] == 2
    assert entry["frame_check"]["response_dn"] > 100
    (rejected,) = events(directory, "frame_rejected")
    assert (rejected["led_id"], rejected["attempt"]) == (7, 1)
    assert (directory / "g0001" / "rejected" / "led07_560nm_a1.png").exists()
    assert capture.document["recording"]["role"] == "white"


def test_retakes_stop_after_three_attempts(tmp_path: Path) -> None:
    session, rig = opened()
    rig.device.dead_leds = {7}
    with pytest.raises(TimingUnstableError) as caught:
        record(session, tmp_path / "c", ScanSettings(role="white"))
    assert caught.value.details["led_id"] == 7
    assert "no light" in caught.value.message
    ons = [c for _, c in rig.device.log if c.function is Function.ON and c.led_id == 7]
    assert len(ons) == 3, "never flashes without end"
    assert rig.device.lit == {}
    capture = load_capture(tmp_path / "c")
    assert capture.status == "partial"
    assert capture.groups[0]["failure"] == {
        "code": "TIMING_UNSTABLE",
        "message": caught.value.message,
        "led_id": 7,
    }
    assert [e["led_id"] for e in capture.groups[0]["frames"]] == [5, 6], "what was good is kept"
    rejects = sorted(p.name for p in (tmp_path / "c" / "g0001" / "rejected").iterdir())
    assert rejects == ["led07_560nm_a1.png", "led07_560nm_a2.png", "led07_560nm_a3.png"]


def test_white_reference_accepts_a_dark_channel_when_told_to(tmp_path: Path) -> None:
    session, rig = opened()
    rig.device.dead_leds = {7}
    capture = record(session, tmp_path / "c", ScanSettings(role="white", allow_dark_signal=True))
    assert capture.status == "complete"


def test_frames_that_never_settle_fail_the_channel(tmp_path: Path) -> None:
    session, rig = opened()
    restless(rig)
    with pytest.raises(TimingUnstableError, match="did not settle") as caught:
        record(session, tmp_path / "c", ScanSettings(channels=(9,), max_extra_frames=4))
    assert caught.value.details["observed_frames"] == 6, "stable_frames + max_extra_frames"
    ons = [c for _, c in rig.device.log if c.function is Function.ON and c.led_id == 9]
    assert len(ons) == 3
    assert rig.device.lit == {}


def test_led_is_switched_off_by_its_own_channel_when_the_camera_drops_out(tmp_path: Path) -> None:
    # The all-off that ends a recording would hide a missing OFF, so look at the order:
    # the command right after the ON must be the OFF of the same LED, not "OFF 1".
    session, rig = opened()

    def stall_once_led_9_is_lit() -> None:
        if 9 in rig.device.lit:
            rig.device.on_write = None
            rig.frames.stall(60.0)

    rig.device.on_write = stall_once_led_9_is_lit
    with pytest.raises(FrameIncompleteError):
        record(session, tmp_path / "c")
    commands = [command for _, command in rig.device.log]
    last_on = max(i for i, command in enumerate(commands) if command.function is Function.ON)
    lit, following = commands[last_on], commands[last_on + 1]
    assert lit.led_id > 1, "an all-off starts with LED 1; this OFF must not be that one"
    assert (following.function, following.led_id) == (Function.OFF, lit.led_id)
    logged = events(tmp_path / "c", "led_command")
    assert [(e["led_id"], e["pwm"]) for e in logged[-2:]] == [
        (lit.led_id, lit.pwm),
        (lit.led_id, None),
    ]


def test_failure_after_a_finished_output_is_a_partial_recording(tmp_path: Path) -> None:
    session, rig = opened()

    def stall_in_the_second_scan() -> None:
        ons = [c for _, c in rig.device.log if c.function is Function.ON]
        if len(ons) == 3:  # two channels per scan: this is the first ON of scan two
            rig.device.on_write = None
            rig.frames.stall(60.0)

    rig.device.on_write = stall_in_the_second_scan
    with pytest.raises(RecordingPartialError) as caught:
        record(session, tmp_path / "c", ScanSettings(channels=(5, 9), cycles=3))
    assert caught.value.details["cause"] == "FRAME_INCOMPLETE"
    assert caught.value.details["outputs"] == 1
    assert caught.value.details["capture"] == "c"
    assert caught.value.exit_code == 7
    capture = load_capture(tmp_path / "c")
    assert (capture.status, capture.outputs) == ("partial", 1)
    assert capture.cube(0).values.shape == (36, 64, 2)
    assert rig.device.lit == {}


def test_camera_dropout_ends_the_scan_with_everything_off(tmp_path: Path) -> None:
    session, rig = opened()

    session.lock_controls()
    before = len(rig.device.log)

    def stall_late() -> None:
        if len(rig.device.log) > before + 28:  # some channels are done, some still to come
            rig.device.on_write = None
            rig.frames.stall(60.0)

    rig.device.on_write = stall_late
    with pytest.raises(FrameIncompleteError):
        record(session, tmp_path / "c")
    assert rig.device.lit == {}
    capture = load_capture(tmp_path / "c")
    assert capture.status == "partial"
    assert capture.groups[0]["failure"]["code"] == "FRAME_INCOMPLETE"
    assert 0 < len(capture.groups[0]["frames"]) < 13


def test_an_unconfirmed_off_is_followed_by_one_more_all_off(tmp_path: Path) -> None:
    session, rig = opened()

    def lose_the_offs_of_led_7() -> None:
        ons = [c for _, c in rig.device.log if c.function is Function.ON and c.led_id == 7]
        if ons and rig.device.on_write is not None:
            rig.device.on_write = None
            rig.device.script(*[SimReply(lost=True)] * 3)  # this write is the first OFF

    rig.device.on_write = lose_the_offs_of_led_7
    with pytest.raises(LedShutdownUnconfirmedError):
        record(session, tmp_path / "c")
    offs = [c for _, c in rig.device.log if c.function is Function.OFF and c.led_id == 7]
    scan_offs = offs[2:]  # after connecting and the all-off that starts the scan
    assert len(scan_offs) == 3 + 1, "three attempts, then the all-off that ends the recording"
    assert rig.device.lit == {}
    assert load_capture(tmp_path / "c").status == "partial"


# --- stopping --------------------------------------------------------------------------


def test_stop_is_honoured_only_once_the_led_is_off(tmp_path: Path) -> None:
    session, rig = opened()
    stop = threading.Event()
    lit_when_raised: list[dict[int, int]] = []

    def stop_while_lit() -> None:
        if rig.device.lit:
            stop.set()

    rig.device.on_write = stop_while_lit
    try:
        record(session, tmp_path / "c", stop=stop)
    except OperationCancelledError:
        lit_when_raised.append(dict(rig.device.lit))
    assert lit_when_raised == [{}]
    capture = load_capture(tmp_path / "c")
    assert capture.status == "partial"
    assert capture.groups[0]["failure"]["code"] == "CANCELLED"


def test_stop_is_noticed_during_a_long_wait(tmp_path: Path) -> None:
    session, rig = opened()
    stop = threading.Event()
    stopped_at: list[float] = []

    def stop_while_lit() -> None:
        if rig.device.lit and not stopped_at:
            stop.set()
            stopped_at.append(rig.clock.now())

    rig.device.on_write = stop_while_lit
    with pytest.raises(OperationCancelledError):
        record(session, tmp_path / "c", ScanSettings(channels=(9,), min_on_ms=8000), stop=stop)
    assert rig.clock.now() - stopped_at[0] < 0.5, "not after the 8 s the LED was to stay lit"
    assert rig.device.lit == {}


def test_stopping_a_continuous_scan_mid_group_keeps_the_finished_outputs(tmp_path: Path) -> None:
    session, rig = opened()
    stop = threading.Event()
    sent = len(rig.device.log)

    def stop_in_the_third_scan() -> None:
        ons = [c for _, c in rig.device.log[sent:] if c.function is Function.ON]
        if len(ons) == 5:
            stop.set()

    rig.device.on_write = stop_in_the_third_scan
    capture = record(session, tmp_path / "c", ScanSettings(channels=(5, 9), cycles=0), stop=stop)
    assert capture.outputs == 2
    assert [group["status"] for group in capture.groups] == ["complete", "complete", "partial"]
    assert capture.groups[2]["failure"]["code"] == "CANCELLED"
    assert rig.device.lit == {}


def test_continuous_scanning_runs_until_stopped_and_ends_cleanly(tmp_path: Path) -> None:
    session, rig = opened()
    stop = threading.Event()
    seen: list[int] = []
    for output in sweep.iter_scans(
        session, tmp_path / "c", ScanSettings(channels=(5, 9), cycles=0), stop=stop
    ):
        seen.append(output)
        if output == 3:
            stop.set()
    assert seen == [1, 2, 3]
    capture = load_capture(tmp_path / "c")
    assert capture.outputs == 3
    assert rig.device.lit == {}


# --- averaging and cycles --------------------------------------------------------------


def test_averaging_records_every_scan_and_loads_as_one_cube(tmp_path: Path) -> None:
    session, _ = opened()
    capture = record(session, tmp_path / "c", ScanSettings(channels=(5, 9), average=3, cycles=2))
    assert [group["group_id"] for group in capture.groups] == [f"g000{n}" for n in range(1, 7)]
    assert capture.outputs == 2
    cube = capture.cube(1)
    assert cube.metadata["group_ids"] == ["g0004", "g0005", "g0006"]
    assert cube.values[0, 0].tolist() == [5 + 139, 5 + 175]


def test_progress_is_reported_per_band(tmp_path: Path) -> None:
    session, _ = opened()
    updates: list[dict[str, Any]] = []
    record(session, tmp_path / "c", ScanSettings(channels=(5, 9)), progress=updates.append)
    assert updates == [
        {"group_id": "g0001", "band": 1, "bands": 2, "led_id": 5},
        {"group_id": "g0001", "band": 2, "bands": 2, "led_id": 9},
    ]


def test_existing_capture_is_not_overwritten_unless_asked(tmp_path: Path) -> None:
    session, _ = opened()
    record(session, tmp_path / "c", ScanSettings(channels=(5,)))
    with pytest.raises(InvalidStateError, match="already holds a capture"):
        record(session, tmp_path / "c", ScanSettings(channels=(9,)))
    capture = record(session, tmp_path / "c", ScanSettings(channels=(9,)), overwrite=True)
    assert [entry["led_id"] for entry in capture.groups[0]["frames"]] == [9]


# --- disk writing ----------------------------------------------------------------------


def test_scan_waits_for_a_slow_disk_only_while_no_led_is_lit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, rig = opened()
    release = threading.Event()
    blocked = threading.Event()
    real_write = CaptureWriter.write_frame

    def slow_write(self: CaptureWriter, name: str, frame: Any) -> dict[str, Any]:
        blocked.set()
        assert release.wait(30)
        return real_write(self, name, frame)

    monkeypatch.setattr(CaptureWriter, "write_frame", slow_write)
    done: list[Any] = []
    scan = threading.Thread(target=lambda: done.append(record(session, tmp_path / "c")))
    scan.start()
    assert blocked.wait(30)
    commands_before = -1
    while commands_before != len(rig.device.log):  # wait until the scan stands still
        commands_before = len(rig.device.log)
        scan.join(0.2)
    assert scan.is_alive(), "the scan is waiting for the disk"
    assert rig.device.lit == {}, "and it waits in the dark"
    release.set()
    scan.join(30)
    assert done[0].status == "complete"


def test_the_scan_writes_its_events_only_while_no_led_is_lit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Writing an event can wait for the disk, so the scan's own thread must never do it
    # with light on. (The disk thread writes whenever it likes.)
    session, rig = opened()
    real_event = CaptureWriter.event
    lit_at_event: list[tuple[str, dict[int, int]]] = []

    def watched(self: CaptureWriter, kind: str, **fields: Any) -> None:
        if threading.current_thread().name != "vispek-hc-disk":
            lit_at_event.append((kind, dict(rig.device.lit)))
        real_event(self, kind, **fields)

    monkeypatch.setattr(CaptureWriter, "event", watched)
    rig.device.dead_leds = {7}
    with pytest.raises(TimingUnstableError):
        record(session, tmp_path / "c", ScanSettings(role="white"))
    kinds = {kind for kind, _ in lit_at_event}
    assert {"led_command", "channel_timing", "timing_history", "frame_rejected"} <= kinds
    assert [entry for entry in lit_at_event if entry[1]] == []


def fail_writes_after(monkeypatch: pytest.MonkeyPatch, count: int, error: OSError) -> None:
    real = cube_module._write_atomically
    left = [count]

    def failing(path: Path, data: bytes) -> None:
        left[0] -= 1
        if left[0] < 0:
            raise error
        real(path, data)

    monkeypatch.setattr(cube_module, "_write_atomically", failing)


def test_disk_that_fills_up_is_storage_low_wherever_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every write fails from the seventh on: frames, capture.json, and the last save
    # when the capture is closed.
    session, rig = opened()
    full = OSError(28, "No space left on device", str(tmp_path / "c" / "capture.json"))
    fail_writes_after(monkeypatch, 6, full)
    with pytest.raises(StorageLowError) as caught:
        record(session, tmp_path / "c")
    assert str(tmp_path) not in caught.value.message
    assert rig.device.lit == {}


def test_capture_folder_that_cannot_even_be_started_is_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, rig = opened()
    fail_writes_after(monkeypatch, 0, OSError(13, "Permission denied", str(tmp_path / "c")))
    sent = len(rig.device.log)
    with pytest.raises(RecordingPartialError, match="Permission denied") as caught:
        record(session, tmp_path / "c")
    assert str(tmp_path) not in caught.value.message
    assert [c for _, c in rig.device.log[sent:] if c.function is Function.ON] == []


@pytest.mark.parametrize("mode", ["single", "video"])
def test_plain_frames_report_a_write_failure_the_same_way(
    mode: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, _ = opened()
    fail_writes_after(monkeypatch, 1, OSError(28, "No space left on device"))
    with pytest.raises(StorageLowError):
        sweep.record_frames(session, tmp_path / "c", mode, count=3)


def test_rejected_frames_kept_for_diagnosis_are_limited_per_scan(tmp_path: Path) -> None:
    session, rig = opened()

    def revive_on_the_third_try() -> None:
        ons = [c.led_id for _, c in rig.device.log if c.function is Function.ON]
        rig.device.dead_leds = {led_id for led_id in (7, 8) if ons.count(led_id) < 2}

    rig.device.dead_leds = {7, 8}
    rig.device.on_write = revive_on_the_third_try
    directory = tmp_path / "c"
    capture = record(session, directory, ScanSettings(channels=(7, 8), role="white"))
    assert [e["frame_check"]["attempts"] for e in capture.groups[0]["frames"]] == [3, 3]
    assert len(events(directory, "frame_rejected")) == 4
    kept = sorted(path.name for path in (directory / "g0001" / "rejected").iterdir())
    assert kept == ["led07_560nm_a1.png", "led07_560nm_a2.png", "led08_610nm_a1.png"]


def test_disk_queue_is_bounded_and_blocks_the_caller_when_full() -> None:
    worker = sweep._DiskWorker()
    release = threading.Event()
    ran: list[int] = []

    def job(number: int) -> None:
        assert release.wait(30)
        ran.append(number)

    for number in range(sweep.QUEUE_SIZE + 1):  # one running, the queue full
        worker.submit(lambda number=number: job(number))
    extra = threading.Thread(target=lambda: worker.submit(lambda: job(99)))
    extra.start()
    extra.join(0.3)
    assert extra.is_alive(), "the caller waits instead of piling frames up in memory"
    release.set()
    extra.join(30)
    worker.flush()
    worker.close()
    assert ran == [*range(sweep.QUEUE_SIZE + 1), 99]


def test_disk_worker_keeps_its_failure_and_runs_nothing_after_it() -> None:
    worker = sweep._DiskWorker()
    ran: list[str] = []

    def fail() -> None:
        raise OSError(5, "Input/output error")

    worker.submit(fail)
    worker.submit(lambda: ran.append("after the failure"))
    for _ in range(2):  # the failure does not go away once reported
        with pytest.raises(OSError, match="Input/output"):
            worker.flush()
    with pytest.raises(OSError, match="Input/output"):
        worker.submit(lambda: ran.append("later"))
    worker.close()
    assert ran == []


def test_disk_failure_is_recorded_without_the_path_it_happened_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, rig = opened()

    def broken(self: CaptureWriter, name: str, frame: Any) -> dict[str, Any]:
        raise OSError(5, "Input/output error", str(tmp_path / "c" / name))

    monkeypatch.setattr(CaptureWriter, "write_frame", broken)
    with pytest.raises(RecordingPartialError, match="Input/output error") as caught:
        record(session, tmp_path / "c")
    assert str(tmp_path) not in caught.value.message
    text = (tmp_path / "c" / "capture.json").read_text("utf-8")
    assert str(tmp_path) not in text
    assert json.loads(text)["groups"][0]["failure"]["message"] == "Input/output error"
    assert rig.device.lit == {}


def test_disk_failure_stops_the_scan_and_switches_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, rig = opened()

    def full(self: CaptureWriter, name: str, frame: Any) -> dict[str, Any]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(CaptureWriter, "write_frame", full)
    with pytest.raises(StorageLowError):
        record(session, tmp_path / "c")
    assert rig.device.lit == {}


# --- frames without a scan -------------------------------------------------------------


def test_single_mode_saves_one_frame_and_lights_nothing(tmp_path: Path) -> None:
    session, rig = opened()
    sent = len(rig.device.log)
    capture = sweep.record_frames(session, tmp_path / "c", "single")
    assert (capture.mode, capture.status) == ("single", "complete")
    assert capture.groups == []
    assert capture.frame().shape == (36, 64, 3)
    assert sorted(p.name for p in (tmp_path / "c").iterdir()) == [
        "capture.json", "events.jsonl", "frame.png",
    ]  # fmt: skip
    assert len(rig.device.log) == sent
    with pytest.raises(Exception, match="single capture"):
        load_capture(tmp_path / "c").cube()


def test_dark_mode_switches_everything_off_before_taking_its_frame(tmp_path: Path) -> None:
    session, rig = opened()
    session.light.on(9, 36)
    capture = sweep.record_frames(session, tmp_path / "c", "dark")
    assert capture.document["recording"]["role"] == "dark"
    assert np.all(capture.frame() == 5)
    assert rig.device.lit == {}


def test_video_mode_saves_every_frame_with_an_index(tmp_path: Path) -> None:
    session, _ = opened()
    capture = sweep.record_frames(session, tmp_path / "c", "video", count=5)
    names = sorted(p.name for p in (tmp_path / "c" / "frames").iterdir())
    assert names == [f"{n:06d}.png" for n in range(1, 6)]
    index = [
        json.loads(line) for line in (tmp_path / "c" / "frames.jsonl").read_text().splitlines()
    ]
    counters = [entry["host_frame_counter"] for entry in index]
    assert counters == list(range(counters[0], counters[0] + 5)), "no frame skipped or repeated"
    assert capture.document["recording"]["frames"] == 5
    assert capture.document["frames_index"] == "frames.jsonl"


def test_video_index_is_on_disk_frame_by_frame(tmp_path: Path) -> None:
    session, rig = opened()
    original = rig.frames.wait_new
    indexed: list[int] = []

    def look(count: int = 1, timeout: float | None = None) -> Any:
        index = tmp_path / "c" / "frames.jsonl"
        indexed.append(len(index.read_text().splitlines()) if index.exists() else 0)
        return original(count, timeout)

    rig.frames.wait_new = look  # type: ignore[method-assign]
    sweep.record_frames(session, tmp_path / "c", "video", count=4)
    assert indexed == [0, 1, 2, 3], "a crash loses no frame that was already written"


def test_video_mode_runs_until_stopped_when_no_count_is_given(tmp_path: Path) -> None:
    session, rig = opened()
    stop = threading.Event()
    original = rig.frames.wait_new

    def stop_after_three(count: int = 1, timeout: float | None = None) -> Any:
        frame = original(count, timeout)
        if len(list((tmp_path / "c" / "frames").glob("*.png"))) >= 2:
            stop.set()
        return frame

    rig.frames.wait_new = stop_after_three  # type: ignore[method-assign]
    capture = sweep.record_frames(session, tmp_path / "c", "video", count=0, stop=stop)
    assert capture.document["recording"]["frames"] == 3


# --- automatic PWM ---------------------------------------------------------------------


def led_9_peak(pwm: int) -> float:
    return 5 + 175 * pwm / 36


@pytest.mark.parametrize(("start", "found"), [(1023, 31), (5, 40), (36, 36)])
def test_auto_pwm_brings_the_white_board_into_range(start: int, found: int, tmp_path: Path) -> None:
    session, rig = opened()
    sent = len(rig.device.log)
    result = sweep.auto_pwm(session, ScanSettings(channels=(9,), pwm=start, role="white"))
    assert result == {9: found}
    assert 150 <= led_9_peak(found) <= 235
    assert session.unit.pwm_by_led == {9: found}
    ons = [c for _, c in rig.device.log[sent:] if c.function is Function.ON]
    assert len(ons) <= 10
    assert rig.device.lit == {}


def glinting() -> np.ndarray:
    """A dark target with a mirror-like spot: what a circuit board looks like to the search."""
    scene = np.full((36, 64), 0.03)
    scene[10:12, 20:22] = 40.0
    return scene


def test_auto_pwm_can_leave_the_profile_alone() -> None:
    session, _ = opened()
    result = sweep.auto_pwm(session, ScanSettings(channels=(9,), pwm=5, role="white"), store=False)
    assert result == {9: 40}
    assert session.unit.pwm_by_led == {}, "nothing is kept until the caller says so"


def test_tuning_that_fails_leaves_the_profile_as_it_was(tmp_path: Path) -> None:
    # Seen on the device with a circuit board in view instead of the white board: the
    # glints clip at any useful power, the search ends at a PWM that gives almost no
    # light, and the white scan is refused. Those values must not stay in the profile.
    session, rig = opened(reflectance=glinting())
    session.unit.pwm_by_led.update({9: 36})
    with pytest.raises(TimingUnstableError, match="no light on the white reference"):
        sweep.tune(session, tmp_path / "t", ScanSettings(channels=(9,)))
    assert session.unit.pwm_by_led == {9: 36}
    assert session.unit.min_on_ms_by_led == {}
    assert rig.device.lit == {}


def test_tuning_that_succeeds_stores_what_it_found(tmp_path: Path) -> None:
    session, _ = opened()
    sweep.tune(session, tmp_path / "t", ScanSettings(channels=(9,), pwm=5))
    assert session.unit.pwm_by_led == {9: 40}
    assert set(session.unit.min_on_ms_by_led) == {9}


def test_auto_pwm_settles_for_the_brightest_safe_value_when_nothing_fits(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session, _ = opened(reflectance=0.02)  # even full power stays dim
    result = sweep.auto_pwm(session, ScanSettings(channels=(9,), role="white"))
    assert result == {9: 1023}
    assert led_9_peak(1023) * 0.02 < 150


def test_auto_pwm_says_so_when_a_channel_never_gets_into_range(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, _ = opened(reflectance=0.02)
    notes: list[str] = []
    sweep.auto_pwm(session, ScanSettings(channels=(9,), role="white"), notes=notes)
    assert len(notes) == 1
    assert "LED 9" in notes[0]
    assert "1023" in notes[0]
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    quiet: list[str] = []
    session, _ = opened()
    sweep.auto_pwm(session, ScanSettings(channels=(9,), role="white"), notes=quiet)
    assert quiet == []


def test_auto_pwm_says_when_every_value_it_tried_clipped() -> None:
    session, _ = opened(reflectance=400.0)  # clips even at PWM 1
    notes: list[str] = []
    result = sweep.auto_pwm(session, ScanSettings(channels=(9,), role="white"), notes=notes)
    assert result == {9: 1}
    assert "every value tried clipped" in notes[0]
    assert "did not clip" not in notes[0]


def test_auto_pwm_locks_the_camera_before_it_lights_anything() -> None:
    session, rig = opened()
    locked_at_first_on: list[bool] = []

    def look() -> None:
        if rig.device.lit and not locked_at_first_on:
            locked_at_first_on.append(session.lock_report is not None)

    rig.device.on_write = look
    sweep.auto_pwm(session, ScanSettings(channels=(9,), role="white"))
    assert locked_at_first_on == [True]
    assert session.lock_report is not None
    assert session.lock_report.awb_off


def test_auto_pwm_is_refused_when_white_balance_stays_automatic() -> None:
    session, rig = opened()
    rig.controls.quantise["auto-white-balance-temp"] = lambda value: 1
    sent = len(rig.device.log)
    with pytest.raises(AWBNotConfirmedError):
        sweep.auto_pwm(session, ScanSettings(channels=(9,), role="white"))
    assert [c for _, c in rig.device.log[sent:] if c.function is Function.ON] == []


def test_auto_pwm_can_be_stopped() -> None:
    session, rig = opened()
    stop = threading.Event()
    rig.device.on_write = lambda: stop.set() if rig.device.lit else None
    with pytest.raises(OperationCancelledError):
        sweep.auto_pwm(session, ScanSettings(role="white"), stop)
    assert rig.device.lit == {}


def test_auto_pwm_judges_only_the_white_region(tmp_path: Path) -> None:
    reflectance = np.ones((36, 64))
    reflectance[:, 32:] = 3.0  # something much brighter than the board, beside it
    session, _ = opened(reflectance=reflectance)
    everywhere = sweep.auto_pwm(session, ScanSettings(channels=(9,), role="white"))
    left_half = ScanSettings(channels=(9,), role="white", white_roi=(0.0, 0.0, 0.5, 1.0), pwm=36)
    inside = sweep.auto_pwm(session, left_half)
    assert everywhere[9] < 36, "the bright half clips, so the PWM comes down"
    assert inside == {9: 36}


def test_auto_pwm_refuses_uv_channels_without_permission() -> None:
    session, rig = opened()
    sent = len(rig.device.log)
    with pytest.raises(UVNotAllowedError):
        sweep.auto_pwm(session, ScanSettings(channels=(1,), role="white"))
    assert len(rig.device.log) == sent


def test_tune_learns_pwm_and_timing_for_the_unit(tmp_path: Path) -> None:
    session, _ = opened()
    capture = sweep.tune(session, tmp_path / "c", ScanSettings(channels=(5, 9), pwm=200))
    assert capture.status == "complete"
    assert capture.document["recording"]["role"] == "white"
    unit = session.unit
    assert set(unit.pwm_by_led) == {5, 9}
    for led_id, pwm in unit.pwm_by_led.items():
        led = HC1500.led(led_id)
        assert 150 <= round(5 + max(led.white_rgb) * pwm / led.starting_pwm) <= 235
    assert set(unit.min_on_ms_by_led) == {5, 9}
    assert all(value % 50 == 0 and value >= 300 for value in unit.min_on_ms_by_led.values())
    assert capture.document["config"]["pwm_by_led"] == {
        str(n): pwm for n, pwm in unit.pwm_by_led.items()
    }


def test_tuning_twice_gives_the_same_timing(tmp_path: Path) -> None:
    session, _ = opened()
    settings = ScanSettings(channels=(5, 9, 13))
    sweep.tune(session, tmp_path / "a", settings)
    first = dict(session.unit.min_on_ms_by_led)
    sweep.tune(session, tmp_path / "b", settings)
    assert session.unit.min_on_ms_by_led == first, "the on-times do not creep upwards"
    document = record(session, tmp_path / "c", settings).document
    assert document["config"]["min_on_ms"]["by_led"] == {str(k): v for k, v in first.items()}


def test_tuning_starts_afresh_instead_of_from_what_it_learned_before(tmp_path: Path) -> None:
    session, _ = opened()
    session.unit.min_on_ms_by_led = {9: 800, 10: 800}  # an earlier, unlucky run
    sweep.tune(session, tmp_path / "c", ScanSettings(channels=(9,)))
    assert session.unit.min_on_ms_by_led[9] <= 350, "back to what this unit needs"
    assert session.unit.min_on_ms_by_led[10] == 800, "a channel that was not tuned keeps its value"


def test_tune_does_not_replace_a_folder_unless_asked(tmp_path: Path) -> None:
    session, rig = opened()
    sweep.tune(session, tmp_path / "c", ScanSettings(channels=(9,)))
    sent = len(rig.device.log)
    with pytest.raises(InvalidStateError):
        sweep.tune(session, tmp_path / "c", ScanSettings(channels=(9,)))
    assert len(rig.device.log) == sent, "refused before the search lit anything"
    capture = sweep.tune(session, tmp_path / "c", ScanSettings(channels=(10,)), overwrite=True)
    assert [entry["led_id"] for entry in capture.groups[0]["frames"]] == [10]
