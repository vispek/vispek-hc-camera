# SPDX-License-Identifier: Apache-2.0
"""The commands that record and work with captures, on the simulated device."""

from __future__ import annotations

import csv
import json
import signal
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from vispek_hc import cli, sweep
from vispek_hc.cube import load_capture
from vispek_hc.errors import DeviceNotFoundError
from vispek_hc.protocol import Function
from vispek_hc.sim import SimulatedRig

pytestmark = pytest.mark.usefixtures("quiet_host")


def read_npz(path: Path) -> dict[str, Any]:
    """The arrays of an .npz file, read without pickle and with the file closed again."""
    with np.load(path, allow_pickle=False) as stored:
        return {name: stored[name] for name in stored.files}


def run(capsys: pytest.CaptureFixture[str], *argv: Any) -> tuple[int, dict[str, Any]]:
    exit_code = cli.main([*map(str, argv), "--json"])
    return exit_code, json.loads(capsys.readouterr().out)


def ok(capsys: pytest.CaptureFixture[str], *argv: Any) -> dict[str, Any]:
    exit_code, envelope = run(capsys, *argv)
    assert exit_code == 0, envelope
    return envelope


@pytest.fixture
def rigs(monkeypatch: pytest.MonkeyPatch) -> list[SimulatedRig]:
    made: list[SimulatedRig] = []

    class Recording(SimulatedRig):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            made.append(self)

    monkeypatch.setattr(cli.session, "SimulatedRig", Recording)
    return made


@pytest.fixture
def recorded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Path]:
    """A sample scan, a white reference and the calibration made from it."""
    paths = {name: tmp_path / name for name in ("sample", "white", "calib")}
    ok(capsys, "record", "-o", paths["sample"], "--simulate")
    ok(capsys, "reference", "white", "-o", paths["white"], "--simulate")
    ok(capsys, "calibrate", "--white", paths["white"], "-o", paths["calib"])
    return paths


# --- record ----------------------------------------------------------------------------


def test_record_scans_and_reports_the_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--simulate")
    data = envelope["data"]
    assert data["capture"] == str(tmp_path / "c")
    assert (data["mode"], data["role"], data["status"]) == ("scan", None, "complete")
    assert (data["groups_done"], data["outputs"]) == (1, 1)
    assert data["channels"] == list(range(5, 18))
    assert data["preview"] == "preview.png"
    assert envelope["warnings"] == []
    assert load_capture(tmp_path / "c").capture_id == data["capture_id"]
    with Image.open(tmp_path / "c" / "preview.png") as preview:
        assert preview.size == (64, 36)
    assert rigs[0].device.lit == {}
    assert rigs[0].device.closed


def test_record_runs_the_pairing_probe_when_the_unit_has_no_timing_yet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    ok(capsys, "record", "-o", tmp_path / "c", "--simulate")
    probe = [e for e in rigs[0].device.lit_intervals() if e[0] == 6][:1]  # before the scan
    assert len(probe) == 1
    config = load_capture(tmp_path / "c").document["config"]
    assert config["pipeline_latency_ms"] is not None
    assert set(config["min_on_ms"]["by_led"].values()) != {300}


@pytest.mark.parametrize(
    ("text", "channels"), [("5,9", [5, 9]), ("5-7", [5, 6, 7]), ("5-6,9,12-13", [5, 6, 9, 12, 13])]
)
def test_record_channel_lists(
    text: str, channels: list[int], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--channels", text, "--simulate")
    assert envelope["data"]["channels"] == channels
    assert envelope["data"]["preview"] == ("preview.png" if {5, 7, 9} <= set(channels) else None)


def test_record_options_reach_the_scan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    options = ["--channels", "5,9", "--pwm", "20", "--average", "2", "--cycles", "2"]
    options += ["--min-on", "500", "--reduction", "luma", "--no-preview"]
    ok(capsys, "record", "-o", tmp_path / "c", *options, "--simulate")
    capture = load_capture(tmp_path / "c")
    config = capture.document["config"]
    assert config["pwm_by_led"] == {"5": 20, "9": 20}
    assert config["min_on_ms"] == {"source": "manual", "by_led": {"5": 500, "9": 500}}
    assert (config["average"], config["cycles"], config["reduction"]) == (2, 2, "luma")
    assert capture.outputs == 2
    assert len(capture.groups) == 4
    assert not (tmp_path / "c" / "preview.png").exists()


def test_setup_label_and_enclosure_are_kept_with_the_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "record", "-o", tmp_path / "c", "--channels", "9", "--setup-label",
       "2.5 mm lens, 30 cm", "--enclosure", "darkbox", "--simulate")  # fmt: skip
    config = load_capture(tmp_path / "c").document["config"]
    assert config["acquisition"] == {
        "enclosure": "darkbox", "setup_label": "2.5 mm lens, 30 cm", "white_patch_roi": None,
    }  # fmt: skip
    assert config["pipeline_latency_ms"] == 185.0, "one decimal is plenty"


