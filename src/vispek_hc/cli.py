# SPDX-License-Identifier: Apache-2.0
"""The ``vispek-hc`` command line.

Contract for scripts and coding agents:

* ``--json`` puts exactly one JSON object (schema ``vispek-hc.cli/1``) on stdout; logs and
  progress go to stderr. The one exception is ``--help``, which always prints usage text.
* The exit code comes from the error's family (see ``vispek_hc.errors``).
* Nothing here ever prompts for input.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import numpy as np

from vispek_hc import (
    __version__,
    controls,
    illuminator,
    safety,
    samples,
    session,
    sweep,
    units,
    video,
    views,
)
from vispek_hc.calibration import Calibration, make_calibration
from vispek_hc.cube import (
    Capture,
    Cube,
    check_target,
    export_envi,
    export_tiff,
    load_capture,
    read_events,
    repair,
)
from vispek_hc.errors import (
    AWBNotConfirmedError,
    BackendMissingError,
    CalibrationQualityError,
    ConfigRejectedError,
    ControlsUnavailableError,
    ExposureNotConfirmedError,
    InvalidStateError,
    OperationCancelledError,
    PermissionDeniedError,
    UsageError,
    VispekHCError,
)
from vispek_hc.illuminator import UV_MAX_ON_S, Illuminator
from vispek_hc.models import HC1500
from vispek_hc.protocol import PWM_RANGE, encode_off, encode_on
from vispek_hc.reflectance import apply_calibration
from vispek_hc.sim import SimulatedRig
from vispek_hc.spectra import Region, spectrum, write_csv
from vispek_hc.sweep import ScanSettings

ENVELOPE_SCHEMA = "vispek-hc.cli/1"
PLATFORM = sys.platform
RIPPLE_EXPOSURE = 63  # below 6.3 ms the 16 kHz PWM shows as more than 1 % row-to-row ripple


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a command hands back: the envelope's ``data`` and any warnings."""

    data: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


class _VersionRequested(Exception):
    pass


class _VersionAction(argparse.Action):
    def __call__(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise _VersionRequested


class _Parser(argparse.ArgumentParser):
    def __init__(self, **kwargs: Any) -> None:
        # No abbreviations: "--js" must not mean "--json", today or after new flags arrive.
        super().__init__(**{"allow_abbrev": False, **kwargs})

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, hint=f"run '{self.prog} --help'")


