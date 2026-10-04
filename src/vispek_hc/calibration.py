# SPDX-License-Identifier: Apache-2.0
"""White reference packages: what a sample is divided by, and when it may be.

A calibration is made from one or more scans of a white board taken with exactly the
settings later used for samples. It stores the white frames with their own dark frame,
the settings they are bound to, and a quality report.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import numpy as np
from numpy.typing import NDArray

from vispek_hc import __version__
from vispek_hc.controls import FOCUS
from vispek_hc.cube import Capture
from vispek_hc.errors import (
    CalibrationMismatchError,
    CalibrationQualityError,
    ConfigRejectedError,
    FrameNotFoundError,
    SchemaUnsupportedError,
)
from vispek_hc.framecheck import MaskBit, Reduction, reduce
from vispek_hc.models import Channel

SCHEMA = "vispek-hc.calibration/1"
FLOOR = {"absolute_dn": 8, "relative": 0.05}
GATES = {"valid_min": 0.95, "saturated_max": 0.02}
WEAK_SIGNAL_DN = 40  # below this, one code value is more than 2.5 % of the white signal
DARK_P99_LIMIT = 12
REFERENCE_MAX_AGE_S = 24 * 3600
# Settings a sample must share with its white reference. The rest of the binding
# (device_id, actual_fps, setup_label) is evidence: a difference is reported, not refused.
MUST_MATCH = (
    "unit_id", "led_table_version", "actual_size", "source", "pixel_format", "color_range",
    "exposure", "gain", "pwm_by_led", "min_on_by_led", "reduction", "camera_controls_values",
    "dark_pause_ms", "camera_gain_state",
)  # fmt: skip
PER_LED = ("pwm_by_led", "min_on_by_led")
# Camera settings that must be known on both sides: two scans whose exposure nobody could
# read are not "the same", they are unknown.
MUST_BE_KNOWN = ("exposure", "gain", "camera_controls_values")
# The camera keeps a gain from the moment of the lock that no control reads back. Only
# locks taken in darkness are known to agree; any other state, or none recorded, is unknown.
GAIN_STATE = "camera_gain_state"
_CLIP_BIT = {"R": MaskBit.RED_CLIPPED, "G": MaskBit.GREEN_CLIPPED, "B": MaskBit.BLUE_CLIPPED}

Roi = tuple[float, float, float, float]


def roi_pixels(roi: Roi, width: int, height: int) -> tuple[int, int, int, int]:
    """Normalised (x, y, w, h) to pixel edges (x0, y0, x1, y1), half-open.

    Both edges are converted, each with ``floor``; converting the width instead gives a
    rectangle one pixel different, so this is the one definition.
    """
    x, y, w, h = roi
    if not (0 <= x < x + w <= 1 and 0 <= y < y + h <= 1):
        raise ConfigRejectedError(f"region {roi} is not inside the picture", details={"roi": roi})
    edges = (
        math.floor(x * width),
        math.floor(y * height),
        math.floor((x + w) * width),
        math.floor((y + h) * height),
    )
    if edges[0] >= edges[2] or edges[1] >= edges[3]:
        raise ConfigRejectedError(f"region {roi} contains no pixel", details={"roi": roi})
    return edges


def _per_led(stored: Any, channels: Sequence[int]) -> dict[str, Any]:
    by_led = stored.get("by_led", stored) if isinstance(stored, Mapping) else {}
    return {str(led_id): by_led.get(str(led_id)) for led_id in channels}


def binding_of(capture_document: Mapping[str, Any], channels: Sequence[int]) -> dict[str, Any]:
    """The settings a capture was made with, as far as they decide comparability."""
    config = capture_document.get("config") or {}
    device = capture_document.get("device") or {}
    controls = config.get("camera_controls") or {}
    identity = (device.get("camera") or {}).get("device_id") or (device.get("controls") or {}).get(
        "device_id"
    )
    exposure = config.get("exposure")
    if isinstance(exposure, Mapping):  # what the camera settled on, not what was asked for
        exposure = exposure.get("effective")
    gain = config.get("gain")
    return {
        "unit_id": capture_document.get("unit_id"),
        "device_id": identity,
        "led_table_version": (capture_document.get("led_table") or {}).get("version"),
        "actual_size": config.get("actual_size"),
        "actual_fps": config.get("actual_fps"),
        "source": config.get("source"),
        "pixel_format": config.get("pixel_format"),
        "color_range": config.get("color_range"),
        "exposure": exposure,
        "gain": controls.get("gain") if gain is None else gain,
        "channels": list(channels),
        "pwm_by_led": _per_led(config.get("pwm_by_led"), channels),
        "min_on_by_led": _per_led(config.get("min_on_ms"), channels),
        "reduction": config.get("reduction"),
        # The camera carries state from the frame before, so how long it saw darkness
        # before a channel changes the reading. Captures older than this key have none.
        "dark_pause_ms": config.get("dark_pause_ms"),
        GAIN_STATE: config.get(GAIN_STATE),
        # The focus read-back of a manual lens is noise, not a setting.
        "camera_controls_values": {k: v for k, v in controls.items() if k != FOCUS},
        "setup_label": (config.get("acquisition") or {}).get("setup_label"),
    }


def binding_differences(sample: Mapping[str, Any], reference: Mapping[str, Any]) -> list[str]:
    """Keys in which a sample must match its white reference and does not.

    The reference may hold more channels than the sample; per-LED settings are compared
    for the sample's channels. A value missing on either side counts as different, for
    per-LED settings and for exposure, gain and the camera controls: a scan made without
    a way to read the camera (``--unlocked``) is comparable with nothing.
    """
    channels = sample.get("channels") or []
    differing = [] if set(channels) <= set(reference.get("channels") or []) else ["channels"]
    for key in MUST_MATCH:
        ours, theirs = sample.get(key), reference.get(key)
        if key in PER_LED:
            ours = ours if isinstance(ours, Mapping) else {}
            theirs = theirs if isinstance(theirs, Mapping) else {}
            same = all(
                ours.get(str(n)) is not None and ours.get(str(n)) == theirs.get(str(n))
                for n in channels
            )
        elif key in MUST_BE_KNOWN:
            same = ours == theirs and ours not in (None, {})
        elif key == GAIN_STATE:
            same = ours == theirs == "dark"
        else:
            same = ours == theirs
        if not same:
            differing.append(key)
    return differing


def age_s(first: str | None, second: str | None) -> float | None:
    """Seconds between two recorded times; None if either is unknown."""
    if not first or not second:
        return None
    return abs((datetime.fromisoformat(first) - datetime.fromisoformat(second)).total_seconds())


def _document_text(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, indent=1).encode("utf-8")


@dataclass(slots=True)
class Calibration:
    document: dict[str, Any]
    white_rgb: NDArray[np.uint8]  # (H, W, B, 3)
    white_dark_rgb: NDArray[np.uint8]  # (H, W, 3)
    white_mask: NDArray[np.uint8]  # (H, W, B)
    band_ids: list[str]
    path: Path | None = None  # the folder it was loaded from or saved to

    @property
    def dominant_channels(self) -> dict[int, Channel]:
        stored = self.document["dominant_channel_by_led"]
        return {int(led_id): channel for led_id, channel in stored.items()}

    @property
    def roi_px(self) -> tuple[int, int, int, int] | None:
        stored = self.document["white_roi_px"]
        return (stored["x0"], stored["y0"], stored["x1"], stored["y1"]) if stored else None

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            directory / "reference.npz",
            white_rgb=self.white_rgb,
            white_dark_rgb=self.white_dark_rgb,
            white_mask=self.white_mask,
            band_ids=np.array(self.band_ids),
            wavelengths_nm=np.array(self.document["wavelengths_nm"], np.float32),
        )
        (directory / "calibration.json").write_bytes(_document_text(self.document))
        self.path = directory
        return directory

    @classmethod
    def load(cls, directory: str | Path) -> Self:
        directory = Path(directory)
        try:
            document = json.loads((directory / "calibration.json").read_text("utf-8"))
        except (FileNotFoundError, NotADirectoryError) as error:
            raise FrameNotFoundError(
                f"{directory.name} has no calibration.json",
                hint="make a calibration with 'vispek-hc calibrate --white <scan> -o <folder>'",
            ) from error
        except ValueError as error:
            raise SchemaUnsupportedError(
                f"calibration.json in {directory.name} is damaged"
            ) from error
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            schema = document.get("schema") if isinstance(document, dict) else None
            raise SchemaUnsupportedError(f"calibration has schema {schema!r}, expected {SCHEMA!r}")
        try:
            with np.load(directory / "reference.npz", allow_pickle=False) as stored:
                return cls(
                    document,
                    stored["white_rgb"],
                    stored["white_dark_rgb"],
                    stored["white_mask"],
                    stored["band_ids"].tolist(),
                    directory,
                )
        except FileNotFoundError as error:
            raise FrameNotFoundError(f"{directory.name} has no reference.npz") from error
        except (ValueError, KeyError, zipfile.BadZipFile) as error:
            raise SchemaUnsupportedError(
                f"reference.npz in {directory.name} is not readable: {type(error).__name__}"
            ) from error

    def sha256(self) -> str:
        """Hash of ``calibration.json``: of the file's bytes once it is on disk."""
        if self.path is not None:
            return hashlib.sha256((self.path / "calibration.json").read_bytes()).hexdigest()
        return hashlib.sha256(_document_text(self.document)).hexdigest()


