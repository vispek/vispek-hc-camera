# SPDX-License-Identifier: Apache-2.0
"""Translate DirectShow camera controls to the SDK's UVC names and units.

The native module is loaded only when opening a real Windows camera. COM pointers
never escape a call, so a session may use its controls from different threads.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, Self

from vispek_hc.controls import AWB, EXPOSURE_AUTOMATIC, EXPOSURE_MODE, FOCUS
from vispek_hc.errors import CameraControlFailedError, ConfigRejectedError, ControlsUnavailableError
from vispek_hc.interfaces import ControlInfo

AUTO, MANUAL = 1, 2
# IAMVideoProcAmp / IAMCameraControl property numbers from strmif.h.
PROPERTIES = {
    "brightness": ("video", 0),
    "contrast": ("video", 1),
    "hue": ("video", 2),
    "saturation": ("video", 3),
    "sharpness": ("video", 4),
    "gamma": ("video", 5),
    "white-balance-temp": ("video", 7),
    "backlight-compensation": ("video", 8),
    "gain": ("video", 9),
    "exposure-time-abs": ("camera", 4),
    FOCUS: ("camera", 6),
}
AUTOMATIC_PROPERTIES = {
    AWB: "white-balance-temp",
    EXPOSURE_MODE: "exposure-time-abs",
    "auto-focus": FOCUS,
}


@dataclass(frozen=True)
class NativeControl:
    current: int
    minimum: int
    maximum: int
    step: int
    default: int
    capabilities: int
    flags: int


class NativeCamera(Protocol):
    device_id: str

    def read_all(self) -> dict[str, NativeControl]: ...

    def write(self, name: str, value: int, flags: int) -> None: ...


def available() -> bool:
    """Check the platform without importing COM or opening a camera."""
    return sys.platform == "win32"


def _exposure(value: int) -> int:
    # DirectShow: log2(seconds). The public SDK uses units of 100 microseconds.
    return round(math.ldexp(10000.0, value))


class DirectShowControls:
    backend = "directshow"

    def __init__(self, native: NativeCamera) -> None:
        self._native = native
        self.device_id = native.device_id

    @classmethod
    def open(cls, device: str, *, factory: Callable[[str], NativeCamera] | None = None) -> Self:
        if factory is None:
            try:
                from vispek_hc._directshow import NativeCamera as WindowsCamera
            except (ImportError, OSError):
                raise ControlsUnavailableError(
                    "Windows DirectShow camera controls could not be loaded",
                    hint="DirectShow controls require a Windows desktop installation",
                ) from None

            factory = WindowsCamera
        made = cls(factory(device))
        made.read_all()  # A missing or inaccessible device fails before a scan starts.
        return made

    def read_all(self) -> dict[str, ControlInfo]:
        native = self._native.read_all()
        result = {}
        for name, control in native.items():
            if name == "exposure-time-abs":
                result[name] = ControlInfo(
                    _exposure(control.current),
                    max(1, _exposure(control.minimum)),
                    _exposure(control.maximum),
                    None,  # Exposure steps are exponential, not a linear UVC grid.
                )
            else:
                result[name] = ControlInfo(
                    control.current, control.minimum, control.maximum, control.step
                )
        for name, source in AUTOMATIC_PROPERTIES.items():
            mode_control = native.get(source)
            if mode_control is None:
                continue
            # Unknown flags must not be mistaken for confirmed manual operation.
            automatic = bool(mode_control.flags & AUTO)
            manual = bool(mode_control.flags & MANUAL) and not automatic
            if not automatic and not manual:
                raise CameraControlFailedError(
                    f"camera returned unknown control flags for {source}"
                )
            current = (
                (1 if manual else EXPOSURE_AUTOMATIC) if name == EXPOSURE_MODE else int(automatic)
            )
            # A manual-only control is still exposed: read-back proves auto is off.
            result[name] = ControlInfo(current)
        return result

    def write(self, name: str, value: int) -> None:
        source = AUTOMATIC_PROPERTIES.get(name, name)
        native = self._native.read_all()
        if source not in native:
            raise CameraControlFailedError(f"the camera has no control {name}")
        info = native[source]
        flags = MANUAL
        sent = value
        if name in AUTOMATIC_PROPERTIES:
            if name == EXPOSURE_MODE:
                if value not in (1, EXPOSURE_AUTOMATIC):
                    raise ConfigRejectedError("DirectShow supports manual or automatic exposure")
                automatic = value != 1
            else:
                if value not in (0, 1):
                    raise ConfigRejectedError(f"{name} must be 0 or 1")
                automatic = bool(value)
            flags = AUTO if automatic else MANUAL
            sent = info.current
        elif name == "exposure-time-abs":
            if value <= 0:
                raise ConfigRejectedError("exposure-time-abs must be positive")
            exponent = math.log2(value / 10000)
            step = max(1, info.step)
            sent = info.minimum + round((exponent - info.minimum) / step) * step
        elif not info.minimum <= value <= info.maximum or (
            info.step > 0 and (value - info.minimum) % info.step
        ):
            raise ConfigRejectedError(f"{name}={value} is outside what the camera accepts")
        if not info.capabilities & flags:
            raise CameraControlFailedError(
                f"the camera does not support the requested mode for {name}"
            )
        if not info.minimum <= sent <= info.maximum:
            raise ConfigRejectedError(f"{name}={value} is outside what the camera accepts")
        self._native.write(source, sent, flags)