def _global_options(*, root: bool) -> _Parser:
    """Options accepted both before and after the command name.

    On sub-commands the defaults are suppressed, so a flag given before the command is not
    overwritten by the sub-parser's default.
    """

    def default(value: Any) -> Any:
        return value if root else argparse.SUPPRESS

    parent = _Parser(add_help=False)
    group = parent.add_argument_group("global options")
    group.add_argument(
        "--json", action="store_true", default=default(False), help="print one JSON envelope"
    )
    group.add_argument(
        "--simulate",
        action="store_true",
        default=default(False),
        help="use the simulated device instead of hardware",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=default(False),
        help="print the plan without touching the device",
    )
    group.add_argument(
        "--unit",
        default=default(session.DEFAULT_UNIT),
        help=f"label of this device (default: {session.DEFAULT_UNIT})",
    )
    group.add_argument("--port", default=default(None), help="serial port of the light source")
    group.add_argument("--camera", default=default(None), help="camera name")
    group.add_argument(
        "-v", "--verbose", action="count", default=default(0), help="-v info, -vv debug (stderr)"
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="vispek-hc",
        description="Vispek HC1500 multispectral camera.",
        parents=[_global_options(root=True)],
    )
    parser.add_argument(
        "--version", action=_VersionAction, nargs=0, help="print the version and exit"
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    shared = [_global_options(root=False)]

    commands.add_parser("check", parents=shared, help="check this computer and the device")

    devices = commands.add_parser("devices", parents=shared, help="list serial ports and cameras")
    devices.add_argument(
        "--pair", action="store_true", help="blink LED 6 and confirm that the camera sees it"
    )

    led = commands.add_parser("led", parents=shared, help="switch LEDs by hand")
    actions = led.add_subparsers(dest="action", metavar="ACTION", required=True)
    on = actions.add_parser(
        "on",
        parents=shared,
        help="light an LED and keep it lit until Ctrl-C or --hold expires",
        description="Light one LED (LED PWM) or every visible LED (--all, at each LED's usual "
        "PWM). The command keeps the port and blocks; when it ends, every LED is switched off.",
    )
    on.add_argument("led_id", nargs="?", type=int, metavar="LED", help="LED number, 1-17")
    on.add_argument("pwm", nargs="?", type=int, metavar="PWM", help="brightness, 0-1023")
    on.add_argument("--all", action="store_true", help="every LED allowed by --allow-uv")
    on.add_argument("--allow-uv", action="store_true", help="permit LEDs 1-4 (read SAFETY.md)")
    on.add_argument("--hold", type=_seconds, metavar="SECONDS", help="switch off after this long")
    off = actions.add_parser(
        "off",
        parents=shared,
        help="switch LEDs off",
        description="Opening the light source switches every LED off, so this always leaves "
        "all of them off; the LED argument is checked and reported.",
    )
    off.add_argument("led_id", nargs="?", type=int, metavar="LED", help="LED number, 1-17")
    off.add_argument("--all", action="store_true", help="every LED")
    _add_data_commands(commands, shared)
    return parser


def _add_data_commands(commands: Any, shared: list[_Parser]) -> None:
    def scan_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("-o", "--output", type=Path, required=True, help="new capture folder")
        command.add_argument("--channels", type=_channels, help="LEDs, e.g. 5-17 (the default)")
        command.add_argument("--allow-uv", action="store_true", help="permit LEDs 1-4")
        command.add_argument("--average", type=int, default=1, help="scans averaged per output")
        command.add_argument("--overwrite", action="store_true", help="replace an existing folder")
        command.add_argument("--unlocked", action="store_true",
                             help="scan even if camera parameters cannot be locked")  # fmt: skip
        command.add_argument("--pwm", type=int, help="one PWM for every channel")
        command.add_argument("--pwm-file", type=Path, help='JSON file, e.g. {"5": 24, "6": 224}')
        command.add_argument("--min-on", type=_min_on, default="auto", help="'auto' or ms")
        command.add_argument("--fast", action="store_true", help="MJPEG at 60 fps, whole scan")
        command.add_argument("--size", type=_size, help="picture size, e.g. 1280x720")
        command.add_argument("--fps", type=float)
        command.add_argument("--source", choices=["uncompressed", "mjpeg"], default="uncompressed")
        command.add_argument("--reduction", choices=["dominant", "luma"], default="dominant")
        command.add_argument(
            "--setup-label",
            metavar="TEXT",
            help="lens and distance, in your words; kept with the capture",
        )
        command.add_argument("--enclosure", choices=["darkbox", "open", "unknown"],
                             default="unknown", help="where the scan was made")  # fmt: skip

    record = commands.add_parser("record", parents=shared, help="record a scan or plain frames")
    scan_options(record)
    record.add_argument("--mode", choices=["scan", "single", "dark", "video"], default="scan")
    record.add_argument("--cycles", type=int, default=1, help="outputs to record (0: until Ctrl-C)")
    record.add_argument("--no-preview", action="store_true", help="do not write preview.png")

    reference = commands.add_parser("reference", parents=shared, help="record a white reference")
    kinds = reference.add_subparsers(dest="kind", metavar="KIND", required=True)
    white = kinds.add_parser("white", parents=shared, help="scan the white board")
    scan_options(white)
    white.add_argument("--auto-pwm", action="store_true", help="first find a PWM per channel")
    white.add_argument("--white-roi", type=_numbers(4, float), metavar="X,Y,W,H",
                       help="the white board, as fractions of the picture")  # fmt: skip

    tune = commands.add_parser("tune", parents=shared, help="learn PWM and timing on a white board")
    scan_options(tune)

    repair_ = commands.add_parser("repair", parents=shared, help="rebuild capture.json from events")
    repair_.add_argument("capture", type=Path)

    calibrate = commands.add_parser("calibrate", parents=shared, help="make a calibration")
    calibrate.add_argument("--white", type=Path, nargs="+", required=True, metavar="CAPTURE")
    calibrate.add_argument("-o", "--output", type=Path, required=True, help="calibration folder")
    calibrate.add_argument("--white-roi", type=_numbers(4, float), metavar="X,Y,W,H")

    def cube_options(command: argparse.ArgumentParser, calibration_required: bool = False) -> None:
        command.add_argument("capture", type=Path, help="capture folder")
        command.add_argument("--calib", type=Path, required=calibration_required,
                             help="calibration folder: work on relative reflectance")  # fmt: skip
        command.add_argument("--force", action="store_true",
                             help="apply a calibration made with other settings")  # fmt: skip

    reflectance = commands.add_parser("reflectance", parents=shared, help="apply a calibration")
    cube_options(reflectance, calibration_required=True)
    reflectance.add_argument("-o", "--output", type=Path, required=True, help="cube.npz to write")
    reflectance.add_argument("--legacy", type=Path, metavar="DARK_CAPTURE",
                             help="legacy definition, with this covered dark frame")  # fmt: skip

    view = commands.add_parser("view", parents=shared, help="render a picture")
    cube_options(view)
    view.add_argument("--mode", default="rgb", help="rgb cir uv ndvi ndwi pca heat band:led_NN")
    view.add_argument("-o", "--output", type=Path, required=True, help="PNG to write")
    view.add_argument("--allow-uncalibrated", action="store_true")
    view.add_argument("--low", type=float)
    view.add_argument("--high", type=float)
    view.add_argument("--gamma", type=float, default=1.0)
    view.add_argument("--palette", default="auto",
                      choices=["auto", "gray", "spectral", "viridis", "classes"])  # fmt: skip

    spectrum_ = commands.add_parser("spectrum", parents=shared, help="spectrum of a point or box")
    cube_options(spectrum_)
    spectrum_.add_argument("--xy", type=_numbers(2, int), required=True, metavar="X,Y")
    spectrum_.add_argument("--wh", type=_numbers(2, int), metavar="W,H", help="box size")
    spectrum_.add_argument("--csv", type=Path, help="also write CSV")

    export = commands.add_parser("export", parents=shared, help="export to ENVI or TIFF")
    cube_options(export)
    export.add_argument("--format", choices=["envi", "tiff"], required=True)
    export.add_argument("-o", "--output", type=Path, required=True, help="folder to write")

    demo = commands.add_parser("demo", parents=shared, help="offline tour on the simulated device")
    demo.add_argument("-o", "--output", type=Path, default=Path("vispek-hc-demo"))

    commands.add_parser(
        "setup", parents=shared, help="prepare this computer: compile the camera-control helper"
    )


# --- check -----------------------------------------------------------------------------


def has_xcode_tools() -> bool:
    try:
        result = subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def list_cameras_without_ffmpeg() -> list[str]:
    """Camera names from the operating system alone (macOS only), for diagnosis."""
    if PLATFORM != "darwin":
        return []
    try:
        result = subprocess.run(
            ["system_profiler", "SPCameraDataType", "-json"],
            capture_output=True, text=True, errors="replace", timeout=12, check=False,
        )  # fmt: skip
        return [entry["_name"] for entry in json.loads(result.stdout)["SPCameraDataType"]]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return []


def find_v4l2_ctl() -> str | None:
    return shutil.which("v4l2-ctl")


BRLTTY_RULES = Path("/usr/lib/udev/rules.d/85-brltty.rules")


def linux_host(groups: Any = None, rules: Path = BRLTTY_RULES) -> dict[str, bool | None]:
    """Two things that keep the light source's serial port away on Linux: this user is
    not in the group that may open serial ports (None: the system has no such group), and
    brltty, a braille display driver that claims CH341 adapters."""
    if groups is None:
        import grp as groups  # not on Windows; this function is only called on Linux
    try:
        gid = groups.getgrnam("dialout").gr_gid
        # These POSIX functions are absent from Windows' type surface.
        posix_os: Any = os
        member: bool | None = posix_os.geteuid() == 0 or gid in posix_os.getgroups()
    except KeyError:
        member = None
    return {"dialout": member, "brltty": shutil.which("brltty") is not None or rules.exists()}


def ripple_warning(exposure_time_abs: int) -> str | None:
    if exposure_time_abs >= RIPPLE_EXPOSURE:
        return None
    return (
        f"exposure {exposure_time_abs / 10:g} ms is short enough for the 16 kHz LED dimming to "
        "show as horizontal ripple of more than 1 %; use 6.3 ms (63) or longer"
    )


def _candidates() -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        cameras = video.list_cameras()
    except BackendMissingError:
        cameras = list_cameras_without_ffmpeg()
    return (
        {"candidates": illuminator.find_ports()},
        {"all": cameras, "matching": video.matching_cameras(cameras)},
    )


def _pair_data(pair: session.PairResult) -> dict[str, Any]:
    return {
        "paired": pair.paired,
        "led_id": pair.led_id,
        "pwm": pair.pwm,
        "rise_dn": round(pair.rise_dn, 2),
        "latency_ms": None if pair.latency_s is None else round(pair.latency_s * 1000, 1),
        "fps": pair.fps,
    }


def _lock_report(
    opened: session.Session, problems: list[VispekHCError], warnings: list[str]
) -> dict[str, Any]:
    try:
        report = opened.lock_controls()
    except VispekHCError as error:
        if not any(type(problem) is type(error) for problem in problems):
            problems.append(error)
        return {
            "locked": False, "awb_off": None, "exposure_manual": None,
            "exposure_time_abs": None, "skipped": [], "gain_state": None,
        }  # fmt: skip
    if not report.awb_off:
        problems.append(AWBNotConfirmedError("automatic white balance could not be switched off"))
    if report.exposure_manual is False:
        problems.append(ExposureNotConfirmedError("manual exposure could not be selected"))
    if report.skipped:
        warnings.append(f"the camera has no control named: {', '.join(report.skipped)}")
    adjusted = [
        f"{name}: asked {report.requested[name]}, got {value}"
        for name, value in report.effective.items()
        if value != report.requested[name]
    ]
    if adjusted:
        warnings.append(f"the camera settled on other values ({'; '.join(adjusted)})")
    exposure = report.effective.get("exposure-time-abs")
    if exposure is not None and (warning := ripple_warning(exposure)):
        warnings.append(warning)
    return {
        "locked": report.awb_off and report.exposure_manual is not False,
        "gain_state": report.gain_state,
        "awb_off": report.awb_off,
        "exposure_manual": report.exposure_manual,
        "exposure_time_abs": exposure,
        "skipped": report.skipped,
    }


def _device_report(
    opened: session.Session, problems: list[VispekHCError], warnings: list[str]
) -> dict[str, Any]:
    locked = _lock_report(opened, problems, warnings)
    pair = opened.pair_probe()
    if not pair.paired:
        problems.append(
            InvalidStateError(
                "the camera did not react to LED 6",
                hint="the serial port and the camera may belong to different devices, the lens "
                "may be covered, or USB1 may not power the light source",
            )
        )
    black = opened.black_level()
    expected = (HC1500.black_level,) * 3
    if black != expected:
        problems.append(
            InvalidStateError(
                f"black level is {black}, expected {expected}",
                hint="light is leaking in (cover the lens to test), or the pixel range "
                "conversion does not match this camera",
            )
        )
    return {
        **opened.describe(),
        "controls": locked,
        "pair": _pair_data(pair),
        "black_level": black,
        "ack": opened.light.ack_stats(),
    }


def _check(args: argparse.Namespace) -> Outcome:
    problems: list[VispekHCError] = []
    warnings: list[str] = []
    home = units.home()
    existing = next(path for path in (home, *home.parents) if path.exists())
    data: dict[str, Any] = {
        "version": __version__,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": PLATFORM,
            "machine": platform.machine(),
            "tier": 1 if (PLATFORM, platform.machine()) == ("darwin", "arm64") else 2,
        },
        "model": HC1500.name,
        "unit": args.unit,
        "led_table": {"version": HC1500.led_table.version, "verified": HC1500.led_table.verified},
        "home": str(home),
        "disk_free_bytes": shutil.disk_usage(existing).free,
        "ffmpeg": {"found": False, "version": None, "fps_flag": None},
        "controls": None,
        "device": None,
    }
    try:
        version = video.ffmpeg_version()
        data["ffmpeg"] = {
            "found": True,
            "version": ".".join(map(str, version)) if version else "development build",
            "fps_flag": video.passthrough_flags(version)[0],
        }
    except BackendMissingError as error:
        problems.append(error)
    data["serial"], data["cameras"] = _candidates()
    if PLATFORM == "darwin":
        binary = controls.find_uvc_util()
        tools = has_xcode_tools()
        data["controls"] = {
            "backend": "uvc-util",
            "binary": str(binary) if binary else None,
            "xcode_tools": tools,
        }
        if binary is None:
            build = "run 'vispek-hc setup'"
            problems.append(
                ControlsUnavailableError(
                    "uvc-util is not built, so camera parameters cannot be locked",
                    hint=build if tools else f"run 'xcode-select --install', then {build}",
                )
            )
    if PLATFORM == "linux":
        binary_path, host = find_v4l2_ctl(), linux_host()
        data["controls"] = {"backend": "v4l2-ctl", "binary": binary_path, **host}
        if binary_path is None:
            problems.append(
                ControlsUnavailableError(
                    "v4l2-ctl is not installed, so camera parameters cannot be locked",
                    hint="install v4l-utils (Debian, Ubuntu: sudo apt install v4l-utils)",
                )
            )
        if host["dialout"] is False and not args.simulate:
            warnings.append(
                "this user is not in the group 'dialout' and may not open the light source's "
                "serial port: run 'sudo usermod -aG dialout $USER' and log in again, or "
                "install tools/99-vispek-hc.rules"
            )
        if host["brltty"] and not args.simulate:
            warnings.append(
                "brltty is installed: it claims CH341 serial adapters, and the light source's "
                "port disappears. Remove it ('sudo apt remove brltty') if you use no braille "
                "display"
            )
    if PLATFORM == "win32":
        from vispek_hc._windows_controls import available

        data["controls"] = {"backend": "directshow", "binary": None}
        if not available():
            problems.append(
                ControlsUnavailableError(
                    "Windows DirectShow camera controls are unavailable",
                    hint="DirectShow controls require a Windows desktop installation",
                )
            )
        if not data["serial"]["candidates"] and not args.simulate:
            warnings.append(
                "no serial port: the light source's CH341 adapter needs the driver of its "
                "maker (WCH). Without it the adapter is listed under 'Other devices' in "
                "Device Manager"
            )
    if args.simulate:
        problems.clear()  # what this computer lacks does not matter to the simulated device
    else:
        for choose, named, found in (
            (illuminator.choose_port, args.port, data["serial"]["candidates"]),
            (video.choose_camera, args.camera, data["cameras"]["all"]),
        ):
            try:
                choose(named, found)
            except VispekHCError as error:
                problems.append(error)
    if not args.dry_run and not problems:
        try:
            with session.open_session(
                simulate=args.simulate, port=args.port, camera=args.camera, unit=args.unit
            ) as opened:
                data["device"] = _device_report(opened, problems, warnings)
        except VispekHCError as error:
            problems.append(error)
    data["problems"] = [problem.to_dict() for problem in problems]
    # None: nothing is known to be wrong, but the device itself was not looked at (--dry-run).
    data["ready"] = False if problems else (None if data["device"] is None else True)
    return Outcome(data, warnings)


