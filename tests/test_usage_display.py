"""Tests for openwebui/usage_display.py — the Token Usage & Cost Display Filter.

Characterization tests: they pin the plugin's current, verified-correct behavior
across providers and its graceful degradation on malformed input. The plugin is
loaded via the `usage_display_module` conftest fixture (SourceFileLoader), same as
the bin/* scripts. No real network, no OWUI runtime: usage/body/output payloads are
built by the factories below, shaped to OWUI 0.10.2.
"""

from __future__ import annotations

import asyncio
import time
from types import ModuleType
from typing import Any

import pytest


def test_module_loads_and_exposes_filter(usage_display_module: ModuleType) -> None:
    mod = usage_display_module
    assert hasattr(mod, "Filter")
    f = mod.Filter()
    assert f.valves.icon_style == "emoji"
    assert f.valves.cost_mode == "auto"
    assert mod._DEFAULT_ORDER[0] == "input"
    assert len(mod._DEFAULT_ORDER) == 13


def run_async(coro: Any) -> Any:
    """Drive a coroutine to completion without pytest-asyncio."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_output(*texts: str) -> list[dict[str, Any]]:
    """A structured `output` array: one message item with output_text parts."""
    return [{"type": "message", "content": [{"type": "output_text", "text": t} for t in texts]}]


def make_message(
    text: str = "Hello world",
    *,
    usage: dict[str, Any] | None = None,
    output: list[dict[str, Any]] | None = None,
    role: str = "assistant",
    content: Any = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": role}
    if content is not None:
        msg["content"] = content
    elif output is None:
        msg["content"] = text
    if output is not None:
        msg["output"] = output
    if usage is not None:
        msg["usage"] = usage
    return msg


def make_usage(**over: Any) -> dict[str, Any]:
    """OWUI-normalized usage (0.10.2). Override any key; nested detail dicts merge shallowly."""
    base: dict[str, Any] = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    base.update(over)
    return base


def make_body(
    messages: list[dict[str, Any]] | None = None,
    *,
    model: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"messages": messages if messages is not None else []}
    if model:
        body["model"] = model
    if metadata is not None:
        body["metadata"] = metadata
    return body


def make_model_dict(model_id: str = "gpt-4o", base: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"id": model_id, "name": model_id}
    if base is not None:
        d["info"] = {"base_model_id": base}
    return d


def make_tokens(**over: Any) -> dict[str, Any]:
    """The bag shape returned by Filter._extract_tokens."""
    base: dict[str, Any] = {
        "input": 100,
        "output": 50,
        "total": 150,
        "reasoning": None,
        "cached": None,
        "cache_write": None,
        "audio": None,
        "is_api": True,
        "is_anthropic": False,
        "input_has_cache": False,
        "fresh_input": 100,
    }
    base.update(over)
    return base


def make_timing(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"seconds": None, "source": "provider", "tps": None}
    base.update(over)
    return base


def make_ctx(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"size": None, "used": None, "source": "none", "matched_key": None}
    base.update(over)
    return base


def make_cost(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "message": None,
        "message_est": False,
        "cumulative": None,
        "cumulative_est": False,
    }
    base.update(over)
    return base


class CapturingEmitter:
    """Async __event_emitter__ stub that records every emitted event."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def statuses(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == "status"]


class _FakeEncoding:
    def __init__(self, per_word: bool) -> None:
        self._per_word = per_word

    def encode(self, text: str) -> list[int]:
        toks = text.split() if self._per_word else list(text)
        return [0] * len(toks)


class _FakeTiktoken:
    def __init__(self, per_word: bool) -> None:
        self._per_word = per_word

    def encoding_for_model(self, model: str) -> _FakeEncoding:
        if not model:
            raise KeyError(model)
        return _FakeEncoding(self._per_word)

    def get_encoding(self, name: str) -> _FakeEncoding:
        return _FakeEncoding(self._per_word)


def install_fake_tiktoken(monkeypatch: pytest.MonkeyPatch, mod: ModuleType, *, per_word: bool = True) -> None:
    """Make the plugin's tiktoken path live: 1 token per whitespace-word (deterministic)."""
    monkeypatch.setattr(mod, "tiktoken", _FakeTiktoken(per_word), raising=False)
    monkeypatch.setattr(mod, "_TIKTOKEN_AVAILABLE", True, raising=False)


class _FakeAiohttpResponse:
    """Stand-in for an aiohttp response: async context manager + async `.json()`."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def __aenter__(self) -> _FakeAiohttpResponse:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def json(self, content_type: Any = None) -> Any:
        return self._payload


class _FakeAiohttpSession:
    """Stand-in for aiohttp.ClientSession: async context manager + `.get(url)`."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses

    async def __aenter__(self) -> _FakeAiohttpSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def get(self, url: str, **_kw: Any) -> _FakeAiohttpResponse:
        return _FakeAiohttpResponse(self._responses.get(url, {}))


class _FakeAiohttpModule:
    """Minimal stand-in for the `aiohttp` module used by the three network probes.

    `responses` maps request URL -> either a JSON-able payload (success) or an Exception
    instance (raised when that URL is fetched). `session_error`, if set, is raised by
    `ClientSession(...)` itself (simulating a connection-level failure before any request).
    """

    def __init__(self, responses: dict[str, Any] | None = None, *, session_error: Exception | None = None) -> None:
        self._responses = responses or {}
        self._session_error = session_error

    def ClientTimeout(self, **_kw: Any) -> Any:  # mirrors aiohttp's real API name
        return object()

    def ClientSession(self, **_kw: Any) -> _FakeAiohttpSession:  # mirrors aiohttp's real API name
        if self._session_error is not None:
            raise self._session_error
        return _FakeAiohttpSession(self._responses)


def install_fake_aiohttp(
    monkeypatch: pytest.MonkeyPatch,
    mod: ModuleType,
    responses: dict[str, Any] | None = None,
    *,
    session_error: Exception | None = None,
) -> None:
    """Make the plugin's aiohttp network probes live against canned responses (no real network)."""
    monkeypatch.setattr(mod, "aiohttp", _FakeAiohttpModule(responses, session_error=session_error), raising=False)
    monkeypatch.setattr(mod, "_AIOHTTP_AVAILABLE", True, raising=False)


def test_make_output_shape_matches_extractor(usage_display_module: ModuleType) -> None:
    out = make_output("foo", "bar")
    assert usage_display_module._extract_output_text(out) == "foobar"


def test_capturing_emitter_records() -> None:
    emitter = CapturingEmitter()
    run_async(emitter({"type": "status", "data": {"description": "x", "done": True}}))
    assert emitter.statuses()[0]["data"]["description"] == "x"


# --------------------------------------------------------------------------- #
# Group A — pure module-level helpers
# --------------------------------------------------------------------------- #


def test_num_rejects_bool_and_nonnumbers(usage_display_module: ModuleType) -> None:
    _num = usage_display_module._num
    assert _num(5) == 5
    assert _num(1.5) == 1.5
    assert _num(0) == 0  # 0 is valid, not falsy-dropped
    assert _num(True) is None  # bool is not a number here
    assert _num("5") is None
    assert _num(None) is None


def test_first_num_returns_first_present_including_zero(usage_display_module: ModuleType) -> None:
    _first_num = usage_display_module._first_num
    assert _first_num({"a": 0, "b": 9}, "a", "b") == 0
    assert _first_num({"b": 9}, "a", "b") == 9
    assert _first_num({}, "a") is None
    assert _first_num("not a dict", "a") is None


def test_detail_num_reads_nested_group(usage_display_module: ModuleType) -> None:
    _detail_num = usage_display_module._detail_num
    usage = {"completion_tokens_details": {"reasoning_tokens": 7}}
    assert _detail_num(usage, "completion_tokens_details", "reasoning_tokens") == 7
    assert _detail_num(usage, "missing_group", "x") is None
    assert _detail_num({"g": "notdict"}, "g", "x") is None


def test_detail_num_non_dict_usage_returns_none(usage_display_module: ModuleType) -> None:
    assert usage_display_module._detail_num("notdict", "g", "k") is None


