# SPDX-License-Identifier: Apache-2.0
"""Locking the camera's parameters, and reading them back to prove it.

A scan is only comparable with its white reference if white balance, exposure and gain
did not move. Opening a video stream can silently put the camera back into automatic
mode, so parameters are written after the stream is running and confirmed by two
read-backs with frames in between.

Backend on macOS: the ``uvc-util`` tool (built from ``tools/uvc-util``), one process per
call.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from vispek_hc import units
from vispek_hc.errors import (
    CameraControlFailedError,
    ConfigRejectedError,
    ControlsUnavailableError,
    choose_one,
)
from vispek_hc.interfaces import CameraControls, ControlInfo
from vispek_hc.models import HC1500, DeviceModel

AWB = "auto-white-balance-temp"
EXPOSURE_MODE = "auto-exposure-mode"
EXPOSURE_AUTOMATIC = 8  # aperture priority: what the camera starts in
FOCUS = "focus-abs"  # the lens is manual: the value read back is not a position; never written
# Automatic modes go off before the values they would otherwise override.
_MANUAL = {AWB: 0, EXPOSURE_MODE: 1, "auto-focus": 0}
_ORDER = (
    AWB,
    "white-balance-temp",
    EXPOSURE_MODE,
    "auto-exposure-priority",
    "exposure-time-abs",
    "gain",
    "auto-focus",
)
CALL_TIMEOUT_S = 8.0
CHECKOUT_SOURCES = Path(__file__).parents[2] / "tools" / "uvc-util"
CHECKOUT_BUILD = CHECKOUT_SOURCES / "build" / "uvc-util"
PACKAGED_SOURCES = Path(__file__).parent / "_uvc_util"  # where a wheel carries them
BUILD_TIMEOUT_S = 300
_HELPER_FILES = ("uvc-util.m", "UVCController.m", "UVCType.m", "UVCValue.m")


@dataclass(frozen=True, slots=True)
class LockReport:
    requested: dict[str, int]
    effective: dict[str, int]  # what the camera settled on; may be a quantised value
    values: dict[str, int]  # every control after locking
    skipped: list[str]  # requested controls this camera does not have
    awb_off: bool
    exposure_manual: bool | None  # None: the camera has no exposure mode control
    # The camera keeps the gain its automatic exposure had chosen when that was switched
    # off, and reports none of it. "dark": the lock was taken in darkness, where that gain
    # is the camera's ceiling and repeatable. "undetermined": anything else.
    gain_state: str = "undetermined"
    dark_level: tuple[float, float, float] | None = None  # the picture before the lock


def _values(info: Mapping[str, ControlInfo]) -> dict[str, int]:
    return {name: control.current for name, control in info.items()}


def lock_parameters(
    controls: CameraControls, wanted: Mapping[str, int], settle: Callable[[], object]
) -> LockReport:
    """Write a parameter profile to a running camera and confirm it by reading back.

    ``settle`` lets a few frames pass; it runs once before writing (the stream has just
    been opened) and once between the two read-backs. A value the camera holds steadily
    is accepted as the effective value even if it differs from the request (exposure is
    quantised); a value that differs between the read-backs is a failure.
    """
    requested = {**wanted, **_MANUAL}
    requested.pop(FOCUS, None)
    names = sorted(requested, key=lambda n: _ORDER.index(n) if n in _ORDER else len(_ORDER))

    settle()
    available = controls.read_all()
    skipped = [name for name in names if name not in available]
    writable = [name for name in names if name in available]
    for name in writable:
        info, value = available[name], requested[name]
        off_grid = info.step and info.minimum is not None and (value - info.minimum) % info.step
        if (
            (info.minimum is not None and value < info.minimum)
            or (info.maximum is not None and value > info.maximum)
            or off_grid
        ):
            raise ConfigRejectedError(
                f"{name}={value} is outside what the camera accepts "
                f"({info.minimum}..{info.maximum}, step {info.step})",
                details={"control": name, "value": value},
            )
    for name in writable:
        controls.write(name, requested[name])

    first = _values(controls.read_all())
    settle()
    second = _values(controls.read_all())
    for name in writable:
        if name not in second or first.get(name) != second[name]:
            raise CameraControlFailedError(
                f"{name} does not hold its value ({first.get(name)} then {second.get(name)})",
                details={"control": name, "requested": requested[name]},
            )
    return LockReport(
        requested=requested,
        effective={name: second[name] for name in writable},
        values=second,
        skipped=skipped,
        awb_off=second.get(AWB) == 0,
        exposure_manual=second[EXPOSURE_MODE] == 1 if EXPOSURE_MODE in second else None,
    )


def changed_controls(before: Mapping[str, int], after: Mapping[str, int]) -> list[str]:
    """Controls whose value differs between two snapshots (the focus read-back is noise)."""
    return [name for name in before if name != FOCUS and before[name] != after.get(name)]


# --- uvc-util --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UvcDevice:
    usb_id: str  # "vid:pid", lower-case hex
    location: str  # USB location id; changes with the port the camera is plugged into
    name: str


def parse_devices(table: str) -> list[UvcDevice]:
    """Rows of ``uvc-util -d``."""
    rows = re.findall(
        r"^\d+\s+0x([0-9a-f]{4}):0x([0-9a-f]{4})\s+(0x[0-9a-f]{8})\s+\S+\s+(.+?)\s*$",
        table,
        flags=re.M | re.I,
    )
    return [UvcDevice(f"{vid}:{pid}".lower(), location, name) for vid, pid, location, name in rows]


def _scalar(text: str) -> int | None:
    if text in ("true", "false"):
        return int(text == "true")
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def parse_controls(listing: str) -> dict[str, ControlInfo]:
    """Single-valued controls from ``uvc-util -S '*'``; multi-component ones are left out."""
    controls = {}
    for name, body in re.findall(r"^([\w-]+) \{\n(.*?)^\}", listing, flags=re.M | re.S):
        fields = {
            key: _scalar(value) for key, value in re.findall(r"^  ([\w-]+): (.+)$", body, re.M)
        }
        current = fields.get("current-value")
        if current is not None:
            controls[name] = ControlInfo(
                current, fields.get("minimum"), fields.get("maximum"), fields.get("step-size")
            )
    return controls


def find_uvc_util() -> Path | None:
    """The uvc-util binary: explicit setting, the user's copy, a source checkout, then PATH."""
    if override := os.environ.get("VISPEK_HC_UVC_UTIL"):
        return Path(override)
    for candidate in (units.home() / "bin" / "uvc-util", CHECKOUT_BUILD):
        if candidate.is_file():
            return candidate
    on_path = shutil.which("uvc-util")
    return Path(on_path) if on_path else None