# --- devices ---------------------------------------------------------------------------


def _devices(args: argparse.Namespace) -> Outcome:
    serial_ports, cameras = _candidates()
    data: dict[str, Any] = {"serial": serial_ports, "cameras": cameras}
    if args.pair and args.dry_run:
        data["plan"] = {
            "action": "pair",
            "led_id": session.PROBE_LED,
            "pwm": _profile(args).pwm_by_led.get(
                session.PROBE_LED, HC1500.led(session.PROBE_LED).starting_pwm
            ),
            "on_s": session.PROBE_ON_S,
        }
    elif args.pair:
        with session.open_session(
            simulate=args.simulate, port=args.port, camera=args.camera, unit=args.unit
        ) as opened:
            data["pair"] = _pair_data(opened.pair_probe())
    return Outcome(data)


# --- led -------------------------------------------------------------------------------


def wait_for_interrupt() -> None:
    """Block until Ctrl-C. (A plain ``Event.wait()`` cannot be interrupted on Windows.)"""
    while True:
        time.sleep(0.2)


def _seconds(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and 0 < value <= 86400):
        raise argparse.ArgumentTypeError("must be a positive number of seconds, at most a day")
    return value


def _open_light(args: argparse.Namespace, allow_uv: bool) -> Illuminator:
    if args.simulate:
        light = SimulatedRig(allow_uv=allow_uv).light
        light.connect(baud=115200)
        return light
    return Illuminator.open(args.port, allow_uv=allow_uv)