def test_get_last_assistant_message_obj(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._get_last_assistant_message_obj
    msgs = [make_message("q", role="user"), make_message("a1"), make_message("a2")]
    assert fn(msgs) == msgs[-1]
    assert fn([make_message("q", role="user")]) == {}
    assert fn([]) == {}


def test_extract_output_text_only_message_output_text(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._extract_output_text
    out = [
        {"type": "reasoning", "content": [{"type": "output_text", "text": "SECRET"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "vis"}, {"type": "other", "text": "no"}]},
    ]
    assert fn(out) == "vis"  # reasoning + non-output_text excluded
    assert fn("not a list") == ""
    assert fn([]) == ""


def test_extract_output_text_skips_empty_text_parts(usage_display_module: ModuleType) -> None:
    """A falsy `text` (e.g. "") is skipped, not appended as an empty string."""
    fn = usage_display_module._extract_output_text
    out = [
        {
            "type": "message",
            "content": [{"type": "output_text", "text": ""}, {"type": "output_text", "text": "kept"}],
        }
    ]
    assert fn(out) == "kept"


def test_extract_output_text_coerces_non_str_text(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._extract_output_text
    out = [{"type": "message", "content": [{"type": "output_text", "text": 123}]}]
    assert fn(out) == "123"


def test_extract_output_text_skips_non_dict_items(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._extract_output_text
    out = ["not-a-dict", {"type": "message", "content": [{"type": "output_text", "text": "kept"}]}]
    assert fn(out) == "kept"


def test_message_text_prefers_content_then_output(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._message_text
    assert fn(make_message(content="plain")) == "plain"
    assert fn(make_message(content=[{"type": "text", "text": "a"}, "b"])) == "a b"
    streaming = make_message(output=make_output("streamed"))  # no content
    assert fn(streaming) == "streamed"
    assert fn({"role": "assistant"}) == ""


def test_message_text_list_content_skips_non_text_non_str_items(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._message_text
    # 42 matches neither the dict-with-type-text branch nor the str branch -> skipped.
    assert fn(make_message(content=[42, "kept"])) == "kept"


def test_message_text_list_content_empty_chunks_falls_back_to_output(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._message_text
    # No item in content produces a chunk -> falls through to the `output` array.
    msg = make_message(content=[42], output=make_output("fallback"))
    assert fn(msg) == "fallback"


def test_count_tokens_tiktoken_available(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_tiktoken(monkeypatch, usage_display_module, per_word=True)
    fn = usage_display_module._count_tokens_tiktoken
    assert fn("one two three", "gpt-4o") == 3
    assert fn("", "gpt-4o") == 0  # empty text, tiktoken available -> 0
    assert fn("falls back", "") == 2  # empty model -> get_encoding branch


def test_count_tokens_tiktoken_unavailable(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(usage_display_module, "_TIKTOKEN_AVAILABLE", False, raising=False)
    assert usage_display_module._count_tokens_tiktoken("x", "gpt-4o") is None


def test_format_duration(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._format_duration
    assert fn(0.25) == "250ms"
    assert fn(0.999) == "999ms"
    assert fn(1.0) == "1.0s"
    assert fn(59.4) == "59.4s"
    assert fn(65) == "1m 5s"
    assert fn(3661) == "61m 1s"


def test_format_k(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._format_k
    assert fn(999) == "999"
    assert fn(3500) == "3.5k"
    assert fn(1_000_000) == "1.0M"
    assert fn(2_500_000) == "2.5M"


def test_format_cost_precision_scales(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._format_cost
    assert fn(0) == "$0.00"
    assert fn(-1) == "$0.00"
    assert fn(0.00005) == "<$0.0001"
    assert fn(0.0123) == "$0.0123"
    assert fn(1) == "$1.00"
    assert fn(1234.5) == "$1,234.50"


def test_longest_key_match(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._longest_key_match
    table = {"gpt-4": 1, "gpt-4o": 2, "": 99}
    assert fn(table, "openai/gpt-4o-mini") == "gpt-4o"  # longest wins, case-insensitive
    assert fn(table, "GPT-4-TURBO") == "gpt-4"
    assert fn(table, "claude") is None
    assert fn(table, "") is None
    assert fn({}, "gpt-4o") is None


def test_modelsdev_match_exact_then_bare_then_substring(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._modelsdev_match
    table = {"gpt-4o": 1, "anthropic/claude-opus-4-8": 2, "claude-opus-4-8": 3}
    assert fn(table, "gpt-4o") == "gpt-4o"  # exact
    assert fn(table, "openai/gpt-4o") == "gpt-4o"  # bare last segment
    assert fn(table, "claude-opus-4-8-20260101") == "claude-opus-4-8"  # substring
    assert fn(table, "unknown") is None


def test_modelsdev_match_empty_or_non_dict_table_returns_none(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._modelsdev_match
    assert fn({}, "gpt-4o") is None
    assert fn(None, "gpt-4o") is None


def test_resolve_model_id_prefers_base_model_id(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._resolve_model_id
    # Workspace/custom model: base_model_id (real LLM) wins over the top-level agent id.
    assert fn({"id": "research", "info": {"base_model_id": "stepfun/step-3.7-flash:free"}}) == (
        "stepfun/step-3.7-flash:free"
    )
    # No base -> top-level id. Empty base string is falsy -> also falls back to id.
    assert fn({"id": "gpt-4o"}) == "gpt-4o"
    assert fn({"id": "gpt-4o", "info": {"base_model_id": ""}}) == "gpt-4o"
    assert fn({"id": "gpt-4o", "info": None}) == "gpt-4o"
    # No usable model dict -> body["model"] fallback, then "".
    assert fn(None, {"model": "gpt-4o"}) == "gpt-4o"
    assert fn({}, {"model": "gpt-4o"}) == "gpt-4o"
    assert fn(None) == ""
    assert fn({}) == ""


def test_normalize_order_dedups_and_flags_unknown(usage_display_module: ModuleType) -> None:
    valid, unknown = usage_display_module._normalize_order(" input, COST , input , bogus, ")
    assert valid == ["input", "cost"]  # deduped, lowercased, order-preserved
    assert unknown == ["bogus"]


def test_normalize_order_dedups_repeated_unknown_keys(usage_display_module: ModuleType) -> None:
    _valid, unknown = usage_display_module._normalize_order("bogus, bogus, also_bogus")
    assert unknown == ["bogus", "also_bogus"]  # repeated unknown key not appended twice


def test_resolve_display_order_is_full_permutation(usage_display_module: ModuleType) -> None:
    mod = usage_display_module
    assert mod._resolve_display_order("") == mod._DEFAULT_ORDER  # empty -> default
    resolved = mod._resolve_display_order("cost, model")
    assert resolved[:2] == ["cost", "model"]
    assert sorted(resolved) == sorted(mod._DEFAULT_ORDER)  # still all 13, no dupes
    assert len(resolved) == len(set(resolved)) == 13


def test_display_order_debug_provenance(usage_display_module: ModuleType) -> None:
    d = usage_display_module._display_order_debug(" total , nope ")
    assert d["source"] == "admin"
    assert d["parsed"] == ["total"]
    assert d["ignored_unknown"] == ["nope"]
    assert d["resolved"][0] == "total"
    assert usage_display_module._display_order_debug("")["source"] == "default"


@pytest.fixture
def valves(usage_display_module: ModuleType) -> Any:
    return usage_display_module.Filter().valves


def test_icon_styles(usage_display_module: ModuleType, valves: Any) -> None:
    _icon = usage_display_module._icon
    valves.icon_style = "emoji"
    assert _icon(valves, "input") == "⬆︎"
    valves.icon_style = "simple"
    assert _icon(valves, "input") == "↑"
    assert _icon(valves, "cost") == ""  # simple cost has no icon ($ self-labels)
    valves.icon_style = "off"
    assert _icon(valves, "input") == ""
    assert _icon(valves, "unknown_key") == ""


def test_with_icon(usage_display_module: ModuleType) -> None:
    fn = usage_display_module._with_icon
    assert fn("⬆︎", "12") == "⬆︎ 12"
    assert fn("", "12") == "12"


def test_context_icon_severity(usage_display_module: ModuleType, valves: Any) -> None:
    _context_icon = usage_display_module._context_icon
    valves.icon_style = "emoji"  # warn=30, critical=70 defaults
    assert _context_icon(valves, 10) == "📐"
    assert _context_icon(valves, 50) == "🟠"
    assert _context_icon(valves, 90) == "🔴"
    valves.icon_style = "off"
    assert _context_icon(valves, 90) == ""


def test_fmt_count_compact_toggle(usage_display_module: ModuleType, valves: Any) -> None:
    _fmt_count = usage_display_module._fmt_count
    valves.compact_numbers = False
    assert _fmt_count(valves, 12345) == "12,345"
    valves.compact_numbers = True
    assert _fmt_count(valves, 12345) == "12.3k"


# --------------------------------------------------------------------------- #
# Group A cont. — all 13 stats-line renderers, each individually
# --------------------------------------------------------------------------- #


def test_render_input(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_input
    valves.icon_style = "emoji"
    assert r(valves, make_tokens(input=100), make_timing(), make_ctx(), make_cost(), None) == "⬆︎ 100"
    valves.show_input_tokens = False
    assert r(valves, make_tokens(input=100), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_input_tokens = True
    assert r(valves, make_tokens(input=None), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_output(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_output
    assert r(valves, make_tokens(output=50), make_timing(), make_ctx(), make_cost(), None) == "⬇︎ 50"
    assert r(valves, make_tokens(output=None), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_output_tokens = False
    assert r(valves, make_tokens(output=50), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_total(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_total
    assert r(valves, make_tokens(total=150), make_timing(), make_ctx(), make_cost(), None) == "Σ 150"
    assert r(valves, make_tokens(total=None), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_total_tokens = False
    assert r(valves, make_tokens(total=150), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_reasoning_hidden_when_zero_or_none(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_reasoning
    assert r(valves, make_tokens(reasoning=32), make_timing(), make_ctx(), make_cost(), None) == "🧠 32"
    # reasoning uses truthiness: 0 and None both omit
    assert r(valves, make_tokens(reasoning=0), make_timing(), make_ctx(), make_cost(), None) is None
    assert r(valves, make_tokens(reasoning=None), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_reasoning_tokens = False
    assert r(valves, make_tokens(reasoning=32), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_cached(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_cached
    assert r(valves, make_tokens(cached=64), make_timing(), make_ctx(), make_cost(), None) == "💾 64"
    assert r(valves, make_tokens(cached=0), make_timing(), make_ctx(), make_cost(), None) is None
    assert r(valves, make_tokens(cached=None), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_cached_tokens = False
    assert r(valves, make_tokens(cached=64), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_audio_default_hidden_flag(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_audio
    assert valves.show_audio_tokens is False  # default off
    assert r(valves, make_tokens(audio=10), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_audio_tokens = True
    assert r(valves, make_tokens(audio=10), make_timing(), make_ctx(), make_cost(), None) == "🔊 10"
    assert r(valves, make_tokens(audio=0), make_timing(), make_ctx(), make_cost(), None) is None
    assert r(valves, make_tokens(audio=None), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_context(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_context
    valves.icon_style = "emoji"
    out = r(valves, make_tokens(), make_timing(), make_ctx(size=200_000, used=50_000), make_cost(), None)
    assert out == "📐 50.0k/200.0k (25%)"
    # used=0 is a valid value (not "no data")
    assert (
        r(valves, make_tokens(), make_timing(), make_ctx(size=200_000, used=0), make_cost(), None) == "📐 0/200.0k (0%)"
    )
    # no size -> omitted
    assert r(valves, make_tokens(), make_timing(), make_ctx(size=None, used=5), make_cost(), None) is None
    valves.show_context_window = False
    assert r(valves, make_tokens(), make_timing(), make_ctx(size=200_000, used=5), make_cost(), None) is None


def test_render_time_wall_prefix(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_time
    assert r(valves, make_tokens(), make_timing(seconds=2.5, source="usage"), make_ctx(), make_cost(), None) == "⏱ 2.5s"
    assert r(valves, make_tokens(), make_timing(seconds=2.5, source="wall"), make_ctx(), make_cost(), None) == "⏱ ~2.5s"
    assert r(valves, make_tokens(), make_timing(seconds=None), make_ctx(), make_cost(), None) is None
    valves.show_generation_time = False
    assert r(valves, make_tokens(), make_timing(seconds=2.5, source="usage"), make_ctx(), make_cost(), None) is None


def test_render_tps(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_tps
    assert r(valves, make_tokens(), make_timing(tps=42.37), make_ctx(), make_cost(), None) == "⚡ 42.4 t/s"
    assert r(valves, make_tokens(), make_timing(tps=0), make_ctx(), make_cost(), None) is None
    valves.show_tokens_per_second = False
    assert r(valves, make_tokens(), make_timing(tps=42.37), make_ctx(), make_cost(), None) is None


def test_render_cost_estimate_marker_and_threshold(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_cost
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=1.5), None) == "💰 $1.50"
    assert (
        r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=1.5, message_est=True), None)
        == "💰 ≈$1.50"
    )
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=None), None) is None
    valves.cost_min_display = 0.01
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=0.005), None) is None


def test_render_cost_total_dedupes_when_equal_to_message(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_cost_total
    # cumulative == message -> omitted (no point repeating)
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=1.5, cumulative=1.5), None) is None
    # cumulative > message -> shown
    assert (
        r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=1.5, cumulative=4.0), None) == "💰Σ $4.00"
    )
    assert (
        r(
            valves,
            make_tokens(),
            make_timing(),
            make_ctx(),
            make_cost(message=1.5, cumulative=4.0, cumulative_est=True),
            None,
        )
        == "💰Σ ≈$4.00"
    )
    valves.show_cumulative_cost = False
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=1.5, cumulative=4.0), None) is None


def test_render_model_uses_base_model_id(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_model
    model = make_model_dict("my-workspace-model", base="gpt-4o")
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), model) == "🤖 gpt-4o"
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), make_model_dict()) is None  # no info
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_model_name = False
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), model) is None


def test_render_source_api_vs_estimate(usage_display_module: ModuleType, valves: Any) -> None:
    r = usage_display_module._render_source
    valves.show_data_source = True
    assert r(valves, make_tokens(is_api=True), make_timing(), make_ctx(), make_cost(), None) == "[API]"
    assert r(valves, make_tokens(is_api=False), make_timing(), make_ctx(), make_cost(), None) == "[est.]"
    valves.show_data_source = False
    assert r(valves, make_tokens(is_api=True), make_timing(), make_ctx(), make_cost(), None) is None


def test_stats_renderers_registry_matches_default_order(usage_display_module: ModuleType) -> None:
    mod = usage_display_module
    assert set(mod._STATS_RENDERERS) == set(mod._DEFAULT_ORDER)
    assert set(mod._STATS_RENDERERS) == set(mod._STATS_KEYS)


# --------------------------------------------------------------------------- #
# Group B — Filter pure methods (cache/tokens/timing/cost/parsers/stats/sanitize)
# --------------------------------------------------------------------------- #


def test_cache_and_fresh_openai_subset(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"prompt_tokens_details": {"cached_tokens": 40}}
    out = f._cache_and_fresh(usage, reported_input=100)
    assert out["cached"] == 40
    assert out["is_anthropic"] is False
    assert out["input_has_cache"] is True
    assert out["fresh_input"] == 60  # subset: cache subtracted from input


def test_cache_and_fresh_anthropic_native_on_top(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"cache_read_input_tokens": 30, "cache_creation_input_tokens": 10}
    out = f._cache_and_fresh(usage, reported_input=100)
    assert out["cached"] == 30
    assert out["cache_write"] == 10
    assert out["is_anthropic"] is True
    assert out["input_has_cache"] is False
    assert out["fresh_input"] == 100  # native: input excludes cache, not subtracted


def test_extract_tokens_openai_chat(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = make_usage(
        input_tokens=100,
        output_tokens=50,
        completion_tokens_details={"reasoning_tokens": 8, "audio_tokens": 2},
        prompt_tokens_details={"cached_tokens": 20, "audio_tokens": 1},
    )
    t = f._extract_tokens(usage, make_message(), [], make_body(), None)
    assert t["input"] == 100
    assert t["output"] == 50
    assert t["is_api"] is True
    assert t["reasoning"] == 8
    assert t["cached"] == 20
    assert t["audio"] == 3  # audio_in + audio_out
    assert t["total"] == f._compute_total(t)


def test_extract_tokens_responses_api_naming(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = make_usage(output_tokens_details={"reasoning_tokens": 5}, input_tokens_details={"cached_tokens": 10})
    t = f._extract_tokens(usage, make_message(), [], make_body(), None)
    assert t["reasoning"] == 5
    assert t["cached"] == 10


def test_extract_tokens_ollama_backup_keys(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"prompt_eval_count": 77, "eval_count": 22}  # Ollama naming, no normalized triple
    t = f._extract_tokens(usage, make_message(), [], make_body(), None)
    assert t["input"] == 77
    assert t["output"] == 22
    assert t["is_api"] is True


def test_extract_tokens_estimate_fallback_when_no_usage(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_tiktoken(monkeypatch, usage_display_module)
    f = usage_display_module.Filter()
    assistant = make_message("one two three")  # 3 words -> 3 output tokens
    user = make_message("alpha beta", role="user")  # 2 words -> input
    t = f._extract_tokens(None, assistant, [user, assistant], make_body(model="gpt-4o"), None)
    assert t["is_api"] is False
    assert t["output"] == 3
    assert t["input"] == 2


def test_extract_tokens_estimate_uses_model_dict_id(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `model` dict's id (not just body['model']) feeds the tiktoken model-name lookup."""
    install_fake_tiktoken(monkeypatch, usage_display_module)
    f = usage_display_module.Filter()
    assistant = make_message("one two three")
    user = make_message("alpha beta", role="user")
    model = make_model_dict("gpt-4o-est-model-dict")
    t = f._extract_tokens(None, assistant, [user, assistant], make_body(), model)
    assert t["output"] == 3
    assert t["input"] == 2


def test_estimate_tokens_skips_output_when_response_text_empty(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_tiktoken(monkeypatch, usage_display_module)
    f = usage_display_module.Filter()
    assistant = make_message(content="")  # no content, no output -> empty response text
    user = make_message("alpha beta", role="user")
    t = f._extract_tokens(None, assistant, [user, assistant], make_body(model="gpt-4o"), None)
    assert t["output"] is None  # empty response text -> tiktoken estimate never attempted
    assert t["input"] == 2


def test_estimate_tokens_est_out_none_when_tiktoken_unavailable_mid_call(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling `_estimate_tokens` directly with tiktoken unavailable: est_out is None, not assigned."""
    f = usage_display_module.Filter()
    monkeypatch.setattr(usage_display_module, "_TIKTOKEN_AVAILABLE", False, raising=False)
    result: dict[str, Any] = {"output": None, "input": None}
    assistant = make_message("one two three")
    user = make_message("alpha beta", role="user")
    f._estimate_tokens(result, assistant, [user, assistant], make_body(model="gpt-4o"), None)
    assert result["output"] is None
    assert result["input"] is None


def test_estimate_tokens_last_user_message_only(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """count_all_messages_for_input=False -> only the last user message is estimated."""
    install_fake_tiktoken(monkeypatch, usage_display_module)
    f = usage_display_module.Filter()
    f.valves.count_all_messages_for_input = False
    assistant = make_message("resp one two")
    user1 = make_message("alpha", role="user")
    user2 = make_message("beta gamma delta", role="user")  # last user message -> wins
    t = f._extract_tokens(None, assistant, [user1, assistant, user2], make_body(model="gpt-4o"), None)
    assert t["input"] == 3  # "beta gamma delta" -> 3 words


def test_estimate_tokens_last_user_message_none_when_absent(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """count_all_messages_for_input=False and no user message in the list -> input stays None."""
    install_fake_tiktoken(monkeypatch, usage_display_module)
    f = usage_display_module.Filter()
    f.valves.count_all_messages_for_input = False
    assistant = make_message("resp one two")
    t = f._extract_tokens(None, assistant, [assistant], make_body(model="gpt-4o"), None)
    assert t["input"] is None
    assert t["output"] == 3


def test_compute_total(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._compute_total(make_tokens(input=None, output=None, fresh_input=None)) is None
    # fresh_input + cached + cache_write + output
    t = make_tokens(input=100, output=50, fresh_input=60, cached=30, cache_write=10)
    assert f._compute_total(t) == 60 + 30 + 10 + 50
    # fresh_input None -> falls back to input
    t2 = make_tokens(input=100, output=50, fresh_input=None, cached=None, cache_write=None)
    assert f._compute_total(t2) == 150


# --- timing ------------------------------------------------------------------ #
# NOTE: the brief drafted these as `source == "usage"` for the provider-native case; the real
# code (usage_display.py:1037/1039) labels that branch "provider", not "usage" — corrected here
# after reading the method (see task-5-report.md).


def test_resolve_timing_usage_native_duration(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"eval_duration": 2_000_000_000}  # Ollama nanoseconds -> 2.0s
    timing = f._resolve_timing(usage, wall_seconds=None, output_tokens=100)
    assert timing["source"] == "provider"
    assert timing["seconds"] == pytest.approx(2.0)
    assert timing["tps"] == pytest.approx(50.0)  # 100 output tokens / 2.0s


def test_resolve_timing_ollama_reported_tps(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"eval_duration": 2_000_000_000, "response_token/s": 55.5}  # Ollama-native tps key
    timing = f._resolve_timing(usage, wall_seconds=None, output_tokens=100)
    assert timing["source"] == "provider"
    assert timing["tps"] == pytest.approx(55.5)  # Ollama-reported tps wins over the computed one


def test_resolve_timing_llama_cpp_reported_tps_wins_over_computed(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"predicted_ms": 500, "predicted_per_second": 999.0}
    timing = f._resolve_timing(usage, wall_seconds=None, output_tokens=50)
    assert timing["source"] == "provider"
    assert timing["seconds"] == pytest.approx(0.5)
    assert timing["tps"] == pytest.approx(999.0)  # provider-reported tps wins over output/seconds


def test_resolve_timing_wall_clock_fallback(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    timing = f._resolve_timing(None, wall_seconds=3.0, output_tokens=30)
    assert timing["source"] == "wall"
    assert timing["seconds"] == pytest.approx(3.0)
    assert timing["tps"] == pytest.approx(10.0)  # approximate: wall-clock includes pre-processing


def test_resolve_timing_neither_source_available(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    timing = f._resolve_timing(None, wall_seconds=None, output_tokens=30)
    assert timing == {"seconds": None, "tps": None, "source": None}


def test_resolve_timing_zero_wall_seconds_no_divide_by_zero(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    timing = f._resolve_timing(None, wall_seconds=0.0, output_tokens=100)
    assert timing["source"] == "wall"
    assert timing["seconds"] == 0.0
    assert timing["tps"] is None  # guarded: wall_seconds=0.0 is falsy, division is skipped


# --- cost ---------------------------------------------------------------------- #


def test_native_cost_recognized_keys(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    val, key = f._native_cost({"cost": 0.0123})
    assert val == pytest.approx(0.0123)
    assert key == "cost"
    val, key = f._native_cost({"total_cost": 0.05})
    assert val == pytest.approx(0.05)
    assert key == "total_cost"
    val, key = f._native_cost({"input_cost": 0.01, "output_cost": 0.02})
    assert val == pytest.approx(0.03)
    assert key == "input_cost+output_cost"
    val, key = f._native_cost({"output_cost": 0.02})
    assert val == pytest.approx(0.02)
    assert key == "input_cost+output_cost"
    val, key = f._native_cost({"cost": 1.0, "total_cost": 2.0})  # "cost" takes priority
    assert val == pytest.approx(1.0)
    assert key == "cost"


def test_native_cost_unrecognized_or_missing(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._native_cost({"foo": 1}) == (None, None)
    assert f._native_cost({}) == (None, None)
    assert f._native_cost(None) == (None, None)


def test_native_cost_details_numeric_only(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"cost": 0.02, "cost_details": {"upstream_inference_cost": 0.018, "cache_discount": 0.001, "note": "x"}}
    # OpenRouter cost_details; the non-numeric "note" is dropped so the payload stays serializable.
    assert f._native_cost_details(usage) == {"upstream_inference_cost": 0.018, "cache_discount": 0.001}


def test_native_cost_details_absent_or_non_dict(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._native_cost_details({"cost": 0.02}) is None  # no cost_details key at all
    assert f._native_cost_details({"cost_details": "nope"}) is None  # present but not a dict
    assert f._native_cost_details({"cost_details": {"note": "x"}}) is None  # dict with no numeric values -> None
    assert f._native_cost_details(None) is None


def test_url_host_extracts_host_or_empty(usage_display_module: ModuleType) -> None:
    m = usage_display_module
    assert m._url_host("https://www.example.com/page?q=1") == "www.example.com"
    assert m._url_host("http://NYTimes.com/x") == "nytimes.com"  # lowercased
    assert m._url_host("ftp://example.com") == ""  # http(s) only
    assert m._url_host("not a url") == ""
    assert m._url_host("") == ""


def test_web_search_debug_detects_url_citations(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    # OWUI url_citation source shape: source.url + metadata[].source both carry the http URL.
    msg = {
        "role": "assistant",
        "sources": [
            {
                "source": {"name": "NYT", "url": "https://nytimes.com/a"},
                "metadata": [{"source": "https://nytimes.com/a"}],
            },
            {
                "source": {"name": "Exa", "url": "https://example.com/b"},
                "metadata": [{"source": "https://example.com/b"}],
            },
        ],
    }
    ws = f._web_search_debug(msg)
    assert ws["detected"] is True
    assert ws["citation_count"] == 2
    assert ws["domains"] == ["example.com", "nytimes.com"]  # sorted, deduped
    assert ws["note"] is not None


def test_web_search_debug_ignores_non_web_sources(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    # RAG/file source carries no http URL -> must not be counted as web search.
    msg = {
        "role": "assistant",
        "sources": [{"source": {"name": "handbook.pdf"}, "metadata": [{"source": "file-abc123"}]}],
    }
    ws = f._web_search_debug(msg)
    assert ws == {"detected": False, "citation_count": 0, "domains": [], "note": None}


def test_web_search_debug_no_sources_key(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._web_search_debug({"role": "assistant", "content": "hi"}) == {
        "detected": False,
        "citation_count": 0,
        "domains": [],
        "note": None,
    }


def test_usage_token_bag_anthropic_native(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 10,
    }
    bag = f._usage_token_bag(usage)
    assert bag["input"] == 100
    assert bag["output"] == 50
    assert bag["cached"] == 30
    assert bag["cache_write"] == 10
    assert bag["is_anthropic"] is True
    assert bag["input_has_cache"] is False
    assert bag["fresh_input"] == 100  # native: on top, not subtracted


def test_usage_token_bag_openai_subset(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"input_tokens": 100, "output_tokens": 50, "prompt_tokens_details": {"cached_tokens": 40}}
    bag = f._usage_token_bag(usage)
    assert bag["cached"] == 40
    assert bag["is_anthropic"] is False
    assert bag["input_has_cache"] is True
    assert bag["fresh_input"] == 60  # subset: cache subtracted


def test_usage_token_bag_non_dict_usage_returns_empty_bag(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    bag = f._usage_token_bag(None)
    assert bag == {
        "input": None,
        "output": None,
        "cached": None,
        "cache_write": None,
        "is_anthropic": False,
        "input_has_cache": False,
        "fresh_input": None,
    }


def test_cost_components_full_breakdown(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    bag = {
        "fresh_input": 1_000_000,
        "cached": 500_000,
        "cache_write": 200_000,
        "output": 1_000_000,
        "input": 1_700_000,
        "is_anthropic": True,
        "input_has_cache": False,
    }
    price = {"input": 2.0, "output": 8.0, "cache_read": 0.5, "cache_write": 1.0}
    comp = f._cost_components(bag, price)
    assert comp is not None
    assert comp["tokens"] == {
        "input": 1_700_000,
        "billable_in": 1_000_000,
        "cached": 500_000,
        "cache_write": 200_000,
        "output": 1_000_000,
    }
    assert comp["usd_per_component"]["input"] == pytest.approx(2.0)
    assert comp["usd_per_component"]["cached"] == pytest.approx(0.25)
    assert comp["usd_per_component"]["cache_write"] == pytest.approx(0.2)
    assert comp["usd_per_component"]["output"] == pytest.approx(8.0)
    assert comp["total_usd"] == pytest.approx(10.45)
    assert comp["is_anthropic"] is True
    assert comp["input_has_cache"] is False


def test_cost_components_cache_defaults_to_input_rate(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    bag = {"fresh_input": 0, "cached": 100, "cache_write": 0, "output": 0}
    price = {"input": 3.0, "output": 5.0}  # no cache_read/cache_write -> falls back to the input rate
    comp = f._cost_components(bag, price)
    assert comp is not None
    assert comp["usd_per_component"]["cached"] == pytest.approx(100 * 3.0 / 1_000_000.0)


def test_cost_components_none_price_or_no_rates(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    bag = {"fresh_input": 100, "output": 50}
    assert f._cost_components(bag, None) is None
    assert f._cost_components(bag, {}) is None
    assert f._cost_components(bag, {"cache_read": 1.0}) is None  # no input/output rate at all


def test_cost_components_zero_token_bag_returns_none(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    bag = {"fresh_input": 0, "cached": 0, "cache_write": 0, "output": 0}
    price = {"input": 2.0, "output": 8.0}
    assert f._cost_components(bag, price) is None


def test_cost_components_fresh_input_falls_back_to_input_key(usage_display_module: ModuleType) -> None:
    """When a bag has no `fresh_input` key at all (e.g. a minimal hand-built bag), fall back to `input`."""
    f = usage_display_module.Filter()
    bag = {"input": 100, "output": 50}  # no fresh_input key
    price = {"input": 2.0, "output": 8.0}
    comp = f._cost_components(bag, price)
    assert comp is not None
    assert comp["tokens"]["billable_in"] == 100


def test_estimate_cost_from_price(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    bag = f._usage_token_bag(make_usage(input_tokens=1_000_000, output_tokens=1_000_000))
    price = {"input": 2.0, "output": 8.0}  # USD per 1M
    est = f._estimate_cost(bag, price)
    assert est == pytest.approx(10.0)  # 1M in @ $2 + 1M out @ $8
    assert f._estimate_cost(bag, None) is None


# --- price/context parsers (no network) --------------------------------------- #


def test_parse_prices_nested_models_dev_shape(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {"openai": {"models": {"gpt-4o": {"id": "openai/gpt-4o", "cost": {"input": 2.5, "output": 10.0}}}}}
    out = f._parse_prices(data)
    assert out["openai/gpt-4o"] == {"input": 2.5, "output": 10.0}
    assert out["gpt-4o"] == {"input": 2.5, "output": 10.0}  # bare last segment also indexed


def test_parse_prices_flat_shape(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {"claude-3-opus": {"id": "anthropic/claude-3-opus", "cost": {"input": 15.0, "output": 75.0}}}
    out = f._parse_prices(data)
    assert out["anthropic/claude-3-opus"]["output"] == 75.0
    assert out["claude-3-opus"]["input"] == 15.0


def test_parse_prices_malformed_input_yields_empty_map(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._parse_prices(None) == {}
    assert f._parse_prices([]) == {}
    assert f._parse_prices({"foo": "not-a-dict"}) == {}
    assert f._parse_prices({"m": {"cost": {"cache_read": 1.0}}}) == {}  # no input/output -> skipped
    assert f._parse_prices({"prov": {"models": {"m": {"cost": "oops"}}}}) == {}  # cost not a dict


def test_parse_prices_nested_shape_skips_non_dict_entry(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {
        "openai": {
            "models": {
                "bad": "not-a-dict",
                "gpt-4o": {"id": "openai/gpt-4o", "cost": {"input": 1.0, "output": 2.0}},
            }
        }
    }
    out = f._parse_prices(data)
    assert "bad" not in out
    assert out["gpt-4o"] == {"input": 1.0, "output": 2.0}


def test_parse_llama_swap_extracts_ctx_size_from_running(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {"running": [{"cmd": "llama-server --model foo.gguf --ctx-size 8192 --port 8080"}]}
    assert f._parse_llama_swap(data) == 8192


def test_parse_llama_swap_skips_non_dict_rows(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {"running": [123, {"cmd": "--ctx-size 999"}]}
    assert f._parse_llama_swap(data) == 999


def test_parse_llama_swap_cmd_as_list(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {"running": [{"cmd": ["llama-server", "--ctx-size", "4096"]}]}
    assert f._parse_llama_swap(data) == 4096


def test_parse_llama_swap_malformed_input_returns_none(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._parse_llama_swap(None) is None
    assert f._parse_llama_swap({"running": []}) is None
    assert f._parse_llama_swap({"running": "not-a-list"}) is None
    assert f._parse_llama_swap({"running": [{"cmd": "llama-server --no-ctx-flag"}]}) is None


# --- stats assembly, provider guess, sanitize, valves snapshot ----------------- #


def test_build_stats_respects_display_order_and_gating(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.display_order = "output, input"
    f.valves.show_total_tokens = False
    f.valves.icon_style = "off"
    parts = f._build_stats(
        f.valves, make_tokens(input=100, output=50, total=150), make_timing(), make_ctx(), make_cost(), None
    )
    assert parts[0] == "50"  # output before input
    assert parts[1] == "100"
    assert "150" not in parts  # total gated off


def test_provider_guess_local_and_arena_authoritative(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._provider_guess({"owned_by": "ollama"}, "llama3") == "ollama/local"
    assert f._provider_guess({"connection_type": "local"}, "gpt-4o") == "ollama/local"  # local wins over model-id
    assert f._provider_guess({"owned_by": "arena"}, "gpt-4o") == "arena"
    assert f._provider_guess({"provider": "custom-corp"}, "whatever") == "custom-corp"  # admin override wins


def test_provider_guess_family_from_model_id(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._provider_guess(None, "claude-3-opus") == "anthropic"
    assert f._provider_guess(None, "gemini-1.5-pro") == "google"
    assert f._provider_guess(None, "llama-3.1-70b") == "meta"
    assert f._provider_guess(None, "gpt-4o-mini") == "openai"
    assert f._provider_guess({}, "totally-unknown-model") == "unknown"
    assert f._provider_guess(None, "") == "unknown"


def test_sanitize_model_whitelist_only_no_secrets(usage_display_module: ModuleType) -> None:
    """Security contract: only whitelisted identity/provider fields leave this method."""
    f = usage_display_module.Filter()
    model = {
        "id": "gpt-4o",
        "name": "GPT-4o",
        "info": {"base_model_id": "gpt-4o-mini"},
        "owned_by": "openai",
        "connection_type": "external",
        "provider": "openai",
        "preset": True,
        "pipe": False,
        "urlIdx": 0,
        "api_key": "sk-SECRET",
        "system": "leaked system prompt",
        "user_id": "user-456",
        "user_message": "hello",
        "session_id": "sess-1",
        "access_grants": ["admin"],
    }
    metadata = {
        "params": {
            "system": "another leaked system prompt",
            "user_id": "user-456",
            "reasoning_tags": True,
            "compact_token_threshold": 500,
            "stream_delta_chunk_size": 10,
            "function_calling": "native",
        }
    }
    out = f._sanitize_model(model, metadata, "gpt-4o-mini-resolved")

    assert set(out) == {
        "resolved_id",
        "id",
        "name",
        "base_model_id",
        "owned_by",
        "connection_type",
        "provider",
        "preset",
        "is_pipe",
        "has_url_idx",
        "provider_guess",
        "function_calling",
        "owui_params",
        "gen_params_available",
        "note",
    }
    for secret_key in ("api_key", "system", "user_id", "user_message", "session_id", "access_grants"):
        assert secret_key not in out
    assert "SECRET" not in str(out)
    assert "leaked" not in str(out)

    assert out["resolved_id"] == "gpt-4o-mini-resolved"
    assert out["id"] == "gpt-4o"
    assert out["name"] == "GPT-4o"
    assert out["base_model_id"] == "gpt-4o-mini"
    assert out["owned_by"] == "openai"
    assert out["connection_type"] == "external"
    assert out["provider"] == "openai"
    assert out["provider_guess"] == "openai"
    assert out["preset"] is True
    assert out["is_pipe"] is False
    assert out["has_url_idx"] is True
    assert out["function_calling"] == "native"
    assert out["owui_params"] == {
        "reasoning_tags": True,
        "compact_token_threshold": 500,
        "stream_delta_chunk_size": 10,
    }
    assert out["gen_params_available"] is False


def test_sanitize_model_none_inputs_are_safe(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    out = f._sanitize_model(None, None, "")
    assert out["id"] is None
    assert out["name"] is None
    assert out["base_model_id"] is None
    assert out["owned_by"] is None
    assert out["connection_type"] is None
    assert out["provider"] is None
    assert out["preset"] is None
    assert out["is_pipe"] is False
    assert out["has_url_idx"] is False
    assert out["resolved_id"] is None
    assert out["function_calling"] is None
    assert out["owui_params"] == {}
    assert out["provider_guess"] == "unknown"


def test_valves_snapshot_masks_local_backend_urls(usage_display_module: ModuleType) -> None:
    """Security contract: the two local-backend URLs are masked when set.

    NOTE: modelsdev_url/modelsdev_api_url are also URL-bearing valves but are NOT masked by
    the current code (usage_display.py:1590 only lists llamacpp_url/llama_swap_url). Per
    git-blame both fields already existed on Valves when _valves_snapshot was authored
    (a02e593), so this reads as a deliberate scope choice (public models.dev endpoints vs.
    local-network addresses) rather than an oversight -- flagged in task-5-report.md, not
    asserted either way here (never assert a possible leak as confirmed-safe behavior).
    """
    f = usage_display_module.Filter()
    f.valves.llamacpp_url = "http://192.168.1.50:8080"
    f.valves.llama_swap_url = "http://192.168.1.51:8081"
    snap = f._valves_snapshot()

    assert snap["llamacpp_url"] == "******"
    assert snap["llama_swap_url"] == "******"
    assert set(snap) == set(f.valves.model_dump())  # masking replaces values, never drops keys


def test_valves_snapshot_unset_url_stays_empty_not_masked(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f.valves.llamacpp_url == ""  # default off
    snap = f._valves_snapshot()
    assert snap["llamacpp_url"] == ""  # nothing to hide when unset


def test_valves_snapshot_returns_empty_dict_on_dump_failure(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`model_dump()` raising is swallowed -> an empty dict, not a crash of the debug payload."""
    mod = usage_display_module
    f = mod.Filter()

    def _boom(_self: Any) -> dict[str, Any]:
        raise RuntimeError("dump failed")

    monkeypatch.setattr(mod.Filter.Valves, "model_dump", _boom, raising=True)
    assert f._valves_snapshot() == {}


# --------------------------------------------------------------------------- #
# Group C — async orchestration: inlet/outlet, context/cost resolution, unhappy flows
# --------------------------------------------------------------------------- #


def _patch_no_network(monkeypatch: pytest.MonkeyPatch, f: Any) -> None:
    """Stub the three network-touching fetchers so nothing in Group C hits the wire."""

    async def _empty_map() -> dict[str, Any]:
        return {}

    async def _no_probe(_model_id: str) -> None:
        return None

    monkeypatch.setattr(f, "_modelsdev_map", _empty_map)
    monkeypatch.setattr(f, "_modelsdev_prices_map", _empty_map)
    monkeypatch.setattr(f, "_probe_context", _no_probe)


# --- inlet ------------------------------------------------------------------- #


def test_inlet_records_timing_key(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    meta: dict[str, Any] = {"chat_id": "c1", "message_id": "m1"}
    body = make_body([make_message("hi", role="user")], metadata={})
    body["num_ctx"] = 8192
    out = run_async(f.inlet(body, __metadata__=meta))
    assert out is body  # inlet returns body
    assert meta["_tud_timing_key"] == "c1:m1"
    assert isinstance(meta["_tud_start"], float)
    assert meta["_tud_num_ctx"] == 8192
    assert body["metadata"]["_tud_timing_key"] == "c1:m1"


def test_inlet_no_metadata_falls_back_to_id_keyed_entry(usage_display_module: ModuleType) -> None:
    """No __metadata__, no chat_id/message_id -> a `fallback:<id(body)>` key, still returns body."""
    f = usage_display_module.Filter()
    body = make_body([make_message("hi", role="user")])
    out = run_async(f.inlet(body))
    assert out is body
    key = body["metadata"]["_tud_timing_key"]
    assert key.startswith("fallback:")
    assert "_tud_num_ctx" not in body["metadata"]  # no num_ctx hint anywhere in body


def test_inlet_metadata_present_without_num_ctx(usage_display_module: ModuleType) -> None:
    """__metadata__ is a real dict but carries no num_ctx hint anywhere -> the hint key is never set."""
    f = usage_display_module.Filter()
    meta: dict[str, Any] = {"chat_id": "c2", "message_id": "m2"}
    body = make_body([make_message("hi", role="user")])
    run_async(f.inlet(body, __metadata__=meta))
    assert meta["_tud_timing_key"] == "c2:m2"
    assert "_tud_num_ctx" not in meta


# --- _resolve_wall_clock: multi-level fallback + leak cleanup, tested directly ------ #


def test_resolve_wall_clock_prefers_metadata_start(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    now = time.time()
    metadata = {"_tud_start": now - 2.0, "_tud_timing_key": "wallclock-meta-key"}
    elapsed = f._resolve_wall_clock(make_body(), metadata)
    assert elapsed == pytest.approx(2.0, abs=0.2)


def test_resolve_wall_clock_body_metadata_not_dict_ignored(usage_display_module: ModuleType) -> None:
    """No __metadata__, and body['metadata'] is not even a dict -> nothing usable anywhere."""
    f = usage_display_module.Filter()
    body = make_body()
    body["metadata"] = "not-a-dict"
    assert f._resolve_wall_clock(body, None) is None


def test_resolve_wall_clock_reconstructs_key_from_chat_and_message_id(usage_display_module: ModuleType) -> None:
    """No _tud_start/_tud_timing_key anywhere -> falls back to reconstructing chat_id:message_id."""
    f = usage_display_module.Filter()
    mod = usage_display_module
    mod._request_timings["wallclock-c9:wallclock-m9"] = time.time() - 1.5
    metadata = {"chat_id": "wallclock-c9", "message_id": "wallclock-m9"}
    elapsed = f._resolve_wall_clock(make_body(), metadata)
    assert elapsed == pytest.approx(1.5, abs=0.2)
    assert "wallclock-c9:wallclock-m9" not in mod._request_timings  # popped after use


def test_resolve_wall_clock_metadata_present_without_chat_or_message_id(usage_display_module: ModuleType) -> None:
    """metadata is a real, truthy dict, but carries no chat_id/message_id to reconstruct a key from."""
    f = usage_display_module.Filter()
    metadata = {"some_other_key": "value"}
    assert f._resolve_wall_clock(make_body(), metadata) is None


def test_resolve_wall_clock_purges_stale_entries(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    mod._request_timings["wallclock-stale-key"] = time.time() - 700  # older than the 600s cutoff
    f._resolve_wall_clock(make_body(), None)
    assert "wallclock-stale-key" not in mod._request_timings


# --- outlet happy path --------------------------------------------------------- #


def test_outlet_emits_status_line(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    usage = make_usage(input_tokens=1000, output_tokens=200)
    body = make_body([make_message("q", role="user"), make_message("a", usage=usage)], model="gpt-4o")
    emitter = CapturingEmitter()
    result = run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-4o")))
    assert result is body
    statuses = emitter.statuses()
    assert len(statuses) == 1
    desc = statuses[0]["data"]["description"]
    assert statuses[0]["data"]["done"] is True
    assert "1,000" in desc  # input counter present
    assert "200" in desc  # output counter present
    assert "%" in desc  # context utilization from static gpt-4o=128000 (1200/128000 rounds to 1%)


def test_outlet_falls_back_to_body_model_when_no_model_dict(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """__model__ is None (not a dict) -> model_id resolution falls back to body['model']."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    usage = make_usage(input_tokens=100, output_tokens=50)
    body = make_body(
        [make_message("q", role="user"), make_message("a", usage=usage)], model="gpt-4o-body-fallback-check"
    )
    emitter = CapturingEmitter()
    result = run_async(f.outlet(body, __event_emitter__=emitter))  # no __model__ kwarg at all
    assert result is body
    assert len(emitter.statuses()) == 1


# --- outlet early exits (each returns body, emits nothing) --------------------- #


def test_outlet_skips_when_user_disabled(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    emitter = CapturingEmitter()

    class _UV:
        enabled = False

    body = make_body([make_message("a", usage=make_usage())])
    result = run_async(f.outlet(body, __user__={"valves": _UV()}, __event_emitter__=emitter))
    assert result is body
    assert emitter.events == []


def test_outlet_skips_background_tasks(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    emitter = CapturingEmitter()
    body = make_body([make_message("a", usage=make_usage())])
    result = run_async(f.outlet(body, __event_emitter__=emitter, __metadata__={"task": "title_generation"}))
    assert result is body
    assert emitter.events == []


@pytest.mark.parametrize(
    "task",
    [
        "title_generation",
        "tags_generation",
        "follow_up_generation",
        "emoji_generation",
        "query_generation",
        "autocomplete_generation",
        "moa_response_generation",
    ],
)
def test_outlet_skips_every_background_task_string(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch, task: str
) -> None:
    """Exact early-exit task set, verified against usage_display.py:787-795."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    emitter = CapturingEmitter()
    body = make_body([make_message("a", usage=make_usage())])
    result = run_async(f.outlet(body, __event_emitter__=emitter, __metadata__={"task": task}))
    assert result is body
    assert emitter.events == []


def test_outlet_no_messages_or_no_assistant(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    emitter = CapturingEmitter()

    empty_body = make_body([])
    result1 = run_async(f.outlet(empty_body, __event_emitter__=emitter))
    assert result1 is empty_body

    user_only_body = make_body([make_message("q", role="user")])
    result2 = run_async(f.outlet(user_only_body, __event_emitter__=emitter))
    assert result2 is user_only_body

    assert emitter.events == []


# --- _resolve_context: offline resolution (override > map > static > probe) --- #


def test_context_size_override_wins(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_override = 12345
    ctx = run_async(
        f._resolve_context(make_body(model="gpt-4o"), {}, make_model_dict("gpt-4o"), make_tokens(total=100))
    )
    assert ctx["size"] == 12345
    assert ctx["source"] == "override"


def test_context_num_ctx_hint_beats_static(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    # num_ctx hint (captured at inlet) wins over the static gpt-4o=128000 entry.
    ctx = run_async(
        f._resolve_context(
            make_body(model="gpt-4o"), {"_tud_num_ctx": 32768}, make_model_dict("gpt-4o"), make_tokens(total=100)
        )
    )
    assert ctx["size"] == 32768
    assert ctx["source"] == "num_ctx"


def test_context_num_ctx_hint_rejected_when_not_positive(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    # hint <= 0 is ignored -> falls through to the static table. Unique id avoids cache coupling.
    ctx = run_async(
        f._resolve_context(
            make_body(model="gpt-4o-numctx-guard"),
            {"_tud_num_ctx": 0},
            make_model_dict("gpt-4o-numctx-guard"),
            make_tokens(total=100),
        )
    )
    assert ctx["size"] == 128000
    assert ctx["source"] == "static_table"


def test_context_static_table_match(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    # Unique-per-test model id: _ctx_size_cache is module-level and session-scoped (shared
    # across every test in this file via the session-scoped usage_display_module fixture), so
    # reusing a bare "gpt-4o" id across context tests with differing valve setups would let an
    # earlier test's cached resolution silently mask this one's. A distinct id sidesteps that.
    ctx = run_async(
        f._resolve_context(
            make_body(model="gpt-4o-static-check"), {}, make_model_dict("gpt-4o-static-check"), make_tokens(total=100)
        )
    )
    assert ctx["size"] == 128000  # from _STATIC_CONTEXT_SIZES["gpt-4o"], matched by substring
    assert ctx["source"] == "static_table"
    assert ctx["matched_key"] == "gpt-4o"
    assert ctx["used"] == 100


def test_context_workspace_model_matches_via_base_model_id(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom/agent model whose id carries no provider token resolves via its base_model_id.

    Regression for the reported bug: id="research-base-check" matches nothing, but the base model
    "gpt-4o-base-check" hits the static gpt-4o entry, so context resolves without a per-agent map.
    """
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    model = make_model_dict("research-base-check", base="gpt-4o-base-check")
    ctx = run_async(f._resolve_context(make_body(model="research-base-check"), {}, model, make_tokens(total=100)))
    assert ctx["size"] == 128000
    assert ctx["source"] == "static_table"
    assert ctx["matched_key"] == "gpt-4o"


def test_context_size_map_valve_overrides_static(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_map = '{"gpt-4o": 999}'
    ctx = run_async(
        f._resolve_context(
            make_body(model="gpt-4o-mapoverride-check"),
            {},
            make_model_dict("gpt-4o-mapoverride-check"),
            make_tokens(total=100),
        )
    )
    assert ctx["size"] == 999
    assert ctx["source"] == "static_table"


def test_context_table_size_map_non_dict_json_ignored(usage_display_module: ModuleType) -> None:
    """Valid JSON that isn't an object (e.g. a list) is ignored, not merged."""
    f = usage_display_module.Filter()
    f.valves.context_size_map = "[1, 2, 3]"
    table = f._context_table()
    assert table == usage_display_module._STATIC_CONTEXT_SIZES


def test_context_table_size_map_malformed_json_ignored(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.context_size_map = "{not valid json"
    table = f._context_table()
    assert table == usage_display_module._STATIC_CONTEXT_SIZES


def test_context_unknown_model_no_match_no_probe(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    ctx = run_async(
        f._resolve_context(
            make_body(model="zzz-totally-unknown-model-007"),
            {},
            None,
            make_tokens(total=100),
        )
    )
    assert not ctx["size"]  # no override, no hint, no static/live match, no probe url configured
    assert ctx["matched_key"] is None
    assert ctx["source"] == "none"


def test_resolve_context_disabled_short_circuits(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.show_context_window = False
    ctx = run_async(
        f._resolve_context(make_body(model="gpt-4o"), {}, make_model_dict("gpt-4o"), make_tokens(total=100))
    )
    assert ctx == {"size": None, "used": None, "source": "disabled", "matched_key": None}


def test_context_size_for_modelsdev_populated_table_match(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A populated (monkeypatched) models.dev table feeds the live-lookup match branch."""
    f = usage_display_module.Filter()

    async def _map() -> dict[str, Any]:
        return {"gpt-4o": 250000}

    monkeypatch.setattr(f, "_modelsdev_map", _map)
    f.valves.fetch_context_from_modelsdev = True
    size, prov = run_async(f._context_size_for("gpt-4o-modelsdev-check", None))
    assert size == 250000
    assert prov == {"source": "modelsdev", "matched_key": "gpt-4o"}


def test_context_size_for_modelsdev_no_match_falls_through_to_static(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """models.dev is enabled but has no match for this model -> falls through to the static table."""
    f = usage_display_module.Filter()

    async def _map() -> dict[str, Any]:
        return {"some-other-model": 999}

    monkeypatch.setattr(f, "_modelsdev_map", _map)
    f.valves.fetch_context_from_modelsdev = True
    size, prov = run_async(f._context_size_for("gpt-4o-modelsdev-nomatch-check", None))
    assert size == 128000  # static _STATIC_CONTEXT_SIZES["gpt-4o"] substring match
    assert prov == {"source": "static_table", "matched_key": "gpt-4o"}


def test_context_size_for_probe_configured_but_returns_none(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe URL is configured, but the probe itself finds nothing -> overall no match."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)  # _probe_context stubbed to always return None
    f.valves.llamacpp_url = "http://localhost:8080"
    size, prov = run_async(f._context_size_for("totally-unmatched-model-probe-none-check", None))
    assert size is None
    assert prov == {"source": "none", "matched_key": None}


def test_context_size_for_probe_success(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """A populated (monkeypatched) probe result feeds the probe-success branch."""
    f = usage_display_module.Filter()

    async def _empty_map() -> dict[str, Any]:
        return {}

    async def _probe(_model_id: str) -> int:
        return 65536

    monkeypatch.setattr(f, "_modelsdev_map", _empty_map)
    monkeypatch.setattr(f, "_modelsdev_prices_map", _empty_map)
    monkeypatch.setattr(f, "_probe_context", _probe)
    f.valves.llamacpp_url = "http://localhost:8080"
    size, prov = run_async(f._context_size_for("totally-unmatched-model-probe-check", None))
    assert size == 65536
    assert prov == {"source": "probe", "matched_key": None}


# --- _resolve_cost: off / auto (native) / estimate branches -------------------- #


def test_resolve_cost_off(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.cost_mode = "off"
    cost = run_async(f._resolve_cost(make_usage(), make_tokens(), [], "gpt-4o"))
    assert cost == {"message": None, "message_est": False, "cumulative": None, "cumulative_est": False}


def test_resolve_cost_estimate_from_static_price(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)  # no live prices -> static _STATIC_PRICES only
    f.valves.cost_mode = "estimate"
    f.valves.fetch_prices_from_modelsdev = False
    usage = make_usage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = run_async(
        f._resolve_cost(
            usage, make_tokens(fresh_input=1_000_000, output=1_000_000), [make_message("a", usage=usage)], "gpt-4o"
        )
    )
    # _STATIC_PRICES["gpt-4o"] = {"input": 2.50, "output": 10.00, ...} -> 1M*2.5 + 1M*10.0 over 1e6
    assert cost["message"] == pytest.approx(12.5)
    assert cost["message_est"] is True  # estimate marked ≈
    assert cost["cumulative"] == pytest.approx(12.5)  # single assistant message, same usage
    assert cost["cumulative_est"] is True


def test_resolve_cost_auto_native_key_used(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.cost_mode = "auto"
    usage = make_usage(cost=0.0123)  # OpenRouter/LiteLLM-style native cost key
    cost = run_async(f._resolve_cost(usage, make_tokens(), [], "gpt-4o"))
    assert cost["message"] == pytest.approx(0.0123)
    assert cost["message_est"] is False


def test_resolve_cost_auto_no_native_key_never_estimates(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto mode must never fall back to estimation, even when tokens/price would allow it."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.cost_mode = "auto"
    cost = run_async(f._resolve_cost(make_usage(), make_tokens(fresh_input=1_000_000, output=1_000_000), [], "gpt-4o"))
    assert cost["message"] is None
    assert cost["message_est"] is False
    assert cost["cumulative"] is None


def test_resolve_cost_estimate_no_price_and_cumulative_disabled(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """estimate mode, no price found anywhere, show_cumulative_cost off -> message stays None, no cumulative work."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.cost_mode = "estimate"
    f.valves.fetch_prices_from_modelsdev = False
    f.valves.show_cumulative_cost = False
    cost = run_async(
        f._resolve_cost(make_usage(), make_tokens(fresh_input=100, output=50), [], "totally-unknown-model-no-price-xyz")
    )
    assert cost["message"] is None
    assert cost["message_est"] is False
    assert cost["cumulative"] is None


def test_resolve_cost_cumulative_mixes_native_and_estimated_messages(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native cost wins for the current message; cumulative still resolves a price to estimate

    the cost of OTHER historical messages that carry no native cost of their own.
    """
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.cost_mode = "estimate"
    f.valves.fetch_prices_from_modelsdev = False
    native_usage = make_usage(cost=0.02)
    est_usage_1 = make_usage(input_tokens=1_000_000, output_tokens=1_000_000)
    est_usage_2 = make_usage(input_tokens=500_000, output_tokens=500_000)
    messages = [
        make_message("a", usage=native_usage),
        make_message("b", usage=est_usage_1),
        make_message("c", usage=est_usage_2),
    ]
    cost = run_async(f._resolve_cost(native_usage, make_tokens(), messages, "gpt-4o"))
    assert cost["message"] == pytest.approx(0.02)  # native wins for the current message
    assert cost["message_est"] is False
    assert cost["cumulative"] is not None
    assert cost["cumulative_est"] is True  # at least one historical message was estimated


def test_resolve_cost_cumulative_skips_non_estimable_message_and_continues_loop(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A historical message with no native cost AND an empty (non-estimable) token bag contributes

    nothing, but the loop still continues on to price the next message.
    """
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.cost_mode = "estimate"
    f.valves.fetch_prices_from_modelsdev = False
    messages = [
        make_message("a", usage={}),  # no native cost, empty token bag -> est_cost is None
        make_message("b", usage=make_usage(cost=0.05)),  # native cost -> contributes normally
    ]
    cost = run_async(f._resolve_cost(make_usage(cost=0.05), make_tokens(), messages, "gpt-4o"))
    assert cost["cumulative"] == pytest.approx(0.05)
    assert cost["cumulative_est"] is False  # the only contributing message was native, not estimated


def test_resolve_price_price_map_wins(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.price_map = '{"gpt-4o": {"input": 1.0, "output": 2.0}}'
    price, prov = run_async(f._resolve_price("gpt-4o-mini"))
    assert price == {"input": 1.0, "output": 2.0}
    assert prov == {"source": "price_map", "matched_key": "gpt-4o"}


def test_resolve_price_modelsdev_populated_map_match(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()

    async def _prices() -> dict[str, Any]:
        return {"gpt-4o": {"input": 3.0, "output": 12.0}}

    monkeypatch.setattr(f, "_modelsdev_prices_map", _prices)
    f.valves.fetch_prices_from_modelsdev = True
    price, prov = run_async(f._resolve_price("gpt-4o-modelsdev-price-check"))
    assert price == {"input": 3.0, "output": 12.0}
    assert prov == {"source": "modelsdev", "matched_key": "gpt-4o"}


def test_resolve_price_no_match_anywhere(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.fetch_prices_from_modelsdev = False
    price, prov = run_async(f._resolve_price("totally-unknown-brand-zzz"))
    assert price is None
    assert prov == {"source": "none", "matched_key": None}


def test_resolve_price_modelsdev_no_match_falls_through_to_static(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """models.dev pricing is enabled but has no match for this model -> falls through to the static table."""
    f = usage_display_module.Filter()

    async def _prices() -> dict[str, Any]:
        return {"some-other-model": {"input": 1.0, "output": 2.0}}

    monkeypatch.setattr(f, "_modelsdev_prices_map", _prices)
    f.valves.fetch_prices_from_modelsdev = True
    price, prov = run_async(f._resolve_price("gpt-4o-price-modelsdev-nomatch-check"))
    assert price == usage_display_module._STATIC_PRICES["gpt-4o"]
    assert prov == {"source": "static", "matched_key": "gpt-4o"}


def test_price_map_table_malformed_json_returns_empty(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.price_map = "{not valid json"
    assert f._price_map_table() == {}


def test_price_map_table_non_dict_json_returns_empty(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.price_map = "[1, 2, 3]"  # valid JSON, but not an object
    assert f._price_map_table() == {}


# --- unhappy flows: malformed input must not crash outlet ---------------------- #


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": "bad"}, {"input_tokens": -5}])
def test_outlet_survives_malformed_usage(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch, usage: Any
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    monkeypatch.setattr(usage_display_module, "_TIKTOKEN_AVAILABLE", False, raising=False)
    body = make_body([make_message("a", usage=usage)], model="mystery-model")
    emitter = CapturingEmitter()
    # Must complete without raising; may or may not emit, but never errors.
    result = run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("mystery-model")))
    assert result is body


def test_outlet_survives_network_resolver_exception(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()

    async def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("network down")

    monkeypatch.setattr(f, "_modelsdev_map", _boom)
    monkeypatch.setattr(f, "_modelsdev_prices_map", _boom)
    f.valves.fetch_context_from_modelsdev = True
    f.valves.cost_mode = "estimate"
    body = make_body([make_message("a", usage=make_usage())], model="gpt-4o-network-exc-test")
    emitter = CapturingEmitter()
    # Degrades gracefully: outlet still finishes and emits its line.
    result = run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-4o-network-exc-test")))
    assert result is body
    assert len(emitter.statuses()) == 1


# --- debug_mode: emits the diagnostic citation --------------------------------- #


def test_outlet_debug_mode_emits_citation(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.debug_mode = True
    f.valves.llamacpp_url = "http://secret:8080"
    body = make_body([make_message("a", usage=make_usage())], model="gpt-4o")
    emitter = CapturingEmitter()
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-4o")))
    kinds = [e.get("type") for e in emitter.events]
    assert "status" in kinds
    assert "citation" in kinds
    citation = next(e for e in emitter.events if e.get("type") == "citation")
    assert citation["data"]["source"]["name"] == "Token Usage & Cost Display - Debug info"
    blob = repr(emitter.events)
    assert "secret:8080" not in blob  # url valve masked in snapshot
    assert "******" in blob


def test_resolve_cost_debug_includes_cost_details(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.cost_mode = "auto"
    f.valves.debug_mode = True
    usage = make_usage(cost=0.02, cost_details={"upstream_inference_cost": 0.018, "cache_discount": 0.001})
    cost = run_async(f._resolve_cost(usage, make_tokens(), [], "gpt-4o"))
    assert cost["debug"]["cost_details"] == {"upstream_inference_cost": 0.018, "cache_discount": 0.001}


def test_outlet_debug_payload_reports_web_search_and_cost_details(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the debug citation JSON carries the new web_search + cost_details blocks."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.debug_mode = True
    f.valves.cost_mode = "auto"
    msg = make_message("answer", usage=make_usage(cost=0.02, cost_details={"upstream_inference_cost": 0.018}))
    msg["sources"] = [
        {"source": {"name": "NYT", "url": "https://nytimes.com/a"}, "metadata": [{"source": "https://nytimes.com/a"}]}
    ]
    body = make_body([msg], model="gpt-4o")
    emitter = CapturingEmitter()
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-4o")))
    citation = next(e for e in emitter.events if e.get("type") == "citation")
    doc = citation["data"]["document"][0]
    assert "web_search" in doc
    assert "nytimes.com" in doc
    assert "cost_details" in doc
    assert "upstream_inference_cost" in doc


def test_emit_debug_json_dumps_failure_falls_back_to_error_string(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`json.dumps(..., default=str)` raising is caught -> the citation shows the error, not a crash."""
    mod = usage_display_module
    f = mod.Filter()

    def _boom(*_a: Any, **_k: Any) -> str:
        raise TypeError("cannot serialize")

    monkeypatch.setattr(mod.json, "dumps", _boom)
    emitter = CapturingEmitter()
    run_async(
        f._emit_debug(
            emitter,
            None,
            [],
            make_message("a", usage=make_usage()),
            make_usage(),
            make_tokens(),
            make_timing(),
            make_ctx(),
            make_cost(),
            None,
            None,
        )
    )
    citation = next(e for e in emitter.events if e.get("type") == "citation")
    doc = citation["data"]["document"][0]
    assert "serialization error" in doc


# --------------------------------------------------------------------------- #
# Group D — the three network fetchers, driven against a fake `aiohttp` (no real network)
# --------------------------------------------------------------------------- #


def test_modelsdev_map_returns_cached_value_without_refetch(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    cached = {"gpt-4o": 999}
    monkeypatch.setitem(mod._modelsdev_cache, "map", cached)
    monkeypatch.setitem(mod._modelsdev_cache, "expiry", time.time() + 100)
    result = run_async(f._modelsdev_map())
    assert result is cached


def test_modelsdev_map_aiohttp_unavailable_returns_empty(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    monkeypatch.setitem(mod._modelsdev_cache, "map", None)
    monkeypatch.setitem(mod._modelsdev_cache, "expiry", 0.0)
    monkeypatch.setattr(mod, "_AIOHTTP_AVAILABLE", False, raising=False)
    result = run_async(f._modelsdev_map())
    assert result == {}
    assert mod._modelsdev_cache["map"] == {}


def test_modelsdev_map_fetches_and_flattens_response(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    monkeypatch.setitem(mod._modelsdev_cache, "map", None)
    monkeypatch.setitem(mod._modelsdev_cache, "expiry", 0.0)
    payload = {
        "openai/gpt-4o": {"limit": {"context": 128000}},
        "bad-entry": {"limit": "not-a-dict"},
        "no-limit-entry": {},
    }
    install_fake_aiohttp(monkeypatch, mod, {f.valves.modelsdev_url: payload})
    result = run_async(f._modelsdev_map())
    assert result["openai/gpt-4o"] == 128000
    assert result["gpt-4o"] == 128000  # bare last segment also indexed
    assert "bad-entry" not in result
    assert "no-limit-entry" not in result
    assert mod._modelsdev_cache["map"] == result


def test_modelsdev_map_non_dict_response_yields_empty_map(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """models.dev responding with something that isn't a JSON object is tolerated, not crashed."""
    f = usage_display_module.Filter()
    mod = usage_display_module
    monkeypatch.setitem(mod._modelsdev_cache, "map", None)
    monkeypatch.setitem(mod._modelsdev_cache, "expiry", 0.0)
    install_fake_aiohttp(monkeypatch, mod, {f.valves.modelsdev_url: ["not", "a", "dict"]})
    result = run_async(f._modelsdev_map())
    assert result == {}


def test_modelsdev_map_network_error_returns_empty_and_caches_briefly(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    monkeypatch.setitem(mod._modelsdev_cache, "map", None)
    monkeypatch.setitem(mod._modelsdev_cache, "expiry", 0.0)
    install_fake_aiohttp(monkeypatch, mod, session_error=RuntimeError("boom"))
    result = run_async(f._modelsdev_map())
    assert result == {}
    assert mod._modelsdev_cache["map"] == {}


def test_modelsdev_prices_map_returns_cached_value_without_refetch(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    cached = {"gpt-4o": {"input": 1.0}}
    monkeypatch.setitem(mod._modelsdev_prices_cache, "map", cached)
    monkeypatch.setitem(mod._modelsdev_prices_cache, "expiry", time.time() + 100)
    result = run_async(f._modelsdev_prices_map())
    assert result is cached


def test_modelsdev_prices_map_aiohttp_unavailable_returns_empty(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    monkeypatch.setitem(mod._modelsdev_prices_cache, "map", None)
    monkeypatch.setitem(mod._modelsdev_prices_cache, "expiry", 0.0)
    monkeypatch.setattr(mod, "_AIOHTTP_AVAILABLE", False, raising=False)
    result = run_async(f._modelsdev_prices_map())
    assert result == {}


def test_modelsdev_prices_map_fetches_and_parses(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    monkeypatch.setitem(mod._modelsdev_prices_cache, "map", None)
    monkeypatch.setitem(mod._modelsdev_prices_cache, "expiry", 0.0)
    payload = {"openai": {"models": {"gpt-4o": {"id": "openai/gpt-4o", "cost": {"input": 2.5, "output": 10.0}}}}}
    install_fake_aiohttp(monkeypatch, mod, {f.valves.modelsdev_api_url: payload})
    result = run_async(f._modelsdev_prices_map())
    assert result["gpt-4o"] == {"input": 2.5, "output": 10.0}
    assert mod._modelsdev_prices_cache["map"] == result


def test_modelsdev_prices_map_network_error_returns_empty(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    monkeypatch.setitem(mod._modelsdev_prices_cache, "map", None)
    monkeypatch.setitem(mod._modelsdev_prices_cache, "expiry", 0.0)
    install_fake_aiohttp(monkeypatch, mod, session_error=RuntimeError("boom"))
    result = run_async(f._modelsdev_prices_map())
    assert result == {}


def test_probe_context_aiohttp_unavailable_returns_none(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.llamacpp_url = "http://localhost:8080"
    assert run_async(f._probe_context("some-model")) is None  # real env has no aiohttp installed


def test_probe_context_llama_swap_success(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llama_swap_url = "http://localhost:8090"
    install_fake_aiohttp(monkeypatch, mod, {"http://localhost:8090/running": {"running": [{"cmd": "--ctx-size 4096"}]}})
    assert run_async(f._probe_context("some-model")) == 4096


def test_probe_context_llama_swap_no_match_and_no_llamacpp_returns_none(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """llama-swap responds, but the payload has no --ctx-size to extract, and llamacpp isn't configured."""
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llama_swap_url = "http://localhost:8090"
    install_fake_aiohttp(monkeypatch, mod, {"http://localhost:8090/running": {"running": []}})
    assert run_async(f._probe_context("some-model")) is None


def test_probe_context_llamacpp_no_match_returns_none(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """llama.cpp responds, but neither top-level nor nested n_ctx is present."""
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(monkeypatch, mod, {"http://localhost:8080/props": {"no_n_ctx_here": True}})
    assert run_async(f._probe_context("some-model")) is None


def test_probe_context_llama_swap_fails_falls_back_to_llamacpp(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llama_swap_url = "http://localhost:8090"
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(
        monkeypatch,
        mod,
        {
            "http://localhost:8090/running": RuntimeError("swap down"),
            "http://localhost:8080/props": {"n_ctx": 8192},
        },
    )
    assert run_async(f._probe_context("some-model")) == 8192


def test_probe_context_llamacpp_nested_generation_settings(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(
        monkeypatch, mod, {"http://localhost:8080/props": {"default_generation_settings": {"n_ctx": 16384}}}
    )
    assert run_async(f._probe_context("some-model")) == 16384


def test_probe_context_llamacpp_fails_returns_none(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(monkeypatch, mod, {"http://localhost:8080/props": RuntimeError("cpp down")})
    assert run_async(f._probe_context("some-model")) is None


def test_probe_context_outer_exception_returns_none(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ClientSession(...) itself raising (e.g. connection-level failure) is caught by the outer guard."""
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(monkeypatch, mod, session_error=RuntimeError("session boom"))
    assert run_async(f._probe_context("some-model")) is None
