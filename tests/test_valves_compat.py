"""Stored valves of every released version must validate against the current `Valves`.

Since Open WebUI 0.12.0, saving new function code validates the valves an admin already stored
against the new code's `Valves` (`utils/valves.py:validate_valves`) and refuses the save with
HTTP 400 on anything but a "missing" error. A narrowed `Literal`, a changed field type or a tighter
constraint would therefore block every existing install from updating the plugin. Each release's
defaults, plus a sample of other values its fields accepted (both booleans, a few numbers and
strings, every `Literal` option), stand in for what an admin may have stored.

The release tags come from git through the `release_tags` fixture (`git fetch --tags` in a clone that
lacks them; CI checks out with `fetch-depth: 0` and fails, rather than skips, without them).
"""

from __future__ import annotations

import re
import sys
import types
from types import NoneType
from typing import TYPE_CHECKING, Any, Literal, get_args, get_origin

import pytest
from pydantic import ValidationError, create_model

if TYPE_CHECKING:
    from types import ModuleType

# Plausible stored values per plain type, tried on top of each field's default.
_SAMPLES: dict[type, tuple[Any, ...]] = {
    bool: (True, False),
    int: (0, 1, 30, 4096),
    float: (0.0, 0.0001, 1.5),
    str: ("", "custom"),
}


def _load_valves(release_module: ModuleType, tag: str) -> Any:
    """The `Filter.Valves` class of a released plugin, loaded the way OWUI does (exec into a fresh module)."""
    source = release_module.tagged_plugin(tag)
    name = "usage_display_" + re.sub(r"\W", "_", tag)
    module = types.ModuleType(name)
    sys.modules[name] = module  # pydantic resolves the module's annotations through sys.modules
    try:
        exec(compile(source, f"{tag}:usage_display.py", "exec"), module.__dict__)  # noqa: S102 - our own released source
    finally:
        sys.modules.pop(name, None)
    return module.Filter.Valves


def _annotation_samples(annotation: Any) -> list[Any]:
    """Sample values for an annotation: Literal options, plain-type samples, unions unwrapped."""
    origin = get_origin(annotation)
    if origin is Literal:
        return list(get_args(annotation))
    if origin is not None:  # Union / `X | None`: sample each member
        return [value for arg in get_args(annotation) for value in _annotation_samples(arg)]
    if annotation is NoneType:
        return []
    return list(_SAMPLES.get(annotation, ()))


def _stored_candidates(old_valves: Any) -> list[tuple[str, Any]]:
    """(field, value) pairs an admin of that release may have saved: the default plus every sample it accepted."""
    candidates: list[tuple[str, Any]] = []
    defaults = old_valves().model_dump()
    for field, info in old_valves.model_fields.items():
        candidates.append((field, defaults[field]))
        # A sample that release accepted could have been stored; one it refused could not.
        candidates.extend(
            (field, value)
            for value in _annotation_samples(info.annotation)
            if value != defaults[field] and not _rejection(old_valves, {field: value})
        )
    return candidates


def _rejection(valves_cls: Any, values: dict[str, Any]) -> list[str]:
    """OWUI's verdict: every error except "missing" refuses the save."""
    try:
        valves_cls(**{key: value for key, value in values.items() if value is not None})
    except ValidationError as error:
        return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors() if e["type"] != "missing"]
    return []


def test_stored_valves_of_every_release_validate_against_current(
    usage_display_module: ModuleType, release_module: ModuleType, release_tags: dict[str, str]
) -> None:
    current = usage_display_module.Filter.Valves
    failures: list[str] = []
    for tag in release_tags.values():
        old = _load_valves(release_module, tag)
        failures += [f"{tag} defaults -> {msg}" for msg in _rejection(current, old().model_dump())]
        for field, value in _stored_candidates(old):
            failures += [f"{tag} {field}={value!r} -> {msg}" for msg in _rejection(current, {field: value})]
    assert not failures, "OWUI 0.12+ would refuse to save this version over:\n" + "\n".join(failures)


def test_rejection_mirrors_owui_validate_valves(usage_display_module: ModuleType) -> None:
    """Guard the guard: a narrowed field is reported, unknown keys and missing values are not."""
    valves = usage_display_module.Filter.Valves
    assert _rejection(valves, {"cost_mode": "bogus"})  # e.g. an option removed in a later release
    assert _rejection(valves, {"removed_valve": 1, "cost_mode": None}) == []  # dropped key: ignored
    # A non-Literal field narrowed later: the stored non-default value is sampled and then refused.
    candidates = _stored_candidates(valves)
    assert ("show_output_tokens", False) in candidates
    assert ("context_size_override", 4096) in candidates
    narrowed: Any = create_model("Narrowed", __base__=valves, show_output_tokens=(Literal[True], True))
    assert _rejection(narrowed, {"show_output_tokens": False})
    # `Literal[...] | None` is unwrapped, and None itself is never offered (OWUI drops it before validating).
    assert _annotation_samples(Literal["a", "b"] | None) == ["a", "b"]


@pytest.mark.parametrize(
    "case", [("true", pytest.fail.Exception, "fetch-depth"), ("", pytest.skip.Exception, "git fetch --tags")]
)
def test_release_tags_fail_in_ci_and_skip_locally(
    release_module: ModuleType,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, type[BaseException], str],
) -> None:
    ci, outcome, message = case
    monkeypatch.setattr(release_module, "release_tags", dict)  # a checkout without tags
    monkeypatch.setenv("CI", ci)
    with pytest.raises(outcome, match=message):
        request.getfixturevalue("release_tags")
