# SPDX-License-Identifier: Apache-2.0
"""A session owns the three devices of one HC1500 and what is known about that unit.

Use it as a context manager. Leaving it switches every LED off, closes the serial port
and stops the camera stream, whatever happened inside.
"""

from __future__ import annotations

import logging
import math
import sys
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Any, Self

import numpy as np

from vispek_hc import units, video
from vispek_hc._windows_controls import DirectShowControls
from vispek_hc.controls import (
    EXPOSURE_AUTOMATIC,
    EXPOSURE_MODE,
    LockReport,
    UvcUtilControls,
    V4l2Controls,
    lock_parameters,
)
from vispek_hc.errors import ControlsUnavailableError, GainNotConfirmedError, VispekHCError
from vispek_hc.framecheck import reduce
from vispek_hc.illuminator import Illuminator
from vispek_hc.interfaces import CameraControls, Clock, Frame, FrameSource, SystemClock
from vispek_hc.models import HC1500, Channel, DeviceModel
from vispek_hc.sim import SimulatedRig
from vispek_hc.units import UnitProfile

log = logging.getLogger(__name__)

PLATFORM = sys.platform
DEFAULT_UNIT = "hc1500-1"
PROBE_LED = 6  # 520 nm: visible, harmless, strong camera response; lit at its scan PWM
BASELINE_FRAMES = 20  # at most this many frames are waited for the picture to settle
BASELINE_STEP_DN = 0.5
PROBE_ON_S = 0.3
PROBE_WAIT_S = 0.7  # how much longer the lit frames may take to arrive
PROBE_RISE_DN = 2.0
SETTLE_S = 0.5  # after switching LEDs, before trusting a frame, when no latency is known


@dataclass(frozen=True, slots=True)
class PairResult:
    paired: bool  # the camera saw the LED: both belong to the same device
    led_id: int
    pwm: int
    rise_dn: float  # largest rise of the frame mean while the LED was on
    latency_s: float | None  # ON acknowledged -> first brighter frame reached the host
    fps: float | None


# The camera's kept gain is settled by a second of darkness in automatic exposure
# (measured: half a second is enough), and "darkness" is a picture near the black level.
DARK_LOCK_SETTLE_S = 1.0
DARK_LOCK_LIMIT = 12


