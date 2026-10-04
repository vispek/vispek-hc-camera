# SPDX-License-Identifier: Apache-2.0
"""Every example script runs offline, on the simulated device."""

from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path

import numpy as np
import pytest

EXAMPLES = Path(__file__).parent.parent / "examples"
SIMULATED = {"01", "02", "03", "04", "08", "09"}
SCRIPTS = sorted(EXAMPLES.glob("[0-9][0-9]_*.py"))

pytestmark = pytest.mark.usefixtures("quiet_host")


def test_the_documented_examples_are_all_there() -> None:
    assert [path.name[:2] for path in SCRIPTS] == [f"{n:02d}" for n in range(1, 14)]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_example_is_short_and_says_what_hardware_it_needs(script: Path) -> None:
    text = script.read_text("utf-8")
    assert len(text.splitlines()) <= 80
    assert "Hardware:" in text
    assert f"python examples/{script.name}" in text


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.stem)
def test_example_runs(
    script: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    argv = [str(script), *(["--simulate"] if script.name[:2] in SIMULATED else [])]
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_path(str(script), run_name="__main__")
    out = capsys.readouterr().out
    assert out.strip(), "an example shows what it did"
    assert "Traceback" not in out
    if script.name.startswith("12"):
        assert out.count("refused:") == 3, "nothing but band arithmetic gets through"
    if script.name.startswith("13"):
        has_sklearn = importlib.util.find_spec("sklearn") is not None
        assert ("cluster sizes:" in out) == has_sklearn


def test_simulated_scene_has_three_patches_on_an_even_board() -> None:
    from vispek_hc import HC1500, samples

    layers = samples.scene()
    assert layers.shape == (72, 128, 17)
    assert layers[2, 0].tolist() == layers[2, 127].tolist() == [samples.BOARD] * 17
    red, near_infrared = HC1500.led(10).led_id - 1, HC1500.led(12).led_id - 1
    x, y, _, _ = samples.PATCHES["leaf"]
    assert layers[y, x, near_infrared] > 5 * layers[y, x, red], "dark in red, bright beyond"
    x, y, w, h = samples.PATCHES["grey"]
    assert np.all(layers[y : y + h, x : x + w] == 0.4)
    assert layers.min() >= 0 and layers.max() <= 1


def test_simulated_light_is_uneven_and_the_white_reference_takes_it_out(tmp_path: Path) -> None:
    import vispek_hc
    from vispek_hc import samples

    light = samples.illumination()
    assert light.shape == (72, 128)
    assert (light[0, 0], light[0, -1]) == (1.0, pytest.approx(0.85))
    sample, white = samples.simulated(tmp_path)
    raw = sample.cube().values[5, :, 4]  # LED 9 along a row of plain board
    assert raw[2] - raw[-3] > 15, "the raw picture is darker on the right"
    relative = vispek_hc.apply_calibration(sample, vispek_hc.make_calibration([white]))
    row = relative.values[5, :, 4]
    assert np.all(np.abs(row - samples.BOARD) < 0.01), "after division the board is even"


def test_example_captures_are_recorded_on_the_simulated_device(tmp_path: Path) -> None:
    import vispek_hc

    sample, white = vispek_hc.example_captures(str(tmp_path / "work"))
    assert sample.document["device"]["simulated"] is True
    assert white.document["recording"]["role"] == "white"
    cube = vispek_hc.apply_calibration(sample, vispek_hc.make_calibration([white]))
    x, y, _, _ = vispek_hc.samples.PATCHES["red"]
    spectrum = cube.values[y + 2, x + 2]
    assert spectrum[0] == pytest.approx(0.05, abs=0.01), "455 nm"
    assert spectrum[4] == pytest.approx(0.70, abs=0.01), "630 nm"