def test_scan_with_a_short_exposure_warns_about_ripple(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class ShortExposure(SimulatedRig):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            self.controls.quantise["exposure-time-abs"] = lambda value: 50

    monkeypatch.setattr(cli.session, "SimulatedRig", ShortExposure)
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--channels", "9", "--simulate")
    assert any("exposure 5 ms" in w and "ripple" in w for w in envelope["warnings"])
    monkeypatch.undo()
    envelope = ok(capsys, "record", "-o", tmp_path / "d", "--channels", "9", "--simulate")
    assert envelope["warnings"] == []


def test_setup_label_is_kept_with_plain_frames_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "record", "-o", tmp_path / "c", "--mode", "dark", "--setup-label", "lens covered",
       "--enclosure", "darkbox", "--simulate")  # fmt: skip
    acquisition = load_capture(tmp_path / "c").document["config"]["acquisition"]
    assert (acquisition["setup_label"], acquisition["enclosure"]) == ("lens covered", "darkbox")


def test_record_takes_pwm_from_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pwm_file = tmp_path / "pwm.json"
    pwm_file.write_text('{"5": 30, "9": 40}')
    ok(capsys, "record", "-o", tmp_path / "c", "--channels", "5,9", "--pwm-file", pwm_file,
       "--simulate")  # fmt: skip
    assert load_capture(tmp_path / "c").document["config"]["pwm_by_led"] == {"5": 30, "9": 40}


def test_pwm_file_naming_an_led_twice_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    for content in ('{"5": 30, "5": 40}', '{"5": 30, "05": 40}'):
        pwm_file = tmp_path / "pwm.json"
        pwm_file.write_text(content)
        exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--pwm-file", pwm_file,
                                  "--simulate")  # fmt: skip
        assert (exit_code, envelope["error"]["code"]) == (5, "CONFIG_REJECTED")
        assert "twice" in envelope["error"]["message"]
    exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--pwm-file",
                              tmp_path / "missing.json", "--simulate")  # fmt: skip
    assert (exit_code, envelope["error"]["code"]) == (5, "CONFIG_REJECTED")
    assert str(tmp_path) not in envelope["error"]["message"]
    assert rigs == []


@pytest.mark.parametrize(
    "content", ['{"18": 10}', '{"5": 1024}', '{"5": 2.5}', '{"five": 3}', "[1, 2]", "x" * 5000]
)
def test_unusable_pwm_file_is_refused_before_any_device_is_opened(
    content: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    pwm_file = tmp_path / "pwm.json"
    pwm_file.write_text(content)
    exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--pwm-file", pwm_file,
                              "--simulate")  # fmt: skip
    assert exit_code == 5
    assert envelope["error"]["code"] == "CONFIG_REJECTED"
    assert rigs == []


def test_record_refuses_uv_channels_before_any_device_is_opened(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--channels", "1-17",
                              "--simulate")  # fmt: skip
    assert exit_code == 5
    assert envelope["error"]["code"] == "UV_NOT_ALLOWED"
    assert rigs == []
    assert not (tmp_path / "c").exists()


def test_record_with_uv_permission(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--channels", "1,5", "--allow-uv",
                  "--simulate")  # fmt: skip
    assert envelope["data"]["channels"] == [1, 5]


def test_record_warns_about_clipping(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--channels", "9", "--pwm", "1023",
                  "--simulate")  # fmt: skip
    assert envelope["warnings"] == ["clipped pixels under LED 9"]


def test_record_does_not_overwrite_unless_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "record", "-o", tmp_path / "c", "--channels", "5", "--simulate")
    exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--channels", "9",
                              "--simulate")  # fmt: skip
    assert exit_code == 5
    assert envelope["error"]["code"] == "INVALID_STATE"
    assert "--overwrite" in envelope["error"]["hint"]
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--channels", "9", "--overwrite",
                  "--simulate")  # fmt: skip
    assert envelope["data"]["channels"] == [9]


def test_record_dry_run_prints_the_plan_and_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--channels", "5,9", "--dry-run",
                  "--simulate")  # fmt: skip
    assert envelope["data"] == {
        "dry_run": True,
        "mode": "scan",
        "role": None,
        "channels": [5, 9],
        "pwm_by_led": {"5": 24, "9": 36},
        "min_on_ms": {"5": 500, "9": 500},
        "estimated_seconds": pytest.approx(2.4, abs=0.5),
    }
    assert rigs == []
    assert not (tmp_path / "c").exists()


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        (["reference", "white"], {"role": "white", "auto_pwm": False}),
        (["reference", "white", "--auto-pwm"], {"role": "white", "auto_pwm": True}),
        (["tune"], {"role": "white", "auto_pwm": True, "saves_to_profile": False}),
    ],
)
def test_reference_and_tune_have_a_dry_run_too(
    command: list[str], extra: dict[str, Any], tmp_path: Path,
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig],
) -> None:  # fmt: skip
    envelope = ok(capsys, *command, "-o", tmp_path / "c", "--channels", "5,9", "--dry-run",
                  "--simulate")  # fmt: skip
    data = envelope["data"]
    assert data["dry_run"] is True
    assert data["channels"] == [5, 9]
    assert {key: data[key] for key in extra} == extra
    assert rigs == [], "no device was opened"
    assert not (tmp_path / "c").exists()


def test_dry_run_refuses_what_the_real_run_would_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "notes.txt").write_text("mine")
    for command in (["record"], ["reference", "white"], ["tune"]):
        exit_code, envelope = run(capsys, *command, "-o", tmp_path, "--dry-run", "--simulate")
        assert (exit_code, envelope["error"]["code"]) == (5, "INVALID_STATE")
        exit_code, envelope = run(capsys, *command, "-o", tmp_path / "c", "--channels", "1",
                                  "--dry-run", "--simulate")  # fmt: skip
        assert (exit_code, envelope["error"]["code"]) == (5, "UV_NOT_ALLOWED")