def _led_on(args: argparse.Namespace) -> Outcome:
    if args.all == (args.led_id is not None) or (args.led_id is not None and args.pwm is None):
        raise UsageError("give either LED and PWM, or --all", hint="vispek-hc led on --help")
    if args.all:
        profile = units.UnitProfile("sim") if args.simulate else units.load_profile(args.unit)
        ids = [led.led_id for led in HC1500.led_table.leds]
        if not args.allow_uv:
            ids = list(HC1500.default_channels)
        targets = [(n, profile.pwm_by_led.get(n, HC1500.led(n).starting_pwm)) for n in ids]
    else:
        targets = [(args.led_id, args.pwm)]
    safety.check_channels(HC1500, [led_id for led_id, _ in targets], allow_uv=args.allow_uv)
    for _, pwm in targets:
        if pwm not in PWM_RANGE:
            raise ConfigRejectedError(f"PWM {pwm} is outside 0-1023", details={"pwm": pwm})
    has_uv = any(HC1500.led(led_id).needs_uv_permission for led_id, _ in targets)
    if has_uv and (args.hold is None or args.hold > UV_MAX_ON_S):
        raise ConfigRejectedError(
            f"ultraviolet LEDs need --hold SECONDS, at most {UV_MAX_ON_S:g}",
            details={"hold": args.hold},
        )
    leds = [{"led_id": n, "nm": HC1500.led(n).nm, "pwm": pwm} for n, pwm in targets]
    if args.dry_run:
        frames = [encode_on(n, pwm).hex(" ") for n, pwm in targets]
        offs = [{**led, "pwm": None, "frame": encode_off(led["led_id"]).hex(" ")} for led in leds]
        plan = [{**led, "frame": frame} for led, frame in zip(leds, frames, strict=True)]
        return Outcome({"dry_run": True, "action": "on", "plan": plan + offs, "hold_s": args.hold})
    warnings = []
    if len(targets) > 1:
        warnings.append(
            f"{len(targets)} LEDs lit together may draw more current than the USB1 port "
            "supplies; a brown-out shows as a serial disconnect"
        )
    ended_by = "hold"
    with _open_light(args, args.allow_uv) as light:
        try:
            for led_id, pwm in targets:
                light.on(led_id, pwm, exclusive=False)
            if args.hold is None:
                wait_for_interrupt()
            else:
                light.clock.sleep(args.hold)
        except KeyboardInterrupt:
            ended_by = "interrupt"
    # Reaching this line means the all-off on leaving the block was confirmed.
    return Outcome(
        {"action": "on", "leds": leds, "hold_s": args.hold, "ended_by": ended_by, "all_off": True},
        warnings,
    )


