# SPDX-License-Identifier: Apache-2.0
"""Cubes and capture folders: building, writing, reading back, exporting."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from vispek_hc import cube as cube_module
from vispek_hc.cube import (
    CaptureWriter,
    Cube,
    build_cube,
    export_envi,
    export_tiff,
    load_capture,
    read_events,
    repair,
)
from vispek_hc.errors import (
    ConfigRejectedError,
    FrameNotFoundError,
    InvalidStateError,
    SchemaUnsupportedError,
)
from vispek_hc.framecheck import MaskBit
from vispek_hc.interfaces import Frame
from vispek_hc.models import HC1500
from vispek_hc.sim import VirtualClock

H, W = 6, 8
LEDS = [HC1500.led(5), HC1500.led(7), HC1500.led(9)]  # B, G, R dominant


def read_npz(path: Path) -> dict[str, Any]:
    """The arrays of an .npz file, read without pickle and with the file closed again."""
    with np.load(path, allow_pickle=False) as stored:
        return {name: stored[name] for name in stored.files}


def picture(red: int, green: int, blue: int) -> np.ndarray:
    return np.full((H, W, 3), (red, green, blue), np.uint8)


def frame(rgb: np.ndarray, seq: int = 1) -> Frame:
    clock = VirtualClock()
    return Frame(seq, 1, 0.25 * seq, clock.utc_at(0.25 * seq), W, H, "rgb24", rgb.tobytes())


def small_cube(**metadata: Any) -> Cube:
    frames = [picture(10, 20, 200), picture(30, 150, 40), picture(250, 60, 70)]
    frames[2][0, 0] = (255, 60, 70)
    return build_cube(frames, picture(5, 5, 5), LEDS, "dominant", metadata)


# --- the cube in memory ----------------------------------------------------------------


def test_cube_keeps_raw_rgb_and_takes_each_bands_dominant_channel() -> None:
    cube = small_cube(unit_id="bench-1")
    assert cube.rgb.shape == (H, W, 3, 3)
    assert cube.rgb.dtype == np.uint8
    assert cube.dark_rgb.shape == (H, W, 3)
    assert cube.values.shape == (H, W, 3)
    assert cube.values.dtype == np.float32
    assert cube.values[1, 1].tolist() == [200.0, 150.0, 250.0], "not dark-subtracted"
    assert [band.band_id for band in cube.bands] == ["led_05", "led_07", "led_09"]
    assert [band.dominant_channel for band in cube.bands] == ["B", "G", "R"]
    assert cube.bands[0].name == "LED 5 · 455 nm"
    assert cube.bands[0].center_nm == 455


def test_cube_metadata_says_what_the_numbers_are() -> None:
    meta = small_cube(unit_id="bench-1").metadata
    assert meta["layer"] == "L1"
    assert meta["unit"] == "image_code_value"
    assert meta["signal_domain"] == "dominant_channel_v1"
    assert meta["reduction"] == "dominant"
    assert meta["axes"] == "YXB"
    assert meta["synchronization"] == "sequential_not_simultaneous"
    assert meta["registration"] == "not_verified"
    assert meta["unit_id"] == "bench-1"
    assert meta["led_table"] == {"version": "hc1500-nominal-1", "verified": False}


def test_luma_reduction_is_the_legacy_signal_domain() -> None:
    cube = build_cube([picture(255, 0, 0)], picture(5, 5, 5), [HC1500.led(9)], "luma", {})
    assert cube.values[0, 0, 0] == 76.0
    assert cube.metadata["signal_domain"] == "luma_bt601_v1"


def test_dominant_channel_can_be_overridden_per_led() -> None:
    cube = build_cube(
        [picture(10, 20, 200)], picture(5, 5, 5), [HC1500.led(5)], "dominant", {}, {5: "G"}
    )
    assert cube.values[0, 0, 0] == 20.0
    assert cube.bands[0].dominant_channel == "G"


def test_mask_marks_clipped_pixels_and_valid_excludes_them() -> None:
    cube = small_cube()
    clipped = MaskBit.RED_CLIPPED | MaskBit.SCALAR_CLIPPED | MaskBit.SATURATED
    assert cube.mask[0, 0].tolist() == [0, 0, clipped]
    assert cube.mask.dtype == np.uint8
    assert not cube.valid[0, 0, 2]
    assert cube.valid.sum() == cube.values.size - 1


def test_valid_excludes_non_finite_values_but_keeps_out_of_range_ones() -> None:
    cube = small_cube()
    cube.values[1, 1, 0] = np.nan
    cube.mask[2, 2, 0] = MaskBit.OUT_OF_RANGE
    cube.mask[3, 3, 0] = MaskBit.SMALL_DENOMINATOR
    assert not cube.valid[1, 1, 0]
    assert cube.valid[2, 2, 0]
    assert not cube.valid[3, 3, 0]


def test_band_lookup_by_id_and_missing_band() -> None:
    cube = small_cube()
    assert cube.band_index("led_07") == 1
    with pytest.raises(ConfigRejectedError) as caught:
        cube.band_index("led_13")
    assert caught.value.details == {
        "band_id": "led_13",
        "available": ["led_05", "led_07", "led_09"],
    }


def test_frames_must_match_the_dark_frame_and_the_led_list() -> None:
    with pytest.raises(ValueError, match="size"):
        build_cube([np.zeros((4, 4, 3), np.uint8)], picture(5, 5, 5), [HC1500.led(5)], "luma", {})
    with pytest.raises(ValueError, match="one frame per LED"):
        build_cube([picture(1, 1, 1)], picture(5, 5, 5), LEDS, "luma", {})


# --- cube.npz --------------------------------------------------------------------------


def test_cube_file_has_the_documented_keys_and_needs_no_pickle(tmp_path: Path) -> None:
    cube = small_cube(unit_id="bench-1")
    path = cube.save(tmp_path / "cube.npz")
    stored = read_npz(path)
    assert sorted(stored) == sorted(
        [
            "schema", "rgb", "dark_rgb", "values", "mask", "band_ids", "wavelengths_nm",
            "dominant_channel", "axes", "layer", "unit", "signal_domain", "reduction",
            "metadata_json",
        ]
    )  # fmt: skip
    assert str(stored["schema"]) == "vispek-hc.cube/1"
    assert stored["band_ids"].tolist() == ["led_05", "led_07", "led_09"]
    assert stored["wavelengths_nm"].dtype == np.float32
    assert stored["dominant_channel"].tolist() == ["B", "G", "R"]
    assert str(stored["axes"]) == "YXB"
    assert json.loads(str(stored["metadata_json"]))["unit_id"] == "bench-1"


def test_the_ten_line_loader_from_the_documentation_works(tmp_path: Path) -> None:
    small_cube().save(tmp_path / "cube.npz")
    z = read_npz(tmp_path / "cube.npz")
    values = z["values"].astype(np.float32)
    valid = np.isfinite(values) & ((z["mask"] & 7) == 0)
    assert values.shape == (H, W, 3)
    assert valid.sum() == values.size - 1
    assert z["rgb"].shape == (H, W, 3, 3)


def test_cube_round_trips_through_its_file(tmp_path: Path) -> None:
    cube = small_cube(unit_id="bench-1")
    cube.values[1, 1, 0] = np.nan
    loaded = Cube.load(cube.save(tmp_path / "cube.npz"))
    assert np.array_equal(loaded.values, cube.values, equal_nan=True)
    assert np.array_equal(loaded.rgb, cube.rgb)
    assert np.array_equal(loaded.mask, cube.mask)
    assert loaded.bands == cube.bands
    assert loaded.metadata == cube.metadata


def test_saving_adds_the_suffix_and_returns_the_real_path(tmp_path: Path) -> None:
    path = small_cube().save(str(tmp_path / "result"))
    assert path == tmp_path / "result.npz"
    assert path.is_file()
    assert Cube.load(str(path)).values.shape == (H, W, 3)


def test_band_descriptions_say_that_wavelengths_are_nominal(tmp_path: Path) -> None:
    stored = read_npz(small_cube().save(tmp_path / "cube.npz"))
    band = json.loads(str(stored["metadata_json"]))["bands"][0]
    assert band["fwhm_nm"] is None
    assert band["source"] == "nominal_LED_not_sensor_response"


def test_cube_metadata_is_strict_json_even_with_numpy_and_non_finite_numbers(
    tmp_path: Path,
) -> None:
    cube = small_cube(reference_age_s=np.float32(1.5), ratio=float("nan"), roi=(1, np.int64(2)))
    stored = read_npz(cube.save(tmp_path / "cube.npz"))
    text = str(stored["metadata_json"])
    assert "NaN" not in text
    metadata = json.loads(text)
    assert (metadata["reference_age_s"], metadata["ratio"], metadata["roi"]) == (1.5, None, [1, 2])


def test_cube_file_with_pickled_content_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cube.npz"
    np.savez(path, schema=np.array({"anything": 1}, dtype=object))
    with pytest.raises(SchemaUnsupportedError, match="not a readable cube"):
        Cube.load(path)


def test_file_that_is_not_a_cube_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cube.npz"
    np.savez(path, values=np.zeros(3))
    with pytest.raises(SchemaUnsupportedError):
        Cube.load(path)
    for content in (b"", b"not a zip file"):
        path.write_bytes(content)
        with pytest.raises(SchemaUnsupportedError, match="not a readable cube"):
            Cube.load(path)


def test_cube_file_of_another_major_version_is_refused(tmp_path: Path) -> None:
    path = small_cube().save(tmp_path / "cube.npz")
    stored = read_npz(path)
    stored["schema"] = np.array("vispek-hc.cube/2")
    np.savez(path, **stored)
    with pytest.raises(SchemaUnsupportedError):
        Cube.load(path)


# --- capture folders -------------------------------------------------------------------


def header(**recording: Any) -> dict[str, Any]:
    return {
        "unit_id": "bench-1",
        "reduction": "dominant",
        "recording": {"mode": "scan", "role": None, "cycles": 1, "average": 1, **recording},
        "config": {"channels": [5, 7, 9]},
    }


def write_group(
    writer: CaptureWriter, group_id: str, shift: int = 0, dark_level: int = 5, bands: int = 3
) -> None:
    dark = writer.write_frame(
        f"{group_id}/dark", frame(picture(dark_level, dark_level, dark_level))
    )
    writer.record_frame(group_id, dark, dark=True)
    pictures = [picture(10, 20, 200 + shift), picture(30, 150, 40), picture(250, 60, 70)]
    for led, rgb in zip(LEDS[:bands], pictures, strict=False):
        entry = writer.write_frame(f"{group_id}/led{led.led_id:02d}_{led.nm}nm", frame(rgb))
        info = {"led_id": led.led_id, "nm": led.nm, "pwm": led.starting_pwm}
        writer.record_frame(group_id, {**info, **entry})
    writer.finish_group(group_id)


def recorded(tmp_path: Path, groups: int = 1, **recording: Any) -> Path:
    directory = tmp_path / "capture"
    with CaptureWriter(directory, header(**recording)) as writer:
        for number in range(1, groups + 1):
            write_group(writer, f"g{number:04d}", shift=number - 1)
    return directory


def test_capture_document_on_disk_grows_with_every_frame(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    writer = CaptureWriter(directory, header())
    seen = []
    for number, led in enumerate(LEDS, start=1):
        entry = writer.write_frame(f"g0001/led{led.led_id:02d}_{led.nm}nm", frame(picture(1, 2, 3)))
        writer.record_frame("g0001", {"led_id": led.led_id, **entry})
        on_disk = json.loads((directory / "capture.json").read_text("utf-8"))
        seen.append(len(on_disk["groups"][0]["frames"]))
        assert number == len(events_of(directory, "frame_selected")), "the event is flushed"
    assert seen == [1, 2, 3]
    writer.close(complete=False)


def events_of(directory: Path, kind: str) -> list[dict[str, Any]]:
    lines = (directory / "events.jsonl").read_text("utf-8").splitlines()
    return [event for event in map(json.loads, lines) if event["type"] == kind]


def test_a_failed_save_leaves_the_previous_capture_document_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "capture"
    writer = CaptureWriter(directory, header())
    write_group(writer, "g0001")

    def no_rename(self: Path, target: Path) -> Path:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Path, "replace", no_rename)
    with pytest.raises(OSError, match="Input/output"):
        writer.finish_group("g0002")
    monkeypatch.undo()
    on_disk = json.loads((directory / "capture.json").read_text("utf-8"))
    assert [group["group_id"] for group in on_disk["groups"]] == ["g0001"]
    writer.close(complete=False)


def test_event_log_uses_unix_line_ends_on_every_platform(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    assert b"\r" not in (directory / "events.jsonl").read_bytes()


def test_capture_folder_layout(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    names = sorted(str(p.relative_to(directory).as_posix()) for p in directory.rglob("*"))
    assert names == [
        "capture.json",
        "events.jsonl",
        "g0001",
        "g0001/dark.png",
        "g0001/led05_455nm.png",
        "g0001/led07_560nm.png",
        "g0001/led09_630nm.png",
    ]


def test_capture_document_identifies_itself_and_finishes_complete(tmp_path: Path) -> None:
    document = json.loads((recorded(tmp_path) / "capture.json").read_text("utf-8"))
    assert document["schema"] == "vispek-hc.capture/1"
    assert len(document["capture_id"]) == 36
    assert document["model"] == "HC1500"
    assert document["led_table"] == {"version": "hc1500-nominal-1", "verified": False}
    assert document["signal_domain"] == "dominant_channel_v1"
    assert document["software"]["name"] == "vispek-hc"
    assert document["software"]["decoder"]["library"] == "Pillow"
    recording = document["recording"]
    assert (recording["status"], recording["groups_done"]) == ("complete", 1)
    assert recording["started_at_utc"].endswith("+00:00")
    assert recording["finished_at_utc"] >= recording["started_at_utc"]


def test_frame_entries_describe_the_bytes_on_disk(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    entry = json.loads((directory / "capture.json").read_text("utf-8"))["groups"][0]["frames"][0]
    data = (directory / "g0001" / entry["file"]).read_bytes()
    assert entry["file"] == "led05_455nm.png"
    assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert entry["bytes"] == len(data)
    assert (entry["encoding"], entry["width"], entry["height"]) == ("png", W, H)
    assert entry["host_frame_counter"] == 1
    assert entry["received_at_utc"] == "2026-01-01T00:00:00.250+00:00"
    assert entry["received_mono_ns"] == 250_000_000
    with Image.open(directory / "g0001" / entry["file"]) as image:
        assert np.array_equal(np.asarray(image), picture(10, 20, 200)), "lossless"


def test_jpeg_frames_are_stored_byte_for_byte(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    Image.fromarray(picture(5, 5, 5)).save(buffer, "JPEG")
    jpeg = buffer.getvalue()
    clock = VirtualClock()
    with CaptureWriter(tmp_path / "c", header(mode="single")) as writer:
        entry = writer.write_frame("frame", Frame(1, 1, 0.0, clock.utc(), W, H, "mjpeg", jpeg))
    assert (entry["file"], entry["encoding"]) == ("frame.jpg", "jpeg")
    assert (tmp_path / "c" / "frame.jpg").read_bytes() == jpeg


def test_capture_document_is_saved_as_partial_while_recording(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    writer = CaptureWriter(directory, header())
    write_group(writer, "g0001")
    during = json.loads((directory / "capture.json").read_text("utf-8"))
    assert during["recording"]["status"] == "partial"
    assert during["recording"]["groups_done"] == 1
    assert during["recording"]["finished_at_utc"] is None
    writer.close(complete=False)
    assert load_capture(directory).status == "partial"
    assert not list(directory.glob("*.tmp")), "written atomically"


def test_leaving_the_writer_after_an_error_leaves_the_capture_partial(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), CaptureWriter(tmp_path / "c", header()) as writer:
        write_group(writer, "g0001")
        raise RuntimeError("boom")
    assert load_capture(tmp_path / "c").status == "partial"


def test_events_are_appended_one_json_object_per_line(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    with CaptureWriter(directory, header()) as writer:
        writer.event("no_led_response", group_id="g0001", led_id=7)
    lines = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    assert lines[0]["type"] == "recording_started"
    assert lines[-1]["type"] == "no_led_response"
    assert (lines[-1]["group_id"], lines[-1]["led_id"]) == ("g0001", 7)
    assert {"t_utc", "t_mono_ns", "type"} <= set(lines[-1])


def test_a_folder_that_is_not_empty_is_refused_unless_overwriting(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    with pytest.raises(InvalidStateError) as caught:
        CaptureWriter(directory, header())
    assert "--overwrite" in (caught.value.hint or "")
    with CaptureWriter(directory, header(), overwrite=True) as writer:
        writer.event("error", message="nothing recorded")
    assert load_capture(directory).groups == []
    assert not (directory / "g0001").exists(), "old frames are gone"


def test_a_folder_that_holds_something_else_is_never_deleted(tmp_path: Path) -> None:
    directory = tmp_path / "thesis"
    directory.mkdir()
    (directory / "chapter1.txt").write_text("irreplaceable")
    for overwrite in (False, True):
        with pytest.raises(InvalidStateError, match="not a capture folder"):
            CaptureWriter(directory, header(), overwrite=overwrite)
    assert (directory / "chapter1.txt").read_text() == "irreplaceable"


def test_a_file_in_place_of_the_folder_is_refused(tmp_path: Path) -> None:
    (tmp_path / "c").write_text("x")
    with pytest.raises(InvalidStateError, match="is a file"):
        CaptureWriter(tmp_path / "c", header(), overwrite=True)
    assert (tmp_path / "c").read_text() == "x"


def test_an_interrupted_capture_without_its_document_can_still_be_overwritten(
    tmp_path: Path,
) -> None:
    directory = recorded(tmp_path)
    (directory / "capture.json").unlink()
    with CaptureWriter(directory, header(), overwrite=True):
        pass
    assert load_capture(directory).groups == []


def test_a_folder_holding_only_finder_metadata_counts_as_empty(tmp_path: Path) -> None:
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / ".DS_Store").write_bytes(b"\0")
    with CaptureWriter(tmp_path / "c", header()):
        pass
    assert load_capture(tmp_path / "c").status == "complete"


def test_a_link_in_place_of_the_folder_is_refused(tmp_path: Path) -> None:
    real = recorded(tmp_path)
    try:
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("this platform does not let the test make a link")
    with pytest.raises(InvalidStateError, match="is a link"):
        CaptureWriter(tmp_path / "link", header(), overwrite=True)
    assert load_capture(real).status == "complete"


def test_an_empty_folder_is_used_as_it_is(tmp_path: Path) -> None:
    (tmp_path / "c").mkdir()
    with CaptureWriter(tmp_path / "c", header()):
        pass
    assert load_capture(str(tmp_path / "c")).status == "complete"


def test_written_files_hold_no_absolute_path(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    for name in ("capture.json", "events.jsonl"):
        text = (directory / name).read_text("utf-8")
        assert str(tmp_path) not in text
        assert str(Path.home()) not in text


def test_capture_loads_back_into_the_same_cube(tmp_path: Path) -> None:
    capture = load_capture(recorded(tmp_path), verify=True)
    assert (capture.mode, capture.status, capture.unit_id) == ("scan", "complete", "bench-1")
    assert [group["group_id"] for group in capture.groups] == ["g0001"]
    cube = capture.cube()
    assert np.array_equal(cube.rgb[:, :, 0], picture(10, 20, 200))
    assert np.array_equal(cube.dark_rgb, picture(5, 5, 5))
    assert cube.values[0, 0].tolist() == [200.0, 150.0, 250.0]
    assert cube.metadata["capture_id"] == capture.capture_id
    assert cube.metadata["group_ids"] == ["g0001"]
    assert cube.metadata["config"] == {"channels": [5, 7, 9]}


def test_averaged_recording_gives_one_cube_per_output(tmp_path: Path) -> None:
    capture = load_capture(recorded(tmp_path, groups=4, average=2, cycles=2))
    assert capture.outputs == 2
    first, second = capture.cube(0), capture.cube(1)
    assert first.metadata["group_ids"] == ["g0001", "g0002"]
    assert first.values[0, 0, 0] == pytest.approx(200.5)
    assert second.values[0, 0, 0] == pytest.approx(202.5)
    with pytest.raises(FrameNotFoundError):
        capture.cube(2)


def test_averaging_rounds_rgb_half_up_and_merges_the_masks(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    with CaptureWriter(directory, header(average=2)) as writer:
        write_group(writer, "g0001")  # blue 200
        write_group(writer, "g0002", shift=55)  # blue 255: clipped in this scan only
        write_group(writer, "g0003")
        write_group(writer, "g0004", shift=1)  # blue 201
    capture = load_capture(directory)
    clipped, plain = capture.cube(0), capture.cube(1)
    assert clipped.values[0, 0, 0] == pytest.approx(227.5)
    assert clipped.mask[0, 0, 0] & MaskBit.SATURATED, "clipped in one scan: flagged in the mean"
    assert plain.rgb[0, 0, 0].tolist() == [10, 20, 201], "200.5 rounds up"
    assert plain.values[0, 0, 0] == pytest.approx(200.5)
    assert plain.values.dtype == np.float32
    assert plain.mask[0, 0, 0] == 0
    assert np.array_equal(plain.dark_rgb, picture(5, 5, 5))


def test_averaging_averages_the_dark_frame_too(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    with CaptureWriter(directory, header(average=2)) as writer:
        write_group(writer, "g0001", dark_level=5)
        write_group(writer, "g0002", dark_level=8)
    cube = load_capture(directory).cube()
    assert np.all(cube.dark_rgb == 7), "6.5 rounds half up"
    assert cube.dark_rgb.dtype == np.uint8


def test_scans_of_one_output_must_have_the_same_channels(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    with CaptureWriter(directory, header(average=2)) as writer:
        write_group(writer, "g0001")
        write_group(writer, "g0002", bands=2)
    with pytest.raises(InvalidStateError, match="same channels"):
        load_capture(directory).cube()


def test_outputs_are_built_from_complete_groups_only(tmp_path: Path) -> None:
    directory = tmp_path / "capture"
    with CaptureWriter(directory, header(cycles=3)) as writer:
        write_group(writer, "g0001")
        write_group(writer, "g0002", shift=20)
        writer.finish_group(
            "g0002", "failed", {"code": "CAMERA_CHANGED", "message": "", "led_id": None}
        )
        write_group(writer, "g0003", shift=3)
    capture = load_capture(directory)
    assert capture.outputs == 2
    assert capture.document["recording"]["groups_done"] == 2
    second = capture.cube(1)
    assert second.metadata["group_ids"] == ["g0003"]
    assert second.values[0, 0, 0] == 203.0
    with pytest.raises(FrameNotFoundError):
        capture.cube(-1)


def test_dominant_channel_can_be_overridden_when_loading(tmp_path: Path) -> None:
    cube = load_capture(recorded(tmp_path)).cube(dominant_by_led={5: "G"})
    assert cube.values[0, 0, 0] == 20.0
    assert cube.bands[0].dominant_channel == "G"


def test_luma_can_be_asked_for_when_loading(tmp_path: Path) -> None:
    cube = load_capture(recorded(tmp_path)).cube(reduction="luma")
    assert cube.metadata["reduction"] == "luma"
    assert cube.values[0, 0, 2] != 250.0


def edit_document(directory: Path, change: Any) -> None:
    path = directory / "capture.json"
    document = json.loads(path.read_text("utf-8"))
    change(document)
    path.write_text(json.dumps(document), "utf-8")


def test_verification_catches_a_frame_that_changed_on_disk(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    Image.fromarray(picture(1, 2, 3)).save(directory / "g0001" / "led07_560nm.png")
    load_capture(directory)  # not verified: fine
    with pytest.raises(FrameNotFoundError, match="sha256"):
        load_capture(directory, verify=True)


def test_verification_covers_the_dark_frame(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    Image.fromarray(picture(6, 6, 6)).save(directory / "g0001" / "dark.png")
    with pytest.raises(FrameNotFoundError, match=r"dark\.png does not match"):
        load_capture(directory, verify=True)


def test_verification_reports_a_missing_file_without_its_full_path(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    (directory / "g0001" / "dark.png").unlink()
    with pytest.raises(FrameNotFoundError) as caught:
        load_capture(directory, verify=True)
    assert "g0001/dark.png is missing" in caught.value.message
    assert str(tmp_path) not in caught.value.message


def single(tmp_path: Path) -> Path:
    directory = tmp_path / "single"
    with CaptureWriter(directory, header(mode="single")) as writer:
        writer.record_single(writer.write_frame("frame", frame(picture(9, 9, 9))))
    return directory


def test_verification_covers_the_frame_of_a_single_capture(tmp_path: Path) -> None:
    directory = single(tmp_path)
    load_capture(directory, verify=True)
    Image.fromarray(picture(1, 2, 3)).save(directory / "frame.png")
    with pytest.raises(FrameNotFoundError, match="sha256"):
        load_capture(directory, verify=True)


def test_verification_covers_the_frames_of_a_video(tmp_path: Path) -> None:
    directory = tmp_path / "video"
    with CaptureWriter(directory, header(mode="video")) as writer:
        writer.document["frames_index"] = "frames.jsonl"
        entries = [writer.write_frame(f"frames/{n:06d}", frame(picture(n, n, n))) for n in (1, 2)]
        (directory / "frames.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    load_capture(directory, verify=True)
    Image.fromarray(picture(7, 7, 7)).save(directory / "frames" / "000002.png")
    with pytest.raises(FrameNotFoundError, match=r"frames/000002\.png"):
        load_capture(directory, verify=True)


def video(tmp_path: Path) -> Path:
    directory = tmp_path / "video"
    with CaptureWriter(directory, header(mode="video")) as writer:
        writer.document["frames_index"] = "frames.jsonl"
        entries = [writer.write_frame(f"frames/{n:06d}", frame(picture(n, n, n))) for n in (1, 2)]
        (directory / "frames.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    return directory


def test_video_index_cut_short_by_a_crash_still_verifies_what_is_whole(tmp_path: Path) -> None:
    directory = video(tmp_path)
    with (directory / "frames.jsonl").open("a") as index:
        index.write('{"file": "000003.png", "sha2')
    load_capture(directory, verify=True)
    Image.fromarray(picture(7, 7, 7)).save(directory / "frames" / "000001.png")
    with pytest.raises(FrameNotFoundError, match="sha256"):
        load_capture(directory, verify=True)


@pytest.mark.parametrize("index", ["../frames.jsonl", "frames/../../x.jsonl", 7])
def test_video_index_outside_the_folder_is_refused(index: Any, tmp_path: Path) -> None:
    directory = video(tmp_path)
    edit_document(directory, lambda document: document.update(frames_index=index))
    with pytest.raises(ConfigRejectedError, match="frame index"):
        load_capture(directory, verify=True)


def test_group_name_that_is_not_text_is_refused(tmp_path: Path) -> None:
    directory = recorded(tmp_path)

    def poison(document: dict[str, Any]) -> None:
        document["groups"][0]["group_id"] = 1

    edit_document(directory, poison)
    with pytest.raises(ConfigRejectedError):
        load_capture(directory).cube()


def test_unreadable_picture_is_reported_without_its_full_path(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    (directory / "g0001" / "led07_560nm.png").write_bytes(b"not a picture")
    with pytest.raises(FrameNotFoundError) as caught:
        load_capture(directory).cube()
    assert "g0001/led07_560nm.png" in caught.value.message
    assert str(tmp_path) not in caught.value.message


def test_missing_frame_file_is_reported(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    (directory / "g0001" / "led07_560nm.png").unlink()
    with pytest.raises(FrameNotFoundError, match="led07_560nm"):
        load_capture(directory).cube()


@pytest.mark.parametrize("name", ["../../etc/passwd", "sub/led.png", "/abs/led.png", "..", ""])
def test_frame_names_that_leave_the_folder_are_refused(name: str, tmp_path: Path) -> None:
    directory = recorded(tmp_path)

    def poison(document: dict[str, Any]) -> None:
        document["groups"][0]["frames"][0]["file"] = name

    edit_document(directory, poison)
    with pytest.raises(ConfigRejectedError):
        load_capture(directory).cube()


@pytest.mark.parametrize("group_id", ["../elsewhere", "g0001/..", "/tmp", "..", "g1"])
def test_group_names_that_leave_the_folder_are_refused(group_id: str, tmp_path: Path) -> None:
    directory = recorded(tmp_path)

    def poison(document: dict[str, Any]) -> None:
        document["groups"][0]["group_id"] = group_id

    edit_document(directory, poison)
    with pytest.raises(ConfigRejectedError):
        load_capture(directory).cube()
    with pytest.raises(ConfigRejectedError):
        load_capture(directory, verify=True)


def test_a_frame_reached_through_a_link_out_of_the_folder_is_refused(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (directory / "g0001").rename(outside / "g0001")
    try:
        (directory / "g0001").symlink_to(outside / "g0001", target_is_directory=True)
    except OSError:
        pytest.skip("this platform does not let the test make a link")
    with pytest.raises(ConfigRejectedError, match="outside the capture folder"):
        load_capture(directory).cube()


def test_oversized_pictures_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = recorded(tmp_path)
    monkeypatch.setattr(cube_module, "MAX_IMAGE_SIDE", 7)
    with pytest.raises(ConfigRejectedError, match="larger"):
        load_capture(directory).cube()


@pytest.mark.parametrize("schema", ["vispek-hc.capture/2", "other/1", None])
def test_other_capture_schemas_are_refused(schema: Any, tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    edit_document(directory, lambda document: document.update(schema=schema))
    with pytest.raises(SchemaUnsupportedError):
        load_capture(directory)


def test_unknown_fields_in_a_capture_are_ignored(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    edit_document(directory, lambda document: document.update(from_the_future={"x": 1}))
    assert load_capture(directory).cube().values.shape == (H, W, 3)


def test_folder_without_a_capture_document(tmp_path: Path) -> None:
    with pytest.raises(FrameNotFoundError, match=r"capture\.json"):
        load_capture(tmp_path)
    with pytest.raises(FrameNotFoundError, match=r"capture\.json"):
        load_capture(tmp_path / "missing")


@pytest.mark.parametrize("content", ['{"schema": "vispek-hc.capt', "", "[1, 2]"])
def test_damaged_capture_document_points_at_repair(content: str, tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    (directory / "capture.json").write_text(content)
    with pytest.raises(SchemaUnsupportedError) as caught:
        load_capture(directory)
    if content != "[1, 2]":
        assert "repair" in (caught.value.hint or "")


def test_repair_rebuilds_the_document_from_events_and_files(tmp_path: Path) -> None:
    directory = recorded(tmp_path, groups=2)
    original = load_capture(directory)
    (directory / "capture.json").unlink()
    (directory / "g0002" / "led09_630nm.png").unlink()  # the crash came before this frame

    repaired = repair(directory)
    assert repaired.status == "partial"
    assert repaired.capture_id == original.capture_id
    first, second = repaired.groups
    assert first == original.groups[0]
    assert second["status"] == "partial"
    assert [entry["led_id"] for entry in second["frames"]] == [5, 7]
    assert np.array_equal(load_capture(directory).cube(0).values, original.cube(0).values)


def test_repair_needs_the_event_log(tmp_path: Path) -> None:
    with pytest.raises(FrameNotFoundError, match=r"events\.jsonl"):
        repair(tmp_path)


def test_repair_skips_the_line_the_crash_cut_short(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    with (directory / "events.jsonl").open("a", encoding="utf-8") as log:
        log.write('{"t_utc": "2026-01-01T00:00:00.000+00:00", "type": "frame_sel')
    (directory / "capture.json").write_text('{"schema": "vispek-hc.capt')
    repaired = repair(str(directory))
    assert len(repaired.groups[0]["frames"]) == 3
    assert repaired.groups[0]["status"] == "complete"
    assert repaired.status == "partial", "a repaired capture never claims to be complete"


def test_repair_keeps_the_document_it_replaces(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    before = (directory / "capture.json").read_bytes()
    repair(directory)
    assert (directory / "capture.json.bak").read_bytes() == before
    repair(directory)
    assert (directory / "capture.json.bak").read_bytes() == before, "the original, still"
    assert load_capture(directory).status == "partial"


def test_lines_of_the_event_log_that_are_not_events_are_passed_over(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    with (directory / "events.jsonl").open("a", encoding="utf-8") as log:
        log.write('[1, 2]\n"text"\n{"no_type": 1}\n')
    assert len(read_events(directory)) == 6
    assert repair(directory).groups[0]["status"] == "complete"


def test_events_can_be_read_back_oldest_first(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    kinds = [event["type"] for event in read_events(str(directory))]
    assert kinds[0] == "recording_started"
    assert kinds[-1] == "group_finished"
    assert kinds.count("frame_selected") == 4


def test_repair_needs_the_start_of_the_recording(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    lines = (directory / "events.jsonl").read_text("utf-8").splitlines()
    (directory / "events.jsonl").write_text("\n".join(lines[1:]) + "\n")
    with pytest.raises(FrameNotFoundError, match="does not start a recording"):
        repair(directory)


def test_repair_restores_the_settings_the_capture_was_made_with(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    original = load_capture(directory).document
    (directory / "capture.json").unlink()
    repaired = repair(directory).document
    assert repaired["config"] == original["config"]
    assert repaired["recording"]["groups_done"] == 1


def test_repair_restores_the_frame_of_a_single_capture(tmp_path: Path) -> None:
    directory = single(tmp_path)
    (directory / "capture.json").unlink()
    repaired = repair(directory)
    assert np.array_equal(repaired.frame(), picture(9, 9, 9))
    repaired.verify()


def test_repair_drops_the_single_frame_whose_file_is_gone(tmp_path: Path) -> None:
    directory = single(tmp_path)
    (directory / "frame.png").unlink()
    assert "frame" not in repair(directory).document


def test_repair_finds_the_frame_index_of_a_video(tmp_path: Path) -> None:
    directory = tmp_path / "video"
    with CaptureWriter(directory, header(mode="video")) as writer:
        entry = writer.write_frame("frames/000001", frame(picture(1, 1, 1)))
        (directory / "frames.jsonl").write_text(json.dumps(entry) + "\n")
    (directory / "capture.json").unlink()
    repaired = repair(directory)
    assert repaired.document["frames_index"] == "frames.jsonl"
    repaired.verify()


def test_repair_refuses_events_that_point_out_of_the_folder(tmp_path: Path) -> None:
    directory = recorded(tmp_path)
    log = directory / "events.jsonl"
    log.write_text(log.read_text("utf-8").replace('"g0001"', '"../g0001"'))
    with pytest.raises(ConfigRejectedError):
        repair(directory)


# --- export ----------------------------------------------------------------------------


def test_envi_export_writes_a_header_other_tools_accept(tmp_path: Path) -> None:
    cube = small_cube(capture_id="abc-123")
    files = export_envi(cube, tmp_path / "out")
    assert sorted(path.name for path in files) == [
        "data.bsq", "data.hdr", "metadata.json", "quality-mask.bsq", "quality-mask.hdr",
    ]  # fmt: skip
    lines = (tmp_path / "out" / "data.hdr").read_text("ascii").splitlines()
    assert lines[0] == "ENVI"
    fields = dict(line.split(" = ", 1) for line in lines[1:])
    assert fields["samples"] == str(W)
    assert fields["lines"] == str(H)
    assert fields["bands"] == "3"
    assert fields["header offset"] == "0"
    assert fields["file type"] == "ENVI Standard"
    assert fields["data type"] == "4"
    assert fields["interleave"] == "bsq"
    assert fields["byte order"] == "0"
    assert fields["band names"] == "{led_05, led_07, led_09}"
    assert fields["wavelength units"] == "Nanometers"
    assert fields["wavelength"] == "{455, 560, 630}"
    assert fields["description"] == "{vispek-hc capture abc-123, layer L1, unit image_code_value}"
    assert fields["default bands"] == "{3, 2, 1}", "630 / 560 / 455 nm, counted from 1"
    mask_fields = (tmp_path / "out" / "quality-mask.hdr").read_text("ascii")
    assert "data type = 1" in mask_fields


def test_envi_data_is_band_sequential_little_endian_float32(tmp_path: Path) -> None:
    cube = small_cube()
    export_envi(cube, tmp_path / "out")
    data = np.fromfile(tmp_path / "out" / "data.bsq", "<f4").reshape(3, H, W)
    assert np.array_equal(data, cube.values.transpose(2, 0, 1))
    mask = np.fromfile(tmp_path / "out" / "quality-mask.bsq", np.uint8).reshape(3, H, W)
    assert np.array_equal(mask, cube.mask.transpose(2, 0, 1))
    metadata = json.loads((tmp_path / "out" / "metadata.json").read_text("utf-8"))
    assert metadata["bands"][0]["band_id"] == "led_05"


def test_envi_files_read_back_through_their_own_header(tmp_path: Path) -> None:
    cube = small_cube()
    export_envi(cube, str(tmp_path / "out"))
    lines = (tmp_path / "out" / "data.hdr").read_text("ascii").splitlines()[1:]
    fields = dict(line.split(" = ", 1) for line in lines)
    kinds = {"1": np.uint8, "4": np.float32}
    order = "<" if fields["byte order"] == "0" else ">"
    shape = (int(fields["bands"]), int(fields["lines"]), int(fields["samples"]))
    kind = np.dtype(kinds[fields["data type"]]).newbyteorder(order)
    data = np.fromfile(tmp_path / "out" / "data.bsq", kind, offset=int(fields["header offset"]))
    assert np.array_equal(data.reshape(shape).transpose(1, 2, 0), cube.values)


def test_envi_export_fails_when_the_file_does_not_read_back_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = np.fromfile

    def flipped(path: Any, dtype: Any) -> Any:
        data = real(path, dtype)
        data[0] += 1
        return data

    monkeypatch.setattr(np, "fromfile", flipped)
    with pytest.raises(InvalidStateError, match="did not read back"):
        export_envi(small_cube(), tmp_path / "out")


def test_envi_default_bands_point_at_red_green_blue_when_present(tmp_path: Path) -> None:
    leds = [HC1500.led(n) for n in (5, 6, 7, 8, 9)]
    cube = build_cube([picture(1, 2, 3)] * 5, picture(5, 5, 5), leds, "dominant", {})
    export_envi(cube, tmp_path / "out")
    assert "default bands = {5, 3, 1}" in (tmp_path / "out" / "data.hdr").read_text("ascii")
    without_green = build_cube(
        [picture(1, 2, 3)] * 2, picture(5, 5, 5), [HC1500.led(5), HC1500.led(9)], "dominant", {}
    )
    export_envi(without_green, tmp_path / "other")
    assert "default bands" not in (tmp_path / "other" / "data.hdr").read_text("ascii")


def test_tiff_export_has_one_float_page_per_band_and_a_description(tmp_path: Path) -> None:
    cube = small_cube(capture_id="abc-123")
    data_path, mask_path = export_tiff(cube, tmp_path / "out")
    with Image.open(data_path) as image:
        assert image.n_frames == 3
        description = json.loads(image.tag_v2[270])
        for index in range(3):
            image.seek(index)
            assert image.mode == "F"
            assert np.array_equal(np.asarray(image), cube.values[:, :, index])
    assert description["bands"][2]["band_id"] == "led_09"
    assert description["layer"] == "L1"
    assert description["mask_file"] == mask_path.name
    with Image.open(mask_path) as image:
        image.seek(2)
        assert np.array_equal(np.asarray(image), cube.mask[:, :, 2])