def uvc_util_sources() -> Path | None:
    """The folder with the helper's source files: inside an installed package, or
    ``tools/uvc-util`` of a source checkout."""
    for folder in (PACKAGED_SOURCES, CHECKOUT_SOURCES):
        if (folder / "src" / _HELPER_FILES[0]).is_file():
            return folder
    return None


def changed_sources(sources: Path) -> list[str]:
    """Files that are missing or differ from ``SHA256SUMS``, which lists every one."""
    changed = []
    for line in (sources / "SHA256SUMS").read_text("ascii").splitlines():
        digest, name = line.split()
        path = sources / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            changed.append(name)
    return changed


def uvc_util_build_command(sources: Path, output: Path) -> list[str]:
    """The compiler call: a universal binary for macOS 13 and later, as ``build.sh`` makes."""
    return [
        "xcrun", "clang", "-fno-objc-arc", "-O2", "-arch", "arm64", "-arch", "x86_64",
        "-mmacosx-version-min=13.0", "-framework", "Foundation", "-framework", "IOKit",
        "-o", str(output), *(str(sources / "src" / name) for name in _HELPER_FILES),
    ]  # fmt: skip


def build_uvc_util(sources: Path | None = None, *, platform: str = sys.platform) -> Path:
    """Compile uvc-util on this Mac and put it where :func:`find_uvc_util` looks first.

    The sources come with the package and are checked against their checksums before the
    compiler sees them. Nothing is downloaded. Needs the Xcode command line tools.
    """
    install = "xcode-select --install"
    if platform != "darwin":
        raise ControlsUnavailableError(
            "uvc-util is the camera-control helper for macOS only",
            hint="Linux: install v4l-utils. Windows: camera controls are not built yet",
        )
    sources = sources or uvc_util_sources()
    if sources is None:
        raise ControlsUnavailableError(
            "the source files of uvc-util are not part of this installation",
            hint="install vispek-hc again",
        )
    if changed := changed_sources(sources):
        raise ControlsUnavailableError(
            f"source files of uvc-util are not the ones that were shipped: {', '.join(changed)}",
            hint="install vispek-hc again",
            details={"changed": changed},
        )
    destination = units.home() / "bin" / "uvc-util"
    destination.parent.mkdir(parents=True, exist_ok=True)
    unfinished = destination.with_name("uvc-util.unfinished")
    try:
        result = subprocess.run(
            uvc_util_build_command(sources, unfinished),
            capture_output=True, text=True, timeout=BUILD_TIMEOUT_S, check=False,
        )  # fmt: skip
        if result.returncode != 0 or not unfinished.is_file():
            raise ControlsUnavailableError(
                f"uvc-util could not be compiled: {result.stderr.strip() or 'no output'}",
                hint=f"the Xcode command line tools are needed: {install}",
            )
        unfinished.chmod(0o755)
        unfinished.replace(destination)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ControlsUnavailableError(
            f"the compiler cannot be run: {error}", hint=f"run {install}"
        ) from error
    finally:
        unfinished.unlink(missing_ok=True)
    return destination