@pytest.mark.parametrize("command", [["record"], ["record", "--mode", "single"],
                                     ["reference", "white"], ["tune"]])  # fmt: skip
def test_a_folder_holding_something_else_is_refused_before_any_device_is_opened(
    command: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str],
    rigs: list[SimulatedRig],
) -> None:  # fmt: skip
    (tmp_path / "notes.txt").write_text("mine")
    exit_code, envelope = run(capsys, *command, "-o", tmp_path, "--overwrite", "--simulate")
    assert (exit_code, envelope["error"]["code"]) == (5, "INVALID_STATE")
    assert "not a capture folder" in envelope["error"]["message"]
    assert rigs == []
    assert (tmp_path / "notes.txt").read_text() == "mine"


@pytest.mark.parametrize("command", [["reference", "white"], ["tune"]])
def test_reference_and_tune_take_the_acquisition_options_of_record(
    command: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    options = ["--channels", "9", "--min-on", "500", "--reduction", "luma", "--pwm", "30"]
    ok(capsys, *command, "-o", tmp_path / "c", *options, "--simulate")
    config = load_capture(tmp_path / "c").document["config"]
    assert config["min_on_ms"] == {"source": "manual", "by_led": {"9": 500}}
    assert config["reduction"] == "luma"
    if command == ["reference", "white"]:
        assert config["pwm_by_led"] == {"9": 30}


def test_pwm_and_pwm_file_together_are_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    pwm_file = tmp_path / "pwm.json"
    pwm_file.write_text('{"5": 30}')
    exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--pwm", "20",
                              "--pwm-file", pwm_file, "--simulate")  # fmt: skip
    assert (exit_code, envelope["error"]["code"]) == (2, "USAGE_ERROR")
    assert rigs == []


@pytest.mark.parametrize(("text", "size"), [("1280x720", (1280, 720)), ("640,480", (640, 480)),
                                            ("1920X1080", (1920, 1080))])  # fmt: skip
def test_picture_size_is_written_width_x_height(text: str, size: tuple[int, int]) -> None:
    assert cli._size(text) == size


def test_stored_pwm_of_the_unit_is_checked_before_any_device_is_opened(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    profile = cli.units.UnitProfile("bench", pwm_by_led={9: 0})
    cli.units.save_profile(profile)

    def never(**options: Any) -> Any:
        raise AssertionError("the device was opened")

    monkeypatch.setattr(cli.session, "open_session", never)
    exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--unit", "bench")
    assert (exit_code, envelope["error"]["code"]) == (5, "CONFIG_REJECTED")
    assert envelope["error"]["details"] == {"led_id": 9, "pwm": 0}


# --- stopping and partial results ------------------------------------------------------


def interrupt_in_group(monkeypatch: pytest.MonkeyPatch, group_id: str, times: int = 1) -> None:
    """Press Ctrl-C (really: raise SIGINT) when the scan reports its first band of a group."""
    real = sweep.record
    left = [times]

    def record(*args: Any, **options: Any) -> Any:
        def progress(update: dict[str, Any]) -> None:
            if update["group_id"] == group_id and left[0] > 0:
                left[0] -= 1
                signal.raise_signal(signal.SIGINT)

        return real(*args, **{**options, "progress": progress})

    monkeypatch.setattr(sweep, "record", record)


def test_continuous_recording_ends_cleanly_on_ctrl_c(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    rigs: list[SimulatedRig],
) -> None:  # fmt: skip
    interrupt_in_group(monkeypatch, "g0003")
    before = signal.getsignal(signal.SIGINT)
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--channels", "5,9", "--cycles", "0",
                  "--no-preview", "--simulate")  # fmt: skip
    assert envelope["data"]["outputs"] == 2
    assert envelope["data"]["status"] == "complete"
    assert rigs[0].device.lit == {}
    assert signal.getsignal(signal.SIGINT) is before, "the handler is put back"


def test_ctrl_c_during_a_scan_is_a_cancellation_with_the_led_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    rigs: list[SimulatedRig],
) -> None:  # fmt: skip
    interrupt_in_group(monkeypatch, "g0001")
    exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--simulate")
    assert (exit_code, envelope["error"]["code"]) == (130, "CANCELLED")
    assert rigs[0].device.lit == {}
    capture = load_capture(tmp_path / "c")
    assert capture.status == "partial"
    assert 0 < len(capture.groups[0]["frames"]) < 13


def interrupting_rig(monkeypatch: pytest.MonkeyPatch, at_on: int) -> list[SimulatedRig]:
    """Simulated rigs that press Ctrl-C once, when the given ON command has been sent."""
    made: list[SimulatedRig] = []

    class Interrupting(SimulatedRig):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            made.append(self)
            self.device.on_write = self.press

        def press(self) -> None:
            ons = [c for _, c in self.device.log if c.function is Function.ON]
            if len(ons) == at_on:
                self.device.on_write = None
                signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(cli.session, "SimulatedRig", Interrupting)
    return made


