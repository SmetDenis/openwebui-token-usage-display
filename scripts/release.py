"""Release bookkeeping for usage_display.py: one version, written in four places, released on merge.

Users paste `usage_display.py` straight from `main`, so `main` must always equal a release: every
merge that changes the plugin carries a new version, and CI tags and publishes it (`ci.yml`, job
`release`). This script holds the rules both the test suite and that job apply. Stdlib only: the
release job runs it with the runner's own Python, outside the project venv.

    python3 scripts/release.py bump minor  # next number from the latest tag (major|minor|patch), in all four files
    python3 scripts/release.py version     # the plugin's version
    python3 scripts/release.py pending     # that version if it has no release tag yet, else nothing
    python3 scripts/release.py notes       # its CHANGELOG section, heading stripped
    python3 scripts/release.py check       # every rule below; exit 1 with the problems
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "usage_display.py"
PROJECT = "openwebui-token-usage-display"
# Release tags are `vX.Y.Z`; the bare `2.7.0` (created by hand in the GitHub UI) is tolerated.
TAG = re.compile(r"^v?(\d+\.\d+\.\d+)$")
FRONTMATTER_VERSION = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)
CHANGELOG_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def semver(version: str) -> tuple[int, int, int]:
    """`X.Y.Z` as a comparable tuple."""
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def plugin_version(root: Path = ROOT) -> str:
    """The `version:` line of the docstring frontmatter — the single source of truth."""
    head = (root / PLUGIN).read_text(encoding="utf-8").split('"""', 2)[1]
    match = FRONTMATTER_VERSION.search(head)
    if match is None:
        msg = f"no `version:` line in the {PLUGIN} frontmatter"
        raise ValueError(msg)
    return match.group(1)


