# SPDX-License-Identifier: Apache-2.0
"""Agent-facing documents stay small and point at each other correctly."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

import pytest

import vispek_hc
from vispek_hc import cli, protocol

REPO = Path(__file__).parent.parent


def test_agents_md_fits_the_context_budget() -> None:
    text = (REPO / "AGENTS.md").read_text("utf-8")
    assert len(text.encode("utf-8")) < 32 * 1024
    assert len(text.splitlines()) <= 300


def test_claude_md_only_points_at_agents_md() -> None:
    lines = (REPO / "CLAUDE.md").read_text("utf-8").splitlines()
    assert lines[0] == "@AGENTS.md"
    assert len(lines) <= 4


def test_readmes_open_with_the_uv_warning() -> None:
    for name in ("README.md", "README.zh-CN.md", "SAFETY.md"):
        head = "\n".join((REPO / name).read_text("utf-8").splitlines()[:25])
        assert "UV-C" in head, name


def test_every_public_name_is_documented_and_nothing_else_is() -> None:
    text = (REPO / "docs" / "api.md").read_text("utf-8")
    documented = set(re.findall(r"^\| `([A-Za-z_]\w*)", text, flags=re.M))
    assert documented == set(vispek_hc.__all__)


def test_public_names_exist() -> None:
    assert all(hasattr(vispek_hc, name) for name in vispek_hc.__all__)


# --- the documents agree with the code -------------------------------------------------

DOCUMENTS = sorted(
    [*REPO.glob("*.md"), *(REPO / "docs").rglob("*.md"), *(REPO / "examples").glob("*.md")]
)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def anchor(heading: str) -> str:
    """The fragment GitHub gives a heading."""
    text = re.sub(r"[`*]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = re.sub(r"```.*?```", "", path.read_text("utf-8"), flags=re.S)  # not code comments
    return {anchor(line.lstrip("#")) for line in text.splitlines() if line[:1] == "#"}


def blocks(path: Path, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", path.read_text("utf-8"), flags=re.S)


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.relative_to(REPO).as_posix())
def test_links_between_documents_lead_somewhere(document: Path) -> None:
    broken = []
    for target in LINK.findall(document.read_text("utf-8")):
        if re.match(r"[a-z]+:", target):
            continue  # a web address
        name, _, fragment = target.partition("#")
        path = (document.parent / name).resolve() if name else document
        if not path.exists() or (fragment and path.is_file() and fragment not in anchors(path)):
            broken.append(target)
    assert broken == []


def test_every_document_can_be_reached_from_agents_md() -> None:
    linked = {
        (REPO / target.partition("#")[0]).resolve()
        for target in LINK.findall((REPO / "AGENTS.md").read_text("utf-8"))
    }
    documents = {
        path for path in (REPO / "docs").glob("*.md") if path.name != "release-checklist.md"
    }
    assert documents <= linked


def test_led_table_in_the_hardware_document_is_the_model() -> None:
    text = (REPO / "docs" / "hardware.md").read_text("utf-8")
    rows = re.findall(r"^\| (\d+) \| (\d+) nm \| ([RGB]) \| (\d+) \|", text, flags=re.M)
    documented = [(int(n), int(nm), channel, int(pwm)) for n, nm, channel, pwm in rows]
    leds = vispek_hc.HC1500.led_table.leds
    assert documented == [
        (led.led_id, led.nm, led.dominant_channel, led.starting_pwm) for led in leds
    ]
    assert f"`{vispek_hc.HC1500.led_table.version}`" in text


def test_frames_in_the_protocol_document_are_what_the_encoder_builds() -> None:
    text = (REPO / "docs" / "protocol.md").read_text("utf-8")
    rows = re.findall(r"^\| (\d+) \| `([0-9a-f ]+)` \| `([0-9a-f ]+)` \|$", text, flags=re.M)
    assert [int(row[0]) for row in rows] == list(range(1, 18))
    for led_id, on, off in rows:
        assert protocol.encode_on(int(led_id), 1023).hex(" ") == on
        assert protocol.encode_off(int(led_id)).hex(" ") == off
    for block in blocks(REPO / "docs" / "protocol.md", "python"):
        exec(compile(block, "protocol.md", "exec"), {})


def test_calibration_document_carries_the_statements_users_must_read() -> None:
    text = " ".join((REPO / "docs" / "calibration.md").read_text("utf-8").split())
    for statement in (
        "not linear radiance",
        "an index, not a physical reflectance",
        "Nothing is normalised by exposure or PWM",
        "black-level clamp",
        "very likely fluorescence",
        "nominal LED centre wavelengths",
        "order of LEDs 13-17 is not confirmed",
        "dark box",
        "drifts with LED temperature",
    ):
        assert statement in text, statement


def test_chinese_readme_has_the_same_commands_and_code_as_the_english_one() -> None:
    def code(name: str) -> list[list[str]]:
        text = (REPO / name).read_text("utf-8")
        found = re.findall(r"```\w+\n(.*?)```", text, flags=re.S)
        return [[line.split("#")[0].strip() for line in block.splitlines()] for block in found]

    assert code("README.zh-CN.md") == code("README.md")


@pytest.mark.usefixtures("quiet_host")
def test_first_measurement_in_the_readme_works_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    shell = "\n".join(blocks(REPO / "README.md", "bash"))
    commands = [
        line.split("#")[0]
        for line in shell.splitlines()
        if "--simulate" in line or "--calib" in line or "calibrate" in line or " demo " in line
    ]
    commands = [
        command
        for command in commands
        if command.startswith("uv run vispek-hc ") and "check" not in command
    ]
    assert len(commands) == 6
    assert commands[0].split()[:4] == ["uv", "run", "vispek-hc", "demo"]
    for command in commands:
        assert cli.main(shlex.split(command)[3:]) == 0, command
    assert (tmp_path / "ndvi.png").is_file()
    assert (tmp_path / "demo" / "rgb.png").is_file()
    capsys.readouterr()
    for name in ("README.md", "AGENTS.md"):
        (tmp_path / name).mkdir()
        shutil.copy(tmp_path / "cube.npz", tmp_path / name)
        monkeypatch.chdir(tmp_path / name)
        for block in blocks(REPO / name, "python"):
            names: dict[str, Any] = {}
            exec(compile(block, name, "exec"), names)
            if "z" in names:
                names["z"].close()  # the loader snippet keeps its file open, as scripts do
        assert (tmp_path / name / "first-scan" / "capture.json").is_file()
    assert (tmp_path / "AGENTS.md" / "frame.png").is_file(), "the session snippet"


CLI_SECTIONS: dict[str, list[str]] = {
    "check": ["check", "--simulate"],
    "devices": ["devices", "--pair", "--simulate"],
    "led": ["led", "on", "9", "36", "--hold", "0.1", "--simulate"],
    "record": ["record", "-o", "s", "--simulate"],
    "record --dry-run": ["record", "-o", "s2", "--dry-run", "--simulate"],
    "reference white": ["reference", "white", "-o", "w", "--simulate"],
    "tune": ["tune", "-o", "t", "--channels", "5,9", "--simulate"],
    "calibrate": ["calibrate", "--white", "w", "-o", "k"],
    "reflectance": ["reflectance", "s", "--calib", "k", "-o", "c.npz"],
    "view": ["view", "s", "-o", "v.png"],
    "spectrum": ["spectrum", "s", "--xy", "3,3"],
    "export": ["export", "s", "--format", "tiff", "-o", "e"],
    "repair": ["repair", "s"],
    "demo": ["demo", "-o", "demo"],
    "setup": ["setup", "--dry-run"],
}


CLI_OPTIONAL_KEYS = {"devices": {"pair"}, "led": {"hold_s", "ended_by"}}  # named in words


@pytest.mark.usefixtures("quiet_host")
def test_command_reference_lists_the_keys_each_command_really_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    text = (REPO / "docs" / "cli.md").read_text("utf-8")
    sections = dict(re.findall(r"^#{2,3} ([^\n]+)\n(.*?)(?=^#{2,3} |\Z)", text, flags=re.M | re.S))
    documented = {}
    for name, body in sections.items():
        if found := re.search(r"Keys of `data`: (.*?)\.\n", body, flags=re.S):
            documented[name] = set(re.findall(r"`(\w+)`", found.group(1)))
    assert set(documented) == set(CLI_SECTIONS)
    assert {name.split()[0] for name in documented} == set(cli.HANDLERS)
    for name, argv in CLI_SECTIONS.items():
        assert cli.main([*argv, "--json"]) == 0, name
        printed = set(json.loads(capsys.readouterr().out)["data"])
        printed -= CLI_OPTIONAL_KEYS.get(name, set())
        assert printed == documented[name], name


def test_package_holds_no_pictures_or_recorded_data() -> None:
    # Real sample captures are not distributed; a folder of them left under src/ would
    # go into the wheel.
    stray = [
        path.relative_to(REPO).as_posix()
        for path in (REPO / "src").rglob("*")
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".npz", ".tif", ".bsq"}
    ]
    assert stray == []


# --- the device guide for people who write their own software ---------------------------


def _guide_code() -> str:
    text = (REPO / "docs" / "integration-guide.md").read_text("utf-8")
    block = re.search(r"### Reference code.*?```python\n(.*?)```", text, flags=re.S)
    assert block, "the guide has a Python reference"
    return block[1]


class _GuidePort:
    """What the guide's code needs of a pyserial port, in front of a device that answers
    like the real one and loses the replies it is told to lose."""

    def __init__(self, lose: int = 0, **_: object) -> None:
        self.lose, self.waiting, self.sent, self.lit = lose, b"", [], set()

    def reset_input_buffer(self) -> None:
        self.waiting = b""

    def write(self, data: bytes) -> None:
        from vispek_hc.protocol import crc8

        self.sent.append(data)
        (self.lit.add if data[1] == 1 else self.lit.discard)(data[3])
        if self.lose:
            self.lose -= 1
            return
        body = bytes([0x3D, data[1], 1, 0])
        self.waiting += body + bytes([crc8(body)]) + b"\r\n"

    def flush(self) -> None: ...

    @property
    def in_waiting(self) -> int:
        return len(self.waiting)

    def read(self, count: int) -> bytes:
        data, self.waiting = self.waiting[:count], self.waiting[count:]
        return data

    def close(self) -> None: ...


def test_the_guides_reference_code_builds_the_same_bytes_and_survives_a_lost_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import time
    import types

    from vispek_hc.protocol import encode_off, encode_on

    code = _guide_code()
    ports: list[_GuidePort] = []

    def opened(*args: object, **options: object) -> _GuidePort:
        ports.append(_GuidePort(lose=2))  # the first OFF goes unanswered twice
        return ports[-1]

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=opened))
    clock = [0.0]
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(
        time, "monotonic", lambda: clock.__setitem__(0, clock[0] + 0.001) or clock[0]
    )
    scope: dict[str, object] = {}
    exec(compile(code, "integration-guide.md", "exec"), scope)  # runs the example to its end
    led_on, led_off = scope["led_on"], scope["led_off"]
    for led in range(1, 18):
        assert led_off(led) == encode_off(led)  # type: ignore[operator]
        for pwm in (0, 1, 36, 255, 256, 500, 1023):
            assert led_on(led, pwm) == encode_on(led, pwm)  # type: ignore[operator]
    (port,) = ports
    assert port.lit == set(), "the example leaves nothing lit"
    assert port.sent[:3] == [encode_off(1)] * 3 + [], "a lost reply is followed by a resend"
    assert encode_on(9, 36) in port.sent
    assert port.sent[-1] == encode_off(17)


def test_the_guide_names_no_wireless_interface_and_no_other_model() -> None:
    text = (REPO / "docs" / "integration-guide.md").read_text("utf-8")
    assert "There is no Bluetooth, no Wi-Fi" in text
    assert set(re.findall(r"\bHC\d+\b", text)) == {"HC1500"}, "the one model that ships"


def test_the_udev_rule_names_the_serial_adapter_and_says_what_it_opens() -> None:
    text = (REPO / "tools" / "99-vispek-hc.rules").read_text("utf-8")
    rules = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert rules == [
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0666", ENV{ID_MM_DEVICE_IGNORE}="1"'
    ]
    assert f"{vispek_hc.HC1500.serial_usb_vid:04x}" == "1a86"
    assert "UV-C" in text and "Not verified on hardware" in text
