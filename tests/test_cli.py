# SPDX-License-Identifier: Apache-2.0
"""CLI contract: one JSON envelope on stdout, exit-code families, never interactive."""

from __future__ import annotations

import argparse
import json
import logging
import math
import platform
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

import vispek_hc
from vispek_hc import cli
from vispek_hc.errors import AmbiguousDeviceError

pytestmark = pytest.mark.usefixtures("quiet_host")

ENVELOPE_KEYS = {"schema", "command", "version", "ok", "warnings", "exit_code"}


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    exit_code = cli.main(list(argv))
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert out.count("\n") == 1, "stdout must hold exactly one JSON line"
    return exit_code, envelope


def test_version_flag_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"vispek-hc {vispek_hc.__version__}\n"


def test_version_with_json_keeps_the_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, envelope = run_json(capsys, "--json", "--version")
    assert exit_code == 0
    assert envelope["command"] is None
    assert envelope["data"] == {"version": vispek_hc.__version__}


HELP_ARGVS = [["--help"], *([name, "--help"] for name in cli.HANDLERS)]


@pytest.mark.parametrize("argv", [*HELP_ARGVS, *(["--json", *argv] for argv in HELP_ARGVS)])
def test_help_prints_usage_and_exits_zero_even_with_json(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Help is the one documented exception to "--json prints an envelope".
    with pytest.raises(SystemExit) as caught:
        cli.main(argv)
    assert caught.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "--json" in out
    assert "hc1500-1" in out


def test_abbreviated_flags_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", "--js"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "USAGE_ERROR" in captured.err


def test_check_json_emits_a_success_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, envelope = run_json(capsys, "check", "--json")
    assert exit_code == 0
    assert set(envelope) == ENVELOPE_KEYS | {"data"}
    assert envelope["schema"] == "vispek-hc.cli/1"
    assert envelope["command"] == "check"
    assert envelope["version"] == vispek_hc.__version__
    assert envelope["ok"] is True
    assert envelope["exit_code"] == 0
    assert envelope["warnings"] == []
    assert envelope["data"]["led_table"] == {"version": "hc1500-nominal-1", "verified": False}
    assert envelope["data"]["python"]["version"] == platform.python_version()
    assert envelope["data"]["unit"] == "hc1500-1"


def test_global_flags_work_before_and_after_the_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, before = run_json(capsys, "--json", "check")
    _, after = run_json(capsys, "check", "--json")
    assert before == after


def test_plain_output_is_ascii_and_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_usage_error_keeps_the_json_contract(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, envelope = run_json(capsys, "--json", "no-such-command")
    assert exit_code == 2
    assert set(envelope) == ENVELOPE_KEYS | {"error"}
    assert envelope["ok"] is False
    assert envelope["exit_code"] == 2
    assert envelope["command"] is None
    assert envelope["error"]["code"] == "USAGE_ERROR"
    assert set(envelope["error"]) == {"code", "message", "hint", "details"}


def test_missing_command_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, envelope = run_json(capsys, "--json")
    assert exit_code == 2
    assert envelope["error"]["code"] == "USAGE_ERROR"


def test_usage_error_without_json_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["no-such-command"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "USAGE_ERROR" in captured.err


def fail_with(error: BaseException) -> Any:
    def handler(args: argparse.Namespace) -> cli.Outcome:
        raise error

    return handler


def test_library_error_maps_to_its_family_exit_code(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    error = AmbiguousDeviceError(
        "2 serial ports match", hint="pass --port", details={"candidates": ["a", "b"]}
    )
    monkeypatch.setitem(cli.HANDLERS, "check", fail_with(error))
    exit_code, envelope = run_json(capsys, "check", "--json")
    assert exit_code == 4
    assert envelope["exit_code"] == 4
    assert envelope["error"] == error.to_dict()


def test_unexpected_exception_becomes_internal_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "check", fail_with(RuntimeError("boom")))
    exit_code, envelope = run_json(capsys, "check", "--json")
    assert exit_code == 1
    assert envelope["error"]["code"] == "INTERNAL_ERROR"
    assert "boom" in envelope["error"]["message"]


def test_keyboard_interrupt_exits_130_as_cancelled(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "check", fail_with(KeyboardInterrupt()))
    exit_code, envelope = run_json(capsys, "check", "--json")
    assert exit_code == 130
    assert envelope["error"]["code"] == "CANCELLED"


def test_warnings_reach_the_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        cli.HANDLERS, "check", lambda args: cli.Outcome({"x": 1}, warnings=["careful"])
    )
    exit_code, envelope = run_json(capsys, "check", "--json")
    assert exit_code == 0
    assert envelope["data"] == {"x": 1}
    assert envelope["warnings"] == ["careful"]


def test_global_options_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []

    def spy(args: argparse.Namespace) -> cli.Outcome:
        seen.append(args)
        return cli.Outcome({})

    monkeypatch.setitem(cli.HANDLERS, "check", spy)
    argv = ["--unit", "bench-2", "check", "--simulate", "--dry-run", "--port", "/dev/x", "-vv"]
    assert cli.main([*argv, "--camera", "LRCP 1080P-60FPS"]) == 0
    (args,) = seen
    assert (args.unit, args.port, args.camera) == ("bench-2", "/dev/x", "LRCP 1080P-60FPS")
    assert (args.simulate, args.dry_run, args.verbose, args.json) == (True, True, 2, False)


def test_default_unit_label(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setitem(cli.HANDLERS, "check", lambda args: seen.append(args) or cli.Outcome({}))
    assert cli.main(["check"]) == 0
    assert seen[0].unit == "hc1500-1"
    assert (seen[0].port, seen[0].camera, seen[0].simulate) == (None, None, False)


def test_plain_error_shows_code_message_and_hint(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    error = AmbiguousDeviceError("2 serial ports match", hint="pass --port")
    monkeypatch.setitem(cli.HANDLERS, "check", fail_with(error))
    assert cli.main(["check"]) == 4
    assert capsys.readouterr().err.splitlines() == [
        "error [AMBIGUOUS_DEVICE]: 2 serial ports match",
        "hint: pass --port",
    ]


def test_plain_warnings_go_to_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        cli.HANDLERS, "check", lambda args: cli.Outcome({"x": 1}, warnings=["careful"])
    )
    assert cli.main(["check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "x: 1\n"
    assert captured.err == "warning: careful\n"


def log_and_finish(args: argparse.Namespace) -> cli.Outcome:
    logger = logging.getLogger("vispek_hc.test")
    logger.debug("fine detail")
    logger.info("progress %d", 1)
    return cli.Outcome({})


def test_logs_are_silent_by_default_and_never_on_stdout(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "check", log_and_finish)
    assert cli.main(["check"]) == 0
    assert capsys.readouterr().err == ""
    assert cli.main(["check", "-v"]) == 0
    captured = capsys.readouterr()
    assert captured.err == "INFO vispek_hc.test: progress 1\n"
    assert "progress" not in captured.out


def test_json_mode_logs_ndjson_on_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "check", log_and_finish)
    assert cli.main(["check", "--json", "-vv"]) == 0
    captured = capsys.readouterr()
    assert [json.loads(line) for line in captured.err.splitlines()] == [
        {"level": "DEBUG", "logger": "vispek_hc.test", "message": "fine detail"},
        {"level": "INFO", "logger": "vispek_hc.test", "message": "progress 1"},
    ]
    assert json.loads(captured.out)["ok"] is True


def test_python_dash_m_runs_the_same_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["vispek_hc", "check", "--json"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("vispek_hc", run_name="__main__")
    assert caught.value.code == 0
    assert json.loads(capsys.readouterr().out)["command"] == "check"


def test_json_envelope_stringifies_values_json_cannot_hold(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        cli.HANDLERS, "check", lambda args: cli.Outcome({"dir": Path("captures") / "first"})
    )
    exit_code, envelope = run_json(capsys, "check", "--json")
    assert exit_code == 0
    assert Path(envelope["data"]["dir"]) == Path("captures") / "first"


def test_json_envelope_never_contains_nan(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "check", lambda args: cli.Outcome({"median": math.nan}))
    exit_code, envelope = run_json(capsys, "check", "--json")
    assert exit_code == 1
    assert envelope["ok"] is False
    assert envelope["command"] == "check"
    assert envelope["error"]["code"] == "INTERNAL_ERROR"


def test_unexpected_exception_traceback_is_logged_at_debug_level(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "check", fail_with(RuntimeError("boom")))
    assert cli.main(["check"]) == 1
    assert "Traceback" not in capsys.readouterr().err
    assert cli.main(["check", "-vv"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "RuntimeError: boom" in err


def test_json_log_lines_carry_the_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(cli.HANDLERS, "check", fail_with(RuntimeError("boom")))
    assert cli.main(["check", "--json", "-vv"]) == 1
    (line,) = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert line["message"] == "unexpected failure"
    assert "RuntimeError: boom" in line["traceback"]
