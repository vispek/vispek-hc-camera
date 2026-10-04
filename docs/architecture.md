# Architecture

## Modules

| Module | Does | Talks to |
|---|---|---|
| `protocol.py` | serial frames: CRC, encode, parse, resynchronise | nothing (pure) |
| `models.py` | HC1500 facts: LED table, camera ids, initial camera profile | - |
| `errors.py` | one class per error code, exit-code families | - |
| `interfaces.py` | the seams: `Clock`, `LightSource`, `FrameSource`, `CameraControls`, `Frame`, `Transaction` | - |
| `safety.py` | UV gate for channel plans; process-level all-off (atexit, signals) | `models` |
| `illuminator.py` | light-source driver: port discovery, exclusive open, baud probe, retries, watchdog, all-off | pyserial |
| `video.py` | frames: ffmpeg child process, YUYV to RGB, JPEG splitting, freshness, reconnect | ffmpeg, numpy, Pillow |
| `controls.py` | camera parameter lock with read-back; macOS backend (uvc-util) | uvc-util |
| `framecheck.py` | per-frame checks: scalar reduction, darkness, clipping, stability | numpy |
| `units.py` | per-device profiles under `VISPEK_HC_HOME` | disk |
| `session.py` | owns the three devices and the unit profile; pairing probe, black level | all of the above |
| `sweep.py` | recording: frame selection, the scan of one group, retries, disk thread, automatic PWM, `capture()` | `session`, `cube` |
| `cube.py` | `Cube`, capture folders (write, read, repair), `cube.npz`, ENVI and TIFF export | disk, Pillow |
| `calibration.py` | white-reference packages: quality gates, binding, region conversion | `cube` |
| `reflectance.py` | relative reflectance and the binding check | `calibration` |
| `views.py` | stretch, colour tables, composites, indices, principal components | numpy, Pillow |
| `spectra.py` | statistics of points, rectangles and polygons; CSV | numpy |
| `samples.py` | the invented scene; simulated captures for `demo` and the examples | `sweep`, `sim` |
| `sim.py` | simulated light source, camera and controls on a virtual clock | - |
| `cli.py` | the `vispek-hc` command | everything above |

A real device and the simulated one implement the same protocols in `interfaces.py`, so
everything above them runs unchanged against either.

## Concurrency

- One reader thread per camera stream; it keeps only the newest frame and wakes waiters
  through a `Condition`. Nothing polls. Starting and ending the ffmpeg process happens
  under one lock, so `stop()` always reaches the process that is running.
- Serial commands are synchronous under one lock: one command, then its reply.
- The emergency all-off (signal handler, interpreter exit) never takes that lock. It sets
  a latch first: until a normal `all_off()` is confirmed, ON commands are refused, so
  interrupted code cannot light an LED behind the shutdown.
- `close()` sets the same latch, then waits for the lock (a command on the wire is given
  time to finish, so that its LED is seen), sends the all-off if any LED is not known to
  be off or the lock never came, releases the port, and raises
  `LED_SHUTDOWN_UNCONFIRMED` if an OFF was not confirmed.
- A watchdog timer thread switches a held LED off (always armed for UV LEDs, before the
  ON is sent).
- Open light sources are held in a process-wide registry until closed; exit and signal
  handlers are installed when the first one is created (signals only from the main thread).
- A scan runs in the caller's thread. Frames are written by one disk thread fed through a
  queue four jobs deep; the scan hands a frame over only after the LED is off, so a slow
  disk makes it wait in the dark, never with light on.
- Stopping a recording (`stop` event) takes effect at the next frame, or within 0.1 s
  inside a longer wait; the error is raised only after the LED is off. On the command
  line the first Ctrl-C sets that event (a continuous recording then ends as complete),
  a second one interrupts.
- Events about a channel (the serial commands, rejected frames, timing) are written to
  `events.jsonl` after its OFF: writing can wait for the disk, and nothing waits for
  the disk with an LED lit.
- No asyncio. A `Session` is not meant to be entered from several threads.

## Timeouts (the one place they are listed)