def _led_off(args: argparse.Namespace) -> Outcome:
    if args.all == (args.led_id is not None):
        raise UsageError("give either an LED or --all", hint="vispek-hc led off --help")
    ids = [led.led_id for led in HC1500.led_table.leds] if args.all else [args.led_id]
    for led_id in ids:
        HC1500.led(led_id)
    if args.dry_run:
        plan = [{"led_id": n, "frame": encode_off(n).hex(" ")} for n in ids]
        return Outcome({"dry_run": True, "action": "off", "plan": plan})
    with _open_light(args, allow_uv=False):
        pass  # opening switches every LED off, and so does leaving
    return Outcome({"action": "off", "leds": ids, "all_off": True})


def _led(args: argparse.Namespace) -> Outcome:
    return _led_on(args) if args.action == "on" else _led_off(args)


# --- recording -------------------------------------------------------------------------

DEMO_TOLERANCE = 0.01  # 8-bit values: one code value of 130 or more is under 0.8 %
PWM_FILE_LIMIT = 4096


def _channels(text: str) -> tuple[int, ...]:
    """'5-17', '5,7,9' or '1-4,9' as LED numbers."""
    numbers: list[int] = []
    for part in text.split(","):
        first, _, last = part.partition("-")
        try:
            numbers += range(int(first), int(last or first) + 1)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{part}' is not an LED number or range") from None
    if not numbers:
        raise argparse.ArgumentTypeError("no LED given")
    return tuple(numbers)


def _numbers(count: int, kind: type) -> Callable[[str], tuple[Any, ...]]:
    def parse(text: str) -> tuple[Any, ...]:
        parts = text.split(",")
        if len(parts) != count:
            raise argparse.ArgumentTypeError(f"expected {count} comma-separated numbers")
        try:
            return tuple(kind(part) for part in parts)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{text}' is not {count} numbers") from None

    return parse


def _min_on(text: str) -> int | str:
    if text == "auto":
        return text
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("give 'auto' or milliseconds") from None


def _size(text: str) -> tuple[int, int]:
    """'1280x720' (or '1280,720') as width and height."""
    width, separator, height = text.lower().replace(",", "x").partition("x")
    try:
        if not separator:
            raise ValueError
        return int(width), int(height)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a size like 1280x720") from None


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("an LED number appears twice")
    return dict(pairs)


def _pwm_from_file(path: Path) -> dict[int, int]:
    """Per-LED PWM from a small JSON object, for example {"5": 24, "6": 224}."""
    try:
        if path.stat().st_size > PWM_FILE_LIMIT:
            raise ValueError("the file is larger than 4 KB")
        stored = json.loads(path.read_text("utf-8"), object_pairs_hook=_unique)
        pwm = {int(led_id): value for led_id, value in stored.items()}
        if len(pwm) != len(stored):
            raise ValueError("an LED number appears twice")
        if not all(type(v) is int and 0 <= v <= 1023 and 1 <= n <= 17 for n, v in pwm.items()):
            raise ValueError("LED numbers must be 1-17 and PWM values whole numbers 0-1023")
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise ConfigRejectedError(f"PWM file {path.name} is not usable: {reason}") from error
    except (ValueError, AttributeError) as error:
        raise ConfigRejectedError(f"PWM file {path.name} is not usable: {error}") from error
    return pwm


def _profile(args: argparse.Namespace) -> units.UnitProfile:
    return units.UnitProfile("sim") if args.simulate else units.load_profile(args.unit)


def _settings(args: argparse.Namespace, **fixed: Any) -> ScanSettings:
    """The scan a command asks for, checked as far as it can be without a device."""
    if args.pwm is not None and args.pwm_file:
        raise UsageError("give --pwm or --pwm-file, not both")
    settings = ScanSettings(
        channels=args.channels,
        pwm=_pwm_from_file(args.pwm_file) if args.pwm_file else args.pwm,
        min_on_ms=args.min_on,
        average=args.average,
        cycles=getattr(args, "cycles", 1),
        reduction=args.reduction,
        require_awb_off=not args.unlocked,
        white_roi=getattr(args, "white_roi", None),
        setup_label=args.setup_label,
        enclosure=args.enclosure,
        **fixed,
    )
    # Channels, UV permission, PWM values and the output folder are refused here, before
    # any device is opened and before the pairing probe lights its LED.
    sweep.check_plan(settings, allow_uv=args.allow_uv, profile_pwm=_profile(args).pwm_by_led)
    check_target(args.output, args.overwrite)
    return settings


def _open(args: argparse.Namespace) -> session.Session:
    fast = getattr(args, "fast", False)
    return session.open_session(
        simulate=args.simulate,
        port=args.port,
        camera=args.camera,
        unit=args.unit,
        allow_uv=getattr(args, "allow_uv", False),
        size=getattr(args, "size", None) or HC1500.default_size,
        fps=60 if fast else getattr(args, "fps", None) or HC1500.default_fps,
        source="mjpeg" if fast else getattr(args, "source", "uncompressed"),
    )


@contextlib.contextmanager
def _stop_on_interrupt() -> Iterator[threading.Event]:
    """While a recording runs, the first Ctrl-C asks it to end in good order (LED off,
    files closed) and a second one interrupts it."""
    stop = threading.Event()
    if threading.current_thread() is not threading.main_thread():
        yield stop
        return

    def ask(signum: int, frame: Any) -> None:
        if not stop.is_set():
            stop.set()
        elif callable(previous):  # the handler that switches every LED off at once
            previous(signum, frame)
        else:
            raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, ask)
    try:
        yield stop
    finally:
        signal.signal(signal.SIGINT, previous)


def _events(directory: Path, kind: str) -> list[dict[str, Any]]:
    return [event for event in read_events(directory) if event["type"] == kind]


