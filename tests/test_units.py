# SPDX-License-Identifier: Apache-2.0
"""Per-device profiles under VISPEK_HC_HOME."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from vispek_hc import units
from vispek_hc.errors import (
    ConfigRejectedError,
    PermissionDeniedError,
    SchemaUnsupportedError,
)
from vispek_hc.units import UnitProfile


def test_home_follows_the_environment_variable(home: Path) -> None:
    assert units.home() == home


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("darwin", {}, "~/Library/Application Support/vispek-hc"),
        ("linux", {}, "~/.config/vispek-hc"),
        ("linux", {"XDG_CONFIG_HOME": "/xdg"}, "/xdg/vispek-hc"),
        ("win32", {"APPDATA": "/roaming"}, "/roaming/vispek-hc"),
    ],
)
def test_default_home_per_platform(
    platform: str, env: dict[str, str], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("VISPEK_HC_HOME", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "platform", platform)
    assert units.home() == Path(expected).expanduser()


def test_a_unit_never_seen_before_loads_as_an_empty_profile(home: Path) -> None:
    profile = units.load_profile("bench-2")
    assert profile == UnitProfile(unit_id="bench-2")
    assert not home.exists(), "loading must not create files"


def test_profile_round_trips_through_disk(home: Path) -> None:
    profile = UnitProfile(
        unit_id="hc1500-1",
        setup_label="2.5mm lens, 120 mm",
        camera_controls={"exposure-time-abs": 157, "gain": 0},
        pwm_by_led={5: 24, 17: 300},
        min_on_ms_by_led={5: 300},
        pipeline_latency_ms=152.5,
        measured_fps=9.98,
    )
    path = units.save_profile(profile)
    assert path == home / "units" / "hc1500-1.json"
    assert units.load_profile("hc1500-1") == profile
    stored = json.loads(path.read_text("utf-8"))
    assert stored["schema"] == "vispek-hc.unit/1"
    assert stored["pwm_by_led"] == {"5": 24, "17": 300}
    assert [p.name for p in path.parent.iterdir()] == ["hc1500-1.json"], "no temp file left"


def test_saved_profile_holds_no_absolute_path_or_host_name(home: Path) -> None:
    text = units.save_profile(UnitProfile(unit_id="hc1500-1")).read_text("utf-8")
    assert str(home) not in text
    assert str(Path.home()) not in text


@pytest.mark.parametrize("unit_id", ["", "../evil", "a/b", "a\\b", ".hidden", "x" * 65, "a b"])
def test_unit_id_must_be_a_plain_label(unit_id: str, home: Path) -> None:
    with pytest.raises(ConfigRejectedError):
        units.load_profile(unit_id)
    with pytest.raises(ConfigRejectedError):
        units.save_profile(UnitProfile(unit_id=unit_id))


def test_unknown_fields_are_ignored_and_missing_ones_default(home: Path) -> None:
    (home / "units").mkdir(parents=True)
    (home / "units" / "u1.json").write_text(
        json.dumps({"schema": "vispek-hc.unit/1", "unit_id": "u1", "from_the_future": 1})
    )
    assert units.load_profile("u1") == UnitProfile(unit_id="u1")


@pytest.mark.parametrize("schema", ["vispek-hc.unit/2", "something-else/1", None])
def test_other_schema_versions_are_refused(schema: str | None, home: Path) -> None:
    (home / "units").mkdir(parents=True)
    (home / "units" / "u1.json").write_text(json.dumps({"schema": schema, "unit_id": "u1"}))
    with pytest.raises(SchemaUnsupportedError):
        units.load_profile("u1")


@pytest.mark.parametrize("content", ["", "{not json", "[1, 2]"])
def test_unreadable_profile_is_reported_with_the_way_out(content: str, home: Path) -> None:
    (home / "units").mkdir(parents=True)
    (home / "units" / "u1.json").write_text(content)
    with pytest.raises(SchemaUnsupportedError) as caught:
        units.load_profile("u1")
    assert "delete" in (caught.value.hint or "")


def test_empty_xdg_setting_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VISPEK_HC_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setattr(sys, "platform", "linux")
    assert units.home() == Path("~/.config/vispek-hc").expanduser()


@pytest.mark.parametrize(
    "fields",
    [
        {"pwm_by_led": [1, 2]},
        {"pwm_by_led": {"abc": 3}},
        {"pwm_by_led": {"5": "bright"}},
        {"camera_controls": "x"},
        {"measured_fps": "fast"},
        {"setup_label": 12},
    ],
)
def test_profile_with_values_of_the_wrong_shape_is_refused(
    fields: dict[str, object], home: Path
) -> None:
    (home / "units").mkdir(parents=True)
    (home / "units" / "u1.json").write_text(
        json.dumps({"schema": "vispek-hc.unit/1", "unit_id": "u1", **fields})
    )
    with pytest.raises(SchemaUnsupportedError):
        units.load_profile("u1")


def test_profile_that_cannot_be_read_is_a_permission_problem(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "units").mkdir(parents=True)
    (home / "units" / "u1.json").write_text("{}")

    def denied(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionDeniedError):
        units.load_profile("u1")
