# SPDX-License-Identifier: Apache-2.0
"""What the software writes matches the published JSON Schemas."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import numpy as np
import pytest

from vispek_hc import cli
from vispek_hc.calibration import make_calibration
from vispek_hc.cube import load_capture
from vispek_hc.reflectance import apply_calibration
from vispek_hc.session import Session
from vispek_hc.sim import SimulatedRig
from vispek_hc.sweep import ScanSettings, record, record_frames

SCHEMAS = Path(__file__).parent.parent / "schemas"
pytestmark = pytest.mark.usefixtures("quiet_host")


def read_npz(path: Path) -> dict[str, Any]:
    """The arrays of an .npz file, read without pickle and with the file closed again."""
    with np.load(path, allow_pickle=False) as stored:
        return {name: stored[name] for name in stored.files}


def validator(name: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text("utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def assert_valid(name: str, document: Any) -> None:
    errors = [
        f"{list(e.absolute_path)}: {e.message}" for e in validator(name).iter_errors(document)
    ]
    assert errors == []


def is_valid(name: str, document: Any) -> bool:
    return validator(name).is_valid(document)


@pytest.fixture
def captures(tmp_path: Path) -> dict[str, Path]:
    rig = SimulatedRig()
    paths = {name: tmp_path / name for name in ("sample", "white", "single", "failed")}
    with Session.from_rig(rig) as session:
        session.pair_probe()
        record(session, paths["sample"], ScanSettings(channels=(5, 7, 9)))
        record(session, paths["white"], ScanSettings(channels=(5, 7, 9), role="white"))
        record_frames(session, paths["single"], "single")
        rig.device.dead_leds = {7}
        with pytest.raises(Exception, match="no light"):
            record(session, paths["failed"], ScanSettings(channels=(5, 7, 9), role="white"))
    return paths


def read(path: Path) -> Any:
    return json.loads(path.read_text("utf-8"))


def test_every_schema_is_a_valid_2020_12_schema_with_its_own_id() -> None:
    names = sorted(path.name for path in SCHEMAS.glob("*.json"))
    assert names == [
        "calibration-1.schema.json", "capture-1.schema.json", "cli-envelope-1.schema.json",
        "cube-metadata-1.schema.json",
    ]  # fmt: skip
    for name in names:
        schema = read(SCHEMAS / name)
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$id"] == "urn:vispek-hc:schema:" + name.removesuffix(".schema.json")


@pytest.mark.parametrize("name", ["sample", "white", "single", "failed"])
def test_recorded_captures_match_the_capture_schema(name: str, captures: dict[str, Path]) -> None:
    assert_valid("capture-1", read(captures[name] / "capture.json"))


def test_capture_schema_refuses_what_a_reader_must_not_trust(captures: dict[str, Path]) -> None:
    good = read(captures["sample"] / "capture.json")
    assert is_valid("capture-1", {**good, "something_new": 1}), "unknown fields are fine"
    for change in (
        lambda d: d.update(schema="vispek-hc.capture/2"),
        lambda d: d["groups"][0]["frames"][0].update(file="../../etc/passwd"),
        lambda d: d["groups"][0]["frames"][0].update(sha256="short"),
        lambda d: d["recording"].update(status="finished"),
        lambda d: d["groups"][0].pop("dark"),
        lambda d: d.pop("capture_id"),
    ):
        broken = copy.deepcopy(good)
        change(broken)
        assert not is_valid("capture-1", broken)


def test_calibration_and_cube_metadata_match_their_schemas(
    captures: dict[str, Path], tmp_path: Path
) -> None:
    calibration = make_calibration([load_capture(captures["white"])])
    calibration.save(tmp_path / "calib")
    assert_valid("calibration-1", read(tmp_path / "calib" / "calibration.json"))

    sample = load_capture(captures["sample"])
    for name, cube in (
        ("raw", sample.cube()),
        ("relative", apply_calibration(sample, calibration)),
    ):
        stored = read_npz(cube.save(tmp_path / f"{name}.npz"))
        assert_valid("cube-metadata-1", json.loads(str(stored["metadata_json"])))


def test_cube_metadata_schema_ties_layer_and_unit_together() -> None:
    band = {"band_id": "led_05", "name": "LED 5", "center_nm": 455, "dominant_channel": "B"}
    raw = {
        "layer": "L1", "unit": "image_code_value", "signal_domain": "dominant_channel_v1",
        "reduction": "dominant", "axes": "YXB", "synchronization": "sequential_not_simultaneous",
        "registration": "not_verified", "led_table": {}, "bands": [band],
    }  # fmt: skip
    assert is_valid("cube-metadata-1", raw)
    assert not is_valid("cube-metadata-1", {**raw, "layer": "L2"}), "L2 needs its calibration"
    assert not is_valid("cube-metadata-1", {**raw, "axes": "XYB"})


def envelope(capsys: pytest.CaptureFixture[str], *argv: Any) -> dict[str, Any]:
    cli.main([*map(str, argv), "--json"])
    document: dict[str, Any] = json.loads(capsys.readouterr().out)
    return document


def test_every_command_prints_an_envelope_that_matches_the_schema(
    captures: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sample, white, calib = captures["sample"], captures["white"], tmp_path / "calib"
    commands: list[list[Any]] = [
        ["--version"],
        ["check", "--simulate"],
        ["check"],
        ["devices"],
        ["devices", "--pair", "--simulate"],
        ["led", "on", 5, 24, "--hold", 1, "--simulate"],
        ["led", "off", "--all", "--simulate"],
        ["led", "on", 1, 10, "--simulate"],
        ["record", "-o", tmp_path / "r", "--channels", "5,9", "--simulate"],
        ["record", "-o", tmp_path / "r2", "--dry-run", "--simulate"],
        ["reference", "white", "-o", tmp_path / "w", "--channels", "5,9", "--simulate"],
        ["tune", "-o", tmp_path / "t", "--channels", "5,9", "--simulate"],
        ["calibrate", "--white", white, "-o", calib],
        ["reflectance", sample, "--calib", calib, "-o", tmp_path / "c.npz"],
        ["view", sample, "-o", tmp_path / "v.png"],
        ["view", sample, "--mode", "ndvi", "-o", tmp_path / "n.png"],
        ["spectrum", sample, "--xy", "1,1"],
        ["export", sample, "--format", "tiff", "-o", tmp_path / "x"],
        ["repair", sample],
        ["demo", "-o", tmp_path / "demo"],
        ["setup", "--dry-run"],
        ["no-such-command"],
        ["view", tmp_path / "nothing", "-o", tmp_path / "v.png"],
    ]
    assert {str(command[0]) for command in commands} >= set(cli.HANDLERS)
    for command in commands:
        document = envelope(capsys, *command)
        assert_valid("cli-envelope-1", document)


def test_envelope_schema_refuses_mixed_or_incomplete_envelopes() -> None:
    success = {"schema": "vispek-hc.cli/1", "command": "check", "version": "0.1.0", "ok": True,
               "data": {}, "warnings": [], "exit_code": 0}  # fmt: skip
    error = {"code": "USAGE_ERROR", "message": "m", "hint": None, "details": {}}
    assert is_valid("cli-envelope-1", success)
    assert is_valid("cli-envelope-1", {**success, "added_later": 1})
    assert not is_valid("cli-envelope-1", {**success, "exit_code": 5})
    assert not is_valid("cli-envelope-1", {**success, "error": error})
    failure = {k: v for k, v in success.items() if k != "data"} | {"ok": False, "exit_code": 2}
    assert not is_valid("cli-envelope-1", failure), "an error envelope needs its error"
    assert is_valid("cli-envelope-1", {**failure, "error": error})
    assert not is_valid("cli-envelope-1", {**failure, "error": {**error, "extra": 1}})
