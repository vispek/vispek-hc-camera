# SPDX-License-Identifier: Apache-2.0
"""Clean-room guard: identifiers and names that must never appear in shipped sources."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SCANNED_DIRS = ["src", "docs", "examples", "schemas", "tools", ".github"]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml", ".cff", ".rules", ".sh"}

# Names of an earlier, unrelated implementation and of a third party must never appear in
# what is shipped. They are kept as SHA-256 digests only, so that this file does not spell
# them either: whole identifiers (exact case), file-name suffixes, pairs of adjacent words
# and single words (all three in lower case).
FORBIDDEN_IDENTIFIER_DIGESTS = frozenset(
    {
        "034f519b2478c03f34b3238dc808c74ed8740989fedf73132ab94643a57efe53",
        "06946dae0067b5be99a5795d5c932a4df88d668579734517f2bd315812244f38",
        "1a4520911fe8f15d09d50be22f871b2627e4e79f9d516927b5a46aa1c4c29337",
        "2208b83bc2cc71b2b4fa40e0b2f478d2e553d5a4a1d6e65be17bb6402d56fcd9",
        "40a302cb6ddf1f3821bc4ffc985aad30bcb38296e9f530714cfe9fcb0b5c8def",
        "4106a1b38a46b41ca937ce0eb6b228f9d8d52b5e1fbb624aea92e95da6d4c644",
        "5696c9f4a0e58aa85c12d312e051162363c3f29a1fcdf0da152f43bf9a7a604b",
        "660b45b08b79098896afb432dae406778de617aec01aff8e8c02e425a933d4d7",
        "66bccc87ee889f2760afb37d423b807e00fb02583dd586d7b5a51426a5ce9fe7",
        "69cce44c1f53815daf61414473985e608b2550d6ff3faed5b5165eddf5f919d5",
        "6aa87a8add4f6c33abd3d85bc7c6aa759916be896fd4ae1612b33617929452f4",
        "6f141b2a390eec32304773d677f683ef29a67aad5d3ad88c6a48bc0d76945167",
        "7c5322293fa2993ebf1a2c96237235dba71d3176f64c3eae8baa76307968deb0",
        "808b87700ed3b53aca88796b0d39e07b7c667302e227b2ee8c790df5ed4123a2",
        "d5ec1bf0eff541967105d853d8d7ea2f029d4a4e5d0eb392f4aa13b3727f5dae",
        "e9ba785b92196be396cb929363df506be03b88bd686825eeb203db74541b3b85",
        "f7ee02a43fef9e5359670c540c1ad1b957bd8042388374be0f71c96f85df4f39",
    }
)
FORBIDDEN_SUFFIX_DIGESTS = frozenset(
    {"5a98923adcf5137b1226ec0315169182b4ad07d1ea662a0f3205b2a8e969b640"}
)
FORBIDDEN_PAIR_DIGESTS = frozenset(
    {"373c1bd8c5a4d0546bc16abaae9cf09daac4a43cf3be99124e7810bf897488c7"}
)
FORBIDDEN_WORD_DIGESTS = frozenset(
    {"1712661a84dfb98c625dd4a3dabf570261bfe16c5f94680191747eaaff196987"}
)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SUFFIX = re.compile(r"\.([A-Za-z0-9]+)(?![A-Za-z0-9_])")
WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")  # also splits CamelCase and snake_case
JOINED = re.compile(r"[\s_-]*")  # what may stand between the two words of a pair


@dataclass(frozen=True)
class Forbidden:
    identifiers: frozenset[str] = FORBIDDEN_IDENTIFIER_DIGESTS
    suffixes: frozenset[str] = FORBIDDEN_SUFFIX_DIGESTS
    pairs: frozenset[str] = FORBIDDEN_PAIR_DIGESTS
    words: frozenset[str] = FORBIDDEN_WORD_DIGESTS


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def find_forbidden(text: str, forbidden: Forbidden = Forbidden()) -> list[str]:  # noqa: B008
    """What kinds of forbidden names a text holds; never the names themselves."""
    found = []
    if any(digest(name) in forbidden.identifiers for name in set(IDENTIFIER.findall(text))):
        found.append("identifier")
    if any(digest(name.lower()) in forbidden.suffixes for name in set(SUFFIX.findall(text))):
        found.append("file-name suffix")
    words = list(WORD.finditer(text))
    for first, second in pairwise(words):
        between = text[first.end() : second.start()]
        pair = f"{first[0].lower()} {second[0].lower()}"
        if JOINED.fullmatch(between) and digest(pair) in forbidden.pairs:
            found.append("earlier product name")
            break
    if any(digest(word[0].lower()) in forbidden.words for word in words):
        found.append("third-party name")
    return found


def is_text(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in {"NOTICE", "CODEOWNERS"}


def shipped_text_files() -> list[Path]:
    files = [path for path in sorted(REPO.iterdir()) if path.is_file() and is_text(path)]
    for name in SCANNED_DIRS:
        files += [
            path for path in sorted((REPO / name).rglob("*")) if path.is_file() and is_text(path)
        ]
    return files


def test_shipped_sources_use_none_of_the_forbidden_names() -> None:
    files = shipped_text_files()
    names = {path.relative_to(REPO).as_posix() for path in files}
    assert {"README.md", "AGENTS.md", "CHANGELOG.md", "docs/provenance.md"} <= names
    assert any(name.endswith("protocol.py") for name in names), "scan found no sources"
    offenders = {
        path.relative_to(REPO).as_posix(): hits
        for path in files
        if (hits := find_forbidden(path.read_text("utf-8")))
    }
    assert offenders == {}


CANARY = Forbidden(
    identifiers=frozenset({digest("ExampleThing"), digest("do_example"), digest("EX")}),
    suffixes=frozenset({digest("exmpl")}),
    pairs=frozenset({digest("example suite")}),
    words=frozenset({digest("example")}),
)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("x = EX[3]", "identifier"),
        ("class ExampleThing:", "identifier"),
        ("def do_example(led):", "identifier"),
        ("cube.do_example(path)", "identifier"),
        ("open('scan.exmpl')", "file-name suffix"),
        ("Example Suite 2.1", "earlier product name"),
        ("example-suite", "earlier product name"),
        ("ExampleSuite", "earlier product name"),
    ],
)
def test_the_guard_catches_what_it_knows_only_by_digest(text: str, kind: str) -> None:
    assert kind in find_forbidden(text, CANARY)


@pytest.mark.parametrize(
    "text",
    [
        "RECORD_EX = ('single', 'scan')",
        "def do_example_only(): ...",
        "save_do_example_file",
        "INDEX = 1",
        "scan.exmpls",
    ],
)
def test_the_guard_leaves_other_identifiers_and_suffixes_alone(text: str) -> None:
    kinds = find_forbidden(text, CANARY)
    assert "identifier" not in kinds and "file-name suffix" not in kinds


def test_a_pair_of_words_must_stand_together() -> None:
    assert "earlier product name" not in find_forbidden("the Example suites", CANARY)
    assert "earlier product name" not in find_forbidden("an example. Suite two", CANARY)


@pytest.mark.parametrize(
    "text", ["made by Example Ltd", "class ExampleCamera:", "example_sdk.open()", "EXAMPLE"]
)
def test_the_guard_finds_a_word_it_knows_only_by_its_digest(text: str) -> None:
    assert "third-party name" in find_forbidden(text, CANARY)
    assert find_forbidden("an exemplary sample", CANARY) == []


def test_every_forbidden_name_is_kept_as_a_digest_only() -> None:
    kept = Forbidden()
    sizes = (len(kept.identifiers), len(kept.suffixes), len(kept.pairs), len(kept.words))
    assert sizes == (17, 1, 1, 1)
    for digests in (kept.identifiers, kept.suffixes, kept.pairs, kept.words):
        assert all(re.fullmatch(r"[0-9a-f]{64}", entry) for entry in digests)


def test_provenance_statement_exists() -> None:
    text = (REPO / "docs" / "provenance.md").read_text("utf-8")
    assert "independent implementation" in text
