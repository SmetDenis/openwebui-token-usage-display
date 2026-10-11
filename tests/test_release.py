"""The release rules of scripts/release.py: on this repository, and on a throwaway git repository.

`main` is what users install, and every merge that changes the plugin is released by CI. So the
version must be written the same in every file, and a changed plugin must carry a version above
every release — a forgotten bump, or two branches taking the same number, fails here before merge.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


def test_version_is_written_the_same_everywhere(release_module: ModuleType) -> None:
    assert release_module.consistency_problems() == []


def test_changed_plugin_carries_a_new_version(release_module: ModuleType, release_tags: dict[str, str]) -> None:
    assert release_module.bump_problems(release_tags) == []


# --- the rules on a throwaway repository -------------------------------------------------------


def _plugin(version: str, body: str = "pass") -> str:
    return f'"""\ntitle: x\nversion: {version}\n"""\n\n{body}\n'


def _git(root: Path, *args: str) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    subprocess.run(  # noqa: S603 - fixed git argv, no shell
        [git, "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        check=True,
    )


def _release(root: Path, version: str, tag: str, body: str = "pass") -> None:
    (root / "usage_display.py").write_text(_plugin(version, body), encoding="utf-8")
    _git(root, "add", "usage_display.py")
    _git(root, "commit", "-q", "-m", version)
    _git(root, "tag", tag)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _release(tmp_path, "1.0.0", "v1.0.0")
    _release(tmp_path, "1.1.0", "1.1.0")  # a bare tag, as `2.7.0` was published
    return tmp_path


def test_bump_rule_on_a_repository(release_module: ModuleType, repo: Path) -> None:
    tags = release_module.release_tags(repo)
    assert tags == {"1.0.0": "v1.0.0", "1.1.0": "1.1.0"}
    assert release_module.bump_problems(tags, repo) == []  # main right after a release

    # the plugin changed but kept its number: the forgotten bump
    (repo / "usage_display.py").write_text(_plugin("1.1.0", "changed"), encoding="utf-8")
    assert release_module.bump_problems(tags, repo) == [
        "usage_display.py changed since 1.1.0, but its version is still 1.1.0: bump it"
    ]
    # a number at or below the latest release (another branch took it, or a typo)
    (repo / "usage_display.py").write_text(_plugin("1.0.5", "changed"), encoding="utf-8")
    assert release_module.bump_problems(tags, repo) == ["1.0.5 is not above the latest release 1.1.0"]
    # semver order, not string order: 1.10.0 is above 1.9.0
    (repo / "usage_display.py").write_text(_plugin("1.10.0", "changed"), encoding="utf-8")
    assert release_module.bump_problems({**tags, "1.9.0": "v1.9.0"}, repo) == []


def test_release_tags_prefer_the_v_form_and_ignore_other_tags(release_module: ModuleType, repo: Path) -> None:
    _git(repo, "tag", "v1.1.0")
    _git(repo, "tag", "nightly")
    assert release_module.release_tags(repo) == {"1.0.0": "v1.0.0", "1.1.0": "v1.1.0"}
    assert release_module.release_tags(repo / "missing") == {}  # not a git checkout


def test_consistency_and_notes_on_a_tree(release_module: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "usage_display.py").write_text(_plugin("2.1.0"), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "other"\nversion = "9.9.9"\n'
        '[[package]]\nname = "openwebui-token-usage-display"\nversion = "2.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\nintro\n\n## [2.0.0] - full rebuild\n\n- Old.\n\n## [1.0.0]\n\n- Older.\n", encoding="utf-8"
    )
    assert release_module.consistency_problems(tmp_path) == [
        "pyproject.toml says 2.0.0, usage_display.py says 2.1.0",
        "the top CHANGELOG.md section is 2.0.0, usage_display.py says 2.1.0",
    ]
    assert release_module.changelog_notes("2.0.0", tmp_path) == "- Old."  # heading and its trailing text dropped
    with pytest.raises(ValueError, match=r"no section for 2\.1\.0"):
        release_module.changelog_notes("2.1.0", tmp_path)
    (tmp_path / "usage_display.py").write_text('"""\ntitle: x\n"""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no `version:` line"):
        release_module.plugin_version(tmp_path)


def test_next_version_by_semver_part(release_module: ModuleType) -> None:
    tags = {"2.7.0": "2.7.0", "2.6.0": "v2.6.0", "2.10.1": "v2.10.1"}
    assert release_module.next_version("patch", tags) == "2.10.2"
    assert release_module.next_version("minor", tags) == "2.11.0"
    assert release_module.next_version("major", tags) == "3.0.0"
    assert release_module.next_version("minor", {}) == "0.1.0"
    with pytest.raises(ValueError, match="major, minor or patch"):
        release_module.next_version("", tags)


def test_write_version_opens_or_renames_the_changelog_section(release_module: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "usage_display.py").write_text(_plugin("1.1.0", "version: 9"), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.1.0"\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        'version = 1\nrevision = 3\n\n[[package]]\nname = "openwebui-token-usage-display"\nversion = "1.1.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [1.1.0]\n\n- Released.\n", encoding="utf-8")
    tags = {"1.1.0": "v1.1.0"}

    release_module.write_version("1.2.0", tags, tmp_path)  # after a release: a new section opens
    assert release_module.plugin_version(tmp_path) == "1.2.0"
    assert "version: 9" in (tmp_path / "usage_display.py").read_text(encoding="utf-8")  # only the frontmatter line
    assert release_module.pyproject_version(tmp_path) == "1.2.0"
    assert release_module.lock_version(tmp_path) == "1.2.0"
    assert "revision = 3" in (tmp_path / "uv.lock").read_text(encoding="utf-8")  # not re-locked
    assert release_module.changelog_versions(tmp_path) == ["1.2.0", "1.1.0"]
    assert release_module.consistency_problems(tmp_path) == ["the CHANGELOG.md section for 1.2.0 is empty"]

    text = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8").replace("## [1.2.0]\n", "## [1.2.0]\n\n- New.\n")
    (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    release_module.write_version("2.0.0", tags, tmp_path)  # unreleased 1.2.0 renumbered, its text kept
    assert release_module.changelog_versions(tmp_path) == ["2.0.0", "1.1.0"]
    assert release_module.changelog_notes("2.0.0", tmp_path) == "- New."
    assert release_module.consistency_problems(tmp_path) == []

    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"pyproject\.toml: no version to replace"):
        release_module.write_version("2.0.1", tags, tmp_path)


def test_cli(release_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    version = release_module.plugin_version()
    assert release_module.main(["release.py", "version"]) == 0
    assert capsys.readouterr().out.strip() == version
    monkeypatch.setattr(release_module, "release_tags", lambda: {version: f"v{version}"})
    assert release_module.main(["release.py", "pending"]) == 0
    assert capsys.readouterr().out == ""  # already released: the release job does nothing
    monkeypatch.setattr(release_module, "release_tags", dict)
    assert release_module.main(["release.py", "pending"]) == 0
    assert capsys.readouterr().out.strip() == version
    assert release_module.main(["release.py", "notes"]) == 0
    assert not capsys.readouterr().out.startswith("## [")
    assert release_module.main(["release.py", "check"]) == 0
    monkeypatch.setattr(release_module, "consistency_problems", lambda: ["boom"])
    assert release_module.main(["release.py", "check"]) == 1
    assert "release check: boom" in capsys.readouterr().err
    assert release_module.main(["release.py", "bogus"]) == 2

    written: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(release_module, "release_tags", lambda: {"2.7.0": "2.7.0"})
    monkeypatch.setattr(release_module, "write_version", lambda v, tags: written.append((v, tags)))
    assert release_module.main(["release.py", "bump", "minor"]) == 0
    assert written == [("2.8.0", {"2.7.0": "2.7.0"})]
    assert "2.8.0: written" in capsys.readouterr().out
