# SPDX-License-Identifier: Apache-2.0
"""Relative reflectance: a sample divided by the white reference, band by band.

    R = (S - D_S) / (W - D_W)

``S`` and its dark frame ``D_S`` come from the sample scan, ``W`` and ``D_W`` from the
calibration. The numbers are 8-bit code values after the camera's own processing, so
``R`` is a relative index and not a physical reflectance. It is comparable only between
scans made with the same exposure, gain, per-channel PWM and timing: that is what the
binding check enforces.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from vispek_hc.calibration import (
    FLOOR,
    REFERENCE_MAX_AGE_S,
    Calibration,
    age_s,
    binding_differences,
    binding_of,
)
from vispek_hc.cube import Capture, Cube, ReductionName
from vispek_hc.errors import CalibrationMismatchError, ConfigRejectedError, FrameNotFoundError
from vispek_hc.framecheck import MaskBit, Reduction, reduce

__all__ = ["apply_calibration", "binding_differences"]


def _warnings(sample: dict[str, Any], reference: dict[str, Any], age: float | None) -> list[str]:
    """What differs without making the result wrong for certain: said, not refused."""
    warnings = []
    if age is not None and age > REFERENCE_MAX_AGE_S:
        warnings.append(f"the white reference is {age / 3600:.0f} h away from the sample")
    if sample.get("device_id") != reference.get("device_id"):
        warnings.append("the device evidence (USB location) differs from the white reference")
    if sample.get("setup_label") != reference.get("setup_label"):
        warnings.append(
            f"setup label {sample.get('setup_label')!r} differs from the white reference's "
            f"{reference.get('setup_label')!r}"
        )
    return warnings


def _only(cube: Cube, band_ids: list[str]) -> Cube:
    """The cube with these bands only."""
    keep = [index for index, band in enumerate(cube.bands) if band.band_id in band_ids]
    return Cube(
        cube.rgb[:, :, keep],
        cube.dark_rgb,
        cube.values[:, :, keep],
        cube.mask[:, :, keep],
        [cube.bands[index] for index in keep],
        cube.metadata,
    )


def apply_calibration(
    capture: Capture,
    calibration: Calibration,
    *,
    output: int = 0,
    force: bool = False,
    covered_dark: NDArray[np.uint8] | None = None,
    partial: bool = False,
) -> Cube:
    """Relative reflectance (layer L2) of one output of a capture.

    ``force`` applies a calibration whose binding differs and records which keys were
    overridden. ``partial`` accepts a capture with bands the calibration has no white
    reference for: the result holds the covered bands only, and the others are named in
    ``metadata["calibration"]["bands_left_out"]``. ``covered_dark`` switches to the legacy
    definition kept for comparing with old data: luma, one covered dark frame for sample
    and white, a 1 DN denominator floor.
    """
    legacy = covered_dark is not None
    if capture.mode != "scan":
        raise ConfigRejectedError(
            f"{capture.path.name} is a {capture.mode} capture; reflectance needs a scan"
        )
    complete = [group for group in capture.groups if group["status"] == "complete"]
    if not complete:
        raise FrameNotFoundError(f"{capture.path.name} holds no complete scan")
    led_ids = [entry["led_id"] for entry in complete[0]["frames"]]
    missing = [f"led_{n:02d}" for n in led_ids if f"led_{n:02d}" not in calibration.band_ids]
    led_ids = [n for n in led_ids if f"led_{n:02d}" in calibration.band_ids]
    if missing and not (partial and led_ids):
        raise CalibrationMismatchError(
            f"the calibration has no white reference for {', '.join(missing)}",
            details={"differing": ["channels"], "missing_bands": missing},
        )
    reference = calibration.document["binding"]
    bound = binding_of(capture.document, led_ids)
    differing = binding_differences(bound, reference)
    if legacy:
        differing = [key for key in differing if key != "reduction"]
    if differing and not force:
        raise CalibrationMismatchError(
            f"the calibration was made with different settings: {', '.join(differing)}",
            hint="scan the sample with the settings of the white reference, make a new "
            "reference, or pass --force and treat the result as not comparable",
            details={"differing": differing},
        )
    luma = legacy or reference.get("reduction") == "luma"
    reduction: ReductionName = "luma" if luma else "dominant"
    # The white reference decides each band's channel; with averaging, every scan is
    # reduced with it before the mean is taken, so values and masks stay those of the scans.
    sample = capture.cube(
        output, reduction=reduction, dominant_by_led=None if luma else calibration.dominant_channels
    )
    if missing:
        sample = _only(sample, calibration.band_ids)
    if sample.dark_rgb.shape != calibration.white_dark_rgb.shape:
        raise ConfigRejectedError("sample and calibration differ in picture size")
    if covered_dark is not None and covered_dark.shape != sample.dark_rgb.shape:
        raise ConfigRejectedError("the covered dark frame differs from the sample in picture size")

    roi = calibration.roi_px
    region = (slice(roi[1], roi[3]), slice(roi[0], roi[2])) if roi else (slice(None), slice(None))
    values = np.full(sample.values.shape, np.nan, np.float32)
    mask = sample.mask.copy()
    floors = []
    for index, band in enumerate(sample.bands):
        position = calibration.band_ids.index(band.band_id)
        scalar: Reduction = "luma" if luma else band.dominant_channel
        white = reduce(calibration.white_rgb[:, :, position], scalar).astype(np.float32)
        white_dark = calibration.white_dark_rgb if covered_dark is None else covered_dark
        sample_dark = sample.dark_rgb if covered_dark is None else covered_dark
        denominator = white - reduce(white_dark, scalar)
        numerator = sample.values[:, :, index] - reduce(sample_dark, scalar)
        if legacy:
            floor = 1.0
        else:
            relative = FLOOR["relative"] * float(np.median(denominator[region]))
            floor = max(float(FLOOR["absolute_dn"]), relative)
        floors.append(floor)
        usable = denominator > floor
        values[:, :, index] = np.divide(
            numerator, denominator, out=values[:, :, index], where=usable
        )
        band_mask = mask[:, :, index] | calibration.white_mask[:, :, position]
        band_mask[~usable] |= np.uint8(MaskBit.SMALL_DENOMINATOR)
        ratio = values[:, :, index]
        band_mask[usable & ((ratio < 0) | (ratio > 1))] |= np.uint8(MaskBit.OUT_OF_RANGE)
        mask[:, :, index] = band_mask

    age = age_s(sample.metadata.get("acquired_at_utc"), calibration.document.get("acquired_at_utc"))
    metadata = {
        **sample.metadata,
        "layer": "L2",
        "unit": "relative_to_white",
        "reduction": reduction,
        "signal_domain": "luma_bt601_v1" if luma else "dominant_channel_v1",
        "calibration": {
            "path": calibration.path.name if calibration.path else None,
            "calibration_id": calibration.document["calibration_id"],
            "calibration_json_sha256": calibration.sha256(),
            "denominator_floor": {"legacy_dn": 1} if legacy else FLOOR,
            "denominator_floor_dn": [round(floor, 3) for floor in floors],
            "white_roi_px": calibration.document["white_roi_px"],
            "binding_overridden": differing,
            "bands_left_out": missing,
            "reference_age_s": age,
            "warnings": _warnings(bound, reference, age),
            "legacy": legacy,
        },
    }
    return Cube(sample.rgb, sample.dark_rgb, values, mask, sample.bands, metadata)
