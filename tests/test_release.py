# SPDX-License-Identifier: Apache-2.0
"""The release helper and the workflows that run without anybody watching."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).parent.parent
CHANGELOG = """# Changelog

## [Unreleased]

### Added

- something new

## [0.1.0] - 2026-11-01

### Added

- the first release

## [0.0.9] - 2026-10-01

- older
"""


@pytest.fixture
def release(tmp_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("release", REPO / "tools" / "release.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, "utf-8")
    return module


def pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', "utf-8")


@pytest.mark.parametrize(
    ("tag", "version"),
    [("v0.1.0", "0.1.0"), ("v0.1.0-rc1", "0.1.0rc1"), ("v1.12.3rc10", "1.12.3rc10")],
)
def test_tag_names_the_package_version(release: ModuleType, tag: str, version: str) -> None:
    assert release.version_of_tag(tag) == version
    assert release.is_candidate(tag) == ("rc" in version)


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "v0.1.0-beta1", "v0.1.0.dev0", "release", ""])
def test_other_tags_are_not_releases(release: ModuleType, tag: str) -> None:
    with pytest.raises(ValueError, match="not a release tag"):
        release.version_of_tag(tag)


def test_release_notes_are_the_changelog_section_of_that_version(
    release: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject(tmp_path, "0.1.0")
    assert release.main(["release.py", "v0.1.0"]) == 0
    assert capsys.readouterr().out.strip() == "### Added\n\n- the first release"


def test_a_candidate_takes_the_section_of_its_final_version_or_unreleased(
    release: ModuleType,
) -> None:
    assert "the first release" in release.changelog_section(CHANGELOG, "0.1.0rc1")
    assert "something new" in release.changelog_section(CHANGELOG, "0.2.0rc1")
    with pytest.raises(ValueError, match="no section"):
        release.changelog_section("# Changelog\n", "0.2.0rc1")


def test_a_final_release_needs_a_section_of_its_own(release: ModuleType) -> None:
    with pytest.raises(ValueError, match=r"no section for 0\.2\.0"):
        release.changelog_section(CHANGELOG, "0.2.0")
    emptied = CHANGELOG.replace("### Added\n\n- the first release", "")
    with pytest.raises(ValueError, match=r"no section for 0\.1\.0"):
        release.changelog_section(emptied, "0.1.0")


def test_tag_that_is_not_the_package_version_stops_the_release(
    release: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject(tmp_path, "0.1.0")
    assert release.main(["release.py", "v0.1.1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "", "no notes for a release that must not happen"
    assert "pyproject.toml says 0.1.0" in captured.err


def test_the_real_changelog_keeps_its_unreleased_heading() -> None:
    # Only the heading: right after a release the section is empty, and that is fine.
    assert "\n## [Unreleased]\n" in (REPO / "CHANGELOG.md").read_text("utf-8")


def test_every_action_in_every_workflow_is_pinned_to_a_commit() -> None:
    uses = []
    for workflow in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        uses += re.findall(r"uses:\s*(\S+)", workflow.read_text("utf-8"))
    assert len(uses) >= 10
    unpinned = [use for use in uses if not re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", use)]
    assert unpinned == []


def test_workflows_ask_for_no_more_than_read_access_by_default() -> None:
    for workflow in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text("utf-8")
        assert "\npermissions:\n  contents: read\n" in text, workflow.name


def test_the_wheel_carries_the_camera_helper_as_source_never_as_a_binary() -> None:
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))
    carried = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert carried == {
        "tools/uvc-util/src": "vispek_hc/_uvc_util/src",
        "tools/uvc-util/LICENSE": "vispek_hc/_uvc_util/LICENSE",
        "tools/uvc-util/SHA256SUMS": "vispek_hc/_uvc_util/SHA256SUMS",
    }
    assert "tools/uvc-util/LICENSE" in project["project"]["license-files"]
    listed = {
        line.split()[1]
        for line in (REPO / "tools" / "uvc-util" / "SHA256SUMS").read_text("ascii").splitlines()
    }
    sources = {f"src/{path.name}" for path in (REPO / "tools" / "uvc-util" / "src").iterdir()}
    assert listed == sources | {"LICENSE"}, "every file that is shipped has a checksum"
    assert all(
        path.suffix in {".m", ".h"} for path in (REPO / "tools" / "uvc-util" / "src").iterdir()
    )


# --- the project description on the package index ---------------------------------------


@pytest.fixture(scope="module")
def pypi_readme() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pypi_readme", REPO / "tools" / "pypi_readme.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_relative_links_of_the_readme_become_links_into_the_repository(
    pypi_readme: ModuleType,
) -> None:
    # On the package index the README stands alone: a link to docs/cli.md leads nowhere.
    text = (
        "[cli](docs/cli.md), [examples](examples/), [中文](README.zh-CN.md), "
        "[web](https://example.org/a), [top](#install), [part](docs/cli.md#the-contract)"
    )
    base = "https://github.com/vispek/vispek-hc-camera"
    assert pypi_readme.absolute_links(text, "v0.1.0") == (
        f"[cli]({base}/blob/v0.1.0/docs/cli.md), [examples]({base}/tree/v0.1.0/examples/), "
        f"[中文]({base}/blob/v0.1.0/README.zh-CN.md), "
        "[web](https://example.org/a), [top](#install), "
        f"[part]({base}/blob/v0.1.0/docs/cli.md#the-contract)"
    )


@pytest.mark.parametrize(
    ("version", "ref"),
    [("0.1.0.dev0", "main"), ("0.1.0", "v0.1.0"), ("0.1.0rc1", "v0.1.0-rc1"), ("1.2.3", "v1.2.3")],
)
def test_a_release_links_to_its_own_tag_and_a_development_version_to_main(
    pypi_readme: ModuleType, release: ModuleType, version: str, ref: str
) -> None:
    assert pypi_readme.ref_of_version(version) == ref
    if ref != "main":
        assert release.version_of_tag(ref) == version, "the tag the release workflow accepts"


def test_the_real_readme_is_left_without_a_relative_link(pypi_readme: ModuleType) -> None:
    text = pypi_readme.absolute_links((REPO / "README.md").read_text("utf-8"), "main")
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    assert len(targets) > 15
    assert [t for t in targets if not t.startswith(("https://", "#"))] == []
    assert pypi_readme.BASE + "/blob/main/docs/integration-guide.md" in targets
    assert pypi_readme.BASE + "/tree/main/examples/" in targets


def test_the_package_description_is_made_by_the_build_hook() -> None:
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))
    assert project["project"]["dynamic"] == ["readme"]
    assert "readme" not in project["project"]
    assert "custom" in project["tool"]["hatch"]["metadata"]["hooks"]
    assert "/hatch_build.py" in project["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    assert project["project"]["urls"]["Repository"] == "https://github.com/vispek/vispek-hc-camera"
