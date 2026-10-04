# SPDX-License-Identifier: Apache-2.0
"""The HC1500 LED table: wavelengths, dominant channels, UV gating, starting PWM."""

from __future__ import annotations

import dataclasses

import pytest

from vispek_hc.errors import ConfigRejectedError
from vispek_hc.models import HC1500

NOMINAL_NM = [255, 275, 370, 400, 455, 520, 560, 610, 630, 667, 700, 804, 850, 890, 933, 973, 1050]
STARTING_PWM = [458, 300, 74, 74, 24, 224, 600, 74, 36, 18, 36, 74, 74, 75, 75, 150, 300]


def test_table_lists_17_leds_in_ascending_wavelength_order() -> None:
    leds = HC1500.led_table.leds
    assert [led.led_id for led in leds] == list(range(1, 18))
    assert [led.nm for led in leds] == NOMINAL_NM
    assert sorted(NOMINAL_NM) == NOMINAL_NM


# Dark-subtracted white-board medians observed per LED (R, G, B).
WHITE_RGB = [
    (17, 40, 153),
    (17, 41, 154),
    (36, 62, 184),
    (35, 32, 154),
    (18, 21, 139),
    (3, 182, 10),
    (79, 154, 3),
    (166, 21, 11),
    (175, 13, 15),
    (133, 12, 17),
    (169, 23, 25),
    (165, 65, 104),
    (182, 95, 154),
    (164, 81, 138),
    (142, 63, 114),
    (154, 74, 128),
    (132, 58, 105),
]


def test_white_board_observation_is_recorded_per_led() -> None:
    assert [led.white_rgb for led in HC1500.led_table.leds] == WHITE_RGB


def test_dominant_channel_is_the_strongest_white_board_component() -> None:
    for led in HC1500.led_table.leds:
        assert led.white_rgb["RGB".index(led.dominant_channel)] == max(led.white_rgb)


def test_dominant_channel_is_blue_then_green_then_red() -> None:
    dominant = "".join(led.dominant_channel for led in HC1500.led_table.leds)
    assert dominant == "BBBBB" + "GG" + "R" * 10


def test_band_identity_is_keyed_by_led_number_not_wavelength() -> None:
    led = HC1500.led(5)
    assert led.band_id == "led_05"
    assert led.name == "LED 5 · 455 nm"
    assert HC1500.led(17).band_id == "led_17"


def test_only_leds_1_to_4_are_uv_gated() -> None:
    gated = [led.led_id for led in HC1500.led_table.leds if led.needs_uv_permission]
    assert gated == [1, 2, 3, 4]
    assert [HC1500.led(n).hazard for n in (1, 2, 3, 4, 5)] == ["UV-C", "UV-C", "UV-A", "UV-A", None]


def test_default_channels_exclude_every_uv_led() -> None:
    assert HC1500.default_channels == tuple(range(5, 18))
    assert not any(HC1500.led(n).needs_uv_permission for n in HC1500.default_channels)


def test_table_is_versioned_and_unverified_until_hardware_signs_it_off() -> None:
    table = HC1500.led_table
    assert table.version
    assert table.verified is False
    assert not any(led.verified for led in table.leds)


def test_starting_pwm_follows_the_observed_white_board_values() -> None:
    assert [led.starting_pwm for led in HC1500.led_table.leds] == STARTING_PWM


@pytest.mark.parametrize("led_id", [0, 18, 22, -3])
def test_unknown_led_number_is_a_config_error(led_id: int) -> None:
    with pytest.raises(ConfigRejectedError) as caught:
        HC1500.led(led_id)
    assert caught.value.details == {"led_id": led_id, "valid": "1-17"}


def test_camera_facts() -> None:
    assert HC1500.camera_usb_id == "0c45:6370"
    assert HC1500.camera_name == "LRCP 1080P-60FPS"
    assert HC1500.serial_usb_vid == 0x1A86
    assert HC1500.black_level == 5
    assert HC1500.default_size == (1280, 720)
    assert HC1500.default_fps == 10
    assert (1920, 1080) in HC1500.sizes


def test_model_data_is_immutable() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        HC1500.led(5).nm = 460  # type: ignore[misc]


def test_initial_camera_profile_is_manual_with_sharpening_off() -> None:
    profile = HC1500.initial_controls
    assert profile["auto-exposure-mode"] == 1, "manual exposure"
    assert profile["auto-white-balance-temp"] == 0
    assert profile["auto-focus"] == 0
    assert profile["exposure-time-abs"] == 157, "15.7 ms, in units of 100 microseconds"
    assert profile["sharpness"] == 0, "sharpening invents signal at edges"
    assert "focus-abs" not in profile, "the lens is focused by hand"
    with pytest.raises(TypeError):
        profile["gain"] = 5  # type: ignore[index]
