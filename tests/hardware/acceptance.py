# SPDX-License-Identifier: Apache-2.0
"""Acceptance checks for a real HC1500.

Each check takes an open :class:`vispek_hc.Session`, fails with an ``AssertionError`` that
says what is wrong, and returns a record of what it measured. ``test_hardware.py`` runs
them on the device (``pytest -m hardware tests/hardware``) and writes the records to a
file; ``tests/test_acceptance_checks.py`` runs the same code on the simulated device, so
the checks themselves are tested without hardware.

No ultraviolet LED is lit: only LEDs 5-17, one at a time, each switched off in a
``finally`` block. LEDs 12-17 are near infrared and invisible: seeing nothing does not
mean nothing is lit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from vispek_hc import (
    HC1500,
    Illuminator,
    ScanSettings,
    Session,
    SimulatedRig,
    VispekHCError,
    sweep,
    video,
)
from vispek_hc.calibration import make_calibration
from vispek_hc.framecheck import reduce
from vispek_hc.reflectance import apply_calibration

PROBE_LED = 6  # 520 nm: visible, and the camera answers in one channel only
NO_UV = tuple(range(5, 18))  # the default channels; 12-17 are near infrared
REHEARSAL = "VISPEK_HC_ACCEPTANCE_SIMULATE"


def simulated() -> bool:
    """Whether this run is a rehearsal on the simulated device.

    Unset or ``0``: the real device. ``1``: the simulated one. Anything else is refused,
    so that a typo can neither turn a rehearsal into a run on hardware nor the reverse.
    """
    value = os.environ.get(REHEARSAL, "0")
    if value not in ("0", "1"):
        raise ValueError(
            f"{REHEARSAL}={value!r}: use 1 for a rehearsal, 0 or nothing for the device"
        )
    return value == "1"


def device_options(**extra: Any) -> dict[str, Any]:
    """How to open the device, from the environment (as --port, --camera, --unit do)."""
    return {
        "simulate": simulated(),
        "port": os.environ.get("VISPEK_HC_PORT"),
        "camera": os.environ.get("VISPEK_HC_CAMERA"),
        "unit": os.environ.get("VISPEK_HC_UNIT", "hc1500-1"),
        **extra,
    }


def open_light(ack_timeout_s: float) -> Illuminator:
    """The light source alone, with a reply timeout of its own."""
    if simulated():
        rig = SimulatedRig()
        light = Illuminator(rig.device, clock=rig.clock, port="sim", ack_timeout_s=ack_timeout_s)
        light.connect(baud=115200)
        return light
    return Illuminator.open(os.environ.get("VISPEK_HC_PORT"), ack_timeout_s=ack_timeout_s)


def _mean(session: Session, channel: str, frames: int = 1) -> float:
    rgb = video.decode(session.frames.wait_new(frames))
    return float(reduce(rgb, channel).mean())  # type: ignore[arg-type]


def pairing(session: Session) -> dict[str, Any]:
    """The camera sees the LED this serial port switches; how long light takes to arrive."""
    result = session.pair_probe(relearn=True)
    assert result.paired, (
        "the camera did not react to LED 6: wrong port, wrong camera or covered lens"
    )
    assert result.latency_s is not None
    assert 0.02 < result.latency_s < 1.5, (
        f"pipeline latency {result.latency_s:.3f} s is implausible"
    )
    assert result.fps, "no frame rate was measured"
    return {
        "rise_dn": round(result.rise_dn, 1),
        "latency_ms": round(result.latency_s * 1000, 1),
        "measured_fps": round(result.fps, 3),
    }


def black_level(session: Session) -> dict[str, Any]:
    """With every LED off, in a dark box, the picture sits at the black-level clamp."""
    level = session.black_level()
    expected = (session.model.black_level,) * 3
    assert level == expected, (
        f"black level is {level}, expected {expected}: light is leaking in, or the pixel "
        "range conversion does not match this camera"
    )
    return {"median_rgb": list(level)}


def visible_led(
    session: Session, led_id: int = PROBE_LED, pwm: int | None = None
) -> dict[str, Any]:
    """One visible LED switches on and off, and the camera shows both.

    At the PWM the unit scans that LED with, unless one is given: a faint LED on a dark
    sample cannot be told from no light at all.
    """
    led = session.model.led(led_id)
    channel = led.dominant_channel
    if pwm is None:
        pwm = session.unit.pwm_by_led.get(led_id, led.starting_pwm)
    session.light.all_off()
    session.clock.sleep(0.5)
    before = _mean(session, channel)
    acked = session.light.on(led_id, pwm)
    try:
        lit = _mean(session, channel, frames=5)
    finally:
        session.light.off(led_id)
    session.clock.sleep(0.5)
    after = _mean(session, channel, frames=2)
    assert lit - before > 5, (
        f"LED {led_id} at PWM {pwm} raised its channel by {lit - before:.1f} only"
    )
    assert abs(after - before) < 2, (
        f"the picture did not return to dark ({before:.1f} -> {after:.1f})"
    )
    return {
        "led_id": led_id,
        "pwm": pwm,
        "dark_dn": round(before, 2),
        "lit_dn": round(lit, 2),
        "after_dn": round(after, 2),
        "ack_delay_ms": round((acked.ack_delay_s or 0) * 1000, 1),
        "ack": session.light.ack_stats(),
    }


def camera_lock(session: Session) -> dict[str, Any]:
    """Camera parameters can be set, and stay set on the running stream."""
    report = session.lock_controls()
    assert report.awb_off, "automatic white balance did not stay off"
    assert report.exposure_manual is not False, "exposure did not stay manual"
    moved = {
        name: {"requested": wanted, "effective": report.effective.get(name)}
        for name, wanted in report.requested.items()
        if report.effective.get(name) != wanted
    }
    return {"effective": dict(report.effective), "differs_from_request": moved,
            "skipped": list(report.skipped)}  # fmt: skip


def exposure_steps(
    session: Session, values: tuple[int, ...] = (63, 100, 157, 200, 333)
) -> dict[str, Any]:
    """Which exposure the camera settles on for each value asked (units of 100 us)."""
    controls = session.controls
    assert controls is not None, "no way to set camera parameters on this computer"
    session.lock_controls()
    table = {}
    try:
        for value in values:
            controls.write("exposure-time-abs", value)
            session.frames.wait_new(3)
            table[str(value)] = controls.read_all()["exposure-time-abs"].current
    finally:
        report = session.lock_controls()  # back to the unit's profile
    assert report.awb_off, "white balance came back on while exposure was changed"
    return {"asked_to_got": table}


def reply_loss(session: Session, gap_ms: int = 10, pairs: int = 30) -> dict[str, Any]:
    """How often a reply is lost at the OFF-to-ON boundary, for one pause between the two.

    The driver itself never leaves less than 10 ms between commands, so the pause is
    ``max(10, gap_ms)``. PWM 1 is used: the LED barely glows. ``failed`` counts pairs
    that got no reply at all after three attempts; the caller decides what that means.
    """
    light = session.light
    light.all_off()
    retried = failed = 0
    delays: list[float] = []
    try:
        for _ in range(pairs):
            try:
                off = light.off(PROBE_LED)
                session.clock.sleep(gap_ms / 1000)
                on = light.on(PROBE_LED, 1)
            except VispekHCError:
                failed += 1
                light.all_off()
                continue
            retried += (off.attempts > 1) + (on.attempts > 1)
            delays += [d * 1000 for d in (off.ack_delay_s, on.ack_delay_s) if d is not None]
    finally:
        light.all_off()
    return {
        "gap_ms": max(10, gap_ms),
        "failed_pairs": failed,
        "ack_timeout_ms": round(light.ack_timeout_s * 1000),
        "commands": 2 * pairs,
        "retried": retried,
        "loss_rate": round(retried / (2 * pairs), 4),
        "delay_ms_median": round(float(np.median(delays)), 2) if delays else None,
        "delay_ms_max": round(max(delays), 2) if delays else None,
    }


def scan(session: Session, directory: Path, channels: tuple[int, ...] = NO_UV) -> dict[str, Any]:
    """A whole scan of something that reflects: every channel gives a settled, lit frame."""
    settings = ScanSettings(channels=channels, enclosure="darkbox")
    started = session.clock.now()
    capture = sweep.record(session, directory, settings)
    seconds = session.clock.now() - started
    assert capture.status == "complete", capture.groups[-1]["failure"]
    frames = capture.groups[0]["frames"]
    dark = [entry["led_id"] for entry in frames if entry["frame_check"]["response_dn"] <= 1]
    assert not dark, f"no light seen under LED {dark}: is something in view, is the lens open?"
    return {
        "seconds": round(seconds, 2),
        "dark_median_rgb": capture.groups[0]["dark"]["median_rgb"],
        "min_on_ms": capture.document["config"]["min_on_ms"],
        "channels": [
            {
                "led_id": entry["led_id"],
                "pwm": entry["pwm"],
                "attempts": entry["frame_check"]["attempts"],
                "observed_frames": entry["frame_check"]["observed_frames"],
                "on_ack_to_selected_ms": entry["frame_check"]["on_ack_to_selected_ms"],
                "response_dn": entry["frame_check"]["response_dn"],
                "rgb_saturated_fraction": entry["frame_check"]["rgb_saturated_fraction"],
            }
            for entry in frames
        ],
    }


def white_reference(
    session: Session, directory: Path, channels: tuple[int, ...] = NO_UV
) -> dict[str, Any]:
    """White board in view: find PWM values, record a reference, and divide the board by itself."""
    notes: list[str] = []
    settings = ScanSettings(channels=channels, role="white", enclosure="darkbox")
    pwm = sweep.auto_pwm(session, settings, notes=notes)
    white = sweep.record(session, directory / "white", settings)
    calibration = make_calibration([white])
    again = sweep.record(session, directory / "again", ScanSettings(channels=channels))
    cube = apply_calibration(again, calibration)
    valid = cube.valid
    medians = [
        float(np.median(cube.values[:, :, i][valid[:, :, i]])) for i in range(len(cube.bands))
    ]
    off = {band.band_id: round(m, 4) for band, m in zip(cube.bands, medians, strict=True)
           if not 0.95 <= m <= 1.05}  # fmt: skip
    assert not off, f"the white board divided by itself is not 1 within 5 %: {off}"
    chosen = calibration.dominant_channels
    return {
        "pwm_by_led": {str(n): value for n, value in pwm.items()},
        "auto_pwm_notes": notes,
        "dominant_channel_by_led": {str(n): channel for n, channel in chosen.items()},
        "differs_from_model": {
            str(n): {"model": HC1500.led(n).dominant_channel, "white": channel}
            for n, channel in chosen.items()
            if HC1500.led(n).dominant_channel != channel
        },
        "quality": calibration.document["quality"]["bands"],
        "warnings": calibration.document["quality"]["warnings"],
        "median_of_board_over_itself": [round(m, 4) for m in medians],
    }


def scalar_noise(session: Session, led_id: int = 9, frames: int = 12) -> dict[str, Any]:
    """Frame-to-frame noise of the dominant-channel value against luma, relative to signal.

    The default scalar is the dominant channel because its signal is larger; this measures
    whether that holds on the device. No verdict is asserted: a person reads the numbers.
    """
    led = session.model.led(led_id)
    pwm = session.unit.pwm_by_led.get(led_id, led.starting_pwm)
    session.light.all_off()
    session.clock.sleep(0.5)
    dark = video.decode(session.frames.wait_new(2))
    session.light.on(led_id, pwm)
    try:
        session.frames.wait_new(5)  # past the frame that straddles the switch-on
        stack = np.stack([video.decode(session.frames.wait_new()) for _ in range(frames)])
    finally:
        session.light.off(led_id)
    record: dict[str, Any] = {"led_id": led_id, "pwm": pwm, "frames": frames}
    for name, kind in (("dominant", led.dominant_channel), ("luma", "luma")):
        values = np.stack([reduce(rgb, kind) for rgb in stack]).astype(np.float64)  # type: ignore[arg-type]
        signal = float(np.median(values.mean(axis=0) - reduce(dark, kind)))  # type: ignore[arg-type]
        noise = float(np.median(values.std(axis=0)))
        assert signal > 5, (
            f"LED {led_id} gives almost no {name} signal ({signal:.1f}): nothing in view?"
        )
        record[name] = {
            "signal_dn": round(signal, 2),
            "temporal_std_dn": round(noise, 3),
            "relative_noise": round(noise / signal, 5),
            "one_code_value_relative": round(1 / signal, 5),
        }
    return record


def clipping(session: Session, led_id: int = 9) -> dict[str, Any]:
    """Enough light on a white board reaches 255: the pixel range is not compressed.

    The PWM is raised in steps and the check stops at the first frame that clips, so the
    LED is not driven harder than needed. Each step is lit for a few frames at most.
    """
    led = session.model.led(led_id)
    steps = sorted({min(1023, led.starting_pwm * factor) for factor in (2, 4, 8, 16, 32)})
    tried = []
    for pwm in steps:
        session.light.on(led_id, pwm, max_on_s=3.0)
        try:
            rgb = video.decode(session.frames.wait_new(4))
        finally:
            session.light.off(led_id)
        peak = int(reduce(rgb, led.dominant_channel).max())
        tried.append({"pwm": pwm, "peak_dn": peak})
        if peak == 255:
            break
        session.clock.sleep(0.2)
    assert peak == 255, (
        f"the brightest pixel under LED {led_id} never reached 255 (PWM steps {tried}): either "
        "nothing bright is in view, or the range conversion compresses the top"
    )
    latest = session.frames.latest()
    return {"led_id": led_id, "steps": tried,
            "pixel_format": latest.pixel_format if latest else None}  # fmt: skip