class UvcUtilControls:
    """Camera controls on macOS through uvc-util."""

    backend = "uvc-util"

    def __init__(self, binary: str | Path, device: UvcDevice) -> None:
        self._binary = str(binary)
        self._device = device
        self.device_id = f"uvc:{device.usb_id}:{device.location}"

    @classmethod
    def open(cls, binary: str | Path | None = None, model: DeviceModel = HC1500) -> Self:
        binary = binary or find_uvc_util()
        if binary is None:
            raise ControlsUnavailableError(
                "uvc-util is not built, so camera parameters cannot be locked",
                hint="run 'vispek-hc setup' (it compiles the helper; needs the Xcode command "
                "line tools), or scan with --unlocked",
            )
        devices = parse_devices(_run([str(binary), "-d"]).stdout)
        matches = {d.location: d for d in devices if d.usb_id == model.camera_usb_id}
        location = choose_one(
            None,
            list(matches),
            what=f"UVC camera with USB id {model.camera_usb_id}",
            option="--camera",
            hint="plug the camera in",
            seen={"devices": [device.name for device in devices]},
        )
        return cls(binary, matches[location])

    def read_all(self) -> dict[str, ControlInfo]:
        result = _run([self._binary, "-L", self._device.location, "-S", "*"])
        controls = parse_controls(result.stdout)
        if result.returncode != 0 or "ERROR" in result.stderr or not controls:
            raise CameraControlFailedError(
                f"camera parameters cannot be read: {result.stderr.strip() or 'no output'}",
                hint="the camera may have been unplugged or moved to another USB port",
            )
        return controls

    def write(self, name: str, value: int) -> None:
        result = _run([self._binary, "-L", self._device.location, "-s", f"{name}={value}"])
        if result.returncode != 0 or "ERROR" in result.stderr:
            raise CameraControlFailedError(
                f"camera refused {name}={value}: {result.stderr.strip()}",
                details={"control": name, "value": value},
            )


def _run(argv: list[str], tool: str = "uvc-util") -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=CALL_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ControlsUnavailableError(f"{tool} cannot be run: {error}") from error


# --- v4l2-ctl (Linux) ------------------------------------------------------------------

