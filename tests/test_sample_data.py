# SPDX-License-Identifier: Apache-2.0
"""Real captures of an HC1500 reproduce the reference medians of the specification.

The captures are not distributed. These tests run only where ``tools/convert_samples.py``
has put them into ``sample-captures/`` at the top of the repository.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vispek_hc.calibration import make_calibration
from vispek_hc.cube import Cube, load_capture
from vispek_hc.reflectance import apply_calibration

SAMPLES = Path(__file__).parent.parent / "sample-captures"
pytestmark = pytest.mark.skipif(
    not (SAMPLES / "sample_pcba" / "capture.json").exists(), reason="sample captures not present"
)

# Whole-frame medians of the valid pixels per band, 255 nm to 1050 nm.
DEFAULT = [0.9815, 0.9809, 0.9886, 0.9847, 0.9836, 0.9940, 0.9871, 0.9825, 0.9886, 0.9828,
           0.9884, 0.9873, 0.9894, 0.9924, 0.9922, 0.9932, 0.9928]  # fmt: skip
LEGACY = [0.9783, 0.9773, 0.9808, 0.9783, 1.0000, 0.9912, 0.9896, 0.9828, 0.9841, 0.9804,
          0.9848, 0.9882, 0.9913, 0.9903, 0.9892, 0.9904, 1.0000]  # fmt: skip


def medians(cube: Cube) -> list[float]:
    return [
        float(np.median(cube.values[:, :, index][cube.valid[:, :, index]]))
        for index in range(len(cube.bands))
    ]


def test_sample_frames_match_their_recorded_hashes() -> None:
    for name in ("sample_pcba", "white_reference"):
        capture = load_capture(SAMPLES / name, verify=True)
        assert capture.status == "complete"
        assert len(capture.groups[0]["frames"]) == 17


def test_all_three_dark_frames_are_at_the_black_level() -> None:
    covered = load_capture(SAMPLES / "dark_reference")
    assert np.all(covered.picture("", covered.document["frame"]) == 5)
    for name in ("sample_pcba", "white_reference"):
        assert np.all(load_capture(SAMPLES / name).cube().dark_rgb == 5)


def test_white_reference_region_and_dominant_channels() -> None:
    calibration = make_calibration([load_capture(SAMPLES / "white_reference")])
    assert calibration.roi_px == (275, 144, 983, 667)
    assert "".join(calibration.dominant_channels[n] for n in range(1, 18)) == "BBBBBGGRRRRRRRRRR"


def test_default_definition_reproduces_the_reference_medians() -> None:
    calibration = make_calibration([load_capture(SAMPLES / "white_reference")])
    # Recorded before anyone knew of the gain the camera keeps: its state is not on
    # record, so the pair is applied by force and says so.
    cube = apply_calibration(load_capture(SAMPLES / "sample_pcba"), calibration, force=True)
    assert cube.metadata["calibration"]["binding_overridden"] == ["camera_gain_state"]
    assert medians(cube) == pytest.approx(DEFAULT, abs=0.001)
    assert cube.metadata["calibration"]["reference_age_s"] == pytest.approx(6272.5, abs=1)


def test_legacy_definition_reproduces_the_reference_medians() -> None:
    calibration = make_calibration([load_capture(SAMPLES / "white_reference")])
    covered = load_capture(SAMPLES / "dark_reference")
    cube = apply_calibration(
        load_capture(SAMPLES / "sample_pcba"),
        calibration,
        covered_dark=covered.picture("", covered.document["frame"]),
        force=True,
    )
    assert medians(cube) == pytest.approx(LEGACY, abs=0.001)


def test_sample_captures_match_the_capture_schema() -> None:
    import json

    import jsonschema

    schema = json.loads((SAMPLES.parent / "schemas" / "capture-1.schema.json").read_text("utf-8"))
    for name in ("sample_pcba", "white_reference", "dark_reference"):
        document = json.loads((SAMPLES / name / "capture.json").read_text("utf-8"))
        jsonschema.Draft202012Validator(schema).validate(document)
