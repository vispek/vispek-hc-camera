# SPDX-License-Identifier: Apache-2.0
"""Convert the extracted demonstration frames into capture folders (vispek-hc.capture/1).

Input: a folder with ``sample_pcba/``, ``white_reference/`` and ``dark_reference/``, each
holding JPEG frames and a ``frames.json`` (recording settings and one record per frame).
Output: three capture folders, by default ``sample-captures/`` beside this repository's
``pyproject.toml``. That folder is ignored by git: the pictures are not distributed, and
``tests/test_sample_data.py`` checks them only where they exist. The JPEG bytes are
copied unchanged; they were re-encoded once by ffmpeg when recorded, so the pixel
path is recorded as ``mjpeg_reencode``.

    uv run python tools/convert_samples.py ../vispek-hc-camera-sample-raw
"""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from vispek_hc.cube import CaptureWriter
from vispek_hc.interfaces import Frame

SAMPLE_CAPTURES = Path(__file__).parent.parent / "sample-captures"
UNIT = "sample-unit"


def frame_of(folder: Path, record: dict[str, Any]) -> Frame:
    data = (folder / record["file"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise SystemExit(f"{record['file']} does not match its recorded sha256")
    received = datetime.fromisoformat(record["host_received_at_utc"])
    return Frame(
        record["host_frame_counter"], 1, received.timestamp(), received,
        record["width"], record["height"], "mjpeg", data,
    )  # fmt: skip


def header(source: dict[str, Any], mode: str, role: str | None) -> dict[str, Any]:
    recorded = source["recording_config"]
    identity = uuid.uuid5(uuid.NAMESPACE_URL, f"vispek-hc-sample/{source['source_recording_id']}")
    width, height = map(int, recorded["actual_size"].split("x"))
    channels = recorded["channel_settings"]
    roi = recorded.get("white_roi")
    return {
        "capture_id": str(identity),  # the same every time the conversion runs
        "unit_id": UNIT,
        "reduction": "dominant",
        "recording": {"mode": mode, "role": role, "cycles": 1, "average": 1},
        "device": {"camera": {"name": "LRCP 1080P-60FPS", "backend": "ffmpeg",
                              "source": "mjpeg_reencode"}},
        "config": {
            "channels": [int(n) for n in channels],
            "pwm_by_led": {n: settings["pwm"] for n, settings in channels.items()},
            "min_on_ms": {"source": "recorded",
                          "by_led": {n: s["on_time_ms"] for n, s in channels.items()}},
            "reduction": "dominant",
            "actual_size": [width, height],
            "actual_fps": recorded["actual_fps"],
            "source": "mjpeg_reencode",
            "pixel_format": "mjpeg",
            "color_range": "full",
            "exposure": {"requested": recorded["exposure"], "effective": recorded["exposure"]},
            "camera_controls": recorded["camera_controls"]["values"],
            "controls_locked": True,
            "white_roi": [roi["x"], roi["y"], roi["width"], roi["height"]] if roi else None,
            "acquisition": {"enclosure": "darkbox", "setup_label": "demonstration 2026-09-16"},
        },
        "notes": f"converted from demonstration recording {source['source_recording_id']}",
    }  # fmt: skip


def convert(folder: Path, target: Path, mode: str, role: str | None) -> None:
    source = json.loads((folder / "frames.json").read_text("utf-8"))
    pwm = source["recording_config"]["channel_settings"]
    with CaptureWriter(target, header(source, mode, role), overwrite=True) as writer:
        for record in source["frames"]:
            frame = frame_of(folder, record)
            if mode == "dark":
                writer.record_single(writer.write_frame("frame", frame))
            elif "led" not in record:
                writer.record_frame("g0001", writer.write_frame("g0001/dark", frame), dark=True)
            else:
                led = record["led"]
                name = f"g0001/led{led:02d}_{record['nominal_nm']}nm"
                entry = writer.write_frame(name, frame)
                info = {"led_id": led, "nm": record["nominal_nm"], "pwm": pwm[str(led)]["pwm"]}
                writer.record_frame("g0001", {**info, **entry})
        if mode == "scan":
            writer.finish_group("g0001")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("raw", type=Path, help="folder with the extracted frames")
    parser.add_argument("-o", "--output", type=Path, default=SAMPLE_CAPTURES)
    args = parser.parse_args()
    convert(args.raw / "sample_pcba", args.output / "sample_pcba", "scan", None)
    convert(args.raw / "white_reference", args.output / "white_reference", "scan", "white")
    convert(args.raw / "dark_reference", args.output / "dark_reference", "dark", "dark")
    print(f"wrote three captures to {args.output}")  # noqa: T201


if __name__ == "__main__":
    main()