| What | Value | Where |
|---|---|---|
| Serial reply | 200 ms (100-2000), 3 attempts | `Illuminator(ack_timeout_s=...)`, `illuminator.ATTEMPTS` |
| Pause before a resend and after a retried command | 60 ms | `illuminator.QUIET_S` |
| Gap between commands | 10 ms | `illuminator.COMMAND_GAP_S` |
| Emergency all-off, per LED | 10 ms gap + 100 ms | `illuminator.EMERGENCY_WAIT_S` |
| `close()` waiting for a command on the wire | 3 x (reply timeout + 60 ms) + 1 s | `Illuminator.close` |
| UV watchdog for a hand-lit LED; longest `led on --hold` for UV | 10 s | `illuminator.UV_MAX_ON_S` |
| Serial read poll / write | 5 ms / 1 s | `illuminator.POLL_S`, `WRITE_TIMEOUT_S` |
| Fresh frames | max(3 s, n / fps + 2 s) | `video.wait_timeout` |
| First frame after opening the stream | 15 s | `video.STARTUP_S` |
| Stream considered gone (then restarted by the next wait) | 3 s without a frame | `video.STALE_S` |
| Stream reopen pause | 1.5 s, doubling to 10 s, back to 1.5 s after a frame | `video.RECONNECT_FIRST_S`, `RECONNECT_MAX_S` |
| ffmpeg stop | terminate, 2 s, then kill | `video.STOP_GRACE_S` |
| Device listing | 12 s | `video.LIST_TIMEOUT_S` |
| One uvc-util call | 8 s | `controls.CALL_TIMEOUT_S` |
| Pairing probe: LED 6 lit; further wait for the lit frames | 0.3 s; 0.7 s | `session.PROBE_ON_S`, `PROBE_WAIT_S` |
| Longest manual minimum on-time when UV LEDs are scanned | 5 s | `sweep.UV_MIN_ON_LIMIT_MS` |
| Minimum on-time of a channel (`auto`) | pipeline latency + one frame period (of the frame rate rounded to a whole number) + exposure, rounded up to 10 ms; 500 ms if no probe ran; never less than a value `tune` stored | `sweep.plan`, `FALLBACK_MIN_ON_MS`, `MIN_ON_STEP_MS` |
| Noticing a stop request during a wait | 0.1 s | `sweep.STOP_POLL_S` |
| Frame selection for one channel | at most `stable_frames + max_extra_frames` frames, or max(5 s, min on-time + 3 s) | `sweep._Selector.select` |
| Attempts at a channel | 3 in all, pause min(1 s, 0.2 s x attempt) | `sweep.ATTEMPTS` |
| Dark pause before every channel | one frame period + exposure + 50 ms for the sensor's read-out, rounded up to 10 ms (170 ms at 10 fps); longer if `inter_channel_ms` asks for more, never shorter | `sweep.plan`, `READOUT_ALLOWANCE_S`, `ScanSettings.inter_channel_ms` |
| Settling before a black-level frame | 0.5 s | `session.SETTLE_S` |
| `check`: `xcode-select -p`; `system_profiler` | 8 s; 12 s | `cli.has_xcode_tools`, `cli.list_cameras_without_ffmpeg` |

## Pixel path

The default path asks ffmpeg for the camera's raw YUYV 4:2:2 buffer (`-pix_fmt yuyv422
-f rawvideo`, so ffmpeg converts nothing) and converts it in `video.yuyv_to_rgb` with
exact integer BT.601 arithmetic (both pixels of a pair share one chroma sample, round
half up). The camera's uncompressed frames are limited range (luma 16-235 stretched to
0-255): measured on the device, every LED off is Y = 20 there and 5 through the JPEG
path. Each frame carries its range (`Frame.color_range`), and a capture records it. Every ffmpeg capture
command carries the frame pass-through flag, `-nostdin` and `-nostats`, and no output
frame rate. What ffmpeg prints goes to a temporary file; its tail travels in the
`details.ffmpeg` of frame errors.

## Scan

Before anything is lit, and before the camera is touched, the plan is checked: channels,
UV permission, every PWM value (settings, else the unit's profile, else the model's
table), and that the output folder is free. `capture()` and the command line do the
part that needs no device before they open one.

One group = one dark frame (every LED off) plus one frame per channel. For each channel:
pause, ON, wait the minimum on-time, then take frames until one agrees with its
predecessor (mean difference, share of strongly changed pixels, largest row or column
difference). A frame that reached the host before light could possibly show in it never
counts as settled, and a frame that cannot be decoded is skipped. OFF is sent in a
`finally` block. A channel whose frame shows no light (its own channel rises by 1 code
value or less over the dark frame, or 99.5 % of it is black) is tried again on a white
reference, 3 attempts in all; on a sample it is recorded with a warning. Camera
parameters are read back after each group (event `camera_control_verification`); in a
locked scan a change marks the group failed and keeps its frames, in an unlocked one
(`--unlocked`) it is only recorded.

The on-time a channel suggests for later (`suggested_min_on_ms`, stored by `tune`) is
the moment the pair of agreeing frames began: the selection time less two frame
periods, never below the physical floor, rounded up to 50 ms. Storing the selection time
itself would add two frame periods at every `tune`.

A failure before the first finished output raises its own error. After at least one
finished output it is `RECORDING_PARTIAL` with the cause in `details`.

Derived data is never stored: a cube is rebuilt from the source frames when a capture is
loaded. Averaging happens there too, one scan at a time, so memory stays at a few
cubes' worth however many scans are averaged.

## What is not built yet

- The OpenCV capture backend; camera controls on Linux and Windows (so `check` reports
  `CONTROLS_UNAVAILABLE` there and is never `ready`); camera discovery on Windows (name
  the camera with `--camera`; Linux lists `/dev/video*` nodes).
- Continuing a scan across a camera reconnect: a dropout fails the group, and the camera
  parameters are locked again at the start of the next recording.
- Retrying with the exact frame rate the device lists when a requested rate is refused.
- Reading recordings of the earlier data format; a median filter for display; the overview
  grid of all views; colouring a single band by its wavelength; drawing the quality
  overlay into the pictures `view` writes (`views.quality_overlay` returns the layer).
- `ambient_leak_dn` (group dark frame against a covered dark frame); choosing an output
  other than the first on the command line; K-means inside the library (example 13 shows
  it with scikit-learn).
- Anything verified on a real HC1500.
