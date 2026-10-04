# SPDX-License-Identifier: Apache-2.0
"""Camera parameter locking: order, range checks, read-back, and the uvc-util backend."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vispek_hc import controls
from vispek_hc.controls import UvcUtilControls, lock_parameters
from vispek_hc.errors import (
    AmbiguousDeviceError,
    CameraControlFailedError,
    ConfigRejectedError,
    ControlsUnavailableError,
    DeviceNotFoundError,
)
from vispek_hc.interfaces import ControlInfo
from vispek_hc.models import HC1500
from vispek_hc.sim import SimControls

WANTED = dict(HC1500.initial_controls)


def lock(sim: SimControls, wanted: dict[str, int] | None = None) -> controls.LockReport:
    return lock_parameters(sim, WANTED if wanted is None else wanted, settle=lambda: None)


# --- the locking procedure -------------------------------------------------------------


def test_simulated_camera_starts_in_automatic_mode() -> None:
    values = SimControls().values
    assert values["auto-white-balance-temp"] == 1
    assert values["auto-exposure-mode"] != 1


def test_lock_switches_automatics_off_and_confirms_by_reading_back() -> None:
    sim = SimControls()
    report = lock(sim)
    assert report.awb_off is True
    assert report.exposure_manual is True
    assert sim.values["auto-white-balance-temp"] == 0
    assert sim.values["auto-exposure-mode"] == 1
    assert sim.values["exposure-time-abs"] == 157
    assert report.requested == {k: v for k, v in WANTED.items() if k != "focus-abs"}
    assert report.effective == report.requested
    assert report.values == sim.values


def test_modes_are_written_before_the_values_that_depend_on_them() -> None:
    sim = SimControls()
    lock(sim)
    order = [name for name, _ in sim.writes]
    assert order.index("auto-white-balance-temp") < order.index("white-balance-temp")
    assert order.index("auto-exposure-mode") < order.index("exposure-time-abs")
    assert order.index("auto-exposure-mode") < order.index("gain")
    assert order[:3] == ["auto-white-balance-temp", "white-balance-temp", "auto-exposure-mode"]


def test_automatics_are_switched_off_even_if_the_profile_forgets_them() -> None:
    sim = SimControls()
    report = lock(sim, {"exposure-time-abs": 100})
    assert report.awb_off
    assert report.exposure_manual
    assert sim.values["auto-focus"] == 0
    assert sim.values["exposure-time-abs"] == 100


def test_focus_position_is_never_written() -> None:
    sim = SimControls()
    report = lock(sim, {**WANTED, "focus-abs": 123})
    assert "focus-abs" not in [name for name, _ in sim.writes]
    assert "focus-abs" not in report.requested


def test_settling_happens_before_writing_and_between_the_two_read_backs() -> None:
    sim = SimControls()
    events: list[str] = []
    original_read, original_write = sim.read_all, sim.write
    sim.read_all = lambda: events.append("read") or original_read()  # type: ignore[method-assign,func-returns-value]
    sim.write = lambda n, v: events.append("write") or original_write(n, v)  # type: ignore[method-assign,func-returns-value]
    lock_parameters(sim, {"gain": 0}, settle=lambda: events.append("settle"))
    writes = events.count("write")
    assert events == ["settle", "read", *["write"] * writes, "read", "settle", "read"]


@pytest.mark.parametrize(("name", "value"), [("exposure-time-abs", 0), ("gain", 101)])
def test_value_outside_the_device_range_is_refused_before_writing_it(name: str, value: int) -> None:
    sim = SimControls()
    with pytest.raises(ConfigRejectedError) as caught:
        lock(sim, {name: value})
    assert caught.value.details["control"] == name
    assert name not in [written for written, _ in sim.writes]


def test_value_off_the_step_grid_is_refused() -> None:
    sim = SimControls()
    sim.info["white-balance-temp"] = ControlInfo(4600, 2800, 6500, 100)
    with pytest.raises(ConfigRejectedError):
        lock(sim, {"white-balance-temp": 4650})


def test_stable_quantised_value_becomes_the_effective_value() -> None:
    sim = SimControls()
    sim.quantise["exposure-time-abs"] = lambda value: value // 4 * 4
    report = lock(sim)
    assert report.requested["exposure-time-abs"] == 157
    assert report.effective["exposure-time-abs"] == 156
    assert report.exposure_manual


def test_value_that_keeps_changing_is_a_failure() -> None:
    sim = SimControls()
    drift = iter(range(200, 300))
    sim.quantise["gain"] = lambda value: next(drift)
    original = sim.read_all

    def reading_changes_it() -> dict[str, ControlInfo]:
        sim.write("gain", 0)
        return original()

    sim.read_all = reading_changes_it  # type: ignore[method-assign]
    with pytest.raises(CameraControlFailedError) as caught:
        lock(sim)
    assert caught.value.details["control"] == "gain"


def test_automatic_mode_that_will_not_switch_off_is_reported_not_confirmed() -> None:
    sim = SimControls()
    sim.quantise["auto-white-balance-temp"] = lambda value: 1
    sim.quantise["auto-exposure-mode"] = lambda value: 8
    report = lock(sim)
    assert report.awb_off is False
    assert report.exposure_manual is False


def test_control_that_vanishes_from_the_read_back_is_a_failure() -> None:
    sim = SimControls()
    reads = iter([sim.read_all(), sim.read_all(), {}])
    sim.read_all = lambda: next(reads)  # type: ignore[method-assign]
    with pytest.raises(CameraControlFailedError) as caught:
        lock(sim, {"gain": 0})
    assert caught.value.details["control"] == "auto-white-balance-temp"


def test_controls_the_device_lacks_are_skipped_and_listed() -> None:
    sim = SimControls()
    del sim.info["backlight-compensation"]
    del sim.info["auto-exposure-mode"]
    report = lock(sim)
    assert report.skipped == ["auto-exposure-mode", "backlight-compensation"]
    assert report.exposure_manual is None, "a camera without the control cannot be locked"
    assert report.awb_off is True


def test_stream_restart_puts_the_simulated_camera_back_into_automatic() -> None:
    sim = SimControls()
    lock(sim)
    sim.reset_to_automatic()
    assert sim.values["auto-white-balance-temp"] == 1
    assert sim.values["auto-exposure-mode"] != 1


def test_changed_controls_are_listed_and_focus_is_ignored() -> None:
    before = {"gain": 0, "exposure-time-abs": 157, "focus-abs": 360}
    after = {"gain": 4, "exposure-time-abs": 157, "focus-abs": 12}
    assert controls.changed_controls(before, after) == ["gain"]
    assert controls.changed_controls(before, before) == []
    assert controls.changed_controls({"gain": 0}, {}) == ["gain"]


# --- uvc-util output -------------------------------------------------------------------

SHOW_ALL = """\
auto-exposure-mode {
  type-description: {
    single value, unsigned 8-bit integer (bitmap)
  },
  default-value: 8
  current-value: 1
}
exposure-time-abs {
  type-description: {
    single value, unsigned 32-bit integer
  },
  minimum: 1
  maximum: 5000
  step-size: 1
  default-value: 157
  current-value: 157
}
auto-white-balance-temp {
  type-description: {
    single value, boolean
  },
  default-value: true
  current-value: false
}
brightness {
  type-description: {
    single value, signed 16-bit integer
  },
  minimum: -64
  maximum: 64
  step-size: 1
  default-value: 0
  current-value: -3
}
pan-tilt-abs {
  type-description: {
    signed 32-bit integer            pan;
    signed 32-bit integer            tilt;
  },
  minimum: {pan=-648000,tilt=-648000}
  maximum: {pan=648000,tilt=648000}
  step-size: {pan=3600,tilt=3600}
  default-value: {pan=0,tilt=0}
  current-value: {pan=0,tilt=0}
}
"""

DEVICES = """\
------------ -------------- ------------ ------------ ------------------------
Index        Vend:Prod      LocationID   UVC Version  Device name
------------ -------------- ------------ ------------ ------------------------
0            0x05ac:0x8600  0x00100000   1.50         FaceTime HD Camera
1            0x0c45:0x6370  0x01122000   1.00         LRCP 1080P-60FPS
------------ -------------- ------------ ------------ ------------------------
"""


def test_show_all_output_is_parsed_into_scalar_controls() -> None:
    parsed = controls.parse_controls(SHOW_ALL)
    assert parsed == {
        "auto-exposure-mode": ControlInfo(1),
        "exposure-time-abs": ControlInfo(157, 1, 5000, 1),
        "auto-white-balance-temp": ControlInfo(0),
        "brightness": ControlInfo(-3, -64, 64, 1),
    }


def test_device_table_is_parsed() -> None:
    assert controls.parse_devices(DEVICES) == [
        controls.UvcDevice("05ac:8600", "0x00100000", "FaceTime HD Camera"),
        controls.UvcDevice("0c45:6370", "0x01122000", "LRCP 1080P-60FPS"),
    ]


class FakeUvcUtil:
    """Stands in for ``subprocess.run`` on the uvc-util binary."""

    def __init__(self, devices: str = DEVICES) -> None:
        self.devices = devices
        self.calls: list[list[str]] = []
        self.show: tuple[int, str, str] = (0, SHOW_ALL, "")
        self.fail_writes: tuple[int, str, str] | None = None

    def __call__(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv[1:])
        assert options == {"capture_output": True, "text": True, "timeout": 8.0, "check": False}
        if argv[1:] == ["-d"]:
            return subprocess.CompletedProcess(argv, 0, self.devices, "")
        if argv[3] == "-S":
            return subprocess.CompletedProcess(argv, *self.show)
        if self.fail_writes:
            return subprocess.CompletedProcess(argv, *self.fail_writes)
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def uvc_util(monkeypatch: pytest.MonkeyPatch) -> FakeUvcUtil:
    fake = FakeUvcUtil()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_backend_selects_the_camera_by_usb_location(uvc_util: FakeUvcUtil) -> None:
    backend = UvcUtilControls.open(binary="uvc-util")
    assert backend.device_id == "uvc:0c45:6370:0x01122000"
    assert backend.read_all()["exposure-time-abs"] == ControlInfo(157, 1, 5000, 1)
    backend.write("gain", 0)
    assert uvc_util.calls == [
        ["-d"],
        ["-L", "0x01122000", "-S", "*"],
        ["-L", "0x01122000", "-s", "gain=0"],
    ]


def test_failed_write_is_a_camera_control_failure(uvc_util: FakeUvcUtil) -> None:
    backend = UvcUtilControls.open(binary="uvc-util")
    for failure in [
        (5, "", "ERROR:  unable to write new value"),
        (0, "", "ERROR:  unable to write"),
    ]:
        uvc_util.fail_writes = failure
        with pytest.raises(CameraControlFailedError) as caught:
            backend.write("gain", 0)
        assert caught.value.details["control"] == "gain"
        assert "unable to write" in caught.value.message


@pytest.mark.parametrize(
    "show",
    [
        (19, SHOW_ALL, ""),
        (0, SHOW_ALL, "ERROR:  no target device selected"),
        (0, "", ""),
    ],
)
def test_failed_read_is_a_camera_control_failure_not_an_empty_camera(
    show: tuple[int, str, str], uvc_util: FakeUvcUtil
) -> None:
    backend = UvcUtilControls.open(binary="uvc-util")
    uvc_util.show = show
    with pytest.raises(CameraControlFailedError):
        backend.read_all()


def test_no_matching_camera_on_the_usb_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", FakeUvcUtil(DEVICES.replace("0x6370", "0x9999")))
    with pytest.raises(DeviceNotFoundError):
        UvcUtilControls.open(binary="uvc-util")


def test_two_matching_cameras_are_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    row = "2            0x0c45:0x6370  0x01130000   1.00         LRCP 1080P-60FPS\n"
    monkeypatch.setattr(subprocess, "run", FakeUvcUtil(DEVICES + row))
    with pytest.raises(AmbiguousDeviceError) as caught:
        UvcUtilControls.open(binary="uvc-util")
    assert caught.value.details["candidates"] == ["0x01122000", "0x01130000"]
    assert "one HC1500 per computer" in (caught.value.hint or "")


def test_binary_that_cannot_run_means_controls_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(argv: list[str], **options: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ControlsUnavailableError):
        UvcUtilControls.open(binary="uvc-util")


def test_uvc_util_is_searched_in_the_documented_places(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VISPEK_HC_UVC_UTIL", raising=False)
    monkeypatch.setattr(controls.shutil, "which", lambda name: None)
    monkeypatch.setattr(controls, "CHECKOUT_BUILD", tmp_path / "checkout" / "uvc-util")
    assert controls.find_uvc_util() is None

    places = [tmp_path / "checkout" / "uvc-util", home / "bin" / "uvc-util"]
    for place in places:  # later places win over earlier ones
        place.parent.mkdir(parents=True)
        place.write_text("")
        assert controls.find_uvc_util() == place
    monkeypatch.setattr(controls.shutil, "which", lambda name: "/usr/local/bin/uvc-util")
    assert controls.find_uvc_util() == places[1], "the user's own copy wins over PATH"
    monkeypatch.setenv("VISPEK_HC_UVC_UTIL", "/custom/uvc-util")
    assert controls.find_uvc_util() == Path("/custom/uvc-util")


def test_missing_uvc_util_explains_how_to_build_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controls, "find_uvc_util", lambda: None)
    with pytest.raises(ControlsUnavailableError) as caught:
        UvcUtilControls.open()
    assert "vispek-hc setup" in (caught.value.hint or "")


# --- building uvc-util on the user's Mac ------------------------------------------------


def helper_sources(tmp_path: Path) -> Path:
    """A copy of the vendored sources, as they sit inside an installed package."""
    import shutil

    copy = tmp_path / "_uvc_util"
    shutil.copytree(controls.CHECKOUT_SOURCES, copy, ignore=shutil.ignore_patterns("build"))
    return copy


def test_the_helper_is_compiled_from_the_shipped_sources_into_the_users_folder(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = helper_sources(tmp_path)
    calls: list[list[str]] = []

    def compiler(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert options == {"capture_output": True, "text": True, "timeout": 300, "check": False}
        Path(argv[argv.index("-o") + 1]).write_bytes(b"binary")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", compiler)
    assert controls.uvc_util_build_command(sources, Path("out"))[:3] == [
        "xcrun",
        "clang",
        "-fno-objc-arc",
    ]
    built = controls.build_uvc_util(sources, platform="darwin")
    assert built == home / "bin" / "uvc-util"
    assert built.read_bytes() == b"binary"
    if sys.platform != "win32":  # no such bit there; the helper is for macOS anyway
        assert built.stat().st_mode & 0o111, "it can be run"
    (argv,) = calls
    assert argv[-4:] == [
        str(sources / "src" / name)
        for name in ("uvc-util.m", "UVCController.m", "UVCType.m", "UVCValue.m")
    ]
    for flag in ("-arch", "arm64", "x86_64", "-mmacosx-version-min=13.0", "IOKit", "Foundation"):
        assert flag in argv
    assert controls.find_uvc_util() == built, "and it is the one that is used from now on"


def test_sources_that_were_changed_are_not_compiled(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = helper_sources(tmp_path)
    with (sources / "src" / "uvc-util.m").open("a") as file:
        file.write("// one more line\n")

    def never(*args: Any, **options: Any) -> None:
        raise AssertionError("the compiler was started")

    monkeypatch.setattr(subprocess, "run", never)
    with pytest.raises(ControlsUnavailableError, match=r"uvc-util\.m") as caught:
        controls.build_uvc_util(sources, platform="darwin")
    assert "install vispek-hc again" in (caught.value.hint or "")
    assert not (home / "bin" / "uvc-util").exists()


def test_a_compiler_that_fails_leaves_no_helper_and_says_what_to_install(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = helper_sources(tmp_path)

    def broken(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 1, "", "xcrun: error: invalid active developer path"
        )

    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(ControlsUnavailableError, match="developer path") as caught:
        controls.build_uvc_util(sources, platform="darwin")
    assert "xcode-select --install" in (caught.value.hint or "")
    assert not (home / "bin" / "uvc-util").exists()

    def missing(argv: list[str], **options: Any) -> None:
        raise FileNotFoundError("xcrun")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ControlsUnavailableError) as caught:
        controls.build_uvc_util(sources, platform="darwin")
    assert "xcode-select --install" in (caught.value.hint or "")


def test_the_helper_is_for_macos_only(home: Path, tmp_path: Path) -> None:
    with pytest.raises(ControlsUnavailableError, match="macOS") as caught:
        controls.build_uvc_util(helper_sources(tmp_path), platform="linux")
    assert "v4l-utils" in (caught.value.hint or "")


def test_the_vendored_sources_are_found_and_match_their_checksums() -> None:
    sources = controls.uvc_util_sources()
    assert sources is not None and (sources / "src" / "uvc-util.m").is_file()
    assert controls.changed_sources(sources) == []


# --- v4l2-ctl (Linux) ------------------------------------------------------------------

# What `v4l2-ctl --list-ctrls` prints for a UVC camera on a recent kernel, laid out as
# v4l-utils does. The values are the HC1500's ranges as uvc-util reports them on macOS.
_INACTIVE = " flags=inactive"
_V4L2_ROWS = [
    "User Controls",
    ("brightness", 0x00980900, "int", "min=-64 max=64 step=1 default=0 value=0"),
    ("contrast", 0x00980901, "int", "min=0 max=64 step=1 default=32 value=32"),
    ("saturation", 0x00980902, "int", "min=0 max=128 step=1 default=66 value=66"),
    ("hue", 0x00980903, "int", "min=-40 max=40 step=1 default=0 value=0"),
    ("white_balance_automatic", 0x0098090C, "bool", "default=1 value=1"),
    ("gamma", 0x00980910, "int", "min=72 max=500 step=1 default=100 value=100"),
    ("gain", 0x00980913, "int", "min=0 max=100 step=1 default=0 value=0"),
    ("power_line_frequency", 0x00980918, "menu", "min=0 max=2 default=1 value=1 (50 Hz)"),
    ("white_balance_temperature", 0x0098091A, "int",
     "min=2800 max=6500 step=1 default=4600 value=4600" + _INACTIVE),
    ("sharpness", 0x0098091B, "int", "min=0 max=6 step=1 default=3 value=3"),
    ("backlight_compensation", 0x0098091C, "int", "min=0 max=2 step=1 default=1 value=1"),
    "Camera Controls",
    ("auto_exposure", 0x009A0901, "menu",
     "min=0 max=3 default=3 value=3 (Aperture Priority Mode)"),
    ("exposure_time_absolute", 0x009A0902, "int",
     "min=1 max=5000 step=1 default=157 value=157" + _INACTIVE),
    ("exposure_dynamic_framerate", 0x009A0903, "bool", "default=0 value=1"),
    ("focus_absolute", 0x009A090A, "int",
     "min=1 max=1023 step=1 default=360 value=360" + _INACTIVE),
    ("focus_automatic_continuous", 0x009A090C, "bool", "default=1 value=1"),
]  # fmt: skip
V4L2_LISTING = "".join(
    f"\n{row}\n\n"
    if isinstance(row, str)
    else f"{row[0]:>31} 0x{row[1]:08x} {'(' + row[2] + ')':<7}: {row[3]}\n"
    for row in _V4L2_ROWS
)
# The same controls under the names older kernels give them.
OLDER_NAMES = {
    "white_balance_automatic": "white_balance_temperature_auto",
    "auto_exposure": "exposure_auto",
    "exposure_time_absolute": "exposure_absolute",
    "exposure_dynamic_framerate": "exposure_auto_priority",
    "focus_automatic_continuous": "focus_auto",
}


class FakeV4l2Ctl:
    """Stands in for ``subprocess.run`` on v4l2-ctl: a camera that keeps what it is set to."""

    def __init__(self, listing: str = V4L2_LISTING) -> None:
        self.listing = listing
        self.calls: list[list[str]] = []
        self.fail_writes: tuple[int, str, str] | None = None
        self.fail_reads: tuple[int, str, str] | None = None

    def __call__(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv[1:])
        assert options == {"capture_output": True, "text": True, "timeout": 8.0, "check": False}
        assert argv[1:3] == ["-d", "/dev/video2"]
        if argv[3] == "--list-ctrls":
            return subprocess.CompletedProcess(argv, *(self.fail_reads or (0, self.listing, "")))
        assert argv[3] == "--set-ctrl"
        if self.fail_writes:
            return subprocess.CompletedProcess(argv, *self.fail_writes)
        name, _, value = argv[4].partition("=")
        changed, count = re.subn(
            rf"^(\s*{name} 0x[0-9a-f]+ .*\bvalue=)-?\d+", rf"\g<1>{value}", self.listing, flags=re.M
        )
        assert count == 1, f"the camera has no control named {name}"
        self.listing = changed
        return subprocess.CompletedProcess(argv, 0, "", "")

    def value(self, name: str) -> int:
        found = re.search(rf"^\s*{name} 0x[0-9a-f]+ .*\bvalue=(-?\d+)", self.listing, flags=re.M)
        assert found, name
        return int(found[1])


@pytest.fixture
def v4l2_ctl(monkeypatch: pytest.MonkeyPatch) -> FakeV4l2Ctl:
    fake = FakeV4l2Ctl()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_v4l2_controls_are_known_by_their_numbers_not_their_names() -> None:
    parsed = controls.parse_v4l2_controls(V4L2_LISTING)
    assert parsed["exposure-time-abs"] == ("exposure_time_absolute", ControlInfo(157, 1, 5000, 1))
    assert parsed["gain"] == ("gain", ControlInfo(0, 0, 100, 1))
    assert parsed["brightness"] == ("brightness", ControlInfo(0, -64, 64, 1))
    assert parsed["auto-white-balance-temp"] == ("white_balance_automatic", ControlInfo(1))
    assert parsed["white-balance-temp"][1] == ControlInfo(4600, 2800, 6500, 1)
    assert parsed["auto-focus"][1].current == 1
    assert parsed["auto-exposure-priority"] == ("exposure_dynamic_framerate", ControlInfo(1))
    assert "hue" in parsed
    assert len(parsed) == 16
    older = V4L2_LISTING
    for new, old in OLDER_NAMES.items():
        older = older.replace(f" {new} ", f" {old} ")
    renamed = controls.parse_v4l2_controls(older)
    assert set(renamed) == set(parsed), "another kernel's names give the same controls"
    assert renamed["exposure-time-abs"][0] == "exposure_absolute"
    assert renamed["auto-exposure-mode"][0] == "exposure_auto"


def test_v4l2_exposure_mode_is_translated_to_the_uvc_numbers() -> None:
    # V4L2 numbers the modes 0-3 (1 manual, 3 aperture priority); UVC, and this software,
    # use the bitmap 1, 2, 4, 8 (1 manual, 8 aperture priority).
    parsed = controls.parse_v4l2_controls(V4L2_LISTING)
    assert parsed["auto-exposure-mode"] == ("auto_exposure", ControlInfo(8))
    manual = V4L2_LISTING.replace("value=3 (Aperture Priority Mode)", "value=1 (Manual Mode)")
    assert controls.parse_v4l2_controls(manual)["auto-exposure-mode"][1].current == 1


def test_v4l2_backend_reads_and_writes_through_the_kernels_names(v4l2_ctl: FakeV4l2Ctl) -> None:
    backend = controls.V4l2Controls.open("/dev/video2", binary="v4l2-ctl")
    assert backend.backend == "v4l2-ctl"
    assert backend.read_all()["exposure-time-abs"] == ControlInfo(157, 1, 5000, 1)
    backend.write("auto-exposure-mode", 1)
    backend.write("exposure-time-abs", 100)
    backend.write("auto-white-balance-temp", 0)
    assert v4l2_ctl.calls[-3:] == [
        ["-d", "/dev/video2", "--set-ctrl", "auto_exposure=1"],
        ["-d", "/dev/video2", "--set-ctrl", "exposure_time_absolute=100"],
        ["-d", "/dev/video2", "--set-ctrl", "white_balance_automatic=0"],
    ]
    values = backend.read_all()
    assert values["auto-exposure-mode"].current == 1
    assert values["exposure-time-abs"].current == 100


def test_automatic_exposure_can_be_asked_for_on_v4l2(v4l2_ctl: FakeV4l2Ctl) -> None:
    # The lock passes through automatic exposure (8 in UVC's numbers, 3 in V4L2's).
    backend = controls.V4l2Controls.open("/dev/video2", binary="v4l2-ctl")
    backend.write("auto-exposure-mode", 1)
    backend.write("auto-exposure-mode", controls.EXPOSURE_AUTOMATIC)
    assert v4l2_ctl.calls[-1] == ["-d", "/dev/video2", "--set-ctrl", "auto_exposure=3"]
    assert backend.read_all()["auto-exposure-mode"].current == 8


def test_the_whole_lock_runs_on_a_v4l2_camera(v4l2_ctl: FakeV4l2Ctl) -> None:
    backend = controls.V4l2Controls.open("/dev/video2", binary="v4l2-ctl")
    report = lock_parameters(backend, WANTED, settle=lambda: None)
    assert report.awb_off is True
    assert report.exposure_manual is True
    assert report.skipped == []
    assert v4l2_ctl.value("auto_exposure") == 1, "manual, in V4L2's numbering"
    assert v4l2_ctl.value("white_balance_automatic") == 0
    assert v4l2_ctl.value("exposure_time_absolute") == 157
    assert v4l2_ctl.value("sharpness") == 0
    assert v4l2_ctl.value("focus_absolute") == 360, "the focus position is never written"
    sets = [call[3] for call in v4l2_ctl.calls if call[2] == "--set-ctrl"]
    assert sets.index("auto_exposure=1") < sets.index("exposure_time_absolute=157")
    assert sets.index("white_balance_automatic=0") < sets.index("white_balance_temperature=4600")


def test_v4l2_failures_are_camera_control_failures(v4l2_ctl: FakeV4l2Ctl) -> None:
    backend = controls.V4l2Controls.open("/dev/video2", binary="v4l2-ctl")
    for failure in [
        (255, "", "VIDIOC_S_EXT_CTRLS: failed: Permission denied"),
        (0, "", "VIDIOC_S_EXT_CTRLS: failed: Input/output error"),
    ]:
        v4l2_ctl.fail_writes = failure
        with pytest.raises(CameraControlFailedError) as caught:
            backend.write("gain", 5)
        assert caught.value.details == {"control": "gain", "value": 5}
    v4l2_ctl.fail_writes = None
    with pytest.raises(CameraControlFailedError, match="no control"):
        backend.write("zoom-abs", 1)
    v4l2_ctl.fail_reads = (1, "", "Cannot open device /dev/video2, exiting.")
    with pytest.raises(CameraControlFailedError, match="cannot be read"):
        backend.read_all()
    v4l2_ctl.fail_reads = (0, "", "")
    with pytest.raises(CameraControlFailedError):
        backend.read_all()


def test_missing_v4l2_ctl_explains_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controls.shutil, "which", lambda name: None)
    with pytest.raises(ControlsUnavailableError) as caught:
        controls.V4l2Controls.open("/dev/video2")
    assert "v4l-utils" in (caught.value.hint or "")
    assert "--unlocked" in (caught.value.hint or "")