def _summary(capture: Capture, warnings: list[str]) -> dict[str, Any]:
    for kind, text in (
        ("saturation_warning", "clipped pixels"),
        ("no_led_response", "no light seen"),
    ):
        leds = sorted({event["led_id"] for event in _events(capture.path, kind)})
        if leds:
            warnings.append(f"{text} under LED {', '.join(map(str, leds))}")
    exposure = (capture.document["config"].get("exposure") or {}).get("effective")
    if exposure is not None and (ripple := ripple_warning(exposure)):
        warnings.append(ripple)
    recording = capture.document["recording"]
    return {
        "capture": str(capture.path),
        "capture_id": capture.capture_id,
        "mode": capture.mode,
        "role": recording["role"],
        "status": capture.status,
        "groups_done": recording["groups_done"],
        "outputs": capture.outputs if capture.mode == "scan" else None,
        "channels": capture.document["config"].get("channels"),
    }


def _scan(
    args: argparse.Namespace, settings: ScanSettings, auto_pwm: bool = False
) -> tuple[Capture, list[str], dict[int, int] | None]:
    """Open the device and record one scan capture; the pairing probe runs first if needed."""
    warnings: list[str] = []
    progress = logging.getLogger("vispek_hc.progress")
    with _open(args) as opened, _stop_on_interrupt() as stop:
        if opened.unit.pipeline_latency_ms is None and not opened.pair_probe().paired:
            warnings.append(
                f"the camera did not react to the probe LED; timing falls back to "
                f"{sweep.FALLBACK_MIN_ON_MS} ms"
            )
        # What the search finds is kept only once the reference recorded with it succeeded.
        chosen = sweep.auto_pwm(opened, settings, stop, warnings, store=False) if auto_pwm else None
        if chosen:
            settings = replace(settings, pwm=chosen)
        capture = sweep.record(
            opened, args.output, settings, overwrite=args.overwrite, stop=stop,
            progress=lambda update: progress.info("band %(band)d of %(bands)d", update),
        )  # fmt: skip
        if chosen:
            sweep.keep_pwm(opened, chosen)
    return capture, warnings, chosen


def _dry_run(args: argparse.Namespace, settings: ScanSettings, **extra: Any) -> Outcome:
    """The plan of a scan, made from the unit's profile without opening a device."""
    stand_in: Any = SimpleNamespace(
        model=HC1500, unit=_profile(args), lock_report=None,
        light=SimpleNamespace(allow_uv=args.allow_uv),
        frames=SimpleNamespace(measured_fps=None),
    )  # fmt: skip
    planned = sweep.plan(stand_in, settings)
    per_channel = [
        planned.min_on_s[led.led_id] + 2 * planned.period_s + planned.dark_pause_s + 0.05
        for led in planned.leds
    ]
    return Outcome(
        {
            "dry_run": True,
            "mode": "scan",
            "role": settings.role,
            "channels": [led.led_id for led in planned.leds],
            "pwm_by_led": {str(n): pwm for n, pwm in planned.pwm.items()},
            "min_on_ms": {str(n): round(s * 1000) for n, s in planned.min_on_s.items()},
            "estimated_seconds": round(
                (sum(per_channel) + 0.9) * settings.average * max(settings.cycles, 1), 1
            ),
            **extra,
        }
    )


def _record(args: argparse.Namespace) -> Outcome:
    if args.mode == "scan":
        settings = _settings(args)
        if args.dry_run:
            return _dry_run(args, settings)
    else:
        check_target(args.output, args.overwrite)
        if args.dry_run:
            return Outcome({"dry_run": True, "mode": args.mode, "frames": args.cycles})
    warnings: list[str] = []
    if args.mode == "scan":
        capture, warnings, _ = _scan(args, settings)
    else:
        with _open(args) as opened, _stop_on_interrupt() as stop:
            capture = sweep.record_frames(
                opened, args.output, args.mode, count=args.cycles, overwrite=args.overwrite,
                stop=stop, setup_label=args.setup_label, enclosure=args.enclosure,
            )  # fmt: skip
    data = _summary(capture, warnings)
    data["preview"] = None
    if args.mode == "scan" and not args.no_preview and capture.outputs:
        cube = capture.cube()
        if all(band in [b.band_id for b in cube.bands] for band in views.COMPOSITES["rgb"]):
            views.save_png(views.render(cube, "rgb"), capture.path / "preview.png")
            data["preview"] = "preview.png"
    return Outcome(data, warnings)


def _reference(args: argparse.Namespace) -> Outcome:
    settings = _settings(args, role="white")
    if args.dry_run:
        return _dry_run(args, settings, auto_pwm=args.auto_pwm)
    capture, warnings, chosen = _scan(args, settings, auto_pwm=args.auto_pwm)
    data = _summary(capture, warnings)
    data["auto_pwm"] = {str(n): pwm for n, pwm in chosen.items()} if chosen else None
    return Outcome(data, warnings)


def _tune(args: argparse.Namespace) -> Outcome:
    settings = _settings(args, role="white")
    if args.dry_run:
        return _dry_run(args, settings, auto_pwm=True, saves_to_profile=not args.simulate)
    warnings: list[str] = []
    with _open(args) as opened, _stop_on_interrupt() as stop:
        opened.pair_probe(relearn=True)  # tuning learns the timing of this unit afresh
        capture = sweep.tune(
            opened, args.output, settings, overwrite=args.overwrite, stop=stop, notes=warnings
        )
        unit = opened.unit
    data = _summary(capture, warnings)
    data["pwm_by_led"] = {str(n): pwm for n, pwm in unit.pwm_by_led.items()}
    data["min_on_ms_by_led"] = {str(n): ms for n, ms in unit.min_on_ms_by_led.items()}
    data["saved_to_profile"] = not args.simulate
    return Outcome(data, warnings)


def _repair(args: argparse.Namespace) -> Outcome:
    return Outcome(_summary(repair(args.capture), []))


# --- calibration, views, spectra, export -----------------------------------------------


def _calibrate(args: argparse.Namespace) -> Outcome:
    calibration = make_calibration([load_capture(path) for path in args.white], args.white_roi)
    calibration.save(args.output)
    document = calibration.document
    return Outcome(
        {
            "calibration": str(args.output),
            "calibration_id": document["calibration_id"],
            "bands": calibration.band_ids,
            "white_roi_px": document["white_roi_px"],
            "dominant_channel_by_led": document["dominant_channel_by_led"],
            "quality": document["quality"]["bands"],
        },
        document["quality"]["warnings"],
    )


