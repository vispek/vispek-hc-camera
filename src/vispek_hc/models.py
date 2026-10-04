# SPDX-License-Identifier: Apache-2.0
"""Device models. Today: the HC1500 and its LED table.

LED number *n* is the *n*-th entry of the ascending wavelength list in the product data
sheet. Wavelengths are nominal LED centre wavelengths, not a measured sensor response.
Until the hardware team signs the table off, every entry is marked ``verified=False``;
captures record which table version labelled them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from vispek_hc.errors import ConfigRejectedError

Channel = Literal["R", "G", "B"]


@dataclass(frozen=True, slots=True)
class Led:
    led_id: int
    nm: int
    # Observed on one white board at one distance: the PWM used, and the dark-subtracted
    # median (R, G, B) it produced. A first guess for other setups, nothing more.
    starting_pwm: int
    white_rgb: tuple[int, int, int]
    hazard: Literal["UV-C", "UV-A"] | None = None
    verified: bool = False

    @property
    def dominant_channel(self) -> Channel:
        """The camera channel that responds most strongly under this LED."""
        channels: tuple[Channel, Channel, Channel] = ("R", "G", "B")
        return channels[self.white_rgb.index(max(self.white_rgb))]

    @property
    def band_id(self) -> str:
        """Stable key of the band this LED produces. The wavelength is not part of it."""
        return f"led_{self.led_id:02d}"

    @property
    def name(self) -> str:
        return f"LED {self.led_id} · {self.nm} nm"

    @property
    def needs_uv_permission(self) -> bool:
        return self.hazard is not None


@dataclass(frozen=True, slots=True)
class LedTable:
    version: str
    verified: bool
    leds: tuple[Led, ...]


@dataclass(frozen=True, slots=True)
class DeviceModel:
    name: str
    led_table: LedTable
    default_channels: tuple[int, ...]
    camera_usb_id: str  # "vid:pid", lower-case hex
    camera_name: str
    serial_usb_vid: int
    sizes: tuple[tuple[int, int], ...]
    default_size: tuple[int, int]
    default_fps: int
    black_level: int  # code value the camera clamps darkness to
    # Range of the camera's uncompressed YUV: "limited" is 16-235 for luma. Measured on
    # macOS (2026-10-02): every LED off is Y = 20 there and 5 through the JPEG path.
    yuv_range: Literal["full", "limited"]
    initial_controls: Mapping[str, int]  # camera parameters to lock before a unit has its own

    def led(self, led_id: int) -> Led:
        leds = self.led_table.leds
        if not 1 <= led_id <= len(leds):
            raise ConfigRejectedError(
                f"{self.name} has no LED {led_id}",
                hint=f"valid LED numbers are 1-{len(leds)}",
                details={"led_id": led_id, "valid": f"1-{len(leds)}"},
            )
        return leds[led_id - 1]


HC1500 = DeviceModel(
    name="HC1500",
    led_table=LedTable(
        version="hc1500-nominal-1",
        verified=False,
        leds=(
            Led(1, 255, 458, (17, 40, 153), hazard="UV-C"),
            Led(2, 275, 300, (17, 41, 154), hazard="UV-C"),
            Led(3, 370, 74, (36, 62, 184), hazard="UV-A"),
            Led(4, 400, 74, (35, 32, 154), hazard="UV-A"),
            Led(5, 455, 24, (18, 21, 139)),
            Led(6, 520, 224, (3, 182, 10)),
            Led(7, 560, 600, (79, 154, 3)),
            Led(8, 610, 74, (166, 21, 11)),
            Led(9, 630, 36, (175, 13, 15)),
            Led(10, 667, 18, (133, 12, 17)),
            Led(11, 700, 36, (169, 23, 25)),
            Led(12, 804, 74, (165, 65, 104)),
            Led(13, 850, 74, (182, 95, 154)),
            Led(14, 890, 75, (164, 81, 138)),
            Led(15, 933, 75, (142, 63, 114)),
            Led(16, 973, 150, (154, 74, 128)),
            Led(17, 1050, 300, (132, 58, 105)),
        ),
    ),
    default_channels=tuple(range(5, 18)),
    camera_usb_id="0c45:6370",
    camera_name="LRCP 1080P-60FPS",
    serial_usb_vid=0x1A86,
    sizes=((640, 480), (1280, 720), (1920, 1080)),
    default_size=(1280, 720),
    default_fps=10,
    black_level=5,
    yuv_range="limited",
    # UVC control names as uvc-util spells them. Exposure is in units of 100 microseconds.
    initial_controls=MappingProxyType(
        {
            "auto-white-balance-temp": 0,
            "white-balance-temp": 4600,
            "auto-exposure-mode": 1,
            "auto-exposure-priority": 0,
            "exposure-time-abs": 157,
            "gain": 0,
            "auto-focus": 0,
            "brightness": 0,
            "contrast": 32,
            "saturation": 66,
            "sharpness": 0,
            "gamma": 100,
            "power-line-frequency": 1,
            "backlight-compensation": 1,
        }
    ),
)
