# SPDX-License-Identifier: Apache-2.0
"""DirectShow units, automatic modes and read-back without opening hardware."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vispek_hc._windows_controls import AUTO, MANUAL, DirectShowControls, NativeControl
from vispek_hc.controls import AWB, EXPOSURE_MODE, lock_parameters
from vispek_hc.errors import CameraControlFailedError, ConfigRejectedError
from vispek_hc.models import HC1500


class Camera:
    device_id = "directshow:camera-2"

    def __init__(self) -> None:
        self.values = {
            "white-balance-temp": NativeControl(4600, 2800, 6500, 100, 4600, 3, AUTO),
            "exposure-time-abs": NativeControl(-6, -13, -1, 1, -6, 3, AUTO),
            "gain": NativeControl(0, 0, 255, 1, 0, MANUAL, MANUAL),
        }
        self.writes: list[tuple[str, int, int]] = []

    def read_all(self) -> dict[str, NativeControl]:
        return dict(self.values)

    def write(self, name: str, value: int, flags: int) -> None:
        self.writes.append((name, value, flags))
        self.values[name] = replace(self.values[name], current=value, flags=flags)


def test_windows_lock_uses_manual_flags_and_reports_effective_exposure() -> None:
    camera = Camera()
    controls = DirectShowControls.open("camera-2", factory=lambda device: camera)
    report = lock_parameters(controls, HC1500.initial_controls, lambda: None)
    assert report.awb_off and report.exposure_manual
    assert report.requested["exposure-time-abs"] == 157
    assert report.effective["exposure-time-abs"] == 156
    assert camera.writes[:3] == [
        ("white-balance-temp", 4600, MANUAL),
        ("white-balance-temp", 4600, MANUAL),
        ("exposure-time-abs", -6, MANUAL),
    ]
    assert "power-line-frequency" in report.skipped


def test_exposure_range_has_no_false_linear_step() -> None:
    control = DirectShowControls(Camera()).read_all()["exposure-time-abs"]
    assert (control.current, control.minimum, control.maximum, control.step) == (156, 1, 5000, None)


def test_mode_changes_keep_the_current_value_and_support_auto() -> None:
    camera = Camera()
    controls = DirectShowControls(camera)
    controls.write(AWB, 0)
    controls.write(EXPOSURE_MODE, 1)
    controls.write(EXPOSURE_MODE, 8)
    assert camera.writes == [
        ("white-balance-temp", 4600, MANUAL),
        ("exposure-time-abs", -6, MANUAL),
        ("exposure-time-abs", -6, AUTO),
    ]
    assert controls.read_all()[EXPOSURE_MODE].current == 8


@pytest.mark.parametrize(
    ("name", "value"), [(AWB, 2), (EXPOSURE_MODE, 4), ("gain", 256), ("exposure-time-abs", 0)]
)
def test_invalid_values_never_reach_the_driver(name: str, value: int) -> None:
    camera = Camera()
    with pytest.raises(ConfigRejectedError):
        DirectShowControls(camera).write(name, value)
    assert not camera.writes


def test_unknown_mode_flags_are_not_reported_as_a_lock() -> None:
    camera = Camera()
    camera.values["white-balance-temp"] = replace(camera.values["white-balance-temp"], flags=0)
    with pytest.raises(CameraControlFailedError, match="unknown control flags"):
        DirectShowControls(camera).read_all()


def test_a_driver_that_ignores_manual_mode_cannot_pass_the_lock() -> None:
    class RefusingCamera(Camera):
        def write(self, name: str, value: int, flags: int) -> None:
            pass

    report = lock_parameters(
        DirectShowControls(RefusingCamera()), HC1500.initial_controls, lambda: None
    )
    assert not report.awb_off and not report.exposure_manual


def test_unsupported_manual_mode_fails_instead_of_claiming_a_lock() -> None:
    camera = Camera()
    camera.values["white-balance-temp"] = replace(
        camera.values["white-balance-temp"], capabilities=AUTO
    )
    with pytest.raises(CameraControlFailedError, match="requested mode"):
        DirectShowControls(camera).write(AWB, 0)
