# SPDX-License-Identifier: Apache-2.0
"""The simulated camera: timing, rolling shutter, faults."""

from __future__ import annotations

import numpy as np
import pytest

from vispek_hc import framecheck, video
from vispek_hc.errors import FrameIncompleteError
from vispek_hc.illuminator import Illuminator
from vispek_hc.models import HC1500
from vispek_hc.sim import SimFrameSource, SimLightSource, VirtualClock

FPS = 10.0
PERIOD = 1 / FPS
EXPOSURE = 0.0157
READOUT = 1 / 30
LATENCY = 0.150


def make(**options: object) -> tuple[SimFrameSource, Illuminator, VirtualClock]:
    clock = VirtualClock()
    device = SimLightSource(clock)
    light = Illuminator(device, clock=clock, allow_uv=True)
    light.connect(baud=115200)
    frames = SimFrameSource(clock, device, **options)  # type: ignore[arg-type]
    frames.start()
    return frames, light, clock


def test_no_frame_exists_before_the_first_one_arrives() -> None:
    frames, _, clock = make()
    opened = clock.now()
    assert frames.latest() is None
    first = frames.wait_new()
    assert first.seq == 1
    assert clock.now() - opened == pytest.approx(EXPOSURE + READOUT + LATENCY)
    assert first.received_at == pytest.approx(clock.now())


def test_frames_arrive_one_period_apart_with_consecutive_numbers() -> None:
    frames, _, _ = make()
    first = frames.wait_new()
    third = frames.wait_new(2)
    assert third.seq == first.seq + 2
    assert third.received_at - first.received_at == pytest.approx(2 * PERIOD)
    assert frames.measured_fps == pytest.approx(FPS)
    assert frames.latest() == third


def test_waiting_counts_only_frames_after_the_call() -> None:
    frames, _, clock = make()
    clock.sleep(1.0)
    seen = frames.latest()
    assert seen is not None
    assert frames.wait_new().seq == seen.seq + 1


def test_dark_frames_sit_at_the_black_level_and_are_byte_identical() -> None:
    frames, _, _ = make()
    first = frames.wait_new()
    second = frames.wait_new()
    assert first.data == second.data
    assert second.seq == first.seq + 1, "identical bytes are still a new frame"
    rgb = video.decode(first)
    assert rgb.shape == (36, 64, 3)
    assert np.all(rgb == HC1500.black_level)


def test_frame_size_and_format() -> None:
    frames, _, _ = make(size=(32, 18))
    frame = frames.wait_new()
    assert (frame.width, frame.height, frame.pixel_format) == (32, 18, "rgb24")
    assert frame.generation == 1


@pytest.mark.parametrize("led_id", [5, 6, 9, 17])
def test_settled_led_frame_shows_the_white_board_observation(led_id: int) -> None:
    frames, light, _ = make()
    led = HC1500.led(led_id)
    light.on(led_id, led.starting_pwm)
    frame = frames.wait_new(4)
    rgb = video.decode(frame)
    expected = [value + HC1500.black_level for value in led.white_rgb]
    assert rgb[0, 0].tolist() == expected
    assert np.all(rgb == rgb[0, 0])