class Session:
    def __init__(
        self,
        light: Illuminator,
        frames: FrameSource,
        controls: CameraControls | None,
        *,
        unit: UnitProfile,
        clock: Clock | None = None,
        model: DeviceModel = HC1500,
        persist: bool = True,
        simulated: bool = False,
        camera_info: dict[str, Any] | None = None,
        controls_info: dict[str, str] | None = None,
    ) -> None:
        self.light = light
        self.frames = frames
        self.controls = controls
        self.unit = unit
        self.model = model
        self.simulated = simulated
        self.lock_report: LockReport | None = None
        self._lock_generation: int | None = None  # the stream the lock was taken on
        self.clock: Clock = clock or SystemClock()
        self._persist = persist
        self._camera_info = camera_info or {}
        self._controls_info = controls_info

    @classmethod
    def from_rig(cls, rig: SimulatedRig) -> Self:
        """A session on simulated devices. Nothing it learns is written to disk."""
        rig.light.connect(baud=115200)
        return cls(
            rig.light,
            rig.frames,
            rig.controls,
            unit=UnitProfile("sim"),
            clock=rig.clock,
            persist=False,
            simulated=True,
            camera_info={
                "name": "simulated",
                "backend": "sim",
                "source": "uncompressed",
                "requested_size": list(rig.size),
                "requested_fps": rig.fps,
            },
            controls_info={"backend": "sim", "device_id": "sim"},
        )

    def __enter__(self) -> Self:
        try:
            self.frames.start()
            # The first frame can take a while: camera start-up, or a permission prompt.
            self.frames.wait_new(timeout=video.STARTUP_S)
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.light.all_off()
        finally:
            self._close()

    def _close(self) -> None:
        try:
            self.light.close()
        finally:
            self.frames.stop()

    def describe(self) -> dict[str, Any]:
        """Evidence about the devices in use, for metadata. Not a key for anything."""
        return {
            "simulated": self.simulated,
            "unit_id": self.unit.unit_id,
            "serial": {"port": self.light.port, "baud": self.light.baud},
            "camera": self._camera_info,
            "controls": self._controls_info,
        }

    def save_unit(self) -> None:
        if self._persist:
            units.save_profile(self.unit)

    @property
    def lock_stale(self) -> bool:
        """Whether the stream was reopened since the lock: the camera may be back in
        automatic mode, and the gain it keeps is no longer known. Costs nothing to ask."""
        return self.lock_report is not None and self._lock_generation != self.frames.generation

    @property
    def lock_is_current(self) -> bool:
        """Whether the last lock still holds: the same stream, the camera still in manual
        exposure. Reads the camera's controls, so do not ask many times a second."""
        if self.lock_report is None or self.controls is None or self.lock_stale:
            return False
        mode = self.controls.read_all().get(EXPOSURE_MODE)
        return mode is None or mode.current == 1

    def lock_controls(self, *, require_dark: bool = True, keep: bool = False) -> LockReport:
        """Lock the camera parameters on the running stream and confirm by reading back.

        The camera keeps the gain its automatic exposure had chosen at the moment that is
        switched off, and reports none of it (a factor of 4.4 between darkness and light).
        So the first lock on a stream is taken from a known state: every LED is switched
        off, the camera is put in automatic exposure and must see darkness, where that
        gain sits at its ceiling. If it sees light, ``GAIN_NOT_CONFIRMED`` is raised;
        ``require_dark=False`` locks anyway and marks the report ``undetermined``.

        ``keep=True`` is for the lock at the start of every scan: if this session took a
        dark lock on the same stream and the camera is still in manual exposure, the
        parameters are only written again and no LED is switched. A lock asked for
        without it always passes through darkness: nothing the camera reports shows
        whether another program moved the gain in between.
        """
        if self.controls is None:
            raise ControlsUnavailableError(
                "no way to set camera parameters on this computer",
                hint="macOS: build tools/uvc-util. Linux: install v4l-utils. Windows: install "
                "the Windows dependencies and allow desktop camera access. Otherwise scan "
                "with --unlocked and accept that white balance and exposure may move",
            )
        wanted = self.unit.camera_controls or self.model.initial_controls
        earlier = self.lock_report
        if keep and earlier is not None and earlier.gain_state == "dark" and self.lock_is_current:
            state, level = earlier.gain_state, earlier.dark_level
        elif EXPOSURE_MODE not in self.controls.read_all():
            state, level = "undetermined", None  # no automatic exposure to pass through
        else:
            self.lock_report = None
            state, level = self._settle_gain(require_dark)
        report = lock_parameters(self.controls, wanted, settle=lambda: self.frames.wait_new(3))
        report = replace(report, gain_state=state, dark_level=level)
        self.lock_report, self._lock_generation = report, self.frames.generation
        if report.awb_off and report.exposure_manual is not False:
            self.unit.camera_controls = dict(report.effective)
            self.save_unit()
        return report

    def _settle_gain(self, require_dark: bool) -> tuple[str, tuple[float, float, float]]:
        """Put the camera's hidden gain in its one repeatable state: the ceiling it
        reaches when automatic exposure sees darkness."""
        assert self.controls is not None
        if any(state.state != "off" for state in self.light.states.values()):
            self.light.all_off()
        self.controls.write(EXPOSURE_MODE, EXPOSURE_AUTOMATIC)
        fps = self.frames.measured_fps or self.unit.measured_fps or self.model.default_fps
        self.frames.wait_new(max(3, math.ceil(DARK_LOCK_SETTLE_S * fps)))
        picture = video.decode(self.frames.wait_new()).reshape(-1, 3).mean(axis=0)
        level = (
            round(float(picture[0]), 1),
            round(float(picture[1]), 1),
            round(float(picture[2]), 1),
        )
        if max(level) <= DARK_LOCK_LIMIT:
            return "dark", level
        if require_dark:
            raise GainNotConfirmedError(
                "the camera sees light, so the gain it keeps from this moment is unknown "
                f"(picture at {level}, darkness is at most {DARK_LOCK_LIMIT})",
                hint="close the lid of the dark box and lock again; every LED is off "
                "already. Without a dark box, scan with --unlocked: such scans are "
                "comparable with nothing",
                details={"dark_level": list(level), "limit": DARK_LOCK_LIMIT},
            )
        return "undetermined", level

    def _next_mean(self, channel: Channel) -> tuple[float, Frame]:
        frame = self.frames.wait_new()
        return float(reduce(video.decode(frame), channel).mean()), frame

    def pair_probe(self, relearn: bool = False) -> PairResult:
        """Blink LED 6 (520 nm, visible) at its usual scan power and watch the camera react.

        Confirms that this serial port and this camera are the same device, and measures
        how long a change of light takes to show up in the frames that reach the host.
        The first measurement is stored in the unit's profile. Later ones are only
        reported, unless ``relearn`` is set: scan timing is derived from the stored value
        and is part of what binds a white reference, so it must not move with every
        measurement.
        """
        led = self.model.led(PROBE_LED)
        channel = led.dominant_channel
        pwm = self.unit.pwm_by_led.get(PROBE_LED, led.starting_pwm)
        # The baseline is taken once two frames in a row agree: frames lit by an LED that
        # has just gone off, or a camera still settling after its parameters changed,
        # would hide the probe's own light.
        baseline, _ = self._next_mean(channel)
        for _ in range(BASELINE_FRAMES):
            level, _ = self._next_mean(channel)
            settled = abs(level - baseline) <= BASELINE_STEP_DN
            baseline = level
            if settled:
                break
        rise, latency = 0.0, None
        try:
            acked = self.light.on(PROBE_LED, pwm)
            assert acked.acked_at is not None
            while self.clock.now() < acked.acked_at + PROBE_ON_S:
                level, frame = self._next_mean(channel)
                rise = max(rise, level - baseline)
                if latency is None and level - baseline > PROBE_RISE_DN:
                    latency = frame.received_at - acked.acked_at
        finally:
            self.light.off(PROBE_LED)
        # A slow pipeline delivers the lit frames only after the LED is off again.
        while latency is None and self.clock.now() < acked.acked_at + PROBE_ON_S + PROBE_WAIT_S:
            level, frame = self._next_mean(channel)
            rise = max(rise, level - baseline)
            if level - baseline > PROBE_RISE_DN:
                latency = frame.received_at - acked.acked_at
        fps = self.frames.measured_fps
        if latency is not None and (relearn or self.unit.pipeline_latency_ms is None):
            self.unit.pipeline_latency_ms = latency * 1000
            self.unit.measured_fps = fps
            self.save_unit()
        return PairResult(latency is not None, PROBE_LED, pwm, rise, latency, fps)

    def black_level(self) -> tuple[int, int, int]:
        """Median RGB of a frame taken with every LED off. This camera gives (5, 5, 5)."""
        self.light.all_off()
        self.clock.sleep(SETTLE_S)
        rgb = video.decode(self.frames.wait_new())
        red, green, blue = (int(np.median(rgb[..., i])) for i in range(3))
        return red, green, blue


