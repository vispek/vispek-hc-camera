# SPDX-License-Identifier: Apache-2.0
"""White-reference packages and relative reflectance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vispek_hc.calibration import (
    Calibration,
    binding_differences,
    binding_of,
    make_calibration,
    roi_pixels,
)
from vispek_hc.cube import Capture, CaptureWriter, load_capture
from vispek_hc.errors import (
    CalibrationMismatchError,
    CalibrationQualityError,
    ConfigRejectedError,
    FrameNotFoundError,
    SchemaUnsupportedError,
)
from vispek_hc.framecheck import MaskBit
from vispek_hc.interfaces import Frame
from vispek_hc.models import HC1500
from vispek_hc.reflectance import apply_calibration
from vispek_hc.session import Session
from vispek_hc.sim import SimulatedRig, VirtualClock
from vispek_hc.sweep import ScanSettings, record, record_frames

H, W = 20, 30
LEDS = (5, 7, 9)  # blue, green and red dominant
WHITE = {5: (20, 30, 205), 7: (30, 185, 10), 9: (165, 20, 20)}
SAMPLE = {5: (10, 15, 105), 7: (15, 95, 8), 9: (85, 12, 12)}
SETTINGS: dict[str, Any] = {
    "pwm_by_led": {"5": 24, "7": 600, "9": 36},
    "min_on_ms": {"source": "auto", "by_led": {"5": 300, "7": 300, "9": 300}},
    "exposure": {"requested": 157, "effective": 157},
    "gain": 0,
    "controls_locked": True,
    "actual_size": [W, H],
    "actual_fps": 10.0,
    "source": "uncompressed",
    "pixel_format": "rgb24",
    "color_range": "full",
    "camera_controls": {"gain": 0, "exposure-time-abs": 157, "focus-abs": 360},
    "camera_gain_state": "dark",
    "acquisition": {"setup_label": "bench"},
}


def read_npz(path: Path) -> dict[str, Any]:
    """The arrays of an .npz file, read without pickle and with the file closed again."""
    with np.load(path, allow_pickle=False) as stored:
        return {name: stored[name] for name in stored.files}


def flat(rgb: tuple[int, int, int]) -> np.ndarray:
    return np.full((H, W, 3), rgb, np.uint8)


def capture(
    tmp_path: Path,
    name: str,
    pictures: dict[int, Any],
    *,
    dark: Any = (5, 5, 5),
    at_s: float = 0.0,
    unit_id: str = "bench-1",
    role: str | None = None,
    reduction: str = "dominant",
    more_scans: tuple[dict[int, Any], ...] = (),
    **config: Any,
) -> Capture:
    """A capture folder with one scan; ``more_scans`` are averaged with it."""
    clock = VirtualClock()
    scans = (pictures, *more_scans)
    header = {
        "unit_id": unit_id,
        "reduction": reduction,
        "recording": {"mode": "scan", "role": role, "cycles": 1, "average": len(scans)},
        "config": {**SETTINGS, "reduction": reduction, **config},
        "device": {"camera": {"device_id": "uvc:0c45:6370:0x1"}},
    }

    def frame(rgb: Any) -> Frame:
        array = flat(rgb) if isinstance(rgb, tuple) else rgb
        return Frame(1, 1, at_s, clock.utc_at(at_s), W, H, "rgb24", array.tobytes())

    with CaptureWriter(tmp_path / name, header) as writer:
        for number, scan in enumerate(scans, start=1):
            group = f"g{number:04d}"
            writer.record_frame(group, writer.write_frame(f"{group}/dark", frame(dark)), dark=True)
            for led_id, rgb in scan.items():
                entry = writer.write_frame(f"{group}/led{led_id:02d}", frame(rgb))
                writer.record_frame(group, {"led_id": led_id, "nm": HC1500.led(led_id).nm, **entry})
            writer.finish_group(group)
    return load_capture(tmp_path / name)


@pytest.fixture
def white(tmp_path: Path) -> Capture:
    return capture(tmp_path, "white", WHITE, role="white")


@pytest.fixture
def sample(tmp_path: Path) -> Capture:
    return capture(tmp_path, "sample", SAMPLE, at_s=600.0)


# --- regions ---------------------------------------------------------------------------


def test_region_is_converted_edge_by_edge() -> None:
    roi = (0.215, 0.201, 0.5529999999999999, 0.726)
    assert roi_pixels(roi, 1280, 720) == (275, 144, 983, 667)


def test_converting_the_width_instead_would_give_another_rectangle() -> None:
    x0, _, x1, _ = roi_pixels((0.215, 0.0, 0.553, 1.0), 1280, 720)
    assert x1 - x0 == 708
    assert int(0.553 * 1280) == 707, "the other definition is one pixel narrower"


@pytest.mark.parametrize(
    "roi", [(-0.1, 0, 0.5, 0.5), (0.5, 0.5, 0.6, 0.2), (0, 0, 0, 1), (0.5, 0, 0.001, 1)]
)
def test_regions_outside_the_picture_or_without_pixels_are_refused(roi: Any) -> None:
    with pytest.raises(ConfigRejectedError):
        roi_pixels(roi, 100, 100)


# --- making a calibration --------------------------------------------------------------


def test_calibration_takes_the_dominant_channel_from_the_white_reference(white: Capture) -> None:
    calibration = make_calibration([white])
    assert calibration.dominant_channels == {5: "B", 7: "G", 9: "R"}
    assert calibration.band_ids == ["led_05", "led_07", "led_09"]
    assert calibration.white_rgb.shape == (H, W, 3, 3)
    assert np.all(calibration.white_dark_rgb == 5)


def test_white_reference_can_overrule_the_model_default_channel(tmp_path: Path) -> None:
    greenish = capture(tmp_path, "w", {9: (60, 190, 20)}, role="white")
    assert HC1500.led(9).dominant_channel == "R"
    assert make_calibration([greenish]).dominant_channels == {9: "G"}


def test_calibration_document(white: Capture) -> None:
    document = make_calibration([white]).document
    assert document["schema"] == "vispek-hc.calibration/1"
    assert len(document["calibration_id"]) == 36
    assert document["denominator_floor"] == {"absolute_dn": 8, "relative": 0.05}
    assert document["linearisation"] is None
    assert document["white_roi"] is None
    assert document["white_roi_px"] is None
    assert document["quality"]["region"] == "frame"
    on_disk = (white.path / "capture.json").read_bytes()
    assert document["inputs"] == [
        {
            "path": "white",
            "capture_id": white.capture_id,
            "capture_json_sha256": hashlib.sha256(on_disk).hexdigest(),
            "group_ids": ["g0001"],
        }
    ]
    assert document["software"]["decoder"]["library"] == "Pillow"
    assert document["acquired_at_utc"] == "2026-01-01T00:00:00.000+00:00"
    assert document["binding"] == {
        "unit_id": "bench-1",
        "device_id": "uvc:0c45:6370:0x1",
        "led_table_version": "hc1500-nominal-1",
        "actual_size": [W, H],
        "actual_fps": 10.0,
        "source": "uncompressed",
        "pixel_format": "rgb24",
        "color_range": "full",
        "exposure": 157,
        "gain": 0,
        "channels": [5, 7, 9],
        "pwm_by_led": {"5": 24, "7": 600, "9": 36},
        "min_on_by_led": {"5": 300, "7": 300, "9": 300},
        "reduction": "dominant",
        "dark_pause_ms": None,  # this capture is older than the key
        "camera_gain_state": "dark",
        "camera_controls_values": {"gain": 0, "exposure-time-abs": 157},  # no focus read-back
        "setup_label": "bench",
    }
    band = document["quality"]["bands"][0]
    assert band == {
        "band_id": "led_05",
        "valid_fraction": 1.0,
        "saturated_fraction": 0.0,
        "white_signal_median": 200.0,
        "dark_median": 5.0,
        "dark_p99": 5.0,
        "rgb_white_median": [15.0, 25.0, 200.0],
        "pass": True,
    }


def test_several_references_are_averaged(tmp_path: Path) -> None:
    first = capture(tmp_path, "w1", {9: (164, 20, 20)}, role="white")
    second = capture(tmp_path, "w2", {9: (167, 20, 20)}, role="white")
    calibration = make_calibration([first, second])
    assert calibration.white_rgb[0, 0, 0].tolist() == [166, 20, 20], "165.5 rounds half up"
    assert len(calibration.document["inputs"]) == 2


def test_reference_scans_with_different_settings_are_not_averaged(tmp_path: Path) -> None:
    first = capture(tmp_path, "w1", WHITE, role="white")
    brighter = capture(tmp_path, "w2", WHITE, role="white", pwm_by_led={"5": 24, "7": 600, "9": 80})
    fewer = capture(tmp_path, "w3", {5: WHITE[5], 7: WHITE[7]}, role="white")
    with pytest.raises(CalibrationMismatchError) as caught:
        make_calibration([first, brighter])
    assert caught.value.details == {"differing": ["pwm_by_led"], "capture": "w2"}
    with pytest.raises(CalibrationMismatchError) as caught:
        make_calibration([first, fewer])
    assert caught.value.details["differing"] == ["channels"]
    with pytest.raises(CalibrationMismatchError):
        make_calibration([fewer, first])


def test_reference_scans_of_different_picture_size_are_not_averaged(tmp_path: Path) -> None:
    first = capture(tmp_path, "w1", {9: WHITE[9]}, role="white")
    second = capture(tmp_path, "w2", {9: WHITE[9]}, role="white")
    small = np.full((H // 2, W, 3), WHITE[9], np.uint8)
    clock = VirtualClock()
    frame = Frame(1, 1, 0.0, clock.utc_at(0.0), W, H // 2, "rgb24", small.tobytes())
    document = json.loads((second.path / "capture.json").read_text("utf-8"))
    for name in ("dark", "led09"):  # replace both frames by half-height ones
        with CaptureWriter(tmp_path / "scratch", document, overwrite=True) as writer:
            entry = writer.write_frame(name, frame)
        (tmp_path / "scratch" / entry["file"]).replace(second.path / "g0001" / entry["file"])
        target = (
            document["groups"][0]["dark"] if name == "dark" else document["groups"][0]["frames"][0]
        )
        target.update(entry)
    (second.path / "capture.json").write_text(json.dumps(document), "utf-8")
    with pytest.raises(CalibrationMismatchError) as caught:
        make_calibration([first, load_capture(second.path)])
    assert caught.value.details["differing"] == ["actual_size"]


def test_averaged_reference_merges_the_masks_of_its_scans(tmp_path: Path) -> None:
    clipped = flat(WHITE[9])
    clipped[0, 0] = (255, 20, 20)
    white = capture(tmp_path, "w", {9: WHITE[9]}, role="white", more_scans=({9: clipped},))
    calibration = make_calibration([white])
    assert calibration.white_mask[0, 0, 0] & MaskBit.SATURATED
    assert calibration.white_mask[1, 1, 0] == 0
    assert calibration.document["inputs"][0]["group_ids"] == ["g0001", "g0002"]


def test_reference_from_several_captures_merges_their_masks(tmp_path: Path) -> None:
    clipped = flat(WHITE[9])
    clipped[0, 0] = (255, 20, 20)
    first = capture(tmp_path, "w1", {9: WHITE[9]}, role="white")
    second = capture(tmp_path, "w2", {9: clipped}, role="white")
    for order in ([first, second], [second, first]):
        mask = make_calibration(order).white_mask
        assert mask[0, 0, 0] & MaskBit.SATURATED
        assert mask[1, 1, 0] == 0


def test_white_reference_too_dark_to_divide_by_is_refused_without_any_saturation(
    tmp_path: Path,
) -> None:
    vignetted = flat(WHITE[9])
    vignetted[:2, :] = (12, 6, 6)  # 10 % of the frame: 7 DN above dark, under the 8 DN floor
    white = capture(tmp_path, "w", {5: WHITE[5], 9: vignetted}, role="white")
    with pytest.raises(CalibrationQualityError) as caught:
        make_calibration([white])
    (band,) = caught.value.details["bands"]
    assert (band["band_id"], band["valid_fraction"], band["saturated_fraction"]) == (
        "led_09", 0.9, 0.0,
    )  # fmt: skip
    assert (
        make_calibration([white], enforce_gates=False).document["quality"]["bands"][1]["pass"]
        is False
    )


def test_quality_floor_follows_the_white_signal(tmp_path: Path) -> None:
    board = flat((245, 20, 20))  # 240 DN of signal: 5 % of it is 12 DN
    board[:2, :] = (15, 6, 6)  # 10 % of the frame at 10 DN: above 8, under 12
    white = capture(tmp_path, "w", {9: board}, role="white")
    with pytest.raises(CalibrationQualityError) as caught:
        make_calibration([white])
    assert caught.value.details["bands"][0]["valid_fraction"] == 0.9


def test_luma_reference_is_judged_in_luma(tmp_path: Path) -> None:
    white = capture(tmp_path, "w", {9: WHITE[9]}, role="white", reduction="luma")
    band = make_calibration([white]).document["quality"]["bands"][0]
    luma = (165 * 19595 + 20 * 38470 + 20 * 7471 + 0x8000) >> 16
    assert band["white_signal_median"] == luma - 5
    assert band["white_signal_median"] != 160, "not the red channel"


def test_valid_fraction_counts_saturated_pixels_as_not_valid(tmp_path: Path) -> None:
    clipped = flat(WHITE[9])
    clipped.reshape(-1, 3)[:9] = (255, 20, 20)  # 9 of 600 pixels: 1.5 %, inside both gates
    white = capture(tmp_path, "w", {9: clipped}, role="white")
    band = make_calibration([white]).document["quality"]["bands"][0]
    assert (band["valid_fraction"], band["saturated_fraction"], band["pass"]) == (
        0.985,
        0.015,
        True,
    )


def test_scalar_clipping_follows_the_channel_the_white_reference_chose(tmp_path: Path) -> None:
    greenish = flat((60, 190, 20))  # LED 9 by the model is red; this white says green
    greenish[0, 0] = (255, 190, 20)  # red clipped: not the scalar
    greenish[0, 1] = (60, 255, 20)  # green clipped: the scalar
    white = capture(tmp_path, "w", {9: greenish}, role="white")
    mask = make_calibration([white]).white_mask
    assert mask[0, 0, 0] == MaskBit.RED_CLIPPED | MaskBit.SATURATED
    assert mask[0, 1, 0] == MaskBit.GREEN_CLIPPED | MaskBit.SATURATED | MaskBit.SCALAR_CLIPPED


def test_what_weakens_a_reference_is_said_in_its_warnings(tmp_path: Path) -> None:
    early = capture(tmp_path, "w1", WHITE, role="white", controls_locked=False)
    late = capture(tmp_path, "w2", WHITE, at_s=26 * 3600.0)  # recorded as a sample
    warnings = make_calibration([early, late]).document["quality"]["warnings"]
    assert any("w1" in w and "not locked" in w for w in warnings)
    assert any("w2 was not recorded as a white reference" in w for w in warnings)
    assert any("26 h apart" in w for w in warnings)


def test_saturated_white_reference_is_refused_and_names_the_band(tmp_path: Path) -> None:
    clipped = flat(WHITE[9])
    clipped[:1, :] = (255, 20, 20)  # 5 % of the frame
    white = capture(tmp_path, "w", {5: WHITE[5], 9: clipped}, role="white")
    with pytest.raises(CalibrationQualityError) as caught:
        make_calibration([white])
    assert "led_09" in caught.value.message
    assert "led_05" not in caught.value.message
    (band,) = caught.value.details["bands"]
    assert band["saturated_fraction"] == pytest.approx(0.05)
    assert "auto-pwm" in (caught.value.hint or "")


def test_quality_is_judged_inside_the_region_when_there_is_one(tmp_path: Path) -> None:
    clipped = flat(WHITE[9])
    clipped[:2, :] = (255, 20, 20)  # outside the region below
    white = capture(tmp_path, "w", {9: clipped}, role="white")
    calibration = make_calibration([white], white_roi=(0.0, 0.5, 1.0, 0.5))
    assert calibration.document["quality"]["region"] == "roi"
    assert calibration.document["white_roi_px"] == {"x0": 0, "y0": 10, "x1": 30, "y1": 20}
    assert calibration.roi_px == (0, 10, 30, 20)
    assert calibration.document["quality"]["bands"][0]["pass"] is True


def test_region_recorded_with_the_white_reference_is_used(tmp_path: Path) -> None:
    white = capture(tmp_path, "w", WHITE, role="white", white_roi=[0.0, 0.0, 0.5, 1.0])
    assert make_calibration([white]).roi_px == (0, 0, 15, 20)


def test_weak_white_signal_and_light_leak_are_warned_about(tmp_path: Path) -> None:
    white = capture(tmp_path, "w", {5: (20, 30, 44), 9: WHITE[9]}, dark=(14, 14, 14), role="white")
    warnings = make_calibration([white]).document["quality"]["warnings"]
    assert any("led_05" in w and "weak" in w for w in warnings), "30 DN of signal"
    assert any("leaking" in w for w in warnings), "dark frame at 14 DN"
    quiet = capture(tmp_path, "q", WHITE, role="white")
    assert make_calibration([quiet]).document["quality"]["warnings"] == []


def test_a_capture_without_a_complete_scan_cannot_calibrate(tmp_path: Path, white: Capture) -> None:
    white.document["groups"][0]["status"] = "failed"
    with pytest.raises(ConfigRejectedError):
        make_calibration([white])
    with pytest.raises(ConfigRejectedError):
        make_calibration([])


def single_frame(tmp_path: Path) -> Capture:
    rig = SimulatedRig()
    with Session.from_rig(rig) as session:
        return record_frames(session, tmp_path / "single", "single")


def test_a_single_frame_is_not_a_white_reference(tmp_path: Path) -> None:
    with pytest.raises(ConfigRejectedError, match="no complete white-reference scan"):
        make_calibration([single_frame(tmp_path)])


def test_calibration_round_trips_through_disk(tmp_path: Path, white: Capture) -> None:
    calibration = make_calibration([white])
    directory = calibration.save(tmp_path / "calib")
    assert sorted(p.name for p in directory.iterdir()) == ["calibration.json", "reference.npz"]
    stored = read_npz(directory / "reference.npz")
    assert sorted(stored) == [
        "band_ids", "wavelengths_nm", "white_dark_rgb", "white_mask", "white_rgb",
    ]  # fmt: skip
    loaded = Calibration.load(str(directory))
    assert loaded.document == calibration.document
    assert loaded.path == directory
    assert np.array_equal(loaded.white_rgb, calibration.white_rgb)
    assert loaded.band_ids == calibration.band_ids
    assert loaded.sha256() == calibration.sha256()
    text = (directory / "calibration.json").read_text("utf-8")
    assert str(tmp_path) not in text


def test_calibration_hash_is_that_of_the_file_on_disk(tmp_path: Path, white: Capture) -> None:
    calibration = make_calibration([white])
    before = calibration.sha256()
    directory = calibration.save(tmp_path / "calib")
    on_disk = (directory / "calibration.json").read_bytes()
    assert calibration.sha256() == before == hashlib.sha256(on_disk).hexdigest()
    reformatted = json.dumps(json.loads(on_disk), indent=4).encode()
    (directory / "calibration.json").write_bytes(reformatted)
    assert Calibration.load(directory).sha256() == hashlib.sha256(reformatted).hexdigest()


def test_missing_or_damaged_calibration_is_reported_as_such(tmp_path: Path, white: Capture) -> None:
    with pytest.raises(FrameNotFoundError, match=r"calibration\.json") as caught:
        Calibration.load(tmp_path / "nothing")
    assert "calibrate" in (caught.value.hint or "")
    directory = make_calibration([white]).save(tmp_path / "calib")
    (directory / "reference.npz").unlink()
    with pytest.raises(FrameNotFoundError, match=r"reference\.npz"):
        Calibration.load(directory)
    (directory / "reference.npz").write_bytes(b"not a zip file")
    with pytest.raises(SchemaUnsupportedError, match="not readable"):
        Calibration.load(directory)
    (directory / "calibration.json").write_text('{"schema": "vispek-hc.calib')
    with pytest.raises(SchemaUnsupportedError, match="damaged"):
        Calibration.load(directory)


def test_calibration_of_another_schema_is_refused(tmp_path: Path, white: Capture) -> None:
    directory = make_calibration([white]).save(tmp_path / "calib")
    document = json.loads((directory / "calibration.json").read_text("utf-8"))
    document["schema"] = "vispek-hc.calibration/2"
    (directory / "calibration.json").write_text(json.dumps(document), "utf-8")
    with pytest.raises(SchemaUnsupportedError):
        Calibration.load(directory)


# --- relative reflectance --------------------------------------------------------------


def test_reflectance_is_sample_over_white_after_each_lost_its_own_dark(
    white: Capture, sample: Capture
) -> None:
    cube = apply_calibration(sample, make_calibration([white]))
    assert cube.values.dtype == np.float32
    assert cube.values[0, 0].tolist() == pytest.approx([100 / 200, 90 / 180, 80 / 160])
    assert np.all(cube.mask == 0)
    assert cube.valid.all()


def test_reflectance_cube_says_what_it_is(white: Capture, sample: Capture) -> None:
    calibration = make_calibration([white])
    meta = apply_calibration(sample, calibration).metadata
    assert (meta["layer"], meta["unit"]) == ("L2", "relative_to_white")
    assert meta["signal_domain"] == "dominant_channel_v1"
    assert meta["capture_id"] == sample.capture_id
    used = meta["calibration"]
    assert used["path"] is None, "this calibration was never saved"
    assert used["white_roi_px"] is None
    assert used["calibration_id"] == calibration.document["calibration_id"]
    assert used["calibration_json_sha256"] == calibration.sha256()
    assert used["denominator_floor"] == {"absolute_dn": 8, "relative": 0.05}
    assert used["denominator_floor_dn"] == [10.0, 9.0, 8.0], "5 % of 200 and 180, then 8 DN"
    assert used["binding_overridden"] == []
    assert used["reference_age_s"] == 600.0
    assert used["warnings"] == []
    assert used["legacy"] is False


def test_reflectance_names_the_calibration_folder_not_its_full_path(
    tmp_path: Path, white: Capture, sample: Capture
) -> None:
    make_calibration([white]).save(tmp_path / "calib")
    calibration = Calibration.load(tmp_path / "calib")
    used = apply_calibration(sample, calibration).metadata["calibration"]
    assert used["path"] == "calib"
    on_disk = (tmp_path / "calib" / "calibration.json").read_bytes()
    assert used["calibration_json_sha256"] == hashlib.sha256(on_disk).hexdigest()


def test_sample_is_reduced_with_the_channel_the_white_reference_chose(tmp_path: Path) -> None:
    white = capture(tmp_path, "w", {9: (60, 190, 20)}, role="white")  # green, not the model's red
    sample = capture(tmp_path, "s", {9: (200, 79, 12)}, at_s=60.0)
    cube = apply_calibration(sample, make_calibration([white]))
    assert cube.bands[0].dominant_channel == "G"
    assert cube.values[0, 0, 0] == pytest.approx((79 - 5) / (190 - 5))


def test_averaged_sample_keeps_the_mean_and_the_masks_of_its_scans(tmp_path: Path) -> None:
    white = capture(tmp_path, "w", {9: (60, 190, 20)}, role="white")  # green dominant
    one = flat((200, 80, 12))
    other = flat((200, 81, 12))
    other[0, 0] = (200, 255, 12)  # clipped in the second scan only
    sample = capture(tmp_path, "s", {9: one}, at_s=60.0, more_scans=({9: other},))
    cube = apply_calibration(sample, make_calibration([white]))
    assert cube.values[1, 1, 0] == pytest.approx((80.5 - 5) / 185), "the mean, not rounded"
    assert cube.mask[0, 0, 0] & MaskBit.SATURATED
    assert cube.mask[0, 0, 0] & MaskBit.SCALAR_CLIPPED
    assert not cube.valid[0, 0, 0]
    assert cube.metadata["group_ids"] == ["g0001", "g0002"]


def test_each_scan_uses_its_own_dark_frame(tmp_path: Path, white: Capture) -> None:
    leaky = capture(tmp_path, "s", {9: (95, 12, 12)}, dark=(15, 15, 15), at_s=60.0)
    cube = apply_calibration(leaky, make_calibration([white]))
    assert cube.values[0, 0, 0] == pytest.approx((95 - 15) / (165 - 5))


def test_pixels_where_the_white_reference_is_too_dark_become_invalid(tmp_path: Path) -> None:
    vignetted = flat(WHITE[9])
    vignetted[:, :3] = (13, 6, 6)  # 8 DN above dark: at the floor, not above it
    vignetted[:, 3:6] = (14, 6, 6)  # 9 DN: just usable
    white = capture(tmp_path, "w", {9: vignetted}, role="white")
    sample = capture(tmp_path, "s", {9: SAMPLE[9]}, at_s=60.0)
    cube = apply_calibration(sample, make_calibration([white], enforce_gates=False))
    assert np.isnan(cube.values[0, 0, 0])
    assert cube.mask[0, 0, 0] == MaskBit.SMALL_DENOMINATOR
    assert not cube.valid[0, 0, 0]
    assert cube.values[0, 3, 0] == pytest.approx(80 / 9)
    assert cube.mask[0, 3, 0] == MaskBit.OUT_OF_RANGE


def test_the_floor_follows_the_white_signal(tmp_path: Path) -> None:
    dim = flat((245, 20, 20))
    dim[:, :3] = (16, 6, 6)  # 11 DN above dark; 5 % of the 240 DN median is 12
    white = capture(tmp_path, "w", {9: dim}, role="white")
    sample = capture(tmp_path, "s", {9: SAMPLE[9]}, at_s=60.0)
    cube = apply_calibration(sample, make_calibration([white], enforce_gates=False))
    assert np.isnan(cube.values[0, 0, 0])
    assert cube.metadata["calibration"]["denominator_floor_dn"] == [12.0]


def test_the_floor_comes_from_the_region_not_from_the_whole_frame(tmp_path: Path) -> None:
    # The board (245 DN) covers the upper 40 % of the picture, the rest is dimmer. In
    # the region the median signal is 240 DN, so the floor is 12; over the whole frame
    # it would be 100 DN and the floor 8.
    board = flat((105, 20, 20))
    board[:8, :] = (245, 20, 20)
    board[0, 0] = (16, 6, 6)  # 11 DN above dark
    white = capture(tmp_path, "w", {9: board}, role="white")
    sample = capture(tmp_path, "s", {9: SAMPLE[9]}, at_s=60.0)
    region = (0.0, 0.0, 1.0, 0.4)
    inside = apply_calibration(sample, make_calibration([white], white_roi=region))
    whole = apply_calibration(sample, make_calibration([white]))
    assert inside.metadata["calibration"]["denominator_floor_dn"] == [12.0]
    assert whole.metadata["calibration"]["denominator_floor_dn"] == [8.0]
    assert np.isnan(inside.values[0, 0, 0])
    assert np.isfinite(whole.values[0, 0, 0])
    assert inside.metadata["calibration"]["white_roi_px"] == {"x0": 0, "y0": 0, "x1": 30, "y1": 8}


def test_values_outside_zero_to_one_are_kept_and_flagged(tmp_path: Path, white: Capture) -> None:
    bright = flat(SAMPLE[9])
    bright[0, 0] = (200, 12, 12)  # brighter than the white board (fluorescence, gloss)
    bright[0, 1] = (2, 12, 12)  # below the dark frame
    sample = capture(tmp_path, "s", {9: bright}, at_s=60.0)
    cube = apply_calibration(sample, make_calibration([white]))
    assert cube.values[0, 0, 0] == pytest.approx(195 / 160)
    assert cube.values[0, 1, 0] == pytest.approx(-3 / 160)
    assert cube.mask[0, 0, 0] == cube.mask[0, 1, 0] == MaskBit.OUT_OF_RANGE
    assert cube.valid[0, 0, 0], "flagged, but it still counts in statistics"


def test_saturation_in_either_scan_marks_the_result(tmp_path: Path) -> None:
    clipped_white = flat(WHITE[9])
    clipped_white[0, 0] = (255, 20, 20)
    clipped_sample = flat(SAMPLE[9])
    clipped_sample[1, 1] = (90, 255, 12)
    white = capture(tmp_path, "w", {9: clipped_white}, role="white")
    sample = capture(tmp_path, "s", {9: clipped_sample}, at_s=60.0)
    cube = apply_calibration(sample, make_calibration([white]))
    assert cube.mask[0, 0, 0] & MaskBit.SATURATED
    assert cube.mask[1, 1, 0] & MaskBit.SATURATED
    assert cube.mask[1, 1, 0] & MaskBit.GREEN_CLIPPED
    assert not cube.valid[0, 0, 0]
    assert not cube.valid[1, 1, 0]
    assert cube.valid.sum() == H * W - 2


def test_sample_may_use_fewer_channels_than_the_reference(tmp_path: Path, white: Capture) -> None:
    sample = capture(tmp_path, "s", {7: SAMPLE[7]}, at_s=60.0)
    cube = apply_calibration(sample, make_calibration([white]))
    assert [band.band_id for band in cube.bands] == ["led_07"]
    assert cube.values[0, 0, 0] == pytest.approx(0.5)


def test_sample_channel_without_a_white_reference_is_a_mismatch(tmp_path: Path) -> None:
    white = capture(tmp_path, "w", {9: WHITE[9]}, role="white")
    sample = capture(tmp_path, "s", {7: SAMPLE[7], 9: SAMPLE[9]}, at_s=60.0)
    with pytest.raises(CalibrationMismatchError) as caught:
        apply_calibration(sample, make_calibration([white]))
    assert caught.value.details["missing_bands"] == ["led_07"]


def test_a_calibration_can_be_applied_to_the_bands_it_covers(tmp_path: Path) -> None:
    # Seen on the device: a scan of LEDs 1-17 and a white reference of LEDs 5-17 (paper
    # fluoresces under UV, so there is none for 1-4). The visible bands are still good.
    white = capture(tmp_path, "w", {5: WHITE[5], 9: WHITE[9]}, role="white")
    sample = capture(tmp_path, "s", SAMPLE, at_s=60.0)  # LEDs 5, 7, 9
    calibration = make_calibration([white])
    cube = apply_calibration(sample, calibration, partial=True)
    assert [band.band_id for band in cube.bands] == ["led_05", "led_09"]
    assert cube.values.shape == (H, W, 2)
    assert cube.rgb.shape == (H, W, 2, 3)
    assert cube.mask.shape == (H, W, 2)
    assert cube.metadata["layer"] == "L2"
    assert cube.metadata["calibration"]["bands_left_out"] == ["led_07"]
    assert np.median(cube.values[:, :, 0]) == pytest.approx((105 - 5) / (205 - 5), abs=0.01)
    assert np.median(cube.values[:, :, 1]) == pytest.approx((85 - 5) / (165 - 5), abs=0.01)
    whole = apply_calibration(
        capture(tmp_path, "s2", {5: SAMPLE[5], 9: SAMPLE[9]}, at_s=60.0), calibration
    )
    assert whole.metadata["calibration"]["bands_left_out"] == []
    assert np.array_equal(whole.values, cube.values), "as if the other band had not been scanned"


def test_a_partial_calibration_still_needs_a_band_and_the_same_settings(tmp_path: Path) -> None:
    white = capture(tmp_path, "w", {9: WHITE[9]}, role="white")
    calibration = make_calibration([white])
    elsewhere = capture(tmp_path, "s", {5: SAMPLE[5], 7: SAMPLE[7]}, at_s=60.0)
    with pytest.raises(CalibrationMismatchError) as caught:
        apply_calibration(elsewhere, calibration, partial=True)
    assert caught.value.details["missing_bands"] == ["led_05", "led_07"]
    changed = {"pwm_by_led": {"5": 24, "7": 600, "9": 99}}
    brighter = capture(tmp_path, "b", SAMPLE, at_s=60.0, **changed)
    with pytest.raises(CalibrationMismatchError) as caught:
        apply_calibration(brighter, calibration, partial=True)
    assert caught.value.details["differing"] == ["pwm_by_led"]
    # A band that is left out does not have to match: its PWM is no one's business.
    other = capture(tmp_path, "o", SAMPLE, at_s=60.0, pwm_by_led={"5": 99, "7": 1, "9": 36})
    assert [b.band_id for b in apply_calibration(other, calibration, partial=True).bands] == [
        "led_09"
    ]


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"pwm_by_led": {"5": 30, "7": 600, "9": 36}}, "pwm_by_led"),
        ({"exposure": {"requested": 100, "effective": 100}}, "exposure"),
        (
            {"min_on_ms": {"source": "manual", "by_led": {"5": 400, "7": 300, "9": 300}}},
            "min_on_by_led",
        ),
        ({"source": "mjpeg"}, "source"),
        ({"pixel_format": "mjpeg"}, "pixel_format"),
        ({"color_range": "limited"}, "color_range"),
        ({"gain": 4}, "gain"),
        ({"actual_size": [W, H + 2]}, "actual_size"),
        ({"reduction": "luma"}, "reduction"),
        ({"camera_controls": {"gain": 0, "exposure-time-abs": 150}}, "camera_controls_values"),
        ({"camera_controls": {"gain": 0}}, "camera_controls_values"),
        ({"pwm_by_led": None}, "pwm_by_led"),
        ({"pwm_by_led": {"5": 24, "7": 600}}, "pwm_by_led"),
        ({"min_on_ms": 300}, "min_on_by_led"),
    ],
)
def test_each_differing_setting_is_named(
    change: dict[str, Any], key: str, tmp_path: Path, white: Capture
) -> None:
    sample = capture(tmp_path, "s", SAMPLE, at_s=60.0, **change)
    with pytest.raises(CalibrationMismatchError) as caught:
        apply_calibration(sample, make_calibration([white]))
    assert key in caught.value.details["differing"]
    assert key in caught.value.message
    assert "--force" in (caught.value.hint or "")


def test_settings_that_are_evidence_do_not_refuse_a_sample(tmp_path: Path, white: Capture) -> None:
    # The measured frame rate differs in its last digits from session to session.
    sample = capture(
        tmp_path, "s", SAMPLE, at_s=60.0, actual_fps=9.98, acquisition={"setup_label": "other"}
    )
    used = apply_calibration(sample, make_calibration([white])).metadata["calibration"]
    assert used["binding_overridden"] == []
    assert used["warnings"] == ["setup label 'other' differs from the white reference's 'bench'"]


def test_exposure_is_bound_by_what_the_camera_settled_on_not_by_what_was_asked(
    tmp_path: Path,
) -> None:
    # The first session asks for 157 and the camera settles on 156; the profile keeps
    # 156, so the next session asks for 156. Both ran at 156.
    white = capture(
        tmp_path, "w", WHITE, role="white", exposure={"requested": 157, "effective": 156}
    )
    sample = capture(
        tmp_path, "s", SAMPLE, at_s=60.0, exposure={"requested": 156, "effective": 156}
    )
    used = apply_calibration(sample, make_calibration([white])).metadata["calibration"]
    assert used["binding_overridden"] == []
    other = capture(tmp_path, "o", SAMPLE, at_s=60.0, exposure={"requested": 157, "effective": 157})
    with pytest.raises(CalibrationMismatchError) as caught:
        apply_calibration(other, make_calibration([white]))
    assert caught.value.details["differing"] == ["exposure"]


def test_camera_settings_nobody_could_read_match_nothing(tmp_path: Path) -> None:
    unknown = {"exposure": None, "gain": None, "camera_controls": None, "controls_locked": False}
    white = capture(tmp_path, "w", WHITE, role="white", **unknown)
    sample = capture(tmp_path, "s", SAMPLE, at_s=60.0, **unknown)
    calibration = make_calibration([white])
    with pytest.raises(CalibrationMismatchError) as caught:
        apply_calibration(sample, calibration)
    assert caught.value.details["differing"] == ["exposure", "gain", "camera_controls_values"]
    forced = apply_calibration(sample, calibration, force=True)
    assert forced.metadata["calibration"]["binding_overridden"] == [
        "exposure", "gain", "camera_controls_values",
    ]  # fmt: skip


def test_gain_is_taken_from_the_camera_controls_when_not_recorded_on_its_own() -> None:
    config = {key: value for key, value in SETTINGS.items() if key != "gain"}
    config["camera_controls"] = {"gain": 3, "exposure-time-abs": 157}
    assert binding_of({"config": config}, [5])["gain"] == 3
    assert binding_of({"config": {**config, "gain": 0}}, [5])["gain"] == 0


def test_per_led_settings_are_compared_for_the_samples_channels_only() -> None:
    reference = binding_of({"config": SETTINGS}, [5, 7, 9])
    sample = binding_of({"config": {**SETTINGS, "pwm_by_led": {"7": 600, "9": 999}}}, [7])
    assert binding_differences(sample, reference) == []
    other = binding_of({"config": {**SETTINGS, "pwm_by_led": {"7": 601}}}, [7])
    assert binding_differences(other, reference) == ["pwm_by_led"]
    extra = binding_of({"config": SETTINGS}, [5, 7, 9, 10])
    assert binding_differences(extra, reference) == ["channels", "pwm_by_led", "min_on_by_led"]


def test_the_dark_pause_before_each_channel_is_part_of_the_binding() -> None:
    # The camera carries state from the frame before: a reference recorded with the old
    # 20 ms pause (no key in its capture) read LED 9 some 20 % differently from a scan
    # that waits a whole dark frame. Such a pair must not be divided silently.
    paused = {**SETTINGS, "dark_pause_ms": 170}
    old = binding_of({"config": SETTINGS}, [5, 7, 9])
    new = binding_of({"config": paused}, [5, 7, 9])
    assert new["dark_pause_ms"] == 170
    assert old["dark_pause_ms"] is None
    assert binding_differences(new, old) == ["dark_pause_ms"]
    assert binding_differences(old, new) == ["dark_pause_ms"]
    assert binding_differences(new, new) == []
    assert binding_differences(old, old) == [], "two old captures are as alike as before"
    longer = binding_of({"config": {**SETTINGS, "dark_pause_ms": 300}}, [5, 7, 9])
    assert binding_differences(longer, new) == ["dark_pause_ms"]
    # A calibration saved before the key existed has no entry at all.
    stored = {key: value for key, value in new.items() if key != "dark_pause_ms"}
    assert binding_differences(new, stored) == ["dark_pause_ms"]


def test_the_gain_the_camera_keeps_is_part_of_the_binding() -> None:
    # Locked in darkness or under light, the camera differs by a factor of up to 4.4 with
    # every recorded control the same. Only two locks in darkness are known to agree.
    dark = binding_of({"config": SETTINGS}, [5])
    assert dark["camera_gain_state"] == "dark"
    assert binding_differences(dark, dark) == []
    light = binding_of({"config": {**SETTINGS, "camera_gain_state": "undetermined"}}, [5])
    assert binding_differences(light, dark) == ["camera_gain_state"]
    assert binding_differences(light, light) == ["camera_gain_state"], "unknown twice: unknown"
    before = {key: value for key, value in SETTINGS.items() if key != "camera_gain_state"}
    old = binding_of({"config": before}, [5])  # recorded before the state was known
    assert binding_differences(old, dark) == ["camera_gain_state"]
    assert binding_differences(dark, old) == ["camera_gain_state"]
    assert binding_differences(old, old) == ["camera_gain_state"]


def test_two_sessions_of_one_unit_agree_although_each_measures_its_own_frame_rate(
    tmp_path: Path,
) -> None:
    captures = []
    for name, fps, role in (("white", 10.0, "white"), ("sample", 9.97, None)):
        session = Session.from_rig(SimulatedRig(fps=fps))
        session.unit.pipeline_latency_ms = 181.3  # what the unit's profile holds
        with session:
            settings = ScanSettings(channels=(5, 9), role=role)  # type: ignore[arg-type]
            captures.append(record(session, tmp_path / name, settings))
    white, sample = captures
    assert white.document["config"]["actual_fps"] != sample.document["config"]["actual_fps"]
    cube = apply_calibration(sample, make_calibration([white]))
    assert cube.metadata["calibration"]["binding_overridden"] == []
    assert np.nanmedian(cube.values) == pytest.approx(1.0, abs=0.02)


def test_reflectance_needs_a_scan(tmp_path: Path, white: Capture) -> None:
    calibration = make_calibration([white])
    with pytest.raises(ConfigRejectedError, match="single capture; reflectance needs a scan"):
        apply_calibration(single_frame(tmp_path), calibration)
    failed = capture(tmp_path, "f", SAMPLE)
    failed.document["groups"][0]["status"] = "failed"
    with pytest.raises(FrameNotFoundError, match="no complete scan"):
        apply_calibration(failed, calibration)


def test_another_unit_is_a_mismatch(tmp_path: Path, white: Capture) -> None:
    sample = capture(tmp_path, "s", SAMPLE, unit_id="bench-2")
    with pytest.raises(CalibrationMismatchError) as caught:
        apply_calibration(sample, make_calibration([white]))
    assert caught.value.details["differing"] == ["unit_id"]


def test_focus_read_back_does_not_break_the_binding(tmp_path: Path, white: Capture) -> None:
    controls = {"gain": 0, "exposure-time-abs": 157, "focus-abs": 280}
    sample = capture(tmp_path, "s", SAMPLE, at_s=60.0, camera_controls=controls)
    assert apply_calibration(sample, make_calibration([white])).valid.all()


def test_force_applies_anyway_and_records_what_was_overridden(
    tmp_path: Path, white: Capture
) -> None:
    sample = capture(tmp_path, "s", SAMPLE, unit_id="bench-2", gain=4)
    cube = apply_calibration(sample, make_calibration([white]), force=True)
    assert cube.metadata["calibration"]["binding_overridden"] == ["unit_id", "gain"]
    assert cube.values[0, 0, 0] == pytest.approx(0.5)


def test_old_reference_and_other_usb_location_are_warned_about(
    tmp_path: Path, white: Capture
) -> None:
    sample = capture(tmp_path, "s", SAMPLE, at_s=25 * 3600.0)
    sample.document["device"] = {"camera": {"device_id": "uvc:0c45:6370:0x2"}}
    warnings = apply_calibration(sample, make_calibration([white])).metadata["calibration"][
        "warnings"
    ]
    assert len(warnings) == 2
    assert "25 h" in warnings[0]
    assert "USB location" in warnings[1]


def test_different_picture_sizes_cannot_be_divided(tmp_path: Path, white: Capture) -> None:
    sample = capture(tmp_path, "s", SAMPLE, at_s=60.0)
    calibration = make_calibration([white])
    calibration.white_dark_rgb = calibration.white_dark_rgb[:10]
    with pytest.raises(ConfigRejectedError, match="size"):
        apply_calibration(sample, calibration)
    with pytest.raises(ConfigRejectedError, match="covered dark frame"):
        apply_calibration(sample, make_calibration([white]), covered_dark=flat((5, 5, 5))[:10])


# --- the legacy definition, kept for comparing with old data ---------------------------


def test_legacy_definition_uses_luma_one_covered_dark_and_a_one_dn_floor(
    tmp_path: Path, white: Capture
) -> None:
    sample = capture(tmp_path, "s", {9: SAMPLE[9]}, dark=(9, 9, 9), at_s=60.0)
    cube = apply_calibration(sample, make_calibration([white]), covered_dark=flat((5, 5, 5)))
    luma_sample = (85 * 19595 + 12 * 38470 + 12 * 7471 + 0x8000) >> 16
    luma_white = (165 * 19595 + 20 * 38470 + 20 * 7471 + 0x8000) >> 16
    assert cube.values[0, 0, 0] == pytest.approx((luma_sample - 5) / (luma_white - 5))
    assert cube.metadata["reduction"] == "luma"
    assert cube.metadata["signal_domain"] == "luma_bt601_v1"
    assert cube.metadata["calibration"]["legacy"] is True
    assert cube.metadata["calibration"]["denominator_floor"] == {"legacy_dn": 1}


def test_legacy_floor_is_one_code_value(tmp_path: Path) -> None:
    dim = flat(WHITE[9])
    dim[0, 0] = (6, 6, 6)  # luma 6: 1 DN above the covered dark
    dim[0, 1] = (7, 7, 7)  # 2 DN
    white = capture(tmp_path, "w", {9: dim}, role="white")
    sample = capture(tmp_path, "s", {9: SAMPLE[9]}, at_s=60.0)
    calibration = make_calibration([white])
    cube = apply_calibration(sample, calibration, covered_dark=flat((5, 5, 5)))
    assert np.isnan(cube.values[0, 0, 0])
    assert np.isfinite(cube.values[0, 1, 0])