def _capture_hash(capture: Capture) -> str | None:
    try:
        return hashlib.sha256((capture.path / "capture.json").read_bytes()).hexdigest()
    except OSError:
        return None


def make_calibration(
    captures: Sequence[Capture], white_roi: Roi | None = None, enforce_gates: bool = True
) -> Calibration:
    """Build a calibration from white-reference captures (their mean, if several).

    Every scan must have the same channels, picture size and settings, or they are not
    measurements of the same thing (``CALIBRATION_MISMATCH``). Raises
    ``CALIBRATION_QUALITY`` when, inside the region (the whole frame without one), a band
    has too few pixels bright enough to divide by or too many saturated ones.
    """
    first = None
    binding: dict[str, Any] = {}
    times: list[str] = []
    count = 0
    for capture in captures:
        for output in range(capture.outputs if capture.mode == "scan" else 0):
            cube = capture.cube(output)
            led_ids = [int(band.band_id.removeprefix("led_")) for band in cube.bands]
            bound = binding_of(capture.document, led_ids)
            if first is None:
                first, binding = cube, bound
                white_sum = cube.rgb.astype(np.float32)
                dark_sum = cube.dark_rgb.astype(np.float32)
            else:
                differing = binding_differences(bound, binding)
                if bound["channels"] != binding["channels"]:
                    differing.append("channels")
                if cube.dark_rgb.shape != first.dark_rgb.shape:
                    differing.append("actual_size")
                if differing:
                    raise CalibrationMismatchError(
                        "the white-reference scans were not made with the same settings: "
                        + ", ".join(sorted(set(differing))),
                        details={"differing": sorted(set(differing)), "capture": capture.path.name},
                    )
                white_sum += cube.rgb
                dark_sum += cube.dark_rgb
                first.mask |= cube.mask
            count += 1
            if cube.metadata.get("acquired_at_utc"):
                times.append(cube.metadata["acquired_at_utc"])
    if first is None:
        raise ConfigRejectedError(
            "no complete white-reference scan was given",
            hint="record one with 'vispek-hc reference white -o <folder>'",
        )
    document = captures[0].document
    white_rgb = np.floor(white_sum / count + 0.5).astype(np.uint8)
    dark_rgb = np.floor(dark_sum / count + 0.5).astype(np.uint8)
    mask = first.mask
    height, width = dark_rgb.shape[:2]
    roi = white_roi or document.get("config", {}).get("white_roi")
    x0, y0, x1, y1 = roi_pixels(tuple(roi), width, height) if roi else (0, 0, width, height)
    region = (slice(y0, y1), slice(x0, x1))
    led_ids = binding["channels"]
    luma = first.metadata["reduction"] != "dominant"

    channel_names: tuple[Channel, Channel, Channel] = ("R", "G", "B")
    dominant: dict[str, Channel] = {}
    bands: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, (band, led_id) in enumerate(zip(first.bands, led_ids, strict=True)):
        signal = white_rgb[:, :, index].astype(np.int16) - dark_rgb
        medians = np.median(signal[region].reshape(-1, 3), axis=0)
        channel = channel_names[int(np.argmax(medians))]
        dominant[str(led_id)] = channel
        scalar: Reduction = "luma" if luma else channel
        # The scalar-clipped bit follows the channel the white reference chose.
        colour_bits = mask[:, :, index] & ~np.uint8(MaskBit.SCALAR_CLIPPED)
        if luma:
            every = np.uint8(sum(_CLIP_BIT.values()))
            scalar_clipped = (colour_bits & every) == every
        else:
            scalar_clipped = (colour_bits & np.uint8(_CLIP_BIT[channel])) != 0
        mask[:, :, index] = colour_bits | scalar_clipped * np.uint8(MaskBit.SCALAR_CLIPPED)

        values = reduce(white_rgb[:, :, index], scalar).astype(np.float32)[region]
        dark_values = reduce(dark_rgb, scalar)[region]
        denominator = values - dark_values
        white_signal = float(np.median(denominator))
        floor = max(float(FLOOR["absolute_dn"]), FLOOR["relative"] * white_signal)
        saturated = (mask[:, :, index][region] & np.uint8(MaskBit.SATURATED)) != 0
        saturated_fraction = float(np.mean(saturated))
        valid = float(np.mean(~saturated & (denominator > floor)))
        dark_p99 = float(np.percentile(dark_values, 99))
        passed = valid >= GATES["valid_min"] and saturated_fraction <= GATES["saturated_max"]
        bands.append(
            {"band_id": band.band_id, "valid_fraction": round(valid, 4),
             "saturated_fraction": round(saturated_fraction, 4),
             "white_signal_median": white_signal, "dark_median": float(np.median(dark_values)),
             "dark_p99": dark_p99, "rgb_white_median": [float(m) for m in medians],
             "pass": passed}
        )  # fmt: skip
        if white_signal < WEAK_SIGNAL_DN:
            warnings.append(
                f"{band.band_id}: white signal {white_signal:g} DN is weak (one code value is "
                f"{100 / max(white_signal, 1):.1f} %)"
            )
        if dark_p99 > DARK_P99_LIMIT:
            warnings.append(
                f"{band.band_id}: dark frame p99 is {dark_p99:g} DN; light is leaking in"
            )
    failed = [str(band["band_id"]) for band in bands if not band["pass"]]
    if failed and enforce_gates:
        raise CalibrationQualityError(
            f"white reference is not good enough in {', '.join(failed)}",
            hint="lower the PWM of saturated channels (reference white --auto-pwm), keep the "
            "white board inside the region, and scan again",
            details={"bands": [band for band in bands if not band["pass"]]},
        )
    span = age_s(min(times), max(times)) if times else None
    if span is not None and span > REFERENCE_MAX_AGE_S:
        warnings.append(f"the white-reference scans are {span / 3600:.0f} h apart")
    for capture in captures:
        if not capture.document.get("config", {}).get("controls_locked", True):
            warnings.append(
                f"{capture.path.name}: camera parameters were not locked; white balance or "
                "exposure may have moved"
            )
        if capture.document["recording"].get("role") != "white":
            warnings.append(f"{capture.path.name} was not recorded as a white reference")
    calibration_document = {
        "schema": SCHEMA,
        "calibration_id": str(uuid.uuid4()),
        "created_at_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "acquired_at_utc": min(times) if times else None,
        "software": {
            "name": "vispek-hc",
            "version": __version__,
            "decoder": document.get("software", {}).get("decoder"),
        },
        "inputs": [
            {"path": c.path.name, "capture_id": c.capture_id,
             "capture_json_sha256": _capture_hash(c),
             "group_ids": [g["group_id"] for g in c.groups if g["status"] == "complete"]}
            for c in captures
        ],
        "binding": binding,
        "white_roi": list(roi) if roi else None,
        "white_roi_px": {"x0": x0, "y0": y0, "x1": x1, "y1": y1} if roi else None,
        "denominator_floor": FLOOR,
        "dominant_channel_by_led": dominant,
        "wavelengths_nm": [band.center_nm for band in first.bands],
        "quality": {"region": "roi" if roi else "frame", "gates": GATES, "bands": bands,
                    "warnings": warnings},
        "linearisation": None,
    }  # fmt: skip
    return Calibration(
        calibration_document, white_rgb, dark_rgb, mask, [b.band_id for b in first.bands]
    )