@pytest.mark.parametrize("command", [["reference", "white", "--auto-pwm"], ["tune"]])
def test_ctrl_c_during_the_pwm_search_stops_before_anything_is_recorded(
    command: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    rigs = interrupting_rig(monkeypatch, at_on=2)  # the pairing probe, then the search
    exit_code, envelope = run(capsys, *command, "-o", tmp_path / "c", "--simulate")
    assert (exit_code, envelope["error"]["code"]) == (130, "CANCELLED")
    assert not (tmp_path / "c").exists(), "the search was stopped, not the scan after it"
    assert rigs[0].device.lit == {}


def test_ctrl_c_ends_an_open_ended_video_with_its_frames_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Interrupting(SimulatedRig):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            original, calls = self.frames.wait_new, iter(range(10**6))

            def wait_new(count: int = 1, timeout: float | None = None) -> Any:
                if next(calls) == 5:
                    signal.raise_signal(signal.SIGINT)
                return original(count, timeout)

            self.frames.wait_new = wait_new  # type: ignore[method-assign]

    monkeypatch.setattr(cli.session, "SimulatedRig", Interrupting)
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--mode", "video", "--cycles", "0",
                  "--simulate")  # fmt: skip
    assert envelope["data"]["status"] == "complete"
    frames = load_capture(tmp_path / "c", verify=True).document["recording"]["frames"]
    assert 3 <= frames <= 6


def test_second_ctrl_c_goes_to_the_handler_that_was_there_before() -> None:
    seen: list[int] = []
    before = signal.signal(signal.SIGINT, lambda signum, frame: seen.append(signum))
    try:
        with cli._stop_on_interrupt() as stop:
            signal.raise_signal(signal.SIGINT)
            assert stop.is_set()
            assert seen == [], "the first press only asks"
            signal.raise_signal(signal.SIGINT)
            assert seen == [signal.SIGINT], "the second reaches the emergency handler"
        signal.raise_signal(signal.SIGINT)
        assert len(seen) == 2, "and afterwards the old handler is back"
    finally:
        signal.signal(signal.SIGINT, before)


def test_second_ctrl_c_interrupts_when_no_handler_was_installed() -> None:
    before = signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        with cli._stop_on_interrupt(), pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, before)


def test_stop_event_is_still_handed_out_away_from_the_main_thread() -> None:
    result: list[Any] = []

    def worker() -> None:
        try:
            with cli._stop_on_interrupt() as stop:
                result.append(stop.is_set())
        except Exception as error:
            result.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert result == [False], "signal handlers can only be set from the main thread"


def test_unlocked_flag_lets_a_scan_run_when_white_balance_cannot_be_fixed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Automatic(SimulatedRig):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            self.controls.quantise["auto-white-balance-temp"] = lambda value: 1

    monkeypatch.setattr(cli.session, "SimulatedRig", Automatic)
    argv = ["record", "-o", tmp_path / "c", "--channels", "9", "--simulate"]
    exit_code, envelope = run(capsys, *argv)
    assert (exit_code, envelope["error"]["code"]) == (5, "AWB_NOT_CONFIRMED")
    ok(capsys, *argv, "--unlocked", "--overwrite")
    assert load_capture(tmp_path / "c").document["config"]["controls_locked"] is False


@pytest.mark.parametrize("command", [["record"], ["reference", "white"], ["tune"]])
def test_camera_options_reach_the_device(
    command: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    asked: list[dict[str, Any]] = []

    def no_device(**options: Any) -> Any:
        asked.append(options)
        raise DeviceNotFoundError("no light source")

    monkeypatch.setattr(cli.session, "open_session", no_device)
    out = ["-o", tmp_path / "c", "--simulate"]
    for options in (["--size", "640x480", "--fps", "30", "--source", "mjpeg"], ["--fast"], []):
        exit_code, _ = run(capsys, *command, *out, *options)
        assert exit_code == 4
    picked = [(a["size"], a["fps"], a["source"]) for a in asked]
    assert picked == [((640, 480), 30.0, "mjpeg"), ((1280, 720), 60, "mjpeg"),
                      ((1280, 720), 10, "uncompressed")]  # fmt: skip


def test_failure_after_a_finished_output_exits_with_the_quality_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[SimulatedRig] = []

    class Stalling(SimulatedRig):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            made.append(self)
            self.device.on_write = self.stall_in_the_second_scan

        def stall_in_the_second_scan(self) -> None:
            ons = [c for _, c in self.device.log if c.function is Function.ON]
            if len(ons) == 1 + 2 + 1:  # the pairing probe, scan one, first ON of scan two
                self.device.on_write = None
                self.frames.stall(60.0)

    monkeypatch.setattr(cli.session, "SimulatedRig", Stalling)
    exit_code, envelope = run(capsys, "record", "-o", tmp_path / "c", "--channels", "5,9",
                              "--cycles", "3", "--simulate")  # fmt: skip
    assert (exit_code, envelope["error"]["code"]) == (7, "RECORDING_PARTIAL")
    details = envelope["error"]["details"]
    assert (details["cause"], details["outputs"], details["capture"]) == (
        "FRAME_INCOMPLETE",
        1,
        "c",
    )
    assert load_capture(tmp_path / "c").outputs == 1
    assert made[0].device.lit == {}


def test_auto_pwm_that_does_not_converge_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Dim(SimulatedRig):
        def __init__(self, **options: Any) -> None:
            super().__init__(**{**options, "reflectance": 0.02})

    monkeypatch.setattr(cli.session, "SimulatedRig", Dim)
    for command in (["reference", "white", "--auto-pwm"], ["tune"]):
        envelope = ok(capsys, *command, "-o", tmp_path / command[0], "--channels", "9",
                      "--simulate")  # fmt: skip
        assert any("LED 9: no PWM puts the white board" in w for w in envelope["warnings"])


def test_a_pwm_search_is_kept_only_when_its_white_reference_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Any] = []
    original = cli.session.open_session

    def remembering(**options: Any) -> Any:
        made = original(**options)
        opened.append(made)
        return made

    monkeypatch.setattr(cli.session, "open_session", remembering)
    ok(capsys, "reference", "white", "--auto-pwm", "-o", tmp_path / "good", "--channels", "9",
       "--pwm", "5", "--simulate")  # fmt: skip
    assert opened[-1].unit.pwm_by_led == {9: 40}

    class Glinting(SimulatedRig):  # a circuit board where the white board should be
        def __init__(self, **options: Any) -> None:
            scene = np.full((36, 64), 0.03)
            scene[10:12, 20:22] = 40.0
            super().__init__(**{**options, "reflectance": scene})

    monkeypatch.setattr(cli.session, "SimulatedRig", Glinting)
    exit_code, envelope = run(capsys, "reference", "white", "--auto-pwm", "-o",
                              tmp_path / "bad", "--channels", "9", "--simulate")  # fmt: skip
    assert exit_code != 0
    assert envelope["error"]["code"] == "TIMING_UNSTABLE"
    assert opened[-1].unit.pwm_by_led == {}, "the refused reference left nothing behind"