# The kernel renamed several controls over the years ("exposure_auto" became
# "auto_exposure", ...), so a control is known by its number, and written under the name
# this kernel lists it with. Numbers from linux/v4l2-controls.h.
_V4L2_IDS = {
    0x00980900: "brightness",
    0x00980901: "contrast",
    0x00980902: "saturation",
    0x00980903: "hue",
    0x0098090C: AWB,
    0x00980910: "gamma",
    0x00980913: "gain",
    0x00980918: "power-line-frequency",
    0x0098091A: "white-balance-temp",
    0x0098091B: "sharpness",
    0x0098091C: "backlight-compensation",
    0x009A0901: EXPOSURE_MODE,
    0x009A0902: "exposure-time-abs",
    0x009A0903: "auto-exposure-priority",
    0x009A090A: FOCUS,
    0x009A090C: "auto-focus",
}
# V4L2 numbers the exposure modes 0-3; UVC, and this software, use the bitmap 1, 2, 4, 8.
_V4L2_EXPOSURE_MODE = {0: 2, 1: 1, 2: 4, 3: 8}
_UVC_EXPOSURE_MODE = {uvc: v4l2 for v4l2, uvc in _V4L2_EXPOSURE_MODE.items()}
_V4L2_LINE = re.compile(r"^\s*(\w+) (0x[0-9a-f]{8}) \((\w+)\)\s*:\s*(.*)$")


def parse_v4l2_controls(listing: str) -> dict[str, tuple[str, ControlInfo]]:
    """Controls from ``v4l2-ctl --list-ctrls``, keyed by the names uvc-util uses.

    Each entry holds the name this kernel gives the control (what ``--set-ctrl`` takes)
    and its values. The exposure mode is translated to the UVC numbers.
    """
    controls: dict[str, tuple[str, ControlInfo]] = {}
    for line in listing.splitlines():
        match = _V4L2_LINE.match(line)
        name = _V4L2_IDS.get(int(match[2], 16)) if match else None
        if match is None or name is None:
            continue
        fields = {key: int(value) for key, value in re.findall(r"(\w+)=(-?\d+)", match[4])}
        if "value" not in fields:
            continue
        if name == EXPOSURE_MODE:
            info = ControlInfo(_V4L2_EXPOSURE_MODE.get(fields["value"], fields["value"]))
        elif match[3] == "bool":
            info = ControlInfo(fields["value"])
        else:
            info = ControlInfo(
                fields["value"], fields.get("min"), fields.get("max"), fields.get("step", 1)
            )
        controls[name] = (match[1], info)
    return controls


class V4l2Controls:
    """Camera controls on Linux through ``v4l2-ctl`` (package v4l-utils)."""

    backend = "v4l2-ctl"

    def __init__(self, binary: str | Path, device: str) -> None:
        self._binary = str(binary)
        self._device = device
        self._names: dict[str, str] = {}  # uvc-util name -> this kernel's name
        self.device_id = f"v4l2:{device}"

    @classmethod
    def open(cls, device: str, binary: str | Path | None = None) -> Self:
        binary = binary or shutil.which("v4l2-ctl")
        if binary is None:
            raise ControlsUnavailableError(
                "v4l2-ctl is not installed, so camera parameters cannot be locked",
                hint="install v4l-utils (Debian, Ubuntu: sudo apt install v4l-utils), or scan "
                "with --unlocked",
            )
        return cls(binary, device)

    def read_all(self) -> dict[str, ControlInfo]:
        result = _run([self._binary, "-d", self._device, "--list-ctrls"], "v4l2-ctl")
        controls = parse_v4l2_controls(result.stdout)
        if result.returncode != 0 or not controls:
            raise CameraControlFailedError(
                f"camera parameters cannot be read: {result.stderr.strip() or 'no output'}",
                hint="the camera may have been unplugged, or this user may not open "
                f"{self._device} (group 'video')",
            )
        self._names = {name: listed for name, (listed, _) in controls.items()}
        return {name: info for name, (_, info) in controls.items()}

    def write(self, name: str, value: int) -> None:
        if name not in self._names:
            self.read_all()
        if name not in self._names:
            raise CameraControlFailedError(
                f"the camera has no control {name}", details={"control": name, "value": value}
            )
        sent = _UVC_EXPOSURE_MODE.get(value, value) if name == EXPOSURE_MODE else value
        setting = f"{self._names[name]}={sent}"
        result = _run([self._binary, "-d", self._device, "--set-ctrl", setting], "v4l2-ctl")
        if result.returncode != 0 or result.stderr.strip():  # it can fail and still exit 0
            raise CameraControlFailedError(
                f"camera refused {name}={value}: {result.stderr.strip()}",
                details={"control": name, "value": value},
            )
