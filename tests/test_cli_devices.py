# SPDX-License-Identifier: Apache-2.0
"""The commands that look at or drive a device: check, devices, led."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vispek_hc import cli, controls, illuminator, video
from vispek_hc.errors import BackendMissingError, CameraControlFailedError
from vispek_hc.protocol import Function, encode_off, encode_on
from vispek_hc.sim import SimulatedRig

pytestmark = pytest.mark.usefixtures("quiet_host")


LINUX_HOST = cli.linux_host  # the real one: a fixture replaces it for every test


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    exit_code = cli.main([*argv, "--json"])
    return exit_code, json.loads(capsys.readouterr().out)


@pytest.fixture
def rigs(monkeypatch: pytest.MonkeyPatch) -> list[SimulatedRig]:
    """Every simulated rig the command line creates, for inspection."""
    made: list[SimulatedRig] = []

    class Recording(SimulatedRig):
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            made.append(self)

    monkeypatch.setattr(cli, "SimulatedRig", Recording)
    monkeypatch.setattr(cli.session, "SimulatedRig", Recording)
    return made


# --- check -----------------------------------------------------------------------------


def test_check_without_a_device_reports_the_host_and_is_not_ready(
    capsys: pytest.CaptureFixture[str], quiet_host: Path
) -> None:
    exit_code, envelope = run(capsys, "check")
    assert exit_code == 0, "check is a diagnosis: finding problems is not a failure"
    data = envelope["data"]
    assert data["ready"] is False
    assert data["device"] is None
    assert data["home"] == str(quiet_host)
    assert data["disk_free_bytes"] > 0
    assert data["ffmpeg"] == {"found": True, "version": "9.0", "fps_flag": "-fps_mode"}
    assert data["serial"] == {"candidates": []}
    assert data["cameras"] == {"all": ["FaceTime HD Camera"], "matching": []}
    port_problem, camera_problem = data["problems"]
    assert port_problem["code"] == camera_problem["code"] == "DEVICE_NOT_FOUND"
    assert "serial port" in port_problem["message"]
    assert "camera" in camera_problem["message"]
    assert set(port_problem) == {"code", "message", "hint", "details"}


def test_check_reports_missing_ffmpeg(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*args: Any) -> None:
        raise BackendMissingError("ffmpeg cannot be run", hint=video.INSTALL_HINT)

    monkeypatch.setattr(video, "ffmpeg_version", missing)
    _, envelope = run(capsys, "check")
    data = envelope["data"]
    assert data["ffmpeg"] == {"found": False, "version": None, "fps_flag": None}
    assert data["problems"][0]["code"] == "BACKEND_MISSING"
    assert "brew install ffmpeg" in data["problems"][0]["hint"]


def test_check_without_ffmpeg_still_lists_macos_cameras(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*args: Any) -> None:
        raise BackendMissingError("ffmpeg cannot be run")

    monkeypatch.setattr(video, "ffmpeg_version", missing)
    monkeypatch.setattr(video, "list_cameras", missing)
    monkeypatch.setattr(cli, "list_cameras_without_ffmpeg", lambda: ["LRCP 1080P-60FPS"])
    monkeypatch.setattr(illuminator, "find_ports", lambda: ["/dev/cu.usbserial-7"])
    _, envelope = run(capsys, "check")
    data = envelope["data"]
    assert data["cameras"] == {"all": ["LRCP 1080P-60FPS"], "matching": ["LRCP 1080P-60FPS"]}
    assert data["serial"] == {"candidates": ["/dev/cu.usbserial-7"]}
    assert data["device"] is None, "no frames without ffmpeg"
    assert [problem["code"] for problem in data["problems"]] == ["BACKEND_MISSING"]


def test_check_reports_old_ffmpeg_flag(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video, "ffmpeg_version", lambda *args: (4, 4))
    _, envelope = run(capsys, "check")
    assert envelope["data"]["ffmpeg"] == {"found": True, "version": "4.4", "fps_flag": "-vsync"}


def test_check_reports_control_backend_on_macos(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    _, envelope = run(capsys, "check")
    assert envelope["data"]["controls"] == {
        "backend": "uvc-util",
        "binary": None,
        "xcode_tools": False,
    }
    codes = [problem["code"] for problem in envelope["data"]["problems"]]
    assert "CONTROLS_UNAVAILABLE" in codes
    hints = " ".join(problem["hint"] or "" for problem in envelope["data"]["problems"])
    assert "xcode-select --install" in hints

    monkeypatch.setattr(controls, "find_uvc_util", lambda: Path("/opt/uvc-util"))
    _, envelope = run(capsys, "check")
    assert envelope["data"]["controls"]["binary"] == str(Path("/opt/uvc-util"))
    assert "CONTROLS_UNAVAILABLE" not in [p["code"] for p in envelope["data"]["problems"]]


def test_check_reports_control_backend_on_linux(
    quiet_host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "find_v4l2_ctl", lambda: "/usr/bin/v4l2-ctl")
    monkeypatch.setattr(cli, "linux_host", lambda: {"dialout": True, "brltty": False})
    _, envelope = run(capsys, "check", "--dry-run")
    assert envelope["data"]["controls"] == {
        "backend": "v4l2-ctl", "binary": "/usr/bin/v4l2-ctl", "dialout": True, "brltty": False,
    }  # fmt: skip
    assert "CONTROLS_UNAVAILABLE" not in [p["code"] for p in envelope["data"]["problems"]]
    assert envelope["warnings"] == []


def test_check_on_linux_says_what_to_install_and_what_blocks_the_serial_port(
    quiet_host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "find_v4l2_ctl", lambda: None)
    monkeypatch.setattr(cli, "linux_host", lambda: {"dialout": False, "brltty": True})
    _, envelope = run(capsys, "check", "--dry-run")
    assert envelope["data"]["controls"]["binary"] is None
    problem = next(p for p in envelope["data"]["problems"] if p["code"] == "CONTROLS_UNAVAILABLE")
    assert "v4l-utils" in problem["hint"]
    dialout, brltty = envelope["warnings"]
    assert "dialout" in dialout and "99-vispek-hc.rules" in dialout
    assert "brltty" in brltty and "CH341" in brltty


def test_linux_host_looks_at_the_dialout_group_and_at_brltty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import types

    groups = types.SimpleNamespace(getgrnam=lambda name: types.SimpleNamespace(gr_gid=20))
    monkeypatch.setattr(cli.os, "getgroups", lambda: [20, 1000], raising=False)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert LINUX_HOST(groups, rules=tmp_path / "no-rules") == {"dialout": True, "brltty": False}
    monkeypatch.setattr(cli.os, "getgroups", lambda: [1000], raising=False)
    (tmp_path / "85-brltty.rules").write_text("")
    assert LINUX_HOST(groups, rules=tmp_path / "85-brltty.rules") == {
        "dialout": False, "brltty": True,
    }  # fmt: skip
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0, raising=False)
    assert LINUX_HOST(groups, rules=tmp_path / "no-rules")["dialout"] is True, "root may"

    def unknown(name: str) -> None:
        raise KeyError(name)

    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    nobody = types.SimpleNamespace(getgrnam=unknown)
    assert LINUX_HOST(nobody, rules=tmp_path / "no-rules")["dialout"] is None, "no such group"


def test_check_on_windows_points_to_the_serial_driver_and_says_controls_are_not_built(
    quiet_host: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "PLATFORM", "win32")
    _, envelope = run(capsys, "check", "--dry-run")
    assert envelope["data"]["controls"] == {"backend": None, "binary": None}
    unavailable = [p for p in envelope["data"]["problems"] if p["code"] == "CONTROLS_UNAVAILABLE"]
    assert len(unavailable) == 1 and "--unlocked" in unavailable[0]["hint"]
    (driver,) = envelope["warnings"]
    assert "CH341" in driver and "WCH" in driver and "Device Manager" in driver
    monkeypatch.setattr(illuminator, "find_ports", lambda: ["COM5"])
    _, envelope = run(capsys, "check", "--dry-run")
    assert envelope["warnings"] == [], "a port was found: the driver is there"


def test_check_with_ambiguous_ports_lists_them_and_touches_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(illuminator, "find_ports", lambda: ["/dev/a", "/dev/b"])
    monkeypatch.setattr(video, "list_cameras", lambda *args: ["LRCP 1080P-60FPS"])
    _, envelope = run(capsys, "check")
    data = envelope["data"]
    assert data["serial"] == {"candidates": ["/dev/a", "/dev/b"]}
    (problem,) = data["problems"]
    assert problem["code"] == "AMBIGUOUS_DEVICE"
    assert problem["details"] == {"candidates": ["/dev/a", "/dev/b"], "option": "--port"}


def test_check_simulated_runs_the_device_checks(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "check", "--simulate")
    assert exit_code == 0
    data = envelope["data"]
    assert data["ready"] is True
    assert data["problems"] == []
    device = data["device"]
    assert device["simulated"] is True
    assert device["pair"]["paired"] is True
    assert 130 <= device["pair"]["latency_ms"] <= 300
    assert device["pair"]["fps"] == pytest.approx(10.0)
    assert device["black_level"] == [5, 5, 5]
    assert device["controls"] == {
        "locked": True,
        "gain_state": "dark",
        "awb_off": True,
        "exposure_manual": True,
        "exposure_time_abs": 157,
        "skipped": [],
    }
    assert device["ack"]["commands"] > 17
    assert device["ack"]["loss_rate"] == 0
    assert device["ack"]["delay_ms_p50"] == pytest.approx(15.0)
    assert rigs[0].device.lit == {}
    assert rigs[0].device.closed


def stubborn_controls(monkeypatch: pytest.MonkeyPatch, **quantise: Any) -> None:
    """Make every simulated camera store what it likes for the named controls."""

    class Stubborn(cli.session.SimulatedRig):  # type: ignore[name-defined,misc]
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            for name, stored in quantise.items():
                if stored is None:
                    del self.controls.info[name.replace("_", "-")]
                else:
                    self.controls.quantise[name.replace("_", "-")] = lambda value, s=stored: s

    monkeypatch.setattr(cli.session, "SimulatedRig", Stubborn)


def test_check_is_not_ready_when_the_camera_stays_in_automatic_mode(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stubborn_controls(monkeypatch, auto_white_balance_temp=1, auto_exposure_mode=8)
    _, envelope = run(capsys, "check", "--simulate")
    data = envelope["data"]
    assert data["device"]["controls"]["locked"] is False
    assert [p["code"] for p in data["problems"]] == ["AWB_NOT_CONFIRMED", "EXPOSURE_NOT_CONFIRMED"]
    assert data["ready"] is False


def test_check_warns_about_controls_the_camera_lacks_or_changed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stubborn_controls(monkeypatch, backlight_compensation=None, exposure_time_abs=156)
    _, envelope = run(capsys, "check", "--simulate")
    assert envelope["data"]["ready"] is True
    skipped, adjusted = envelope["warnings"]
    assert "backlight-compensation" in skipped
    assert "exposure-time-abs: asked 157, got 156" in adjusted


def test_check_still_probes_the_device_when_a_parameter_cannot_be_written(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Any) -> None:
        raise CameraControlFailedError("camera refused gain=0", details={"control": "gain"})

    monkeypatch.setattr(cli.session.Session, "lock_controls", refuse)
    _, envelope = run(capsys, "check", "--simulate")
    data = envelope["data"]
    assert [problem["code"] for problem in data["problems"]] == ["CAMERA_CONTROL_FAILED"]
    assert data["device"]["pair"]["paired"] is True
    assert data["device"]["black_level"] == [5, 5, 5]
    assert data["device"]["controls"]["locked"] is False


def test_check_reports_a_camera_that_does_not_see_the_led(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Blind(cli.session.SimulatedRig):  # type: ignore[name-defined,misc]
        def __init__(self, **options: Any) -> None:
            super().__init__(**options, reflectance=0.0)

    monkeypatch.setattr(cli.session, "SimulatedRig", Blind)
    _, envelope = run(capsys, "check", "--simulate")
    (problem,) = envelope["data"]["problems"]
    assert problem["code"] == "INVALID_STATE"
    assert "did not react" in problem["message"]


def test_check_simulated_ignores_what_the_host_lacks(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*args: Any) -> None:
        raise BackendMissingError("ffmpeg cannot be run")

    monkeypatch.setattr(video, "ffmpeg_version", missing)
    monkeypatch.setattr(video, "list_cameras", missing)
    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    _, envelope = run(capsys, "check", "--simulate")
    assert envelope["data"]["ffmpeg"]["found"] is False
    assert envelope["data"]["problems"] == []
    assert envelope["data"]["ready"] is True


SYSTEM_PROFILER = """{"SPCameraDataType": [
  {"_name": "FaceTime HD Camera", "spcamera_model-id": "FaceTime HD Camera"},
  {"_name": "LRCP 1080P-60FPS", "spcamera_model-id": "UVC Camera VendorID_3141 ProductID_25456"}
]}"""


def test_macos_cameras_can_be_listed_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run_profiler(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, SYSTEM_PROFILER, "")

    monkeypatch.undo()  # the quiet host replaced the function under test
    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    monkeypatch.setattr(subprocess, "run", run_profiler)
    assert cli.list_cameras_without_ffmpeg() == ["FaceTime HD Camera", "LRCP 1080P-60FPS"]
    assert calls == [["system_profiler", "SPCameraDataType", "-json"]]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")
    )
    assert cli.list_cameras_without_ffmpeg() == []
    monkeypatch.setattr(cli, "PLATFORM", "linux")
    assert cli.list_cameras_without_ffmpeg() == []


def test_check_warns_about_pwm_ripple_at_short_exposure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cli.session.Session.lock_controls

    def short_exposure(self: Any) -> Any:
        self.unit.camera_controls = {"exposure-time-abs": 40}
        return original(self)

    monkeypatch.setattr(cli.session.Session, "lock_controls", short_exposure)
    _, envelope = run(capsys, "check", "--simulate")
    assert envelope["data"]["device"]["controls"]["exposure_time_abs"] == 40
    (warning,) = envelope["warnings"]
    assert "4 ms" in warning
    assert "ripple" in warning


def test_ripple_warning_starts_below_6_3_ms() -> None:
    assert cli.ripple_warning(62) is not None
    assert cli.ripple_warning(63) is None
    assert cli.ripple_warning(157) is None


def test_check_black_level_problem(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli.session.Session, "black_level", lambda self: (0, 0, 0))
    _, envelope = run(capsys, "check", "--simulate")
    data = envelope["data"]
    assert data["ready"] is False
    (problem,) = data["problems"]
    assert problem["code"] == "INVALID_STATE"
    assert "black level" in problem["message"]


def test_check_dry_run_touches_no_device(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    _, envelope = run(capsys, "check", "--simulate", "--dry-run")
    assert envelope["data"]["device"] is None
    assert envelope["data"]["ready"] is None, "nothing was probed, so nothing is known"
    assert rigs == []


def test_check_dry_run_still_reports_missing_candidates(capsys: pytest.CaptureFixture[str]) -> None:
    _, envelope = run(capsys, "check", "--dry-run")
    data = envelope["data"]
    assert [problem["code"] for problem in data["problems"]] == ["DEVICE_NOT_FOUND"] * 2
    assert data["ready"] is False


def test_check_plain_output_is_readable(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", "--simulate"]) == 0
    out = capsys.readouterr().out
    assert "ready: True" in out
    assert "black_level: 5, 5, 5" in out
    assert out.isascii()


# --- devices ---------------------------------------------------------------------------


def test_devices_lists_candidates(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(illuminator, "find_ports", lambda: ["/dev/cu.usbserial-7"])
    monkeypatch.setattr(video, "list_cameras", lambda *args: ["FaceTime", "LRCP 1080P-60FPS"])
    exit_code, envelope = run(capsys, "devices")
    assert exit_code == 0
    assert envelope["data"] == {
        "serial": {"candidates": ["/dev/cu.usbserial-7"]},
        "cameras": {"all": ["FaceTime", "LRCP 1080P-60FPS"], "matching": ["LRCP 1080P-60FPS"]},
    }


def test_devices_pair_blinks_and_reports(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "devices", "--pair", "--simulate")
    assert exit_code == 0
    assert set(envelope["data"]) == {"serial", "cameras", "pair"}, "same shape as without --pair"
    pair = envelope["data"]["pair"]
    assert pair["paired"] is True
    assert pair["led_id"] == 6
    assert rigs[0].device.lit == {}


def test_devices_pair_dry_run_touches_nothing(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "devices", "--pair", "--simulate", "--dry-run")
    assert exit_code == 0
    assert envelope["data"]["plan"] == {"action": "pair", "led_id": 6, "pwm": 224, "on_s": 0.3}
    assert "pair" not in envelope["data"]
    assert rigs == []


def test_devices_works_without_ffmpeg(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*args: Any) -> None:
        raise BackendMissingError("ffmpeg cannot be run")

    monkeypatch.setattr(video, "list_cameras", missing)
    monkeypatch.setattr(cli, "list_cameras_without_ffmpeg", lambda: ["LRCP 1080P-60FPS"])
    exit_code, envelope = run(capsys, "devices")
    assert exit_code == 0
    assert envelope["data"]["cameras"]["matching"] == ["LRCP 1080P-60FPS"]


def test_devices_pair_without_a_device_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, envelope = run(capsys, "devices", "--pair")
    assert exit_code == 4
    assert envelope["error"]["code"] == "DEVICE_NOT_FOUND"


# --- led -------------------------------------------------------------------------------


def test_led_on_holds_then_switches_off(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "led", "on", "5", "24", "--hold", "2", "--simulate")
    assert exit_code == 0
    assert envelope["data"] == {
        "action": "on",
        "leds": [{"led_id": 5, "nm": 455, "pwm": 24}],
        "hold_s": 2.0,
        "ended_by": "hold",
        "all_off": True,
    }
    (rig,) = rigs
    (lit,) = [entry for entry in rig.device.lit_intervals() if entry[0] == 5]
    assert lit[3] - lit[2] >= 2.0
    assert rig.device.lit == {}
    assert rig.device.closed


def test_led_on_without_hold_ends_on_interrupt(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted() -> None:
        assert rigs[0].device.lit == {5: 24}, "the LED is on while the command blocks"
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "wait_for_interrupt", interrupted)
    exit_code, envelope = run(capsys, "led", "on", "5", "24", "--simulate")
    assert exit_code == 0
    assert envelope["data"]["ended_by"] == "interrupt"
    assert envelope["data"]["hold_s"] is None
    assert rigs[0].device.lit == {}


def test_led_uv_needs_permission(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "led", "on", "1", "100", "--hold", "2", "--simulate")
    assert exit_code == 5
    assert envelope["error"]["code"] == "UV_NOT_ALLOWED"
    assert rigs == [], "refused while planning, before any device is opened"


@pytest.mark.parametrize("hold", ["0", "-1", "nan", "inf", "1e308", "soon"])
def test_led_hold_must_be_a_positive_finite_number_of_seconds(
    hold: str, capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "led", "on", "5", "24", "--hold", hold, "--simulate")
    assert exit_code == 2
    assert envelope["error"]["code"] == "USAGE_ERROR"
    assert rigs == []


@pytest.mark.parametrize("hold", [[], ["--hold", "10.5"]])
def test_led_uv_needs_a_hold_of_at_most_ten_seconds(
    hold: list[str], capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "led", "on", "1", "100", "--allow-uv", *hold, "--simulate")
    assert exit_code == 5
    assert envelope["error"]["code"] == "CONFIG_REJECTED"
    assert "--hold" in envelope["error"]["message"]
    assert rigs == []


def test_led_uv_with_permission_and_hold(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(
        capsys, "led", "on", "1", "100", "--allow-uv", "--hold", "5", "--simulate"
    )
    assert exit_code == 0
    assert envelope["data"]["leds"] == [{"led_id": 1, "nm": 255, "pwm": 100}]
    assert rigs[0].device.lit == {}


def test_led_on_all_lights_the_visible_leds_and_warns_about_power(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def all_lit() -> None:
        assert sorted(rigs[0].device.lit) == list(range(5, 18))
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "wait_for_interrupt", all_lit)
    exit_code, envelope = run(capsys, "led", "on", "--all", "--simulate")
    assert exit_code == 0
    leds = envelope["data"]["leds"]
    assert [led["led_id"] for led in leds] == list(range(5, 18))
    assert leds[0] == {"led_id": 5, "nm": 455, "pwm": 24}
    assert any("USB1" in warning for warning in envelope["warnings"])
    assert rigs[0].device.lit == {}


def test_led_on_all_with_uv(capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]) -> None:
    exit_code, envelope = run(
        capsys, "led", "on", "--all", "--allow-uv", "--hold", "1", "--simulate"
    )
    assert exit_code == 0
    assert [led["led_id"] for led in envelope["data"]["leds"]] == list(range(1, 18))


def test_led_off_one_and_all(capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]) -> None:
    # Opening the light source switches every LED off, so both forms leave everything off.
    exit_code, envelope = run(capsys, "led", "off", "5", "--simulate")
    assert exit_code == 0
    assert envelope["data"] == {"action": "off", "leds": [5], "all_off": True}
    exit_code, envelope = run(capsys, "led", "off", "--all", "--simulate")
    assert envelope["data"] == {"action": "off", "leds": list(range(1, 18)), "all_off": True}
    for rig in rigs:
        assert {cmd.led_id for _, cmd in rig.device.log} == set(range(1, 18))
        assert all(cmd.function is Function.OFF for _, cmd in rig.device.log)
        assert rig.device.closed


def test_led_off_refuses_an_led_that_does_not_exist(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, "led", "off", "18", "--simulate")
    assert exit_code == 5
    assert envelope["error"]["code"] == "CONFIG_REJECTED"
    assert rigs == []


def test_led_dry_run_prints_the_plan_and_opens_nothing(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dry run must not open the port")

    monkeypatch.setattr(cli.Illuminator, "open", never)
    exit_code, envelope = run(capsys, "led", "on", "5", "24", "--hold", "2", "--dry-run")
    assert exit_code == 0
    assert envelope["data"] == {
        "dry_run": True,
        "action": "on",
        "plan": [
            {"led_id": 5, "nm": 455, "pwm": 24, "frame": encode_on(5, 24).hex(" ")},
            {"led_id": 5, "nm": 455, "pwm": None, "frame": encode_off(5).hex(" ")},
        ],
        "hold_s": 2.0,
    }
    assert rigs == []


def test_led_dry_run_still_enforces_the_uv_gate(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, envelope = run(capsys, "led", "on", "2", "100", "--dry-run")
    assert exit_code == 5
    assert envelope["error"]["code"] == "UV_NOT_ALLOWED"


@pytest.mark.parametrize(
    "argv",
    [
        ["led", "on"],
        ["led", "on", "5"],
        ["led", "on", "5", "24", "--all"],
        ["led", "off"],
        ["led", "off", "5", "--all"],
        ["led", "on", "five", "24"],
        ["led"],
    ],
)
def test_led_usage_errors(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, envelope = run(capsys, *argv, "--simulate")
    assert exit_code == 2
    assert envelope["error"]["code"] == "USAGE_ERROR"


@pytest.mark.parametrize("argv", [["led", "on", "18", "10"], ["led", "on", "5", "1024"]])
def test_led_out_of_range_values_are_refused(
    argv: list[str], capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig]
) -> None:
    exit_code, envelope = run(capsys, *argv, "--hold", "1", "--simulate")
    assert exit_code == 5
    assert envelope["error"]["code"] == "CONFIG_REJECTED"
    assert rigs == []


def test_led_real_device_uses_the_named_port(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = SimulatedRig()
    seen: dict[str, Any] = {}

    def open_light(port: str | None, *, baud: int | None = None, allow_uv: bool = False) -> Any:
        seen.update(port=port, allow_uv=allow_uv)
        rig.light.connect(baud=115200)
        return rig.light

    monkeypatch.setattr(cli.Illuminator, "open", open_light)
    exit_code, _ = run(capsys, "led", "on", "5", "24", "--hold", "0.5", "--port", "/dev/x")
    assert exit_code == 0
    assert seen == {"port": "/dev/x", "allow_uv": False}
    assert rig.device.closed


def test_led_reports_unconfirmed_shutdown_as_a_hardware_failure(
    capsys: pytest.CaptureFixture[str], rigs: list[SimulatedRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unplug() -> None:
        rigs[0].device.unplugged = True
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "wait_for_interrupt", unplug)
    exit_code, envelope = run(capsys, "led", "on", "5", "24", "--simulate")
    assert exit_code == 6
    assert envelope["error"]["code"] == "LED_SHUTDOWN_UNCONFIRMED"


# --- setup -----------------------------------------------------------------------------


def test_setup_on_macos_compiles_the_camera_helper(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[Path] = []

    def build() -> Path:
        built.append(home / "bin" / "uvc-util")
        return built[-1]

    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    monkeypatch.setattr(controls, "build_uvc_util", build)
    exit_code, envelope = run(capsys, "setup", "--dry-run")
    assert exit_code == 0 and built == [], "a dry run compiles nothing"
    data = envelope["data"]
    assert (data["platform"], data["built"]) == ("darwin", False)
    assert data["helper"] == str(home / "bin" / "uvc-util")
    assert data["command"][:2] == ["xcrun", "clang"]
    exit_code, envelope = run(capsys, "setup")
    assert exit_code == 0 and len(built) == 1
    assert envelope["data"]["built"] is True
    assert envelope["data"]["helper"] == str(home / "bin" / "uvc-util")


def test_setup_reports_a_compiler_that_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def build() -> Path:
        raise controls.ControlsUnavailableError("no compiler", hint="xcode-select --install")

    monkeypatch.setattr(cli, "PLATFORM", "darwin")
    monkeypatch.setattr(controls, "build_uvc_util", build)
    exit_code, envelope = run(capsys, "setup")
    assert exit_code == 3
    assert envelope["error"]["code"] == "CONTROLS_UNAVAILABLE"
    assert envelope["error"]["hint"] == "xcode-select --install"


def test_setup_on_linux_and_windows_builds_nothing_and_says_what_is_needed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "PLATFORM", "linux")
    monkeypatch.setattr(cli, "find_v4l2_ctl", lambda: None)
    exit_code, envelope = run(capsys, "setup")
    assert exit_code == 0
    assert envelope["data"] == {
        "platform": "linux",
        "helper": None,
        "built": False,
        "command": None,
    }
    assert "v4l-utils" in envelope["warnings"][0]
    monkeypatch.setattr(cli, "find_v4l2_ctl", lambda: "/usr/bin/v4l2-ctl")
    _, envelope = run(capsys, "setup")
    assert envelope["data"]["helper"] == "/usr/bin/v4l2-ctl" and envelope["warnings"] == []
    monkeypatch.setattr(cli, "PLATFORM", "win32")
    _, envelope = run(capsys, "setup")
    assert envelope["data"]["helper"] is None
    assert "not built" in envelope["warnings"][0]
