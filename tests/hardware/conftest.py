# SPDX-License-Identifier: Apache-2.0
"""Fixtures for the tests that need a real HC1500. See docs/acceptance/hardware-runbook.md."""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import vispek_hc


def load_checks() -> Any:
    """The checks live in a plain file beside this one; the tests folder is not a package."""
    spec = importlib.util.spec_from_file_location(
        "acceptance", Path(__file__).with_name("acceptance.py")
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_mode() -> str:
    try:
        return "REHEARSAL on the simulated device" if load_checks().simulated() else "REAL DEVICE"
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error


def pytest_sessionstart(session: pytest.Session) -> None:
    run_mode()  # a mistyped rehearsal switch ends the run here, before anything is opened


def pytest_report_header(config: pytest.Config) -> str:
    return f"vispek-hc hardware acceptance: {run_mode()}"


@pytest.fixture(scope="session")
def acceptance() -> Any:
    run_mode()  # refuses a mistyped rehearsal switch before any device is opened
    return load_checks()


@pytest.fixture(scope="session")
def acceptance_dir(tmp_path_factory: pytest.TempPathFactory, acceptance: Any) -> Path:
    """Where records, captures and the unit profile of this run go: a folder of its own,
    so a second run never meets the captures of the first."""
    chosen = os.environ.get("VISPEK_HC_ACCEPTANCE_DIR")
    if not chosen:
        return tmp_path_factory.mktemp("acceptance")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(chosen) / (stamp + ("-rehearsal" if acceptance.simulated() else ""))
    path.mkdir(parents=True)
    return path


@pytest.fixture(autouse=True)
def home(acceptance_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The run keeps its own unit profile, so it starts from the model's defaults."""
    path = acceptance_dir / "home"
    monkeypatch.setenv("VISPEK_HC_HOME", str(path))
    return path


@pytest.fixture(scope="session")
def records(acceptance_dir: Path, acceptance: Any) -> Iterator[dict[str, Any]]:
    """What the checks measured; written to ``records.json`` when the run ends."""
    collected: dict[str, Any] = {
        "started_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "software_version": vispek_hc.__version__,
        "unit": os.environ.get("VISPEK_HC_UNIT", "hc1500-1"),
        "simulated": acceptance.simulated(),
        "checks": {},
    }
    yield collected
    target = acceptance_dir / "records.json"
    target.write_text(json.dumps(collected, indent=1, default=str) + "\n", "utf-8")
    print(f"\nacceptance records ({run_mode()}): {target}")  # noqa: T201


@pytest.fixture
def session(acceptance: Any) -> Iterator[vispek_hc.Session]:
    with vispek_hc.open_session(**acceptance.device_options()) as opened:
        yield opened