def _cube(args: argparse.Namespace, warnings: list[str]) -> Cube:
    """The cube a command works on: raw (L1), or relative to white when --calib is given."""
    capture = load_capture(args.capture)
    if not getattr(args, "calib", None):
        return capture.cube()
    covered = load_capture(args.legacy).frame() if getattr(args, "legacy", None) else None
    cube = apply_calibration(
        capture, Calibration.load(args.calib), force=args.force, covered_dark=covered
    )
    used = cube.metadata["calibration"]
    warnings += used["warnings"]
    if used["binding_overridden"]:
        warnings.append(f"calibration applied by force; differing: {used['binding_overridden']}")
    return cube


def _medians(cube: Cube, digits: int | None = 4) -> list[float | None]:
    """Median of the valid pixels of every band (None: no valid pixel)."""
    valid = cube.valid
    medians: list[float | None] = []
    for index in range(len(cube.bands)):
        chosen = cube.values[:, :, index][valid[:, :, index]]
        median = float(np.median(chosen)) if chosen.size else None
        medians.append(round(median, digits) if median is not None and digits else median)
    return medians


def _reflectance(args: argparse.Namespace) -> Outcome:
    warnings: list[str] = []
    cube = _cube(args, warnings)
    written = cube.save(args.output)
    return Outcome(
        {
            "cube": str(written),
            "layer": cube.metadata["layer"],
            "unit": cube.metadata["unit"],
            "bands": [band.band_id for band in cube.bands],
            "median_by_band": _medians(cube),
            "valid_fraction": round(float(cube.valid.mean()), 4),
        },
        warnings,
    )


def _view(args: argparse.Namespace) -> Outcome:
    warnings: list[str] = []
    cube = _cube(args, warnings)
    image = views.render(
        cube, args.mode, low=args.low, high=args.high, gamma=args.gamma, palette=args.palette,
        allow_uncalibrated=args.allow_uncalibrated,
    )  # fmt: skip
    views.save_png(image, args.output)
    return Outcome(
        {"image": str(args.output), "mode": args.mode, "layer": cube.metadata["layer"],
         "size": [image.shape[1], image.shape[0]]},
        warnings,
    )  # fmt: skip


def _spectrum(args: argparse.Namespace) -> Outcome:
    warnings: list[str] = []
    cube = _cube(args, warnings)
    x, y = args.xy
    geometry = {"rect": [x, y, *args.wh]} if args.wh else {"point": [x, y]}
    region = Region("region", geometry)
    if args.csv:
        write_csv(args.csv, cube, [region])
    return Outcome(
        {
            "geometry": geometry,
            "layer": cube.metadata["layer"],
            "unit": cube.metadata["unit"],
            "csv": str(args.csv) if args.csv else None,
            "bands": [asdict(band) for band in spectrum(cube, region)],
        },
        warnings,
    )


def _export(args: argparse.Namespace) -> Outcome:
    warnings: list[str] = []
    cube = _cube(args, warnings)
    writer = export_envi if args.format == "envi" else export_tiff
    files = [path.name for path in writer(cube, args.output)]
    return Outcome(
        {"directory": str(args.output), "format": args.format, "files": files,
         "layer": cube.metadata["layer"]},
        warnings,
    )  # fmt: skip


def _demo(args: argparse.Namespace) -> Outcome:
    """Offline tour on the simulated device: scan, white reference, relative reflectance,
    pictures and spectra; and a check of the result against the scene that was simulated."""
    out: Path = args.output
    out.mkdir(parents=True, exist_ok=True)
    sample, white = samples.simulated(out / "captures")
    calibration = make_calibration([white])
    cube = apply_calibration(sample, calibration)
    files = []
    for mode in ("rgb", "cir", "ndvi"):
        # A fixed 0-1 range for the composites: stretched band by band, a grey patch
        # would not come out grey.
        low, high = (None, None) if mode == "ndvi" else (0.0, 1.0)
        views.save_png(views.render(cube, mode, low=low, high=high), out / f"{mode}.png")
        files.append(f"{mode}.png")
    regions = [Region(name, {"rect": list(rect)}) for name, rect in samples.PATCHES.items()]
    write_csv(out / "spectra.csv", cube, regions)

    # Sample and white board are lit alike and the board reflects 1.0, so dividing pixel
    # by pixel must give back the scene itself.
    leds = [int(band.band_id.removeprefix("led_")) for band in cube.bands]
    truth = samples.scene()[:, :, [led_id - 1 for led_id in leds]]
    valid = cube.valid
    deviation = float(np.abs(cube.values - truth)[valid].max()) if valid.any() else math.inf
    index, _ = views.index_of(cube, *views.INDICES["ndvi"])
    x, y, w, h = samples.PATCHES["leaf"]
    leaf_index = float(np.nanmedian(index[y : y + h, x : x + w]))
    # Written out here, not taken from views.INDICES: LED 12 is 804 nm, LED 10 is 667 nm.
    near_infrared, red = truth[y, x, leds.index(12)], truth[y, x, leds.index(10)]
    expected_index = float((near_infrared - red) / (near_infrared + red))
    checks: dict[str, dict[str, Any]] = {
        "relative_reflectance": {
            "max_deviation": round(deviation, 5) if math.isfinite(deviation) else None,
            "valid_fraction": round(float(valid.mean()), 4),
            "passed": deviation <= DEMO_TOLERANCE and bool(valid.all()),
        },
        "ndvi_of_the_leaf_patch": {
            "value": round(leaf_index, 4),
            "expected": round(expected_index, 4),
            "passed": abs(leaf_index - expected_index) <= 2 * DEMO_TOLERANCE,
        },
    }
    passed = all(check["passed"] for check in checks.values())
    data = {
        "passed": passed,
        "result": "PASS" if passed else "FAIL",
        "simulated": True,
        "output": str(out),
        "files": [*files, "spectra.csv"],
        "captures": ["captures/sample", "captures/white"],
        "tolerance": DEMO_TOLERANCE,
        "checks": checks,
    }
    if not passed:
        raise CalibrationQualityError(
            "the simulated scene did not come back out of the processing chain",
            hint="this is a bug in the installation or the software; please report it with "
            "the output of 'vispek-hc check --json'",
            details=data,
        )
    return Outcome(data)