@pytest.mark.parametrize(("mode", "names"), [
    ("single", ["capture.json", "events.jsonl", "frame.png"]),
    ("dark", ["capture.json", "events.jsonl", "frame.png"]),
    ("video", ["capture.json", "events.jsonl", "frames", "frames.jsonl"]),
])  # fmt: skip
def test_record_frame_modes(
    mode: str, names: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "record", "-o", tmp_path / "c", "--mode", mode, "--cycles", "3",
                  "--simulate")  # fmt: skip
    assert (envelope["data"]["mode"], envelope["data"]["outputs"]) == (mode, None)
    assert sorted(p.name for p in (tmp_path / "c").iterdir()) == names
    if mode == "video":
        assert len(list((tmp_path / "c" / "frames").iterdir())) == 3


@pytest.mark.parametrize(
    "argv",
    [
        ["record", "--simulate"],
        ["record", "-o", "x", "--channels", "a-b"],
        ["record", "-o", "x", "--channels", ""],
        ["record", "-o", "x", "--min-on", "soon"],
        ["record", "-o", "x", "--mode", "burst"],
        ["record", "-o", "x", "--size", "1280"],
        ["record", "-o", "x", "--size", "bigxsmall"],
        ["reference", "white", "-o", "x", "--cycles", "2"],
        ["spectrum", "x", "--xy", "1"],
        ["spectrum", "x", "--xy", "1,b"],
        ["reference", "-o", "x"],
        ["export", "x", "-o", "y"],
    ],
)
def test_usage_errors(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, envelope = run(capsys, *argv)
    assert exit_code == 2
    assert envelope["error"]["code"] == "USAGE_ERROR"


def test_record_out_of_range_setting_is_a_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(
        capsys, "record", "-o", tmp_path / "c", "--average", "0", "--simulate"
    )
    assert exit_code == 5
    assert envelope["error"]["details"]["setting"] == "average"
    assert rigs == []


# --- white reference, calibration, reflectance -----------------------------------------


def test_reference_white_with_auto_pwm_and_region(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "reference", "white", "-o", tmp_path / "w", "--channels", "5,9",
                  "--auto-pwm", "--white-roi", "0.25,0.25,0.5,0.5", "--simulate")  # fmt: skip
    data = envelope["data"]
    assert (data["role"], data["status"]) == ("white", "complete")
    assert set(data["auto_pwm"]) == {"5", "9"}
    config = load_capture(tmp_path / "w").document["config"]
    assert config["white_roi"] == [0.25, 0.25, 0.5, 0.5]
    assert config["pwm_by_led"] == data["auto_pwm"]


def test_calibrate_reports_quality_per_band(
    recorded: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "calibrate", "--white", recorded["white"], "-o", recorded["calib"])
    data = envelope["data"]
    assert data["bands"] == [f"led_{n:02d}" for n in range(5, 18)]
    assert data["dominant_channel_by_led"]["5"] == "B"
    assert all(band["pass"] for band in data["quality"])
    assert data["white_roi_px"] is None
    assert sorted(p.name for p in recorded["calib"].iterdir()) == [
        "calibration.json",
        "reference.npz",
    ]


def test_calibrate_refuses_a_clipped_white_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "reference", "white", "-o", tmp_path / "w", "--channels", "9", "--simulate")
    document = json.loads((tmp_path / "w" / "capture.json").read_text())
    name = document["groups"][0]["frames"][0]["file"]
    Image.new("RGB", (64, 36), (255, 20, 20)).save(tmp_path / "w" / "g0001" / name)
    exit_code, envelope = run(capsys, "calibrate", "--white", tmp_path / "w", "-o", tmp_path / "k")
    assert exit_code == 7
    assert envelope["error"]["code"] == "CALIBRATION_QUALITY"
    assert not (tmp_path / "k").exists()


