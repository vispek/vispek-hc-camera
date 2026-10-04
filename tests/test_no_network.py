# SPDX-License-Identifier: Apache-2.0
"""The SDK makes no network connections, runs no shell and never prompts: AST scan of ``src/``."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src"

NETWORK_MODULES = {
    "aiohttp",
    "ftplib",
    "http",
    "httpx",
    "imaplib",
    "poplib",
    "requests",
    "smtplib",
    "socket",
    "socketserver",
    "ssl",
    "telnetlib",
    "urllib3",
    "websocket",
    "websockets",
    "xmlrpc",
}
NETWORK_SUBMODULES = {"urllib.request", "urllib.error", "urllib.robotparser"}

# (file relative to src/, module) pairs that may import a network module. Empty on purpose:
# the only planned entry is the opt-in uvc-util download command.
ALLOWED: set[tuple[str, str]] = set()


def is_network_module(module: str) -> bool:
    return module.split(".")[0] in NETWORK_MODULES or any(
        module == name or module.startswith(name + ".") for name in NETWORK_SUBMODULES
    )


def imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules += [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    return modules


def network_imports(source: str) -> list[str]:
    return sorted({m for m in imported_modules(ast.parse(source)) if is_network_module(m)})


SHELL_FUNCTIONS = {"system", "popen", "getoutput", "getstatusoutput"}
PROMPT_FUNCTIONS = {"input", "getpass"}


def called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def calls(source: str) -> list[ast.Call]:
    return [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]


def shell_calls(source: str) -> list[int]:
    return [
        node.lineno
        for node in calls(source)
        if called_name(node) in SHELL_FUNCTIONS
        or any(
            keyword.arg == "shell"
            and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
            for keyword in node.keywords
        )
    ]


def prompt_calls(source: str) -> list[int]:
    return [node.lineno for node in calls(source) if called_name(node) in PROMPT_FUNCTIONS]


def source_files() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, "scan found no sources"
    return files


def test_sources_import_no_network_modules() -> None:
    offenders = {
        path.relative_to(SRC).as_posix(): hits
        for path in source_files()
        if (
            hits := [
                module
                for module in network_imports(path.read_text("utf-8"))
                if (path.relative_to(SRC).as_posix(), module) not in ALLOWED
            ]
        )
    }
    assert offenders == {}


def test_sources_never_run_a_shell() -> None:
    offenders = {
        path.relative_to(SRC).as_posix(): lines
        for path in source_files()
        if (lines := shell_calls(path.read_text("utf-8")))
    }
    assert offenders == {}


def test_sources_never_prompt_for_input() -> None:
    offenders = {
        path.relative_to(SRC).as_posix(): lines
        for path in source_files()
        if (lines := prompt_calls(path.read_text("utf-8")))
    }
    assert offenders == {}


@pytest.mark.parametrize(
    "source",
    [
        "import socket",
        "import urllib.request",
        "from urllib import request",
        "from urllib.request import urlopen",
        "import http.client",
        "from http.server import HTTPServer",
        "import requests",
        "def f():\n    import ssl",
    ],
)
def test_the_scan_catches_network_imports(source: str) -> None:
    assert network_imports(source)


@pytest.mark.parametrize(
    "source",
    ["import urllib.parse", "from urllib.parse import quote", "import json", "import serial"],
)
def test_the_scan_allows_offline_modules(source: str) -> None:
    assert network_imports(source) == []


def test_the_scan_catches_shell_true() -> None:
    assert shell_calls("import subprocess\nsubprocess.run('ls', shell=True)") == [2]
    assert shell_calls("import subprocess\nsubprocess.run('ls', shell=flag)") == [2]
    assert shell_calls("import subprocess\nsubprocess.run(['ls'], shell=False)") == []


def test_the_scan_catches_shell_like_functions() -> None:
    assert shell_calls("import os\nos.system('ls')") == [2]
    assert shell_calls("import subprocess\nsubprocess.getoutput('ls')") == [2]


def test_the_scan_catches_prompts() -> None:
    assert prompt_calls("answer = input('port? ')") == [1]
    assert prompt_calls("import getpass\nsecret = getpass.getpass()") == [2]
    assert prompt_calls("data = stream.read()") == []