def pyproject_version(root: Path = ROOT) -> str:
    """`[project] version` in pyproject.toml."""
    with (root / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def lock_version(root: Path = ROOT) -> str:
    """The project's own package version recorded in uv.lock."""
    with (root / "uv.lock").open("rb") as f:
        packages = tomllib.load(f)["package"]
    return next(str(p["version"]) for p in packages if p["name"] == PROJECT)


def changelog_versions(root: Path = ROOT) -> list[str]:
    """Versions of the CHANGELOG sections, top first."""
    return CHANGELOG_HEADING.findall((root / "CHANGELOG.md").read_text(encoding="utf-8"))


def changelog_notes(version: str, root: Path = ROOT) -> str:
    """The body of one CHANGELOG section (its heading dropped: the release title carries the version)."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = re.split(r"^(?=## \[)", text, flags=re.MULTILINE)
    for section in sections:
        heading, _, body = section.partition("\n")
        if heading.startswith(f"## [{version}]"):
            return body.strip()
    msg = f"CHANGELOG.md has no section for {version}"
    raise ValueError(msg)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout  # noqa: S603, S607 - fixed git argv, no shell


def release_tags(root: Path = ROOT) -> dict[str, str]:
    """Released version -> its tag name; empty outside a git checkout or without tags."""
    try:
        names = _git(root, "tag", "--list").split()
    except (OSError, subprocess.CalledProcessError):
        return {}
    tags: dict[str, str] = {}
    for name in names:
        match = TAG.match(name)
        if match and (match.group(1) not in tags or name.startswith("v")):
            tags[match.group(1)] = name  # prefer `vX.Y.Z` should both forms exist
    return tags


def tagged_plugin(tag: str, root: Path = ROOT) -> str:
    """`usage_display.py` as released under a tag."""
    return _git(root, "show", f"{tag}:{PLUGIN}")


def consistency_problems(root: Path = ROOT) -> list[str]:
    """Every place the version is written must agree, and the CHANGELOG must lead with it."""
    version = plugin_version(root)
    problems = [
        f"{where} says {found}, {PLUGIN} says {version}"
        for where, found in (("pyproject.toml", pyproject_version(root)), ("uv.lock", lock_version(root)))
        if found != version
    ]
    top = next(iter(changelog_versions(root)), None)
    if top != version:
        problems.append(f"the top CHANGELOG.md section is {top}, {PLUGIN} says {version}")
    elif not changelog_notes(version, root):
        problems.append(f"the CHANGELOG.md section for {version} is empty")
    return problems


def next_version(part: str, tags: dict[str, str]) -> str:
    """The number after the latest release, by semver part: major (breaking), minor (feature), patch (fix)."""
    major, minor, patch = semver(max(tags, key=semver, default="0.0.0"))
    bumped = {"major": (major + 1, 0, 0), "minor": (major, minor + 1, 0), "patch": (major, minor, patch + 1)}
    if part not in bumped:
        msg = f"bump takes major, minor or patch, not {part!r}"
        raise ValueError(msg)
    return ".".join(map(str, bumped[part]))


def _replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        msg = f"{path.name}: no version to replace"
        raise ValueError(msg)
    path.write_text(new, encoding="utf-8")


def write_version(version: str, tags: dict[str, str], root: Path = ROOT) -> None:
    """Write `version` into the docstring, pyproject.toml and uv.lock, and head CHANGELOG.md with it.

    An unreleased top CHANGELOG section (a number another branch took first) is renamed, keeping its
    text; otherwise a new empty section opens above the last release. uv.lock gets only its project
    line edited: `uv lock` from a newer uv would also raise the lock's `revision`, which the older uv
    bundled with Dependabot may not read.
    """
    _replace_once(root / PLUGIN, r"^version:\s*\S+\s*$", f"version: {version}")
    _replace_once(root / "pyproject.toml", r'^version = "[^"]*"', f'version = "{version}"')
    _replace_once(root / "uv.lock", rf'(^name = "{PROJECT}"\nversion = )"[^"]*"', rf'\g<1>"{version}"')
    top = next(iter(changelog_versions(root)), None)
    if top is not None and top not in tags:
        _replace_once(root / "CHANGELOG.md", rf"^## \[{re.escape(top)}\]", f"## [{version}]")
    else:
        _replace_once(root / "CHANGELOG.md", r"^(?=## \[)", f"## [{version}]\n\n")


def bump_problems(tags: dict[str, str], root: Path = ROOT) -> list[str]:
    """The release rule: a changed plugin needs a version above every release; an unchanged one keeps its tag."""
    version = plugin_version(root)
    if version in tags:
        released = tagged_plugin(tags[version], root)
        if released != (root / PLUGIN).read_text(encoding="utf-8"):
            return [f"{PLUGIN} changed since {tags[version]}, but its version is still {version}: bump it"]
        return []
    latest = max(tags, key=semver, default=None)
    if latest is not None and semver(version) <= semver(latest):
        return [f"{version} is not above the latest release {tags[latest]}"]
    return []


def main(argv: list[str]) -> int:
    """Run one command (see the module docstring); returns the exit status."""
    command = argv[1] if len(argv) > 1 else "check"
    if command == "bump":
        # Always counted from the latest release, so a rerun gives the same number, a changed mind
        # (patch -> minor) is one more run, and so is a number another branch released first.
        tags = release_tags()
        version = next_version(argv[2] if len(argv) > 2 else "", tags)  # noqa: PLR2004 - argv position
        write_version(version, tags)
        print(f"{version}: written; describe it under `## [{version}]` in CHANGELOG.md")  # noqa: T201 - CLI output
        return 0
    version = plugin_version()
    if command == "version":
        print(version)  # noqa: T201 - CLI output
    elif command == "pending":
        if version not in release_tags():
            print(version)  # noqa: T201 - CLI output
    elif command == "notes":
        print(changelog_notes(version))  # noqa: T201 - CLI output
    elif command == "check":
        problems = consistency_problems() + bump_problems(release_tags())
        for problem in problems:
            print(f"release check: {problem}", file=sys.stderr)  # noqa: T201 - CLI output
        return 1 if problems else 0
    else:
        print(__doc__, file=sys.stderr)  # noqa: T201 - CLI usage
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