def test_reflectance_writes_a_cube_and_reports_medians(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "reflectance", recorded["sample"], "--calib", recorded["calib"],
                  "-o", tmp_path / "cube.npz")  # fmt: skip
    data = envelope["data"]
    assert (data["layer"], data["unit"]) == ("L2", "relative_to_white")
    assert data["median_by_band"] == [1.0] * 13, "the simulated sample is the white board"
    assert data["valid_fraction"] == 1.0
    stored = read_npz(tmp_path / "cube.npz")
    assert str(stored["layer"]) == "L2"


def test_reflectance_reports_the_file_it_really_wrote(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "reflectance", recorded["sample"], "--calib", recorded["calib"],
                  "-o", tmp_path / "result")  # fmt: skip
    assert envelope["data"]["cube"] == str(tmp_path / "result.npz")
    assert (tmp_path / "result.npz").is_file()


def test_reflectance_with_the_legacy_definition(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "record", "-o", tmp_path / "dark", "--mode", "dark", "--simulate")
    argv = ["reflectance", recorded["sample"], "--calib", recorded["calib"], "-o",
            tmp_path / "legacy.npz"]  # fmt: skip
    ok(capsys, *argv, "--legacy", tmp_path / "dark")
    stored = read_npz(tmp_path / "legacy.npz")
    assert str(stored["reduction"]) == "luma"
    assert json.loads(str(stored["metadata_json"]))["calibration"]["legacy"] is True
    exit_code, envelope = run(capsys, *argv, "--legacy", recorded["sample"])
    assert (exit_code, envelope["error"]["code"]) == (6, "FRAME_NOT_FOUND"), "a scan is no frame"


