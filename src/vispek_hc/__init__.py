# SPDX-License-Identifier: Apache-2.0
"""SDK for the Vispek HC1500 multispectral camera.

Importing this package has no side effects: no device is opened, no signal handler is
installed, and nothing is written to disk.

Two levels. One call does everything::

    import vispek_hc
    capture = vispek_hc.capture("first-scan", simulate=True)   # drop simulate with hardware
    cube = capture.cube()            # values (H, W, bands), mask, bands

Or hold a session and use the parts: ``open_session``, ``record``, ``make_calibration``,
``apply_calibration``, ``render``, ``spectrum``. ``docs/api.md`` lists every public name.
"""

from importlib.metadata import version

__version__ = version("vispek-hc")

from vispek_hc import illuminator, video, views
from vispek_hc.calibration import Calibration, make_calibration
from vispek_hc.cube import Band, Capture, Cube, export_envi, export_tiff, load_capture
from vispek_hc.errors import VispekHCError
from vispek_hc.framecheck import MaskBit
from vispek_hc.illuminator import Illuminator
from vispek_hc.models import HC1500
from vispek_hc.reflectance import apply_calibration
from vispek_hc.samples import example_captures
from vispek_hc.session import Session, open_session
from vispek_hc.sim import SimulatedRig
from vispek_hc.spectra import Region, spectrum, write_csv
from vispek_hc.sweep import (
    ScanSettings,
    auto_pwm,
    capture,
    iter_scans,
    keep_pwm,
    record,
    record_frames,
    tune,
)
from vispek_hc.video import FfmpegSource
from vispek_hc.views import principal_components, render, save_png

__all__ = [
    "HC1500",
    "Band",
    "Calibration",
    "Capture",
    "Cube",
    "FfmpegSource",
    "Illuminator",
    "MaskBit",
    "Region",
    "ScanSettings",
    "Session",
    "SimulatedRig",
    "VispekHCError",
    "__version__",
    "apply_calibration",
    "auto_pwm",
    "capture",
    "example_captures",
    "export_envi",
    "export_tiff",
    "illuminator",
    "iter_scans",
    "keep_pwm",
    "load_capture",
    "make_calibration",
    "open_session",
    "principal_components",
    "record",
    "record_frames",
    "render",
    "save_png",
    "spectrum",
    "tune",
    "video",
    "views",
    "write_csv",
]