def _setup(args: argparse.Namespace) -> Outcome:
    """What this computer needs once before camera parameters can be locked. On macOS that
    is uvc-util, compiled here from the sources that come with the package; nothing is
    downloaded."""
    if PLATFORM == "darwin":
        sources = controls.uvc_util_sources()
        destination = units.home() / "bin" / "uvc-util"
        command = controls.uvc_util_build_command(sources, destination) if sources else None
        if args.dry_run:
            return Outcome(
                {"platform": PLATFORM, "helper": str(destination), "built": False,
                 "command": command}
            )  # fmt: skip
        helper = controls.build_uvc_util()
        return Outcome(
            {"platform": PLATFORM, "helper": str(helper), "built": True, "command": command}
        )
    warnings = []
    helper_path = find_v4l2_ctl() if PLATFORM == "linux" else None
    if PLATFORM == "linux" and helper_path is None:
        warnings.append(
            "v4l2-ctl is not installed, so camera parameters cannot be locked: install "
            "v4l-utils (Debian, Ubuntu: sudo apt install v4l-utils)"
        )
    elif PLATFORM == "win32":
        from vispek_hc._windows_controls import available

        if not available():
            warnings.append("DirectShow controls require a Windows desktop installation")
    elif PLATFORM != "linux":
        warnings.append("camera controls are not built on this system yet; scans need --unlocked")
    return Outcome(
        {"platform": PLATFORM, "helper": helper_path, "built": False, "command": None}, warnings
    )


HANDLERS: dict[str, Callable[[argparse.Namespace], Outcome]] = {
    "check": _check,
    "devices": _devices,
    "led": _led,
    "record": _record,
    "reference": _reference,
    "tune": _tune,
    "repair": _repair,
    "calibrate": _calibrate,
    "reflectance": _reflectance,
    "view": _view,
    "spectrum": _spectrum,
    "export": _export,
    "demo": _demo,
    "setup": _setup,
}


# --- output ----------------------------------------------------------------------------


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            line["traceback"] = self.formatException(record.exc_info)
        return json.dumps(line)


def _plain_lines(value: Any, indent: int = 0) -> list[str]:
    """Indented ``key: value`` text for people; scripts should ask for ``--json``."""
    pad = " " * indent
    if isinstance(value, dict) and value:
        lines = []
        for key, item in value.items():
            body = _plain_lines(item, indent + 2)
            if len(body) > 1 or (isinstance(item, dict) and item):
                lines += [f"{pad}{key}:", *body]
            else:
                lines.append(f"{pad}{key}: {body[0].strip()}")
        return lines
    if isinstance(value, list | tuple) and any(isinstance(entry, dict) for entry in value):
        return [line for entry in value for line in [f"{pad}-", *_plain_lines(entry, indent + 2)]]
    if isinstance(value, list | tuple | dict):
        return [pad + (", ".join(map(str, value)) or "none")]
    return [f"{pad}{value}"]


def _emit(
    command: str | None, as_json: bool, result: Outcome | VispekHCError, warnings: list[str]
) -> int:
    failed = isinstance(result, VispekHCError)
    exit_code = result.exit_code if isinstance(result, VispekHCError) else 0
    if as_json:
        envelope = {
            "schema": ENVELOPE_SCHEMA,
            "command": command,
            "version": __version__,
            "ok": not failed,
            ("error" if failed else "data"): result.to_dict()
            if isinstance(result, VispekHCError)
            else result.data,
            "warnings": warnings,
            "exit_code": exit_code,
        }
        try:
            # Values JSON has no type for (paths, ...) become strings; NaN is never emitted.
            print(json.dumps(envelope, allow_nan=False, default=str))
        except ValueError as error:
            return _emit(command, True, VispekHCError(f"result is not valid JSON: {error}"), [])
        return exit_code
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if isinstance(result, VispekHCError):
        print(f"error [{result.code}]: {result.message}", file=sys.stderr)
        if result.hint:
            print(f"hint: {result.hint}", file=sys.stderr)
    else:
        print("\n".join(_plain_lines(result.data)))
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in arguments
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")  # consoles that cannot print every character
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _JsonLogFormatter() if as_json else logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    logger = logging.getLogger("vispek_hc")
    logger.addHandler(handler)
    command: str | None = None
    result: Outcome | VispekHCError
    try:
        args = build_parser().parse_args(arguments)
        command = args.command
        logger.setLevel([logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)])
        result = HANDLERS[args.command](args)
    except VispekHCError as error:
        result = error
    except _VersionRequested:
        if not as_json:
            print(f"vispek-hc {__version__}")
            return 0
        result = Outcome({"version": __version__})
    except KeyboardInterrupt:
        result = OperationCancelledError("interrupted")
    except OSError as error:
        logger.debug("unexpected failure", exc_info=True)
        if error.filename:  # a path that cannot be read or written; not a bug
            name, reason = Path(str(error.filename)).name, error.strerror or type(error).__name__
            denied = isinstance(error, PermissionError)
            kind = PermissionDeniedError if denied else InvalidStateError
            result = kind(f"cannot use {name}: {reason}")
        else:
            result = VispekHCError(
                f"{type(error).__name__}: {error}",
                hint="this is a bug; please report it with the output of 'vispek-hc check --json'",
            )
    except Exception as error:
        logger.debug("unexpected failure", exc_info=True)
        result = VispekHCError(
            f"{type(error).__name__}: {error}",
            hint="this is a bug; please report it with the output of 'vispek-hc check --json'",
        )
    finally:
        logger.removeHandler(handler)
    warnings = result.warnings if isinstance(result, Outcome) else []
    return _emit(command, as_json, result, warnings)