def open_session(
    *,
    simulate: bool = False,
    port: str | None = None,
    baud: int | None = None,
    camera: str | None = None,
    unit: str = DEFAULT_UNIT,
    allow_uv: bool = False,
    size: tuple[int, int] = HC1500.default_size,
    fps: float = HC1500.default_fps,
    source: video.Source = "uncompressed",
) -> Session:
    """Find and open the light source, the camera and (where possible) its controls."""
    if simulate:
        return Session.from_rig(SimulatedRig(allow_uv=allow_uv))
    profile = units.load_profile(unit)  # refuses a bad label before any hardware is touched
    light = Illuminator.open(port, baud=baud, allow_uv=allow_uv)
    try:
        version = video.ffmpeg_version()
        name = video.choose_camera(camera, video.list_cameras())
        frames = video.FfmpegSource(name, size=size, fps=fps, source=source, version=version)
        controls: UvcUtilControls | V4l2Controls | DirectShowControls | None = None
        try:
            if PLATFORM == "darwin":
                controls = UvcUtilControls.open()
            elif PLATFORM == "linux":
                controls = V4l2Controls.open(name)  # the node the frames come from
            elif PLATFORM == "win32":
                controls = DirectShowControls.open(name)
        except VispekHCError as error:  # preview still works; scans will insist on a lock
            log.warning("camera parameters cannot be locked: %s", error.message)
    except BaseException:
        light.close()
        raise
    return Session(
        light,
        frames,
        controls,
        unit=profile,
        camera_info={
            "name": name,
            "backend": "ffmpeg",
            "source": video.pixel_path(PLATFORM, source),
            "ffmpeg_version": ".".join(map(str, version)) if version else None,
            "requested_size": list(size),
            "requested_fps": fps,
        },
        controls_info={"backend": controls.backend, "device_id": controls.device_id}
        if controls
        else None,
    )