def test_signal_scales_with_pwm_and_clips_at_255() -> None:
    frames, light, _ = make()
    led = HC1500.led(9)
    light.on(9, led.starting_pwm // 2)
    half = video.decode(frames.wait_new(4))[0, 0, 0]
    assert half == pytest.approx(5 + led.white_rgb[0] / 2, abs=1)
    light.on(9, 1023)
    assert video.decode(frames.wait_new(4))[0, 0, 0] == 255


def test_reflectance_map_shapes_the_scene() -> None:
    reflectance = np.ones((36, 64))
    reflectance[:, 32:] = 0.5
    frames, light, _ = make(reflectance=reflectance)
    led = HC1500.led(9)
    light.on(9, led.starting_pwm)
    rgb = video.decode(frames.wait_new(4))
    assert rgb[0, 0, 0] == 5 + led.white_rgb[0]
    assert rgb[0, 63, 0] == pytest.approx(5 + led.white_rgb[0] / 2, abs=1)


def test_frame_exposed_while_the_led_switches_is_half_lit() -> None:
    frames, light, clock = make(size=(64, 100))
    opened = clock.now()
    frames.wait_new(3)
    # Switch on in the middle of the read-out of the frame that starts 0.4 s after opening.
    switch_at = opened + 4 * PERIOD + EXPOSURE + READOUT / 2
    clock.advance_to(switch_at)
    acked = light.on(9, HC1500.led(9).starting_pwm)
    assert acked.started_at == pytest.approx(switch_at)

    crossing = frames.wait_new(2)  # the frame in flight was exposed before the switch
    rows = framecheck.reduce(video.decode(crossing), "R").mean(axis=1)
    assert rows[0] == 5, "top rows were exposed before the LED came on"
    assert rows[-1] == 5 + HC1500.led(9).white_rgb[0], "bottom rows after"

    settled = frames.wait_new()
    confirmed = frames.wait_new()
    a, b, c = (framecheck.reduce(video.decode(f), "R") for f in (crossing, settled, confirmed))
    assert not framecheck.compare(a, b, tolerance=8).stable
    assert framecheck.compare(b, c, tolerance=8).stable


def test_frames_know_their_exposure_window() -> None:
    frames, light, _ = make()
    frames.wait_new(2)
    acked = light.on(9, 36)
    frame = frames.wait_new(3)
    assert frame.exposure_window is not None
    start, end = frame.exposure_window
    assert end - start == pytest.approx(EXPOSURE + READOUT * 35 / 36)
    assert start >= acked.started_at, "settled frame was exposed entirely under the LED"
    assert frame.received_at == pytest.approx(start + EXPOSURE + READOUT + LATENCY)


def test_first_brighter_frame_arrives_one_pipeline_latency_after_switch_on() -> None:
    frames, light, _ = make()
    frames.wait_new(2)
    acked = light.on(9, 36)
    assert acked.acked_at is not None
    brighter = frames.wait_new()
    while video.decode(brighter).mean() <= HC1500.black_level:
        brighter = frames.wait_new()
    delay = brighter.received_at - acked.acked_at
    assert LATENCY - 0.02 <= delay <= LATENCY + PERIOD + EXPOSURE + READOUT


def test_stalled_stream_times_out_and_then_continues_in_the_same_generation() -> None:
    frames, _, clock = make()
    before = frames.wait_new()
    frames.stall(5.0)
    started = clock.now()
    with pytest.raises(FrameIncompleteError) as caught:
        frames.wait_new()
    assert caught.value.details == {"wanted": 1, "arrived": 0}
    assert clock.now() - started == pytest.approx(3.0), "default wait: max(3 s, n/fps + 2 s)"
    clock.sleep(0.5)
    assert frames.is_stale()
    after = frames.wait_new(timeout=5.0)
    assert (after.seq, after.generation) == (before.seq + 1, before.generation)
    assert not frames.is_stale()


def test_explicit_timeout_is_honoured() -> None:
    frames, _, clock = make()
    frames.wait_new()
    frames.stall(1.0)
    started = clock.now()
    with pytest.raises(FrameIncompleteError):
        frames.wait_new(timeout=0.25)
    assert clock.now() - started == pytest.approx(0.25)


def test_dropped_stream_comes_back_as_a_new_generation_with_continuing_numbers() -> None:
    frames, _, clock = make()
    restarts: list[int] = []
    frames.on_restart = lambda: restarts.append(frames.generation)
    before = frames.wait_new()
    frames.drop_out(1.5)
    started = clock.now()
    after = frames.wait_new()
    assert (after.seq, after.generation) == (before.seq + 1, 2)
    assert clock.now() - started == pytest.approx(1.5 + EXPOSURE + READOUT + LATENCY)
    assert restarts == [2]


def test_stopped_source_delivers_nothing() -> None:
    frames, _, _ = make()
    frames.wait_new()
    frames.stop()
    with pytest.raises(FrameIncompleteError):
        frames.wait_new(timeout=1.0)


def test_reflectance_can_differ_from_led_to_led() -> None:
    # A scene that reflects LED 9 fully and LED 5 not at all in its left half.
    clock = VirtualClock()
    device = SimLightSource(clock)
    light = Illuminator(device, clock=clock)
    light.connect(baud=115200)
    scene = np.ones((36, 64, 17))
    scene[:, :32, 4] = 0.0  # LED 5, left half
    scene[:, :, 8] = 0.5  # LED 9, everywhere
    frames = SimFrameSource(clock, device, reflectance=scene)
    frames.start()
    pictures = {}
    for led_id in (5, 9):
        light.on(led_id, HC1500.led(led_id).starting_pwm)
        pictures[led_id] = video.decode(frames.wait_new(5))
        light.off(led_id)
        frames.wait_new(5)
    blue, red = max(HC1500.led(5).white_rgb), max(HC1500.led(9).white_rgb)
    assert pictures[5][10, 5].tolist() == [5, 5, 5], "left half: no light back under LED 5"
    assert pictures[5][10, 60, 2] == 5 + blue
    assert pictures[9][10, 5, 0] == pictures[9][10, 60, 0] == 5 + round(red * 0.5)


def test_another_target_can_be_shown_to_the_running_camera() -> None:
    frames, light, _ = make()
    led = HC1500.led(9)
    light.on(9, led.starting_pwm)
    assert video.decode(frames.wait_new(4))[0, 0, 0] == 5 + led.white_rgb[0]
    frames.show(0.5)  # a grey card in place of the white board
    assert video.decode(frames.wait_new(2))[0, 0, 0] == pytest.approx(
        5 + led.white_rgb[0] / 2, abs=1
    )
    with pytest.raises(ValueError, match="one layer per LED"):
        frames.show(np.ones((36, 64, 3)))


def test_reflectance_per_led_must_cover_every_led() -> None:
    clock = VirtualClock()
    with pytest.raises(ValueError, match="one layer per LED"):
        SimFrameSource(clock, SimLightSource(clock), reflectance=np.ones((36, 64, 3)))


# --- the gain the camera keeps ---------------------------------------------------------


def test_the_simulated_camera_keeps_a_low_gain_when_locked_under_light() -> None:
    # Measured on the device: locked while it saw light, the camera gave LED 9 a reading
    # 4.4 times lower than locked in darkness, with the same controls read back.
    from vispek_hc.sim import SimulatedRig

    readings = {}
    for label, ambient in (("dark", 0.0), ("light", 40.0)):
        rig = SimulatedRig()
        rig.light.connect(baud=115200)
        rig.frames.start()
        rig.frames.ambient = ambient
        rig.controls.write("auto-exposure-mode", 1)  # automatic exposure off: the gain stays
        rig.frames.ambient = 0.0
        rig.light.on(9, 36)
        rig.frames.wait_new(8)
        readings[label] = float(video.decode(rig.frames.latest())[..., 0].mean()) - 5
        rig.light.off(9)
    assert readings["dark"] == pytest.approx(HC1500.led(9).white_rgb[0], abs=1)
    assert readings["dark"] / readings["light"] == pytest.approx(4.4, rel=0.05)


def test_ambient_light_shows_in_the_simulated_picture() -> None:
    from vispek_hc.sim import SimulatedRig

    rig = SimulatedRig()
    rig.frames.start()
    rig.frames.ambient = 40.0
    rig.frames.wait_new(3)
    assert video.decode(rig.frames.latest()).mean() == pytest.approx(45.0)