def test_mistakes_with_folders_are_named_not_reported_as_bugs(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = ["-o", tmp_path / "c.npz"]
    exit_code, envelope = run(capsys, "reflectance", recorded["sample"], "--calib",
                              tmp_path / "nothing", *out)  # fmt: skip
    assert (exit_code, envelope["error"]["code"]) == (6, "FRAME_NOT_FOUND")
    assert "calibration.json" in envelope["error"]["message"]
    ok(capsys, "record", "-o", tmp_path / "single", "--mode", "single", "--simulate")
    exit_code, envelope = run(capsys, "reflectance", tmp_path / "single", "--calib",
                              recorded["calib"], *out)  # fmt: skip
    assert (exit_code, envelope["error"]["code"]) == (5, "CONFIG_REJECTED")
    exit_code, envelope = run(capsys, "calibrate", "--white", tmp_path / "single", "-o",
                              tmp_path / "k")  # fmt: skip
    assert (exit_code, envelope["error"]["code"]) == (5, "CONFIG_REJECTED")
    exit_code, envelope = run(capsys, "view", recorded["sample"], "-o",
                              tmp_path / "no" / "such" / "folder" / "v.png")  # fmt: skip
    assert (exit_code, envelope["error"]["code"]) == (5, "INVALID_STATE")
    assert envelope["error"]["message"].startswith("cannot use v.png")
    assert str(tmp_path) not in envelope["error"]["message"]


def test_calibrate_takes_a_white_region(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "calibrate", "--white", recorded["white"], "-o", tmp_path / "k",
                  "--white-roi", "0.25,0.25,0.5,0.5")  # fmt: skip
    assert envelope["data"]["white_roi_px"] == {"x0": 16, "y0": 9, "x1": 48, "y1": 27}
    ok(capsys, "reflectance", recorded["sample"], "--calib", tmp_path / "k", "-o",
       tmp_path / "c.npz")  # fmt: skip
    stored = read_npz(tmp_path / "c.npz")
    used = json.loads(str(stored["metadata_json"]))["calibration"]
    assert used["white_roi_px"] == {"x0": 16, "y0": 9, "x1": 48, "y1": 27}
    assert used["path"] == "k"


def test_reflectance_with_other_settings_is_a_mismatch_unless_forced(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "record", "-o", tmp_path / "other", "--pwm", "20", "--simulate")
    argv = [
        "reflectance",
        tmp_path / "other",
        "--calib",
        recorded["calib"],
        "-o",
        tmp_path / "c.npz",
    ]
    exit_code, envelope = run(capsys, *argv)
    assert exit_code == 5
    assert envelope["error"]["code"] == "CALIBRATION_MISMATCH"
    assert envelope["error"]["details"]["differing"] == ["pwm_by_led"]
    assert not (tmp_path / "c.npz").exists()
    envelope = ok(capsys, *argv, "--force")
    assert "pwm_by_led" in envelope["warnings"][0]


# --- views, spectra, export ------------------------------------------------------------


def test_view_renders_raw_and_calibrated_pictures(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "view", recorded["sample"], "-o", tmp_path / "raw.png")
    assert envelope["data"] == {"image": str(tmp_path / "raw.png"), "mode": "rgb", "layer": "L1",
                                "size": [64, 36]}  # fmt: skip
    options = ["--mode", "band:led_13", "--palette", "viridis", "--gamma", "2"]
    target = tmp_path / "b.png"
    envelope = ok(
        capsys, "view", recorded["sample"], "--calib", recorded["calib"], *options, "-o", target
    )
    assert envelope["data"]["layer"] == "L2"
    with Image.open(tmp_path / "b.png") as image:
        assert image.size == (64, 36)


def test_view_with_a_fixed_display_range(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["view", recorded["sample"], "--calib", recorded["calib"], "--mode", "band:led_09"]
    ok(capsys, *argv, "--low", "0", "--high", "2", "-o", tmp_path / "wide.png")
    ok(capsys, *argv, "--low", "0", "--high", "4", "-o", tmp_path / "wider.png")
    with Image.open(tmp_path / "wide.png") as wide, Image.open(tmp_path / "wider.png") as wider:
        assert np.asarray(wide)[10, 10].tolist() == [128, 128, 128], "1.0 on a 0-2 scale"
        assert np.asarray(wider)[10, 10].tolist() == [64, 64, 64]


def test_index_view_needs_a_calibration(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["view", recorded["sample"], "--mode", "ndvi", "-o", tmp_path / "n.png"]
    exit_code, envelope = run(capsys, *argv)
    assert exit_code == 5
    assert envelope["error"]["code"] == "INVALID_STATE"
    ok(capsys, *argv, "--allow-uncalibrated")
    ok(capsys, *argv, "--calib", recorded["calib"])


def test_view_of_a_band_that_was_not_recorded(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, envelope = run(capsys, "view", recorded["sample"], "--mode", "uv", "-o",
                              tmp_path / "u.png")  # fmt: skip
    assert exit_code == 5
    assert envelope["error"]["details"]["band_id"] == "led_04"


def test_spectrum_of_a_point_and_a_box(
    recorded: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "spectrum", recorded["sample"], "--xy", "10,10")
    data = envelope["data"]
    assert data["geometry"] == {"point": [10, 10]}
    assert (data["layer"], data["csv"]) == ("L1", None)
    assert [band["band_id"] for band in data["bands"]] == [f"led_{n:02d}" for n in range(5, 18)]
    assert data["bands"][0]["mean"] == 5 + 139

    envelope = ok(capsys, "spectrum", recorded["sample"], "--calib", recorded["calib"], "--xy",
                  "4,4", "--wh", "8,6", "--csv", tmp_path / "s.csv")  # fmt: skip
    assert envelope["data"]["bands"][0]["total"] == 48
    with (tmp_path / "s.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 13
    assert rows[0]["unit"] == "relative_to_white"


@pytest.mark.parametrize(("kind", "files"), [
    ("envi", ["data.bsq", "data.hdr", "metadata.json", "quality-mask.bsq", "quality-mask.hdr"]),
    ("tiff", ["data.tif", "quality-mask.tif"]),
])  # fmt: skip
def test_export(
    kind: str, files: list[str], recorded: dict[str, Path], tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    envelope = ok(capsys, "export", recorded["sample"], "--calib", recorded["calib"], "--format",
                  kind, "-o", tmp_path / "out")  # fmt: skip
    assert sorted(envelope["data"]["files"]) == files
    assert envelope["data"]["layer"] == "L2"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == files


def test_command_on_a_folder_that_is_no_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, envelope = run(capsys, "view", tmp_path, "-o", tmp_path / "x.png")
    assert exit_code == 6
    assert envelope["error"]["code"] == "FRAME_NOT_FOUND"


# --- tune, repair ----------------------------------------------------------------------


def test_tune_replaces_a_capture_only_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["tune", "-o", tmp_path / "t", "--channels", "9", "--simulate"]
    ok(capsys, *argv)
    exit_code, envelope = run(capsys, *argv)
    assert (exit_code, envelope["error"]["code"]) == (5, "INVALID_STATE")
    ok(capsys, *argv, "--overwrite")


def test_tune_reports_what_it_learned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], home: Path
) -> None:
    envelope = ok(capsys, "tune", "-o", tmp_path / "t", "--channels", "5,9", "--simulate")
    data = envelope["data"]
    assert set(data["pwm_by_led"]) == set(data["min_on_ms_by_led"]) == {"5", "9"}
    assert data["role"] == "white"
    assert data["saved_to_profile"] is False, "a simulated unit is never written to disk"
    assert not home.exists()


def test_repair_copes_with_the_line_a_crash_cut_short(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "record", "-o", tmp_path / "c", "--channels", "5,9", "--no-preview", "--simulate")
    (tmp_path / "c" / "capture.json").unlink()
    with (tmp_path / "c" / "events.jsonl").open("a", encoding="utf-8") as log:
        log.write('{"t_utc": "2026-01-01T00:00:00.000+00:00", "type": "saturation_wa')
    envelope = ok(capsys, "repair", tmp_path / "c")
    assert envelope["data"]["groups_done"] == 1
    (tmp_path / "c" / "events.jsonl").unlink()
    exit_code, envelope = run(capsys, "repair", tmp_path / "c")
    assert (exit_code, envelope["error"]["code"]) == (6, "FRAME_NOT_FOUND")


def test_repair_rebuilds_a_capture_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "record", "-o", tmp_path / "c", "--channels", "5,9", "--no-preview", "--simulate")
    (tmp_path / "c" / "capture.json").unlink()
    envelope = ok(capsys, "repair", tmp_path / "c")
    assert envelope["data"]["status"] == "partial"
    assert envelope["data"]["groups_done"] == 1
    assert load_capture(tmp_path / "c").cube().values.shape == (36, 64, 2)


# --- demo ------------------------------------------------------------------------------


def test_demo_runs_on_the_simulated_device_and_checks_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope = ok(capsys, "demo", "-o", tmp_path / "demo")
    data = envelope["data"]
    assert (data["passed"], data["result"], data["simulated"]) == (True, "PASS", True)
    assert data["files"] == ["rgb.png", "cir.png", "ndvi.png", "spectra.csv"]
    assert data["captures"] == ["captures/sample", "captures/white"]
    assert sorted(p.name for p in (tmp_path / "demo").iterdir()) == [
        "captures", "cir.png", "ndvi.png", "rgb.png", "spectra.csv",
    ]  # fmt: skip
    check = data["checks"]["relative_reflectance"]
    assert check["passed"] is True
    assert check["max_deviation"] <= data["tolerance"] == 0.01
    assert check["valid_fraction"] == 1.0
    assert data["checks"]["ndvi_of_the_leaf_patch"]["value"] > 0.7
    with Image.open(tmp_path / "demo" / "ndvi.png") as image:
        assert image.size == (128, 72)
        picture = np.asarray(image)
    leaf, board = picture[26, 32].tolist(), picture[5, 5].tolist()
    assert leaf != board, "the leaf-like patch stands out in the index picture"
    with Image.open(tmp_path / "demo" / "rgb.png") as image:
        rgb = np.asarray(image)
    grey = rgb[55, 60].astype(int)
    assert np.all(np.abs(grey - 102) <= 2), "the grey patch (0.4) is grey on the 0-1 scale"
    assert rgb[20, 90, 0] > 150 > 30 > rgb[20, 90, 2], "the red patch is red"
    assert load_capture(tmp_path / "demo" / "captures" / "sample", verify=True).outputs == 1


def test_demo_notices_a_white_reference_that_is_not_divided_pixel_by_pixel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The simulated light is uneven, and only dividing each pixel by the white reference
    # at the same place takes that out. Mirror the reference: the scene no longer comes back.
    real = cli.make_calibration

    def mirrored(captures: Any, *args: Any, **options: Any) -> Any:
        calibration = real(captures, *args, **options)
        calibration.white_rgb = calibration.white_rgb[:, ::-1].copy()
        return calibration

    monkeypatch.setattr(cli, "make_calibration", mirrored)
    exit_code, envelope = run(capsys, "demo", "-o", tmp_path / "demo")
    assert exit_code == 7
    check = envelope["error"]["details"]["checks"]["relative_reflectance"]
    assert check["passed"] is False
    assert check["max_deviation"] > 0.05


def test_demo_notices_an_index_built_from_the_wrong_bands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli.views.INDICES, "ndvi", ("led_10", "led_12"))  # the two swapped
    exit_code, envelope = run(capsys, "demo", "-o", tmp_path / "demo")
    assert exit_code == 7
    checks = envelope["error"]["details"]["checks"]
    assert checks["relative_reflectance"]["passed"] is True
    assert checks["ndvi_of_the_leaf_patch"]["passed"] is False
    assert checks["ndvi_of_the_leaf_patch"]["expected"] == pytest.approx(0.8333, abs=1e-4)


def test_demo_does_not_replace_a_capture_that_came_from_a_real_device(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "demo", "-o", tmp_path / "demo")
    document = tmp_path / "demo" / "captures" / "sample" / "capture.json"
    edited = json.loads(document.read_text("utf-8"))
    edited["device"]["simulated"] = False  # as if a real scan had been recorded here
    document.write_text(json.dumps(edited), "utf-8")
    exit_code, envelope = run(capsys, "demo", "-o", tmp_path / "demo")
    assert (exit_code, envelope["error"]["code"]) == (5, "INVALID_STATE")
    assert "not a simulated capture" in envelope["error"]["message"]
    assert json.loads(document.read_text("utf-8"))["device"]["simulated"] is False


def test_demo_can_be_run_again_into_the_same_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "demo", "-o", tmp_path / "demo")
    assert ok(capsys, "demo", "-o", tmp_path / "demo")["data"]["passed"] is True


def test_demo_writes_spectra_of_the_three_patches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ok(capsys, "demo", "-o", tmp_path / "demo")
    with (tmp_path / "demo" / "spectra.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert sorted({row["name"] for row in rows}) == ["grey", "leaf", "red"]
    grey = [float(row["median"]) for row in rows if row["name"] == "grey"]
    assert len(grey) == 13
    assert all(value == pytest.approx(0.4, abs=0.01) for value in grey)


def test_demo_fails_loudly_when_the_numbers_do_not_come_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cli.samples.scene
    calls = iter(range(10))

    def scene_changes_after_the_recording() -> Any:
        return real() if next(calls) == 0 else real() * 0.9

    monkeypatch.setattr(cli.samples, "scene", scene_changes_after_the_recording)
    exit_code, envelope = run(capsys, "demo", "-o", tmp_path / "demo")
    assert exit_code == 7
    assert envelope["error"]["code"] == "CALIBRATION_QUALITY"
    assert envelope["error"]["details"]["result"] == "FAIL"
    assert envelope["error"]["details"]["checks"]["relative_reflectance"]["passed"] is False
