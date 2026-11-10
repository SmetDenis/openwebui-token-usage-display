"""Tests for openwebui/usage_display.py — the Token Usage & Cost Display Filter.

Characterization tests: they pin the plugin's current, verified-correct behavior
across providers and its graceful degradation on malformed input. The plugin is
loaded via the `usage_display_module` conftest fixture (SourceFileLoader), same as
the bin/* scripts. No real network, no OWUI runtime: usage/body/output payloads are
built by the factories below, shaped to OWUI 0.10.2.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType


def test_module_loads_and_exposes_filter(usage_display_module: ModuleType) -> None:
    mod = usage_display_module
    assert hasattr(mod, "Filter")
    f = mod.Filter()
    assert f.valves.icon_style == "emoji"
    assert f.valves.cost_mode == "auto"
    assert mod._DEFAULT_ORDER[0] == "input"
    assert len(mod._DEFAULT_ORDER) == 17
    assert mod._DEFAULT_ORDER.index("tokens_total") == mod._DEFAULT_ORDER.index("total") + 1  # tokens grouped
    assert f.valves.show_cumulative_tokens is True  # on by default, like show_cumulative_cost
    # the truncation flag sits next to the output count, early enough to survive the one-line clamp
    assert mod._DEFAULT_ORDER.index("finish") == mod._DEFAULT_ORDER.index("output") + 1
    assert f.valves.show_finish_reason is True
    # the OpenRouter balance follows the two costs and is opt-in (it sends a request per response)
    assert mod._DEFAULT_ORDER.index("balance") == mod._DEFAULT_ORDER.index("cost_total") + 1
    assert f.valves.show_balance is False


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
        "context_used": None,
        "context_est": False,
        "incomplete": False,
        "cumulative": None,
        "cumulative_est": False,
    }
    base.update(over)
    return base


def make_timing(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"seconds": None, "source": "provider", "tps": None}
    base.update(over)
    return base


def make_ctx(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "size": None,
        "used": None,
        "used_source": None,
        "used_est": False,
        "source": "none",
        "matched_key": None,
    }
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
    def __init__(self, *, per_word: bool) -> None:
        self._per_word = per_word

    def encode(self, text: str) -> list[int]:
        toks = text.split() if self._per_word else list(text)
        return [0] * len(toks)


class _FakeTiktoken:
    def __init__(self, *, per_word: bool) -> None:
        self._per_word = per_word

    def encoding_for_model(self, model: str) -> _FakeEncoding:
        if not model:
            raise KeyError(model)
        return _FakeEncoding(per_word=self._per_word)

    def get_encoding(self, name: str) -> _FakeEncoding:  # noqa: ARG002 - mirrors tiktoken.get_encoding
        return _FakeEncoding(per_word=self._per_word)


def install_fake_tiktoken(monkeypatch: pytest.MonkeyPatch, mod: ModuleType, *, per_word: bool = True) -> None:
    """Make the plugin's tiktoken path live: 1 token per whitespace-word (deterministic)."""
    monkeypatch.setattr(mod, "tiktoken", _FakeTiktoken(per_word=per_word), raising=False)
    monkeypatch.setattr(mod, "_TIKTOKEN_AVAILABLE", True, raising=False)


@dataclass(frozen=True)
class HttpStatus:
    """A canned reply that is only an HTTP status (e.g. 503), for `install_fake_aiohttp` responses."""

    code: int


class _FakeAiohttpResponse:
    """Stand-in for an aiohttp response: async context manager, `.status` and async `.json()`."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status = payload.code if isinstance(payload, HttpStatus) else 200

    async def __aenter__(self) -> Self:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self, content_type: str | None = None) -> Any:  # noqa: ARG002 - mirrors aiohttp
        return self._payload


class _FakeAiohttpSession:
    """Stand-in for aiohttp.ClientSession: async context manager + `.get(url)`."""

    def __init__(self, responses: dict[str, Any], requests: list[tuple[str, dict[str, str]]]) -> None:
        self._responses = responses
        self._requests = requests

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, headers: dict[str, str] | None = None, **_kw: Any) -> _FakeAiohttpResponse:
        self._requests.append((url, headers or {}))
        return _FakeAiohttpResponse(self._responses.get(url, {}))


class _FakeAiohttpModule:
    """Minimal stand-in for the `aiohttp` module used by the three network probes.

    `responses` maps request URL -> either a JSON-able payload (success) or an Exception
    instance (raised when that URL is fetched). `session_error`, if set, is raised by
    `ClientSession(...)` itself (simulating a connection-level failure before any request).
    `requests` records every (url, headers) fetched, in order.
    """

    def __init__(self, responses: dict[str, Any] | None = None, *, session_error: Exception | None = None) -> None:
        self._responses = responses or {}
        self._session_error = session_error
        self.requests: list[tuple[str, dict[str, str]]] = []

    def ClientTimeout(self, **_kw: Any) -> object:  # noqa: N802 - mirrors aiohttp's real API name
        return object()

    def ClientSession(self, **_kw: Any) -> _FakeAiohttpSession:  # noqa: N802 - mirrors aiohttp's real API name
        if self._session_error is not None:
            raise self._session_error
        return _FakeAiohttpSession(self._responses, self.requests)


def install_fake_aiohttp(
    monkeypatch: pytest.MonkeyPatch,
    mod: ModuleType,
    responses: dict[str, Any] | None = None,
    *,
    session_error: Exception | None = None,
) -> _FakeAiohttpModule:
    """Make the plugin's aiohttp network probes live against canned responses (no real network)."""
    fake = _FakeAiohttpModule(responses, session_error=session_error)
    monkeypatch.setattr(mod, "aiohttp", fake, raising=False)
    monkeypatch.setattr(mod, "_AIOHTTP_AVAILABLE", True, raising=False)
    return fake


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
    assert _num(True) is None  # noqa: FBT003 - bool is not a number here
    assert _num("5") is None
    assert _num(None) is None
    assert _num(float("nan")) is None  # non-finite: would crash int() in _compute_total
    assert _num(float("inf")) is None
    assert _num(float("-inf")) is None


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
    assert sorted(resolved) == sorted(mod._DEFAULT_ORDER)  # still all metric keys, no duplicates
    assert len(resolved) == len(set(resolved)) == 17


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


def renderer(mod: ModuleType, name: str) -> Callable[..., str | None]:
    """A `_render_*(stats)` renderer: `r(valves, tokens, timing, ctx, cost, model[, finish_reason, balance])`."""
    render = getattr(mod, name)
    return lambda *fields: render(mod._Stats(*fields))


def test_icon_styles(usage_display_module: ModuleType, valves: Any) -> None:
    _icon = usage_display_module._icon
    valves.icon_style = "emoji"
    assert _icon(valves, "input") == "⬆︎"
    valves.icon_style = "simple"
    assert _icon(valves, "input") == "↑"
    assert _icon(valves, "cost") == ""  # simple cost has no icon ($ self-labels)
    assert _icon(valves, "tokens_total") == "ΣΣ"
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
# Group A cont. — all 15 stats-line renderers, each individually
# --------------------------------------------------------------------------- #


def test_render_input(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_input")
    valves.icon_style = "emoji"
    assert r(valves, make_tokens(input=100), make_timing(), make_ctx(), make_cost(), None) == "⬆︎ 100"
    valves.show_input_tokens = False
    assert r(valves, make_tokens(input=100), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_input_tokens = True
    assert r(valves, make_tokens(input=None), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_output(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_output")
    assert r(valves, make_tokens(output=50), make_timing(), make_ctx(), make_cost(), None) == "⬇︎ 50"
    assert r(valves, make_tokens(output=None), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_output_tokens = False
    assert r(valves, make_tokens(output=50), make_timing(), make_ctx(), make_cost(), None) is None


@pytest.mark.parametrize(
    ("style", "expected"), [("emoji", "✂️ truncated"), ("simple", "✂︎ truncated"), ("off", "truncated")]
)
def test_render_finish_flags_only_length(
    usage_display_module: ModuleType, valves: Any, style: str, expected: str
) -> None:
    r = renderer(usage_display_module, "_render_finish")
    valves.icon_style = style
    bags = (make_tokens(), make_timing(), make_ctx(), make_cost(), None)
    assert r(valves, *bags, "length") == expected
    # the normal stop, a turn paused on a tool call, a filter stop and "unknown" (no value) all stay silent
    for reason in ("stop", "tool_calls", "content_filter", None):
        assert r(valves, *bags, reason) is None
    assert r(valves, *bags) is None  # _Stats default: OWUI < 0.12 sends no finish_reason
    valves.show_finish_reason = False
    assert r(valves, *bags, "length") is None


def test_outlet_flags_truncated_response_next_to_output(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OWUI 0.12.0+ puts `finish_reason` on the current outlet message (last model call of the turn)."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    cut = make_message("partial answer", usage=make_usage(output_tokens=4096))
    cut["finish_reason"] = "length"
    emitter = CapturingEmitter()
    run_async(f.outlet(make_body([cut], model="gpt-4o"), __event_emitter__=emitter, __model__=make_model_dict()))
    assert "⬇︎ 4,096 · ✂️ truncated · Σ" in emitter.statuses()[0]["data"]["description"]

    for finish in ("stop", 4096):  # a normal end, and a malformed non-string value
        msg = make_message("answer", usage=make_usage())
        msg["finish_reason"] = finish
        emitter = CapturingEmitter()
        run_async(f.outlet(make_body([msg], model="gpt-4o"), __event_emitter__=emitter, __model__=make_model_dict()))
        assert "truncated" not in emitter.statuses()[0]["data"]["description"]


def test_outlet_debug_payload_reports_finish_reason(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.debug_mode = True
    for finish, expected in (("tool_calls", '"finish_reason": "tool_calls"'), (None, '"finish_reason": null')):
        msg = make_message("answer", usage=make_usage())
        if finish is not None:
            msg["finish_reason"] = finish
        emitter = CapturingEmitter()
        run_async(f.outlet(make_body([msg], model="gpt-4o"), __event_emitter__=emitter, __model__=make_model_dict()))
        citation = next(e for e in emitter.events if e.get("type") == "citation")
        assert expected in citation["data"]["document"][0]


def test_render_total(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_total")
    assert r(valves, make_tokens(total=150), make_timing(), make_ctx(), make_cost(), None) == "Σ 150"
    assert r(valves, make_tokens(total=None), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_total_tokens = False
    assert r(valves, make_tokens(total=150), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_tokens_total_dedupes_and_marks_estimate(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_tokens_total")
    valves.icon_style = "emoji"
    shown = make_tokens(total=150, cumulative=4200)
    assert r(valves, shown, make_timing(), make_ctx(), make_cost(), None) == "🧮 4,200"
    # equal to the message Σ (first turn / unsaved chat) -> omitted, like 💰Σ
    assert r(valves, make_tokens(total=150, cumulative=150), make_timing(), make_ctx(), make_cost(), None) is None
    # no data -> omitted
    assert r(valves, make_tokens(cumulative=None), make_timing(), make_ctx(), make_cost(), None) is None
    # current turn has no Σ at all but history does -> still shown
    assert r(valves, make_tokens(total=None, cumulative=900), make_timing(), make_ctx(), make_cost(), None) == "🧮 900"
    estimated = make_tokens(total=150, cumulative=4200, cumulative_est=True)
    assert r(valves, estimated, make_timing(), make_ctx(), make_cost(), None) == "🧮 ≈4,200"
    valves.compact_numbers = True
    valves.icon_style = "simple"
    assert r(valves, shown, make_timing(), make_ctx(), make_cost(), None) == "ΣΣ 4.2k"
    valves.show_cumulative_tokens = False
    assert r(valves, shown, make_timing(), make_ctx(), make_cost(), None) is None


def test_render_reasoning_hidden_when_zero_or_none(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_reasoning")
    assert r(valves, make_tokens(reasoning=32), make_timing(), make_ctx(), make_cost(), None) == "🧠 32"
    # reasoning uses truthiness: 0 and None both omit
    assert r(valves, make_tokens(reasoning=0), make_timing(), make_ctx(), make_cost(), None) is None
    assert r(valves, make_tokens(reasoning=None), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_reasoning_tokens = False
    assert r(valves, make_tokens(reasoning=32), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_cached(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_cached")
    assert r(valves, make_tokens(cached=64), make_timing(), make_ctx(), make_cost(), None) == "💾 64"
    assert r(valves, make_tokens(cached=0), make_timing(), make_ctx(), make_cost(), None) is None
    assert r(valves, make_tokens(cached=None), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_cached_tokens = False
    assert r(valves, make_tokens(cached=64), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_audio_default_hidden_flag(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_audio")
    assert valves.show_audio_tokens is False  # default off
    assert r(valves, make_tokens(audio=10), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_audio_tokens = True
    assert r(valves, make_tokens(audio=10), make_timing(), make_ctx(), make_cost(), None) == "🔊 10"
    assert r(valves, make_tokens(audio=0), make_timing(), make_ctx(), make_cost(), None) is None
    assert r(valves, make_tokens(audio=None), make_timing(), make_ctx(), make_cost(), None) is None


def test_render_context(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_context")
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
    r = renderer(usage_display_module, "_render_time")
    assert r(valves, make_tokens(), make_timing(seconds=2.5, source="usage"), make_ctx(), make_cost(), None) == "⏱ 2.5s"
    assert r(valves, make_tokens(), make_timing(seconds=2.5, source="wall"), make_ctx(), make_cost(), None) == "⏱ ~2.5s"
    assert r(valves, make_tokens(), make_timing(seconds=None), make_ctx(), make_cost(), None) is None
    valves.show_generation_time = False
    assert r(valves, make_tokens(), make_timing(seconds=2.5, source="usage"), make_ctx(), make_cost(), None) is None


def test_render_tps(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_tps")
    assert r(valves, make_tokens(), make_timing(tps=42.37), make_ctx(), make_cost(), None) == "⚡ 42.4 t/s"
    assert r(valves, make_tokens(), make_timing(tps=0), make_ctx(), make_cost(), None) is None
    valves.show_tokens_per_second = False
    assert r(valves, make_tokens(), make_timing(tps=42.37), make_ctx(), make_cost(), None) is None


def test_render_cost_estimate_marker_and_threshold(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_cost")
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=1.5), None) == "💰 $1.50"
    assert (
        r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=1.5, message_est=True), None)
        == "💰 ≈$1.50"
    )
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=None), None) is None
    valves.cost_min_display = 0.01
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(message=0.005), None) is None


def test_render_cost_total_dedupes_when_equal_to_message(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_cost_total")
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


def test_render_balance_signs_a_negative_balance(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_balance")

    def line(balance: dict[str, Any] | None) -> str | None:
        return r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), None, None, balance)

    valves.show_balance = True
    assert line({"usd": 17.054976898}) == "💳 $17.05"
    assert line({"usd": 0.42}) == "💳 $0.4200"
    assert line({"usd": -3.1}) == "💳 -$3.10"  # OpenRouter lets an account run below zero
    assert line({"usd": None, "status": "error 403: refused"}) is None
    assert line(None) is None
    valves.icon_style = "simple"
    assert line({"usd": 17.05}) == "¤ $17.05"  # a glyph, unlike simple cost: both carry "$"
    valves.show_balance = False
    assert line({"usd": 17.05}) is None


def test_render_model_uses_base_model_id(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_model")
    model = make_model_dict("my-workspace-model", base="gpt-4o")
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), model) == "🤖 gpt-4o"
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), make_model_dict()) is None  # no info
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), None) is None
    valves.show_model_name = False
    assert r(valves, make_tokens(), make_timing(), make_ctx(), make_cost(), model) is None


def test_render_source_api_vs_estimate(usage_display_module: ModuleType, valves: Any) -> None:
    r = renderer(usage_display_module, "_render_source")
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


def test_cache_and_fresh_zero_cache_read_keeps_proxy_subset_semantics(usage_display_module: ModuleType) -> None:
    """A measured `cached_tokens: 0` is a detail key, not an absent one (issue #38).

    Cache-write-only turn behind a proxy that folds Anthropic cache into prompt_tokens (LiteLLM): the detail
    dict says 0 cache reads, the top-level cache_creation carries the write. Read as Anthropic-native, the
    write was added on top of an input that already contains it, doubling Σ, 📐 and the cost estimate.
    """
    f = usage_display_module.Filter()
    usage = make_usage(
        input_tokens=5100,
        output_tokens=50,
        prompt_tokens=5100,
        completion_tokens=50,
        prompt_tokens_details={"cached_tokens": 0},
        cache_creation_input_tokens=5000,
    )
    out = f._cache_and_fresh(usage, reported_input=5100)
    assert out["cached"] is None  # top-level fields win the value; no top-level read was reported
    assert out["cache_write"] == 5000
    assert out["input_has_cache"] is True
    assert out["fresh_input"] == 100

    tokens = f._extract_tokens(usage, make_message("a", usage=usage), [], "")
    assert tokens["total"] == 5150
    assert tokens["context_used"] == 5150

    # Same when the zero arrives under the Responses API detail group.
    responses = {"cache_creation_input_tokens": 5000, "input_tokens_details": {"cached_tokens": 0}}
    assert f._cache_and_fresh(responses, reported_input=5100)["input_has_cache"] is True

    # A zero detail beside a positive top-level read is an OpenAI-shaped skeleton, not a fold (a fold copies
    # the read into the detail): it stays Anthropic-native, cache on top, as before 2.9.1.
    skeleton = {"prompt_tokens_details": {"cached_tokens": 0}, "cache_read_input_tokens": 4000}
    out = f._cache_and_fresh(skeleton, reported_input=100)
    assert out["input_has_cache"] is False
    assert out["fresh_input"] == 100


def test_extract_tokens_openai_chat(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = make_usage(
        input_tokens=100,
        output_tokens=50,
        completion_tokens_details={"reasoning_tokens": 8, "audio_tokens": 2},
        prompt_tokens_details={"cached_tokens": 20, "audio_tokens": 1},
    )
    t = f._extract_tokens(usage, make_message(), [], "")
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
    t = f._extract_tokens(usage, make_message(), [], "")
    assert t["reasoning"] == 5
    assert t["cached"] == 10


def test_extract_tokens_ollama_backup_keys(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    usage = {"prompt_eval_count": 77, "eval_count": 22}  # Ollama naming, no normalized triple
    t = f._extract_tokens(usage, make_message(), [], "")
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
    t = f._extract_tokens(None, assistant, [user, assistant], "gpt-4o")
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
    model_id = usage_display_module._resolve_model_id(model, make_body())  # as outlet resolves it
    t = f._extract_tokens(None, assistant, [user, assistant], model_id)
    assert t["output"] == 3
    assert t["input"] == 2


def test_estimate_tokens_skips_output_when_response_text_empty(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_tiktoken(monkeypatch, usage_display_module)
    f = usage_display_module.Filter()
    assistant = make_message(content="")  # no content, no output -> empty response text
    user = make_message("alpha beta", role="user")
    t = f._extract_tokens(None, assistant, [user, assistant], "gpt-4o")
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
    f._estimate_tokens(result, assistant, [user, assistant], "gpt-4o")
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
    t = f._extract_tokens(None, assistant, [user1, assistant, user2], "gpt-4o")
    assert t["input"] == 3  # "beta gamma delta" -> 3 words


def test_estimate_tokens_last_user_message_none_when_absent(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """count_all_messages_for_input=False and no user message in the list -> input stays None."""
    install_fake_tiktoken(monkeypatch, usage_display_module)
    f = usage_display_module.Filter()
    f.valves.count_all_messages_for_input = False
    assistant = make_message("resp one two")
    t = f._extract_tokens(None, assistant, [assistant], "gpt-4o")
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


# --- running chat token total ----------------------------------------------------- #


def test_cumulative_tokens_sums_history_and_current_cache_aware(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    anthropic_native = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 30}  # cache on top
    openai_subset = make_usage(input_tokens=100, output_tokens=50, prompt_tokens_details={"cached_tokens": 40})
    current = make_message("now", usage=make_usage())
    messages = [
        make_message("q1", role="user"),
        make_message("a1", usage=anthropic_native),
        make_message("q2", role="user"),
        make_message("a2", usage=openai_subset),
        current,
    ]
    cum = f._cumulative_tokens(messages, current, make_tokens(total=150, is_api=True))
    # 180 (100+30+50) + 150 (cache is a subset) + 150 (current Σ as displayed)
    assert cum == {"total": 480, "estimated": False, "messages_counted": 3, "messages_skipped_no_usage": 0}


def test_cumulative_tokens_skips_history_without_usage(usage_display_module: ModuleType) -> None:
    """Unsaved chats hand outlet a role+content-only history; malformed entries are skipped, never fatal."""
    f = usage_display_module.Filter()
    current = make_message("now", usage=make_usage())
    messages: list[Any] = [
        "not-a-dict",
        make_message("a0"),  # no usage key (temporary chat history)
        make_message("a1", usage="bad"),  # type: ignore[arg-type]
        make_message("a2", usage={"input_tokens": "bad"}),
        make_message("a3", usage=make_usage(input_tokens=10, output_tokens=5)),
        current,
    ]
    cum = f._cumulative_tokens(messages, current, make_tokens(total=150))
    assert cum["total"] == 165
    assert cum["messages_counted"] == 2
    assert cum["messages_skipped_no_usage"] == 3


def test_cumulative_tokens_estimated_current_turn_and_empty(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    current = make_message("now")
    history = make_message("a1", usage=make_usage())
    est = f._cumulative_tokens([history, current], current, make_tokens(total=12, is_api=False))
    assert est["total"] == 162
    assert est["estimated"] is True
    # nothing countable anywhere -> None (renderer omits), not a misleading 0
    empty = f._cumulative_tokens([current], current, make_tokens(total=None, input=None, output=None, is_api=False))
    # the data-less current turn is reported as skipped, so the counters add up to the assistant messages
    assert empty == {"total": None, "estimated": False, "messages_counted": 0, "messages_skipped_no_usage": 1}


def test_extract_tokens_fills_cumulative(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    current = make_message("a2", usage=make_usage(input_tokens=300, output_tokens=20))
    messages = [make_message("a1", usage=make_usage()), make_message("q", role="user"), current]
    t = f._extract_tokens(current["usage"], current, messages, "")
    assert t["total"] == 320
    assert t["cumulative"] == 470
    assert t["cumulative_est"] is False


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
    assert timing == {
        "seconds": None,
        "tps": None,
        "source": None,
        "prefill_tps": None,
        "prefill_seconds": None,
        "prefill_tokens": None,
        "prefill_source": None,
    }


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
    assert f._parse_llama_swap(data, "some-model") == (8192, None)


def test_parse_llama_swap_skips_non_dict_rows(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {"running": [123, {"cmd": "--ctx-size 999"}]}
    assert f._parse_llama_swap(data, "some-model") == (999, None)


def test_parse_llama_swap_cmd_as_list(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {"running": [{"cmd": ["llama-server", "--ctx-size", "4096"]}]}
    assert f._parse_llama_swap(data, "some-model") == (4096, None)


def test_parse_llama_swap_malformed_input_returns_none(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._parse_llama_swap(None, "some-model") is None
    assert f._parse_llama_swap({"running": []}, "some-model") is None
    assert f._parse_llama_swap({"running": "not-a-list"}, "some-model") is None
    assert f._parse_llama_swap({"running": [{"cmd": "llama-server --no-ctx-flag"}]}, "some-model") is None


def test_parse_llama_swap_picks_the_called_model_among_several(usage_display_module: ModuleType) -> None:
    """llama-swap runs model groups side by side; the window must be the called model's, not the first row's."""
    f = usage_display_module.Filter()
    data = {
        "running": [
            {"model": "gemma-3-4b", "cmd": "llama-server --ctx-size 8192"},
            {"model": "qwen3-30b", "cmd": "llama-server --ctx-size 32768"},
        ]
    }
    assert f._parse_llama_swap(data, "qwen3-30b") == (32768, "qwen3-30b")  # exact id
    assert f._parse_llama_swap(data, "llama-swap.qwen3-30b") == (32768, "qwen3-30b")  # OWUI connection prefix
    assert f._parse_llama_swap(data, "mistral-7b") is None  # several rows, none matches: no guessing


def test_parse_llama_swap_no_loose_substring_match(usage_display_module: ModuleType) -> None:
    """A matched row outranks the tables, so a short local id must not claim a longer cloud id."""
    f = usage_display_module.Filter()
    data = {
        "running": [
            {"model": "qwen", "cmd": "llama-server --ctx-size 8192"},
            {"model": "gemma", "cmd": "llama-server --ctx-size 4096"},
        ]
    }
    assert f._parse_llama_swap(data, "openrouter.qwen3-max") is None  # "qwen" is a substring, not the model
    assert f._parse_llama_swap(data, "local/qwen") == (8192, "qwen")  # path suffix still matches


def test_parse_llamacpp_models_matches_id_or_alias(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    data = {
        "data": [
            {"id": "gemma-3-4b", "aliases": [], "meta": {"n_ctx": 8192, "n_ctx_train": 131072}},
            {"id": "Qwen3.6-27B-UD-Q5_K_XL", "aliases": ["qwen-fast"], "meta": {"n_ctx": 16384}},
            {"id": "not-loaded", "meta": {"n_ctx": 0}},  # router mode: unloaded -> skipped
            "junk",
        ]
    }
    assert f._parse_llamacpp_models(data, "qwen3.6-27b-ud-q5_k_xl") == (16384, "qwen3.6-27b-ud-q5_k_xl")
    assert f._parse_llamacpp_models(data, "llamacpp.qwen-fast") == (16384, "qwen-fast")
    assert f._parse_llamacpp_models(data, "gpt-4o") is None  # two rows, no match: no guessing
    single = {"data": [{"id": "whatever", "aliases": "not-a-list", "meta": {"n_ctx": 4096}}]}
    assert f._parse_llamacpp_models(single, "my-alias") == (4096, None)
    assert f._parse_llamacpp_models({"data": "nope"}, "x") is None
    assert f._parse_llamacpp_models(None, "x") is None


def test_parse_llamacpp_props_names_the_loaded_model(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    props = {"model_path": "/models/Qwen3.6-27B-UD-Q5_K_XL.gguf", "default_generation_settings": {"n_ctx": 16384}}
    assert f._parse_llamacpp_props(props, "qwen3.6-27b-ud-q5_k_xl") == (16384, "qwen3.6-27b-ud-q5_k_xl")
    assert f._parse_llamacpp_props({"model_alias": "qwen", "n_ctx": 8192}, "qwen") == (8192, "qwen")
    assert f._parse_llamacpp_props({"model_alias": "qwen", "n_ctx": 8192}, "gpt-4o") == (8192, None)
    assert f._parse_llamacpp_props({"n_ctx": 0}, "qwen") is None  # router mode placeholder
    assert f._parse_llamacpp_props(["not", "a", "dict"], "qwen") is None


def test_parse_llama_swap_single_row_is_used_despite_id_mismatch(usage_display_module: ModuleType) -> None:
    """One running model is unambiguous, so an alias or differently-spelled OWUI id still gets its window."""
    f = usage_display_module.Filter()
    data = {"running": [{"model": "qwen3-30b", "cmd": "llama-server --ctx-size 32768"}]}
    assert f._parse_llama_swap(data, "my-alias") == (32768, None)  # flagged unmatched


# --- stats assembly, provider guess, sanitize, valves snapshot ----------------- #


def test_build_stats_respects_display_order_and_gating(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.display_order = "output, input"
    f.valves.show_total_tokens = False
    f.valves.icon_style = "off"
    tokens = make_tokens(input=100, output=50, total=150)
    parts = f._build_stats(usage_display_module._Stats(f.valves, tokens, make_timing(), make_ctx(), make_cost(), None))
    assert parts[0] == "50"  # output before input
    assert parts[1] == "100"
    assert "150" not in parts  # total gated off


def test_provider_guess_local_and_arena_authoritative(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    assert f._provider_guess({"owned_by": "ollama"}, "llama3") == "ollama/local"
    # A local connection is not necessarily Ollama (issue #38): its listed provider, else the id's family,
    # else plain "local" (connection_type is reported next to it in the debug payload anyway).
    assert f._provider_guess({"connection_type": "local", "provider": "llama.cpp"}, "qwen3") == "llama.cpp"
    assert f._provider_guess({"connection_type": "local"}, "qwen3-coder") == "qwen"
    assert f._provider_guess({"connection_type": "local"}, "my-finetune") == "local"
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


def test_sanitize_model_backend_context_numbers_only(usage_display_module: ModuleType) -> None:
    """The debug payload shows the running vs trained window from the listing row, and nothing else from it."""
    f = usage_display_module.Filter()
    row = {"id": "qwen", "meta": {"n_ctx": 16384, "n_ctx_train": 262144, "model_path": "/home/secret/q.gguf"}}
    out = f._sanitize_model({"id": "qwen", "openai": row, **row}, None, "qwen")
    assert out["backend_context"] == {"n_ctx": 16384, "n_ctx_train": 262144, "max_model_len": None}
    assert "secret" not in str(out)
    vllm = f._sanitize_model({"id": "m", "max_model_len": 32768}, None, "m")
    assert vllm["backend_context"] == {"n_ctx": None, "n_ctx_train": None, "max_model_len": 32768}


def test_sanitize_model_whitelist_only_no_secrets(usage_display_module: ModuleType) -> None:
    """Security contract: only whitelisted identity/provider fields leave this method."""
    f = usage_display_module.Filter()
    model = {
        "id": "gpt-4o",
        "name": "GPT-4o",
        "info": {
            "base_model_id": "gpt-4o-mini",
            "meta": {
                "capabilities": {"status_updates": False, "citations": True, "vision": True},
                "description": "leaked description",
            },
        },
        "owned_by": "openai",
        "connection_type": "external",
        "provider": "openai",
        "loaded": True,
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
        "backend_context",
        "live_backend",
        "provider_guess",
        "capabilities",
        "loaded",
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
    # Only the three render/usage switches; unset ones are null (= OWUI's default), others never copied.
    assert out["capabilities"] == {"status_updates": False, "citations": True, "usage": None}
    assert out["loaded"] is True
    assert f._sanitize_model({"id": "raw"}, None, "raw")["capabilities"] is None  # plain model: no info at all
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
    assert out["backend_context"] is None  # no /v1/models row numbers on this model
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
    """A real, truthy metadata dict that carries no chat_id/message_id to reconstruct a key from."""
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
    assert "🧮" not in desc  # single turn: chat total == message Σ -> deduped


def test_outlet_status_line_shows_chat_token_total_after_total(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    messages = [
        make_message("q1", role="user"),
        make_message("a1", usage=make_usage(input_tokens=1000, output_tokens=200)),
        make_message("q2", role="user"),
        make_message("a2", usage=make_usage(input_tokens=1300, output_tokens=100)),
    ]
    emitter = CapturingEmitter()
    run_async(f.outlet(make_body(messages, model="gpt-4o"), __event_emitter__=emitter, __model__=make_model_dict()))
    parts = emitter.statuses()[0]["data"]["description"].split(f.valves.separator)
    assert parts[2:4] == ["Σ 1,400", "🧮 2,600"]  # 1,200 + 1,400, right after the message Σ


def test_outlet_chat_token_total_marks_estimated_current_turn(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API history + a usage-less current turn estimated by tiktoken -> the chat total reaches the line as ≈."""
    mod = usage_display_module
    install_fake_tiktoken(monkeypatch, mod)  # 1 token per word
    f = mod.Filter()
    _patch_no_network(monkeypatch, f)
    messages = [
        make_message("q1", role="user"),
        make_message("a1", usage=make_usage(input_tokens=1000, output_tokens=200)),
        make_message("q2", role="user"),
        make_message("three word answer"),  # current turn: no usage
    ]
    emitter = CapturingEmitter()
    run_async(f.outlet(make_body(messages, model="gpt-4o"), __event_emitter__=emitter, __model__=make_model_dict()))
    desc = emitter.statuses()[0]["data"]["description"]
    # estimate: input = q1 a1 q2 (3 words), output = 3 words -> Σ 6; chat total 1,200 + 6
    assert "Σ 6" in desc
    assert "🧮 ≈1,206" in desc


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_outlet_survives_non_finite_usage_in_history_and_current(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch, bad: float
) -> None:
    """A non-finite count (valid in Python json) used to raise from int() on the unguarded token path."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    monkeypatch.setattr(usage_display_module, "_TIKTOKEN_AVAILABLE", False, raising=False)
    history = make_message("a1", usage=make_usage(input_tokens=bad, output_tokens=7))
    current = make_message("a2", usage=make_usage(input_tokens=100, output_tokens=bad))
    emitter = CapturingEmitter()
    body = make_body([history, make_message("q", role="user"), current], model="gpt-4o")
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict()))
    desc = emitter.statuses()[0]["data"]["description"]
    assert "⬆︎ 100" in desc  # the finite counters still render
    assert "🧮 107" in desc  # 7 (history, bad input dropped) + 100 (current, bad output dropped)


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


def test_outlet_does_no_work_where_nothing_can_be_shown(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No emitter (raw API call without chat_id) or a channel: chat -> no resolver runs (issue #38).

    The resolvers reach the network (connection probe, OpenRouter balance, models.dev); OWUI still runs the
    outlet there, and on the non-streaming API path the HTTP response waits for it.
    """
    f = usage_display_module.Filter()
    calls: list[str] = []

    async def _tracked(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        calls.append("resolver")
        return {"usd": None, "status": "off", "key_source": None, "on_openrouter": None}

    monkeypatch.setattr(f, "_resolve_balance", _tracked)
    _patch_no_network(monkeypatch, f)
    body = make_body([make_message("a", usage=make_usage())], model="gpt-4o")

    assert run_async(f.outlet(body, __metadata__={"chat_id": "", "message_id": "m1"})) is body
    channel = {"chat_id": "channel:c1", "message_id": "m1"}
    emitter = CapturingEmitter()
    assert run_async(f.outlet(body, __event_emitter__=emitter, __metadata__=channel)) is body
    body_only = make_body([make_message("a", usage=make_usage())], model="gpt-4o")
    body_only["chat_id"] = "channel:c1"  # the outlet body carries chat_id too
    run_async(f.outlet(body_only, __event_emitter__=emitter))
    assert calls == []
    assert emitter.events == []

    # debug mode still runs in a channel chat: its stdout copy is the only output that survives there.
    f.valves.debug_mode = True
    run_async(f.outlet(body, __event_emitter__=emitter, __metadata__=channel))
    assert calls == ["resolver"]
    assert "[TUD debug]" in capsys.readouterr().out

    # An ordinary saved chat is unaffected.
    f.valves.debug_mode = False
    saved = CapturingEmitter()
    run_async(f.outlet(body, __event_emitter__=saved, __metadata__={"chat_id": "c1", "message_id": "m1"}))
    assert len(saved.statuses()) == 1


def test_outlet_releases_the_inlet_timing_entry_on_every_early_return(usage_display_module: ModuleType) -> None:
    """An outlet that returns early must still pop the inlet's `_request_timings` entry, or it leaks.

    Raw API traffic (no chat_id -> no emitter) returns early on every request since 2.9.1, so an entry left
    behind there would make the module dict grow with every call.
    """
    timings = usage_display_module._request_timings
    f = usage_display_module.Filter()

    class _Off:
        enabled = False

    for meta, kwargs in (
        ({"chat_id": "", "message_id": "m1"}, {}),  # raw API: no emitter
        ({"chat_id": "c1", "message_id": "m2"}, {"__user__": {"valves": _Off()}}),  # stats switched off
    ):
        run_async(f.inlet(make_body([make_message("hi", role="user")]), __metadata__=meta))
        assert meta["_tud_timing_key"] in timings
        run_async(f.outlet(make_body([make_message("a", usage=make_usage())]), __metadata__=meta, **kwargs))
        assert meta["_tud_timing_key"] not in timings


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


def test_context_used_is_last_round_not_cross_round_sum(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (#26): context 'used' is the LAST round's request, not the cross-round Σ."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_override = 8192
    tokens = make_tokens(
        total=2750,  # Σ of round1 (1050) + round2 (1700)
        input=2400,  # Σ of the two prompts
        output=350,  # Σ of the two completions
        context_used=1700,  # last round only: 1400 + 300
    )
    ctx = run_async(f._resolve_context(make_body(model="gpt-4o"), {}, make_model_dict("gpt-4o"), tokens))
    assert ctx["size"] == 8192
    assert ctx["used"] == 1700  # 21%, not the summed 2750 (34%)
    assert ctx["used_source"] == "last_round"


def test_context_used_falls_back_to_total_without_per_round_prompt(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When context_used is None (tiktoken-estimate path) the summed total is used, and debug says so."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_override = 8192
    tokens = make_tokens(total=150, context_used=None)
    ctx = run_async(f._resolve_context(make_body(model="gpt-4o"), {}, make_model_dict("gpt-4o"), tokens))
    assert (ctx["used"], ctx["used_source"]) == (150, "total")
    hand_built = make_tokens(total=None, input=100, output=50, context_used=None)
    ctx = run_async(f._resolve_context(make_body(model="gpt-4o"), {}, make_model_dict("gpt-4o"), hand_built))
    assert (ctx["used"], ctx["used_source"]) == (150, "input+output")
    empty = make_tokens(total=None, input=None, output=None, context_used=None)
    ctx = run_async(f._resolve_context(make_body(model="gpt-4o"), {}, make_model_dict("gpt-4o"), empty))
    assert (ctx["used"], ctx["used_source"]) == (None, None)


@pytest.mark.parametrize(
    ("usage", "bag", "expected"),
    [
        pytest.param(
            {"input_tokens": 1000, "output_tokens": 100, "prompt_tokens": 1000, "completion_tokens": 100},
            {"input_has_cache": True, "cached": None, "cache_write": None},
            1100,
            id="single-round",
        ),
        pytest.param(
            {"input_tokens": 2400, "output_tokens": 350, "prompt_tokens": 1400, "completion_tokens": 300},
            {"input_has_cache": True, "cached": None, "cache_write": None},
            1700,
            id="multi-round-last-wins",
        ),
        pytest.param(
            {"input_tokens": 900, "output_tokens": 300, "cache_read_input_tokens": 2000},
            {"input_has_cache": False, "cached": 2000, "cache_write": None},
            3200,
            id="anthropic-native-cache-added-back",
        ),
        pytest.param(
            None, {"input_has_cache": False, "cached": None, "cache_write": None}, None, id="no-usage-estimate-path"
        ),
        pytest.param({"total_tokens": 150}, {"input_has_cache": True}, None, id="no-prompt-or-completion"),
    ],
)
def test_last_round_context_helper(
    usage_display_module: ModuleType, usage: dict[str, Any] | None, bag: dict[str, Any], expected: int | None
) -> None:
    assert usage_display_module.Filter._last_round_context(usage, bag) == expected


# `usage` exactly as Open WebUI 0.11.4's merge_usage leaves it after two rounds (generated by feeding
# per-round provider payloads through the real utils/response.py): input/output/total and the
# *_tokens_details are summed, prompt/completion_tokens and top-level Anthropic cache are the last round.
_MERGED_TWO_ROUND_USAGE: dict[str, tuple[dict[str, Any], str]] = {
    "chat-completions": (
        {
            "input_tokens": 2400,
            "output_tokens": 350,
            "total_tokens": 2750,
            "prompt_tokens": 1400,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 1800},
        },
        "📐 1.7k/10.0k (17%)",  # the summed Σ would read 2.8k (28%)
    ),
    "responses-api": (
        {
            "input_tokens": 2500,
            "output_tokens": 240,
            "total_tokens": 2740,
            "prompt_tokens": 1500,  # absent from the raw rounds; merge_usage derives it from input_tokens
            "completion_tokens": 200,
            "input_tokens_details": {"cached_tokens": 1000},
        },
        "📐 1.7k/10.0k (17%)",
    ),
    "anthropic-native": (
        {
            "input_tokens": 400,
            "output_tokens": 250,
            "total_tokens": 650,
            "prompt_tokens": 300,
            "completion_tokens": 200,
            "cache_read_input_tokens": 2100,
            "cache_creation_input_tokens": 0,
        },
        "📐 2.6k/10.0k (26%)",  # 300 + 2100 cache + 200; the summed Σ would be 2750
    ),
}


@pytest.mark.parametrize("shape", list(_MERGED_TWO_ROUND_USAGE))
def test_outlet_context_reads_last_round_of_owui_merged_usage(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """End-to-end (#26): a merged multi-round usage renders the last request in 📐, and debug names it."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_override = 10_000
    f.valves.debug_mode = True
    usage, expected = _MERGED_TWO_ROUND_USAGE[shape]
    body = make_body([make_message("q", role="user"), make_message("a", usage=usage)], model="gpt-4o")
    emitter = CapturingEmitter()
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-4o")))
    assert expected in emitter.statuses()[0]["data"]["description"]
    doc = next(e for e in emitter.events if e.get("type") == "citation")["data"]["document"][0]
    assert '"used_source": "last_round"' in doc


# `usage` exactly as Open WebUI 0.12.5 leaves it (generated by feeding per-call payloads through the real
# utils/response.py: update_usage_snapshot per call, merge_usage across calls, and the middleware's
# `{**usage, "incomplete": True}` for a later call that reported no usage).
_OWUI_0125_USAGE: dict[str, dict[str, Any]] = {
    # Three Anthropic-native calls (a pass-through pipe), cache read 9000/9500/9800, write 500/300/0: the cache
    # is summed; the last request really held 100 + 9800 + 150 = 10050.
    "anthropic-native-3-calls": {
        "cache_read_input_tokens": 28300,
        "cache_creation_input_tokens": 800,
        "incomplete": False,
        "input_tokens": 600,
        "output_tokens": 290,
        "total_tokens": 890,
        "prompt_tokens": 100,
        "completion_tokens": 150,
    },
    # One Chat Completions call with usage, then one without any.
    "later-call-without-usage": {
        "input_tokens": 1000,
        "output_tokens": 100,
        "total_tokens": 1100,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "incomplete": True,
    },
    # Two complete Chat Completions calls: merged, but nothing is missing.
    "two-complete-calls": {
        "input_tokens": 2300,
        "output_tokens": 150,
        "total_tokens": 2450,
        "prompt_tokens": 1300,
        "completion_tokens": 50,
        "incomplete": False,
    },
}


def _outlet_line(f: Any, usage: dict[str, Any], metadata: dict[str, Any] | None = None, past: Any = None) -> str:
    messages = [make_message("q", role="user")]
    if past is not None:
        messages += [past, make_message("q2", role="user")]
    messages.append(make_message("a", usage=usage))
    emitter = CapturingEmitter()
    body = make_body(messages, model="gpt-4o")
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-4o"), __metadata__=metadata))
    return str(emitter.statuses()[0]["data"]["description"])


def test_outlet_marks_owui_0125_incomplete_usage_approximate(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model call of the turn reported no count (issue #39): every token counter gets ≈, and only then."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_override = 10_000
    line = _outlet_line(f, _OWUI_0125_USAGE["later-call-without-usage"])
    assert "⬆︎ ≈1,000 · ⬇︎ ≈100 · Σ ≈1,100" in line
    assert "📐 ≈1.1k/10.0k (11%)" in line

    clean = _outlet_line(f, _OWUI_0125_USAGE["two-complete-calls"])
    assert "≈" not in clean  # a merge alone (incomplete: false) is exact
    assert "📐 1.4k/10.0k" in clean

    # 🧮: a past turn with partial usage makes the chat total approximate too.
    past = make_message("old", usage=_OWUI_0125_USAGE["later-call-without-usage"])
    assert "🧮 ≈" in _outlet_line(f, _OWUI_0125_USAGE["two-complete-calls"], past=past)


def test_outlet_splits_summed_anthropic_native_cache_per_model_call(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OWUI 0.12.5 sums Anthropic-native cache over the turn's calls (issue #39): 📐 adds the per-call average.

    The `request` hook counts the calls. The average runs a little low (later calls read a longer prefix), so
    📐 carries ≈; without a count (OWUI < 0.11.2 never calls the hook) the sum stays, still with ≈.
    """
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_override = 100_000
    usage = _OWUI_0125_USAGE["anthropic-native-3-calls"]

    meta: dict[str, Any] = {"chat_id": "c1", "message_id": "m1"}
    for _ in range(3):
        run_async(f.request({"messages": []}, __metadata__=meta))
    assert meta["_tud_model_calls"] == 3
    line = _outlet_line(f, usage, meta)
    assert "📐 ≈9.9k/100.0k (10%)" in line  # 100 + 150 + (28300 + 800) / 3; really 10050
    assert "⬆︎ 600 ·" in line  # the counters themselves are complete: no ≈ there

    assert "📐 ≈29.4k/100.0k (29%)" in _outlet_line(f, usage, {"chat_id": "c1", "message_id": "m2"})

    # Before 0.12.5 the cache was last-call already (no `incomplete` key): unchanged, exact.
    last_call = {k: v for k, v in usage.items() if k != "incomplete"}
    assert "📐 29.4k/100.0k" in _outlet_line(f, last_call, dict(meta))

    # Debug names the call count and the estimate.
    f.valves.debug_mode = True
    emitter = CapturingEmitter()
    body = make_body([make_message("q", role="user"), make_message("a", usage=usage)], model="gpt-4o")
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-4o"), __metadata__=meta))
    doc = next(e for e in emitter.events if e.get("type") == "citation")["data"]["document"][0]
    assert '"model_calls": 3' in doc
    assert '"used_est": true' in doc


# Non-streaming Responses API `usage` exactly as Open WebUI 0.13.0 leaves it (generated by feeding raw Responses
# usage through the real utils/response.py: convert_responses_usage_to_chat_completions per call, merge_usage
# across calls): the input_*/output_* keys are copied to prompt_*/completion_*, so both detail families are there.
_OWUI_0130_RESPONSES_USAGE: dict[str, dict[str, Any]] = {
    "one-call": {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 800},
        "output_tokens": 200,
        "output_tokens_details": {"reasoning_tokens": 50},
        "total_tokens": 1200,
        "prompt_tokens": 1000,
        "prompt_tokens_details": {"cached_tokens": 800},
        "completion_tokens": 200,
        "completion_tokens_details": {"reasoning_tokens": 50},
    },
    # A second call of 1500 in (1200 cached) / 100 out (30 reasoning): each family is summed on its own.
    "two-calls": {
        "input_tokens": 2500,
        "input_tokens_details": {"cached_tokens": 2000},
        "output_tokens": 300,
        "output_tokens_details": {"reasoning_tokens": 80},
        "total_tokens": 2800,
        "prompt_tokens": 1500,
        "prompt_tokens_details": {"cached_tokens": 2000},
        "completion_tokens": 100,
        "completion_tokens_details": {"reasoning_tokens": 80},
        "incomplete": False,
    },
}


@pytest.mark.parametrize(
    ("shape", "counters", "cached", "reasoning", "context"),
    [
        ("one-call", "⬆︎ 1,000 · ⬇︎ 200 · Σ 1,200", 800, 50, "📐 1.2k/10.0k (12%)"),
        ("two-calls", "⬆︎ 2,500 · ⬇︎ 300 · Σ 2,800", 2000, 80, "📐 1.6k/10.0k (16%)"),
    ],
)
def test_outlet_reads_owui_0130_responses_usage_with_both_detail_families_once(  # noqa: PLR0913, PLR0917 - parametrized case
    usage_display_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    counters: str,
    cached: int,
    reasoning: int,
    context: str,
) -> None:
    """OWUI 0.13.0 copies Responses detail keys to the Chat Completions names: each counter is read once."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_override = 10_000
    usage = _OWUI_0130_RESPONSES_USAGE[shape]
    t = f._extract_tokens(usage, make_message(), [], "")
    assert (t["cached"], t["reasoning"], t["fresh_input"]) == (cached, reasoning, t["input"] - cached)
    line = _outlet_line(f, usage)
    assert counters in line
    assert context in line  # the last call: prompt_tokens + completion_tokens
    assert "≈" not in line


def test_request_hook_times_from_the_first_model_call(usage_display_module: ModuleType) -> None:
    """The wall clock starts at the first model call, after RAG and web search (OWUI 0.11.2+, issue #39)."""
    f = usage_display_module.Filter()
    meta: dict[str, Any] = {"chat_id": "c1", "message_id": "m1"}
    run_async(f.inlet(make_body([make_message("hi", role="user")]), __metadata__=meta))
    meta["_tud_start"] -= 60  # inlet 60 s ago: RAG and web search took a minute
    body: dict[str, Any] = {"messages": []}
    assert run_async(f.request(body, __metadata__=meta)) is body
    first = meta["_tud_model_start"]
    run_async(f.request(body, __metadata__=meta))  # a tool-loop round
    assert meta["_tud_model_start"] == first  # the first call stays the start
    assert meta["_tud_model_calls"] == 2
    meta["_tud_model_start"] -= 5
    elapsed = f._resolve_wall_clock({}, meta)
    assert elapsed is not None
    assert 5 <= elapsed < 30  # well below the 60 s an inlet start would give, with CI-runner slack

    # No hook call (OWUI < 0.11.2): from inlet, as before.
    old: dict[str, Any] = {"chat_id": "c1", "message_id": "m2"}
    run_async(f.inlet(make_body([make_message("hi", role="user")]), __metadata__=old))
    old["_tud_start"] -= 60
    elapsed = f._resolve_wall_clock({}, old)
    assert elapsed is not None
    assert elapsed >= 60
    assert run_async(f.request(body)) is body  # no metadata: nothing to stamp, body passes through


def test_outlet_context_on_owui_010_merged_usage_stays_summed(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OWUI 0.10.x merge_usage summed prompt/completion_tokens too: no per-round numbers, so 📐 keeps the Σ."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_override = 10_000
    usage = make_usage(
        input_tokens=2400, output_tokens=350, total_tokens=2750, prompt_tokens=2400, completion_tokens=350
    )
    body = make_body([make_message("q", role="user"), make_message("a", usage=usage)], model="gpt-4o")
    emitter = CapturingEmitter()
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-4o")))
    assert "(28%)" in emitter.statuses()[0]["data"]["description"]  # 2750 / 10000, same as before the fix


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


def test_context_size_map_valve_wins_as_user_map_tier(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """context_size_map resolves in its own 'user_map' tier (above models.dev/static), not merged."""
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
    assert ctx["source"] == "user_map"
    assert ctx["matched_key"] == "gpt-4o"


def test_context_size_map_beats_modelsdev(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reported bug: an explicit context_size_map wins over the live models.dev fetch.

    models.dev is monkeypatched to a match AND fetch is enabled, yet the user's map takes
    precedence — mirroring how price_map beats models.dev for cost.
    """
    f = usage_display_module.Filter()

    async def _map() -> dict[str, Any]:
        return {"stepfun/step-3.7-flash": 256000}  # what models.dev would report

    monkeypatch.setattr(f, "_modelsdev_map", _map)
    f.valves.fetch_context_from_modelsdev = True
    f.valves.context_size_map = '{"stepfun/step-3.7-flash:free": 260000}'  # user override
    size, prov = run_async(f._context_size_for("stepfun/step-3.7-flash:free-mapwins-check", None))
    assert size == 260000
    assert prov["source"] == "user_map"
    assert prov["matched_key"] == "stepfun/step-3.7-flash:free"


def test_context_size_map_zero_value_falls_through(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A falsy map size (0) is skipped and resolution falls through to the automatic sources."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.context_size_map = '{"gpt-4o": 0}'
    ctx = run_async(
        f._resolve_context(
            make_body(model="gpt-4o-mapzero-check"),
            {},
            make_model_dict("gpt-4o-mapzero-check"),
            make_tokens(total=100),
        )
    )
    assert ctx["size"] == 128000  # static gpt-4o, not the 0 from the map
    assert ctx["source"] == "static_table"


def test_context_size_map_table_skips_infinite_entry(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.context_size_map = '{"bad": Infinity, "good": 4096}'  # json.loads accepts Infinity
    assert f._context_size_map_table() == {"good": 4096}


def test_context_size_map_table_parsing(usage_display_module: ModuleType) -> None:
    """_context_size_map_table: per-entry tolerant, lowercased keys; bad shapes -> empty."""
    f = usage_display_module.Filter()
    f.valves.context_size_map = '{"GPT-4o": 999, "bad": "nope", "b": 200}'
    assert f._context_size_map_table() == {"gpt-4o": 999, "b": 200}  # bad value dropped, keys lowered
    f.valves.context_size_map = "[1, 2, 3]"  # valid JSON, not an object
    assert f._context_size_map_table() == {}
    f.valves.context_size_map = "{not valid json"
    assert f._context_size_map_table() == {}
    f.valves.context_size_map = ""
    assert f._context_size_map_table() == {}


def test_context_table_is_static_only(usage_display_module: ModuleType) -> None:
    """_context_table no longer merges the user map (map is its own tier now)."""
    f = usage_display_module.Filter()
    f.valves.context_size_map = '{"gpt-4o": 999}'
    assert f._context_table() == usage_display_module._STATIC_CONTEXT_SIZES


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
    assert ctx == {
        "size": None,
        "used": None,
        "used_source": None,
        "used_est": False,
        "source": "disabled",
        "matched_key": None,
    }


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

    async def _probe(_model_id: str) -> tuple[int, None]:
        return 65536, None  # the backend's only model, under a different id

    monkeypatch.setattr(f, "_modelsdev_map", _empty_map)
    monkeypatch.setattr(f, "_modelsdev_prices_map", _empty_map)
    monkeypatch.setattr(f, "_probe_context", _probe)
    f.valves.llamacpp_url = "http://localhost:8080"
    size, prov = run_async(f._context_size_for("totally-unmatched-model-probe-check", None))
    assert size == 65536
    assert prov == {"source": "probe", "matched_key": None}


def _llamacpp_model(model_id: str, n_ctx: int) -> dict[str, Any]:
    """An OWUI model dict for a llama.cpp connection: the raw /v1/models row under `openai` and spread."""
    row = {"id": model_id, "owned_by": "llamacpp", "meta": {"n_ctx": n_ctx, "n_ctx_train": 262144}}
    return {**row, "name": model_id, "owned_by": "openai", "openai": row, "urlIdx": 0}


class _FakeRequest:
    """Stand-in for OWUI's `__request__`: only `app.state.MODELS` is read."""

    def __init__(self, models: Any) -> None:
        state = type("State", (), {"MODELS": models})()
        self.app = type("App", (), {"state": state})()


def test_context_backend_n_ctx_beats_static_table(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #6: llama-server --ctx-size 16384 showed Qwen3's trained window from the static table."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    model = _llamacpp_model("qwen3.6-27b-issue6", 16384)
    ctx = run_async(f._resolve_context(make_body(model=model["id"]), {}, model, make_tokens(total=7200)))
    assert ctx["size"] == 16384
    assert ctx["source"] == "backend"
    assert ctx["matched_key"] == "meta.n_ctx"
    # Not cached: the same id without a listing row falls back to the table at once.
    plain = run_async(
        f._resolve_context(make_body(), {}, make_model_dict("qwen3.6-27b-issue6"), make_tokens(total=7200))
    )
    assert plain["size"] == usage_display_module._STATIC_CONTEXT_SIZES["qwen3.6"]
    assert plain["source"] == "static_table"


def test_context_backend_vllm_max_model_len(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    model = {"id": "llama-3.1-8b-vllm", "max_model_len": 32768}
    size, prov = run_async(f._context_size_for(model["id"], None, model))
    assert size == 32768
    assert prov == {"source": "backend", "matched_key": "max_model_len"}


def test_context_backend_ignores_non_positive_and_malformed(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    for model in (
        {"id": "gpt-4o-backend-zero", "meta": {"n_ctx": 0}},  # llama.cpp router: model not loaded
        {"id": "gpt-4o-backend-bad", "meta": "nope", "openai": "nope", "max_model_len": "big"},
    ):
        size, prov = run_async(f._context_size_for(model["id"], None, model))
        assert size == 128000
        assert prov["source"] == "static_table"


def test_context_user_map_and_num_ctx_beat_backend(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    model = _llamacpp_model("qwen3-backend-vs-map", 16384)
    size, prov = run_async(f._context_size_for(model["id"], {"_tud_num_ctx": 4096}, model))
    assert (size, prov["source"]) == (4096, "num_ctx")
    f.valves.context_size_map = '{"qwen3-backend": 8192}'
    size, prov = run_async(f._context_size_for(model["id"], None, model))
    assert (size, prov["source"]) == (8192, "user_map")


def test_context_workspace_model_reads_backend_row_of_its_base_model(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preset carries no listing row; its base model's row comes from OWUI's model registry."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    preset = make_model_dict("qwen-no-think", base="qwen3.6-27b-preset")
    request = _FakeRequest({"qwen3.6-27b-preset": _llamacpp_model("qwen3.6-27b-preset", 16384)})
    size, prov = run_async(f._context_size_for("qwen3.6-27b-preset", None, preset, request))
    assert size == 16384
    assert prov == {"source": "backend", "matched_key": "meta.n_ctx"}


def test_context_workspace_model_registry_failure_falls_through(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    preset = make_model_dict("qwen-agent", base="qwen3.6-27b-registry-broken")

    class _BrokenModels:
        def get(self, _key: str) -> None:
            msg = "redis down"
            raise ConnectionError(msg)

    for request in (_FakeRequest(_BrokenModels()), object(), _FakeRequest({})):
        size, prov = run_async(f._context_size_for("qwen3.6-27b-registry-broken", None, preset, request))
        assert size == usage_display_module._STATIC_CONTEXT_SIZES["qwen3.6"]
        assert prov["source"] == "static_table"


def test_context_matched_probe_beats_tables_unmatched_loses(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.llamacpp_url = "http://localhost:8080"

    async def _matched(model_id: str) -> tuple[int, str]:
        return 16384, model_id

    monkeypatch.setattr(f, "_probe_context", _matched)
    size, prov = run_async(f._context_size_for("qwen3-probe-matched", None))
    assert size == 16384
    assert prov == {"source": "probe", "matched_key": "qwen3-probe-matched"}

    async def _unmatched(_model_id: str) -> tuple[int, None]:
        return 16384, None

    monkeypatch.setattr(f, "_probe_context", _unmatched)
    size, prov = run_async(f._context_size_for("gpt-4o-probe-unmatched", None))
    assert size == 128000  # a cloud model is not given the local server's window
    assert prov["source"] == "static_table"


def test_outlet_passes_request_for_workspace_backend_context(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    usage = make_usage(input_tokens=6590, output_tokens=645, total_tokens=7235)
    body = make_body([make_message("q", role="user"), make_message("a", usage=usage)], model="qwen-no-think-outlet")
    preset = make_model_dict("qwen-no-think-outlet", base="qwen3.6-27b-outlet")
    request = _FakeRequest({"qwen3.6-27b-outlet": _llamacpp_model("qwen3.6-27b-outlet", 16384)})
    emitter = CapturingEmitter()
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=preset, __request__=request))
    desc = emitter.statuses()[0]["data"]["description"]
    assert "7.2k/16.4k (44%)" in desc


# --- Issue #14: live window through the model's OWUI connection (llama-swap / llama.cpp router) --- #


def _swap_model(listed_id: str, url_idx: int, **meta: Any) -> dict[str, Any]:
    """An OWUI model dict for a llama-swap connection (listed id already carries any OWUI prefix)."""
    row = {"id": listed_id, "owned_by": "llama-swap", "meta": {"llamaswap": {"type": "model"}, **meta}}
    return {**row, "name": listed_id, "owned_by": "openai", "openai": row, "urlIdx": url_idx}


def _router_model(listed_id: str, url_idx: int) -> dict[str, Any]:
    """An OWUI model dict for a llama.cpp router connection (rows carry a `status` block)."""
    row = {"id": listed_id, "owned_by": "llamacpp", "status": {"value": "loaded"}}
    return {**row, "name": listed_id, "owned_by": "openai", "openai": row, "urlIdx": url_idx}


def _install_owui_connection(
    monkeypatch: pytest.MonkeyPatch, url: str, key: str, api_config: dict[str, Any] | Exception
) -> None:
    """Fake OWUI 0.10+ `open_webui.routers.openai.get_openai_connection` (one connection for any index)."""

    async def get_openai_connection(_idx: int) -> tuple[str, str, dict[str, Any]]:
        if isinstance(api_config, Exception):
            raise api_config
        return url, key, api_config

    fake = types.ModuleType("open_webui.routers.openai")
    fake.get_openai_connection = get_openai_connection  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "open_webui", types.ModuleType("open_webui"))
    monkeypatch.setitem(sys.modules, "open_webui.routers", types.ModuleType("open_webui.routers"))
    monkeypatch.setitem(sys.modules, "open_webui.routers.openai", fake)


def test_live_backend_detects_only_llama_swap_and_llamacpp_router(usage_display_module: ModuleType) -> None:
    live = usage_display_module._live_backend
    assert live(_swap_model("ls.qwen3", 0)) == ("llama-swap", "ls.qwen3")
    alias = {
        "openai": {"id": "fast", "owned_by": "llama-swap", "meta": {"llamaswap": {"type": "alias", "modelID": "Q3"}}}
    }
    assert live(alias) == ("llama-swap", "Q3")
    assert live({"openai": {"id": "old-swap", "owned_by": "llama-swap"}}) == ("llama-swap", "old-swap")
    assert live(_router_model("gemma", 0)) == ("llamacpp-router", "gemma")
    for entry in (
        {"openai": {"id": "p:m", "owned_by": "llama-swap", "meta": {"llamaswap": {"type": "peer"}}}},
        {"openai": {"id": "single", "owned_by": "llamacpp", "meta": {"n_ctx": 4096}}},  # plain llama-server
        {"openai": {"id": "gpt-4o", "owned_by": "openai"}},
        {"openai": {"id": "", "owned_by": "llama-swap"}},
        {"id": "no-row", "owned_by": "llama-swap"},
        None,
    ):
        assert live(entry) is None


def test_llama_swap_ready_id_needs_a_ready_matched_row(usage_display_module: ModuleType) -> None:
    ready_id = usage_display_module._llama_swap_ready_id
    running = {
        "running": [
            {"model": "Qwen3", "state": "ready"},
            {"model": "wen3", "state": "ready"},
            {"model": "gemma", "state": "starting"},
            "junk",
            {"model": 5, "state": "ready"},
        ]
    }
    assert ready_id(running, "ls.qwen3") == "Qwen3"  # prefix stripped by suffix match, original case kept
    assert ready_id(running, "gemma") is None  # not ready: asking /props?model= would wait for or start a load
    assert ready_id(running, "qwen3-max") is None
    assert ready_id({"running": "nope"}, "Qwen3") is None
    assert ready_id(None, "Qwen3") is None


def test_props_n_ctx_reads_top_level_or_generation_settings(usage_display_module: ModuleType) -> None:
    n_ctx = usage_display_module._props_n_ctx
    assert n_ctx({"n_ctx": 8192}) == 8192
    assert n_ctx({"default_generation_settings": {"n_ctx": 40960}}) == 40960
    assert n_ctx({"default_generation_settings": {"n_ctx": 0}}) is None  # llama.cpp router's own dummy /props
    assert n_ctx({"error": {"message": "model is not loaded"}}) is None
    assert n_ctx("nope") is None


def test_probe_llama_swap_reads_fit_window_from_props_of_ready_model(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #14: `--fit` leaves no --ctx-size in the command; /props?model= has the real window."""
    f = usage_display_module.Filter()
    f.valves.llama_swap_url = "http://swap:8080/"
    running = {"running": [{"model": "qwen3/fit", "state": "ready", "cmd": "llama-server --fit on -m q.gguf"}]}
    fake = install_fake_aiohttp(
        monkeypatch,
        usage_display_module,
        {
            "http://swap:8080/running": running,
            "http://swap:8080/props?model=qwen3%2Ffit": {"default_generation_settings": {"n_ctx": 40960}},
        },
    )
    assert run_async(f._probe_context("qwen3/fit")) == (40960, "qwen3/fit")
    assert [url for url, _ in fake.requests] == ["http://swap:8080/running", "http://swap:8080/props?model=qwen3%2Ffit"]


def test_probe_llama_swap_never_asks_props_for_a_model_that_is_not_ready(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    f.valves.llama_swap_url = "http://swap:8080"
    running = {"running": [{"model": "qwen3-loading", "state": "starting", "cmd": "--ctx-size 16384"}]}
    fake = install_fake_aiohttp(monkeypatch, usage_display_module, {"http://swap:8080/running": running})
    assert run_async(f._probe_context("qwen3-loading")) == (16384, "qwen3-loading")
    assert [url for url, _ in fake.requests] == ["http://swap:8080/running"]


def test_probe_llama_swap_props_failure_falls_back_to_ctx_size(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A llama-swap upstream without /props (e.g. vLLM) or a /props error keeps the launch command's size."""
    f = usage_display_module.Filter()
    f.valves.llama_swap_url = "http://swap:8080"
    running = {"running": [{"model": "vllm-model", "state": "ready", "cmd": "--ctx-size 32768"}]}
    for props in (RuntimeError("404"), {"error": "not found"}):
        install_fake_aiohttp(
            monkeypatch,
            usage_display_module,
            {"http://swap:8080/running": running, "http://swap:8080/props?model=vllm-model": props},
        )
        assert run_async(f._probe_context("vllm-model")) == (32768, "vllm-model")


def test_connection_probe_llama_swap_uses_owui_url_key_and_prefix(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #14 end to end: no plugin valves, the connection's URL/key/prefix are reused, live beats listed."""
    mod = usage_display_module
    f = mod.Filter()
    _patch_no_network(monkeypatch, f)
    _install_owui_connection(monkeypatch, "http://swap:8080/v1/", "sk-swap", {"prefix_id": "ls"})
    running = {"running": [{"model": "qwen3-conn", "state": "ready", "cmd": "llama-server --fit on"}]}
    fake = install_fake_aiohttp(
        monkeypatch,
        mod,
        {
            "http://swap:8080/running": running,
            "http://swap:8080/props?model=qwen3-conn": {"default_generation_settings": {"n_ctx": 40960}},
        },
    )
    model = _swap_model("ls.qwen3-conn", 101, n_ctx=131072)  # llama-swap capabilities.context from its config
    size, prov = run_async(f._context_size_for("ls.qwen3-conn", None, model))
    assert size == 40960
    assert prov == {"source": "connection_probe", "matched_key": "llama-swap:qwen3-conn"}
    assert fake.requests[0] == ("http://swap:8080/running", {"Authorization": "Bearer sk-swap"})
    assert fake.requests[1][1] == {"Authorization": "Bearer sk-swap"}

    # A hit is cached: the next response asks nothing.
    fake.requests.clear()
    assert run_async(f._context_size_for("ls.qwen3-conn", None, model)) == (size, prov)
    assert fake.requests == []

    # The user's map still wins, without touching the network.
    f.valves.context_size_map = '{"qwen3-conn": 8192}'
    assert run_async(f._context_size_for("ls.qwen3-conn", None, model))[0] == 8192


def test_connection_probe_miss_falls_through_to_listing_and_is_not_cached(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = mod.Filter()
    _patch_no_network(monkeypatch, f)
    _install_owui_connection(monkeypatch, "http://swap:8080/v1", "", {"auth_type": "none"})
    fake = install_fake_aiohttp(monkeypatch, mod, {"http://swap:8080/running": {"running": []}})  # swapped out
    model = _swap_model("qwen3-swapped-out", 102, n_ctx=65536)
    size, prov = run_async(f._context_size_for("qwen3-swapped-out", None, model))
    assert (size, prov["source"]) == (65536, "backend")
    assert fake.requests == [("http://swap:8080/running", {})]  # no key configured -> no auth header
    run_async(f._context_size_for("qwen3-swapped-out", None, model))
    assert len(fake.requests) == 2  # a miss is asked again next time


def test_connection_probe_llamacpp_router_never_autoloads(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = mod.Filter()
    _patch_no_network(monkeypatch, f)
    _install_owui_connection(monkeypatch, "http://router:8080/v1", "sk-r", {"auth_type": "bearer"})
    props_url = "http://router:8080/props?model=org%2Fgemma-3-router%3AQ4&autoload=false"
    install_fake_aiohttp(monkeypatch, mod, {props_url: {"default_generation_settings": {"n_ctx": 12288}}})
    model = _router_model("org/gemma-3-router:Q4", 103)
    size, prov = run_async(f._context_size_for("org/gemma-3-router:Q4", None, model))
    assert (size, prov["matched_key"]) == (12288, "llamacpp-router:org/gemma-3-router:Q4")

    install_fake_aiohttp(
        monkeypatch, mod, {props_url.replace("Q4", "Q8"): {"error": {"message": "model is not loaded"}}}
    )
    size, prov = run_async(
        f._context_size_for("org/gemma-3-router:Q8", None, _router_model("org/gemma-3-router:Q8", 104))
    )
    assert (size, prov["source"]) == (None, "none")  # refused as not loaded: the next tiers answer (none here)


def test_connection_probe_workspace_model_on_owui_09_config_without_bearer(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preset -> base entry from the registry; OWUI 0.9.x has no get_openai_connection, only app.state.config."""
    mod = usage_display_module
    f = mod.Filter()
    _patch_no_network(monkeypatch, f)
    monkeypatch.setitem(sys.modules, "open_webui.routers.openai", None)  # import fails like on 0.9.x
    base = _swap_model("qwen3-preset09", 1)
    request = _FakeRequest({"qwen3-preset09": base})
    request.app.state.config = types.SimpleNamespace(
        OPENAI_API_BASE_URLS=["http://cloud/v1", "http://swap09:8080"],
        OPENAI_API_KEYS=["sk-cloud", "per-user"],
        OPENAI_API_CONFIGS={"http://swap09:8080": {"auth_type": "session"}},  # legacy url-keyed config
    )
    running = {"running": [{"model": "qwen3-preset09", "state": "ready"}]}
    fake = install_fake_aiohttp(
        monkeypatch,
        mod,
        {"http://swap09:8080/running": running, "http://swap09:8080/props?model=qwen3-preset09": {"n_ctx": 20480}},
    )
    preset = make_model_dict("qwen-preset09-agent", base="qwen3-preset09")
    size, prov = run_async(f._context_size_for("qwen3-preset09", None, preset, request))
    assert (size, prov["source"]) == (20480, "connection_probe")
    assert all(headers == {} for _, headers in fake.requests)  # a session connection's key is not reused


def test_connection_probe_degrades_silently(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = usage_display_module
    f = mod.Filter()
    _patch_no_network(monkeypatch, f)
    fake = install_fake_aiohttp(monkeypatch, mod)
    model = _swap_model("qwen3-degrade", 105)

    def static(entry: Any, request: Any = None) -> Any:
        return run_async(f._context_size_for("qwen3-degrade", None, entry, request))[1]["source"]

    # Connection lookup raises / returns junk / request missing on 0.9.x.
    _install_owui_connection(monkeypatch, "http://swap:8080", "k", RuntimeError("config db down"))
    assert static(model) == "static_table"
    _install_owui_connection(monkeypatch, "", "k", {})
    assert static(model) == "static_table"
    monkeypatch.setitem(sys.modules, "open_webui.routers.openai", None)
    assert static(model) == "static_table"
    # Not a detectable backend, or no usable urlIdx: nothing is looked up at all.
    _install_owui_connection(monkeypatch, "http://swap:8080", "k", {})
    assert static({**model, "urlIdx": True}) == "static_table"
    assert static({**model, "openai": {"id": "qwen3-degrade", "owned_by": "openai"}}) == "static_table"
    assert static(make_model_dict("qwen3-degrade")) == "static_table"
    assert static(None) == "static_table"
    assert fake.requests == []
    # Session-level failure and aiohttp missing.
    install_fake_aiohttp(monkeypatch, mod, session_error=RuntimeError("boom"))
    assert static(model) == "static_table"
    monkeypatch.setattr(mod, "_AIOHTTP_AVAILABLE", False)
    assert static(model) == "static_table"


def test_sanitize_model_reports_live_backend_without_secrets(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    out = f._sanitize_model(_swap_model("ls.qwen3-debug", 0), None, "ls.qwen3-debug")
    assert out["live_backend"] == ("llama-swap", "ls.qwen3-debug")
    assert f._sanitize_model(make_model_dict("gpt-4o"), None, "gpt-4o")["live_backend"] is None


# --- OpenRouter balance (issue #36) -------------------------------------------- #

_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
_CREDITS_OK = {"data": {"total_credits": 130, "total_usage": 112.945023102}}  # shape measured 2026-11-03
_ADMIN = {"role": "admin"}


def _openrouter_model(listed_id: str = "openai/gpt-4o", url_idx: int = 3) -> dict[str, Any]:
    """An OWUI model dict for an OpenRouter connection (its /v1/models row carries no owned_by)."""
    row = {"id": listed_id, "name": listed_id, "context_length": 128000}
    return {**row, "owned_by": "openai", "openai": row, "urlIdx": url_idx}


def _balance_filter(mod: ModuleType, **valves: Any) -> Any:
    f = mod.Filter()
    f.valves.show_balance = True
    for name, value in valves.items():
        setattr(f.valves, name, value)
    return f


def test_balance_is_off_by_default_and_asks_nothing(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1", "sk-or-conn", {})
    fake = install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    result = run_async(mod.Filter()._resolve_balance(_openrouter_model(), _ADMIN, None))
    assert result == {"usd": None, "status": "off", "key_source": None, "on_openrouter": None}
    assert fake.requests == []


def test_balance_reuses_the_openrouter_connection_key_on_every_response(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod)
    _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1/", "sk-or-conn", {"prefix_id": "or"})
    fake = install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    result = run_async(f._resolve_balance(_openrouter_model("or.openai/gpt-4o"), _ADMIN, None))
    assert result["usd"] == pytest.approx(17.054976898)
    assert (result["status"], result["key_source"], result["on_openrouter"]) == ("ok", "connection", True)
    assert fake.requests == [(_CREDITS_URL, {"Authorization": "Bearer sk-or-conn"})]
    # Not cached: the next response asks again, so the number follows the spending.
    run_async(f._resolve_balance(_openrouter_model(), _ADMIN, None))
    assert len(fake.requests) == 2


def test_balance_valve_key_beats_the_connection_key(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod, openrouter_api_key="  sk-or-valve\n")  # pasted with stray whitespace
    _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1", "sk-or-conn", {})
    fake = install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    result = run_async(f._resolve_balance(_openrouter_model(), _ADMIN, None))
    assert (result["status"], result["key_source"]) == ("ok", "valve")
    assert fake.requests == [(_CREDITS_URL, {"Authorization": "Bearer sk-or-valve"})]


def test_balance_workspace_model_finds_the_connection_of_its_base_model(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod)
    _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1", "sk-or-conn", {})
    install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    request = _FakeRequest({"openai/gpt-4o": _openrouter_model()})
    preset = make_model_dict("research-agent", base="openai/gpt-4o")
    assert run_async(f._resolve_balance(preset, _ADMIN, request))["key_source"] == "connection"


def test_balance_never_sends_a_key_for_a_model_not_served_by_openrouter(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod, openrouter_api_key="sk-or-valve")
    fake = install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    # Another provider, and a proxy whose URL merely mentions openrouter.ai: judged by host.
    for url in ("https://api.openai.com/v1", "https://proxy.example/openrouter.ai/v1"):
        _install_owui_connection(monkeypatch, url, "sk-other", {})
        result = run_async(f._resolve_balance(_openrouter_model(), _ADMIN, None))
        assert (result["status"], result["on_openrouter"]) == ("not_openrouter", False)
    # No connection to look at: no model dict, no urlIdx, a bool urlIdx, a failing lookup.
    _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1", "sk-or-conn", {})
    for model in (None, make_model_dict("llama3"), {**_openrouter_model(), "urlIdx": True}):
        assert run_async(f._resolve_balance(model, _ADMIN, None))["status"] == "not_openrouter"
    _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1", "k", RuntimeError("config db down"))
    assert run_async(f._resolve_balance(_openrouter_model(), _ADMIN, None))["status"] == "not_openrouter"
    assert fake.requests == []


def test_balance_on_every_model_serves_openrouter_behind_a_proxy(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod, balance_on_every_model=True, openrouter_api_key="sk-or-valve")
    fake = install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    result = run_async(f._resolve_balance(make_model_dict("litellm/gpt-4o"), _ADMIN, None))
    # The connection is not even looked up: the valve key and the valve decide.
    assert (result["status"], result["key_source"], result["on_openrouter"]) == ("ok", "valve", None)
    assert fake.requests == [(_CREDITS_URL, {"Authorization": "Bearer sk-or-valve"})]

    # Without a valve key a non-OpenRouter connection has no key to give.
    f.valves.openrouter_api_key = ""
    _install_owui_connection(monkeypatch, "http://litellm:4000/v1", "sk-litellm", {})
    result = run_async(f._resolve_balance(_openrouter_model(), _ADMIN, None))
    assert (result["status"], result["on_openrouter"]) == ("no_key", False)
    assert len(fake.requests) == 1


def test_balance_is_shown_to_admins_only_unless_widened(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod)
    _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1", "sk-or-conn", {})
    fake = install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    for user in ({"role": "user"}, {"role": "pending"}, {}, None):
        assert run_async(f._resolve_balance(_openrouter_model(), user, None))["status"] == "admins_only"
    assert fake.requests == []
    f.valves.balance_for_all_users = True
    assert run_async(f._resolve_balance(_openrouter_model(), {"role": "user"}, None))["status"] == "ok"


def test_balance_needs_a_bearer_key_on_the_connection(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod)
    fake = install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    for key, api_config in (("", {}), ("per-user", {"auth_type": "session"})):
        _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1", key, api_config)
        result = run_async(f._resolve_balance(_openrouter_model(), _ADMIN, None))
        assert (result["status"], result["on_openrouter"]) == ("no_key", True)
    assert fake.requests == []


def test_fetch_openrouter_balance_reports_why_it_failed(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module

    def fetch(payload: Any) -> Any:
        install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: payload})
        return run_async(mod.Filter._fetch_openrouter_balance({}))

    refused = {"error": {"code": 403, "message": "Only management keys can perform this operation"}}
    assert fetch(refused) == (None, "error 403: Only management keys can perform this operation")
    assert fetch({"error": {"code": 500, "message": "x" * 500}})[1] == "error 500: " + "x" * 200
    assert fetch(RuntimeError("connection reset")) == (None, "error: RuntimeError")
    for junk in ({"data": {"total_credits": "130", "total_usage": 1}}, {"data": None}, ["data"], None):
        assert fetch(junk) == (None, "malformed_response")
    assert fetch({"data": {"total_credits": 5, "total_usage": 8.1}})[0] == pytest.approx(-3.1)

    install_fake_aiohttp(monkeypatch, mod, session_error=RuntimeError("boom"))
    assert run_async(mod.Filter._fetch_openrouter_balance({})) == (None, "error: RuntimeError")
    monkeypatch.setattr(mod, "_AIOHTTP_AVAILABLE", False)
    assert run_async(mod.Filter._fetch_openrouter_balance({})) == (None, "aiohttp_unavailable")


def test_outlet_shows_balance_after_chat_cost_and_keeps_the_key_out_of_debug(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod, openrouter_api_key="sk-or-secret", debug_mode=True)
    _patch_no_network(monkeypatch, f)
    _install_owui_connection(monkeypatch, "https://openrouter.ai/api/v1", "sk-or-conn", {})
    install_fake_aiohttp(monkeypatch, mod, {_CREDITS_URL: _CREDITS_OK})
    messages = [
        make_message("q1", role="user"),
        make_message("a1", usage=make_usage(cost=0.01)),
        make_message("q2", role="user"),
        make_message("a2", usage=make_usage(cost=0.02)),
    ]
    emitter = CapturingEmitter()
    run_async(f.outlet(make_body(messages), __event_emitter__=emitter, __model__=_openrouter_model(), __user__=_ADMIN))
    assert "💰 $0.0200 · 💰Σ $0.0300 · 💳 $17.05" in emitter.statuses()[0]["data"]["description"]
    debug = next(e for e in emitter.events if e.get("type") == "citation")["data"]["document"][0]
    assert '"key_source": "valve"' in debug
    assert '"openrouter_api_key": "******"' in debug
    assert "sk-or-secret" not in debug


def test_outlet_survives_a_balance_resolver_bug(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = usage_display_module
    f = _balance_filter(mod)
    _patch_no_network(monkeypatch, f)

    async def broken(*_args: Any) -> dict[str, Any]:
        raise RuntimeError("bug")

    monkeypatch.setattr(f, "_resolve_balance", broken)
    emitter = CapturingEmitter()
    run_async(f.outlet(make_body([make_message("a", usage=make_usage())]), __event_emitter__=emitter, __user__=_ADMIN))
    assert "💳" not in emitter.statuses()[0]["data"]["description"]


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
    monkeypatch.setattr(usage_display_module, "_STATIC_PRICES", {"gpt-4o": {"input": 2.5, "output": 10.0}})
    f.valves.cost_mode = "estimate"
    f.valves.fetch_prices_from_modelsdev = False
    usage = make_usage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = run_async(
        f._resolve_cost(
            usage, make_tokens(fresh_input=1_000_000, output=1_000_000), [make_message("a", usage=usage)], "gpt-4o"
        )
    )
    # 1M*2.5 + 1M*10.0 over 1e6
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
    """Auto mode must never fall back to estimation, even when tokens/price would allow it."""
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
    """Estimate mode, no price found anywhere, show_cumulative_cost off -> message stays None, no cumulative work."""
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
    """Native cost wins for the current message, while the chat total still estimates the rest.

    The cumulative walk resolves a price to estimate OTHER historical messages that carry no
    native cost of their own.
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
    """A non-estimable historical message contributes nothing, and the loop still continues.

    The message has no native cost AND an empty token bag; the next message is still priced.
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
    price, prov = run_async(f._resolve_price("openai/gpt-4o"))
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

    static = {"gpt-4o": {"input": 2.5, "output": 10.0}}
    monkeypatch.setattr(usage_display_module, "_STATIC_PRICES", static)
    monkeypatch.setattr(f, "_modelsdev_prices_map", _prices)
    f.valves.fetch_prices_from_modelsdev = True
    price, prov = run_async(f._resolve_price("my-proxy.gpt-4o"))
    assert price == static["gpt-4o"]
    assert prov == {"source": "static", "matched_key": "gpt-4o"}


def test_price_key_matches_whole_ids_never_families(usage_display_module: ModuleType) -> None:
    """Issue #49: a related id is a different tariff, so only the id itself or a provider prefix is cut off."""
    fn = usage_display_module._price_key
    ids = ("deepseek-v4", "claude-opus-4-8", "anthropic.claude-sonnet-5", "gpt-6.1-terra", "qwen3.8-27b")
    table: dict[str, Any] = {model_id: {"input": 1.0} for model_id in ids}
    assert fn(table, "GPT-6.1-Terra") == "gpt-6.1-terra"
    assert fn(table, "openai/gpt-6.1-terra") == "gpt-6.1-terra"  # org/model
    assert fn(table, "openai.gpt-6.1-terra") == "gpt-6.1-terra"  # an OWUI connection prefix_id
    assert fn(table, "au.anthropic.claude-opus-4-8") == "claude-opus-4-8"  # region + vendor, cut one at a time
    assert fn(table, "eu.anthropic.claude-sonnet-5") == "anthropic.claude-sonnet-5"  # the longest listed id wins
    assert fn(table, "groq/qwen/qwen3.8-27b") == "qwen3.8-27b"
    # never a family or a neighbour: the version dot is not a cut, and no substring match
    assert fn(table, "deepseek-v4-1-flash") is None
    assert fn(table, "claude-opus-4-8-fast") is None
    assert fn(table, "gpt-6.1-terra-2026-10-01") is None  # a dated snapshot needs its own entry or price_map
    assert fn(table, "6.1-terra") is None
    assert fn({}, "gpt-6.1-terra") is None
    assert fn(table, "") is None


def test_resolve_price_modelsdev_no_longer_matches_a_family(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #49: deepseek-v4-1-flash took deepseek-v4's price (about 4x too low) from models.dev or the table."""
    f = usage_display_module.Filter()

    async def _prices() -> dict[str, Any]:
        return {"deepseek-v4": {"input": 0.14, "output": 0.28}}

    monkeypatch.setattr(f, "_modelsdev_prices_map", _prices)
    monkeypatch.setattr(usage_display_module, "_STATIC_PRICES", {"deepseek-v4": {"input": 0.14, "output": 0.28}})
    f.valves.fetch_prices_from_modelsdev = True
    assert run_async(f._resolve_price("deepseek-v4-1-flash")) == (None, {"source": "none", "matched_key": None})
    # the admin's own price_map still matches by substring: its keys are a deliberate choice
    f.valves.price_map = '{"deepseek-v4": {"input": 0.6, "output": 1.45}}'
    price, prov = run_async(f._resolve_price("deepseek-v4-1-flash"))
    assert (price, prov["source"]) == ({"input": 0.6, "output": 1.45}, "price_map")


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


def test_outlet_reads_owui_0125_incomplete_usage_as_no_usage(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OWUI 0.12.5 leaves `{"incomplete": True}` for a streamed call that reported no usage, persisted too."""
    mod = usage_display_module
    f = mod.Filter()
    _patch_no_network(monkeypatch, f)
    install_fake_tiktoken(monkeypatch, mod)
    f.valves.show_data_source = True
    past = make_message("old answer", usage={"incomplete": True})
    current = make_message("three word answer", usage={"incomplete": True})
    messages = [make_message("q1", role="user"), past, make_message("q2", role="user"), current]
    emitter = CapturingEmitter()
    run_async(f.outlet(make_body(messages, model="gpt-4o"), __event_emitter__=emitter, __model__=make_model_dict()))
    line = emitter.statuses()[0]["data"]["description"]
    assert "⬇︎ 3" in line  # tiktoken estimate of the reply
    assert line.endswith("[est.]")
    assert "🧮" not in line  # the past turn has no counter, so the chat total equals this turn and is hidden
    tokens = f._extract_tokens(current["usage"], current, messages, "")
    cumulative = f._cumulative_tokens(messages, current, tokens)
    # The current turn counts with its estimated Σ; the past one is skipped, never re-estimated.
    assert (cumulative["messages_counted"], cumulative["messages_skipped_no_usage"]) == (1, 1)
    assert cumulative["estimated"] is True


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
    assert '"cumulative_tokens_debug"' in doc
    assert '"messages_counted": 1' in doc


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
    turn = mod._Turn(
        task=None, messages=[], assistant_msg=make_message("a", usage=make_usage()), usage=make_usage(), metadata=None
    )
    stats = mod._Stats(f.valves, make_tokens(), make_timing(), make_ctx(), make_cost(), None)
    run_async(f._emit_debug(emitter, turn, stats))
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


def _reset_modelsdev_caches(monkeypatch: pytest.MonkeyPatch, mod: ModuleType) -> None:
    for cache in (mod._modelsdev_cache, mod._modelsdev_prices_cache):
        monkeypatch.setitem(cache, "map", None)
        monkeypatch.setitem(cache, "expiry", 0.0)
        monkeypatch.setitem(cache, "status", "not_fetched")
        monkeypatch.setitem(cache, "fetched_at", None)


@pytest.mark.parametrize(
    ("reply", "aiohttp_on", "status"),
    [
        ({"openai": {"models": {"gpt-x": {"cost": {"input": 1.0, "output": 2.0}}}}}, True, "ok"),
        ({"openai": {"models": {}}}, True, "empty"),  # reachable, but nothing usable: a changed format
        (HttpStatus(503), True, "http 503"),
        (RuntimeError("boom"), True, "error: RuntimeError"),  # DNS, timeout, refused, bad JSON ...
        ({}, False, "aiohttp_unavailable"),
    ],
)
def test_modelsdev_fetch_reports_why_it_failed(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch, reply: Any, *, aiohttp_on: bool, status: str
) -> None:
    """Issue #49: a failed models.dev fetch looked exactly like an unlisted model; now debug says which it was."""
    f = usage_display_module.Filter()
    mod = usage_display_module
    _reset_modelsdev_caches(monkeypatch, mod)
    assert mod._modelsdev_status(mod._modelsdev_prices_cache) == {
        "status": "not_fetched",
        "entries": None,
        "age_s": None,
    }
    install_fake_aiohttp(monkeypatch, mod, {f.valves.modelsdev_api_url: reply})
    monkeypatch.setattr(mod, "_AIOHTTP_AVAILABLE", aiohttp_on, raising=False)
    result = run_async(f._modelsdev_prices_map())
    debug = mod._modelsdev_status(mod._modelsdev_prices_cache)
    assert debug == {"status": status, "entries": len(result), "age_s": 0}
    assert bool(result) is (status == "ok")


def test_modelsdev_context_fetch_reports_its_status_too(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    _reset_modelsdev_caches(monkeypatch, mod)
    install_fake_aiohttp(monkeypatch, mod, {f.valves.modelsdev_url: HttpStatus(404)})
    assert run_async(f._modelsdev_map()) == {}
    assert mod._modelsdev_cache["status"] == "http 404"
    _reset_modelsdev_caches(monkeypatch, mod)
    install_fake_aiohttp(monkeypatch, mod, {f.valves.modelsdev_url: {"gpt-x": {"limit": {"context": 1000}}}})
    assert run_async(f._modelsdev_map()) == {"gpt-x": 1000}
    assert mod._modelsdev_cache["status"] == "ok"


def test_outlet_issue_49_prices_from_the_table_when_modelsdev_fails_and_says_why(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reporter's turn: gpt-6.1-terra, estimate mode, models.dev unreachable -> the built-in price, ≈."""
    mod = usage_display_module
    f = mod.Filter()
    _reset_modelsdev_caches(monkeypatch, mod)
    install_fake_aiohttp(monkeypatch, mod, session_error=TimeoutError())
    monkeypatch.setattr(mod, "_STATIC_PRICES", {"gpt-6.1-terra": {"input": 1.0, "output": 5.0, "cache_read": 0.05}})
    f.valves.cost_mode = "estimate"
    f.valves.debug_mode = True
    usage = {
        "input_tokens": 1841,
        "input_tokens_details": {"cache_write_tokens": 1838, "cached_tokens": 0},
        "output_tokens": 12,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 1853,
    }
    emitter = CapturingEmitter()
    body = make_body([make_message("answer", usage=usage)], model="gpt-6.1-terra")
    run_async(f.outlet(body, __event_emitter__=emitter, __model__=make_model_dict("gpt-6.1-terra")))
    assert "💰 ≈$0.0019" in emitter.statuses()[0]["data"]["description"]
    citation = next(e for e in emitter.events if e.get("type") == "citation")
    payload = json.loads(citation["data"]["document"][0].split("```json\n", 1)[1].rsplit("```", 1)[0])
    assert payload["cost_debug"]["price"]["source"] == "static"
    assert payload["cost_debug"]["modelsdev"] == {"status": "error: TimeoutError", "entries": 0, "age_s": 0}
    assert payload["cost_debug"]["cumulative_unpriced_messages"] == 0
    assert payload["context_debug"]["modelsdev"]["status"] == "not_fetched"  # context fetch is opt-in, off


def test_cumulative_cost_marks_a_total_that_misses_unpriced_turns(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn with tokens but no cost leaves 💰Σ short -> ≈ and a count; a turn without usage is skipped silently."""
    f = usage_display_module.Filter()
    _patch_no_network(monkeypatch, f)
    f.valves.cost_mode = "auto"
    messages = [
        make_message("native", usage=make_usage(cost=0.05)),
        make_message("no cost reported", usage=make_usage(input_tokens=100, output_tokens=10)),
        make_message("unsaved chat history"),  # no usage at all
        make_message("streamed call without usage", usage={"incomplete": True}),  # no counters -> no usage
        make_message("aborted", usage=make_usage(input_tokens=0, output_tokens=0, total_tokens=0)),  # costs nothing
        make_message("native too", usage=make_usage(cost=0.02)),
    ]
    assert f._cumulative_cost(messages, None) == (pytest.approx(0.07), True, 1)
    assert f._cumulative_cost([messages[0], *messages[2:5]], None) == (pytest.approx(0.05), False, 0)
    cost = run_async(f._resolve_cost(make_usage(cost=0.02), make_tokens(), messages, "gpt-x"))
    assert (cost["cumulative"], cost["cumulative_est"]) == (pytest.approx(0.07), True)
    render = renderer(usage_display_module, "_render_cost_total")
    assert render(f.valves, make_tokens(), make_timing(), make_ctx(), cost, None) == "💰Σ ≈$0.0700"


def test_probe_context_aiohttp_unavailable_returns_none(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.llamacpp_url = "http://localhost:8080"
    assert run_async(f._probe_context("some-model")) is None  # real env has no aiohttp installed


def test_probe_context_llama_swap_success(usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llama_swap_url = "http://localhost:8090"
    install_fake_aiohttp(monkeypatch, mod, {"http://localhost:8090/running": {"running": [{"cmd": "--ctx-size 4096"}]}})
    assert run_async(f._probe_context("some-model")) == (4096, None)


def test_probe_context_llama_swap_unmatched_model_falls_back_to_llamacpp(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llama_swap_url = "http://localhost:8090"
    f.valves.llamacpp_url = "http://localhost:8080"
    running = {"running": [{"model": "a", "cmd": "--ctx-size 1024"}, {"model": "b", "cmd": "--ctx-size 2048"}]}
    install_fake_aiohttp(
        monkeypatch,
        mod,
        {"http://localhost:8090/running": running, "http://localhost:8080/props": {"n_ctx": 8192}},
    )
    assert run_async(f._probe_context("some-model")) == (8192, None)


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
    assert run_async(f._probe_context("some-model")) == (8192, None)


def test_probe_context_llamacpp_nested_generation_settings(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(
        monkeypatch, mod, {"http://localhost:8080/props": {"default_generation_settings": {"n_ctx": 16384}}}
    )
    assert run_async(f._probe_context("some-model")) == (16384, None)


def test_probe_context_llamacpp_v1_models_match_skips_props(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080/"
    models = {"data": [{"id": "a", "meta": {"n_ctx": 1024}}, {"id": "qwen3.6", "meta": {"n_ctx": 16384}}]}
    install_fake_aiohttp(
        monkeypatch,
        mod,
        {"http://localhost:8080/v1/models": models, "http://localhost:8080/props": RuntimeError("must not be asked")},
    )
    assert run_async(f._probe_context("qwen3.6")) == (16384, "qwen3.6")


def test_probe_context_llamacpp_old_build_matches_via_props(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-May-2026 llama.cpp lists no meta.n_ctx; /props names the model by its GGUF file."""
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(
        monkeypatch,
        mod,
        {
            "http://localhost:8080/v1/models": {"data": [{"id": "Qwen3.6.gguf", "meta": {"n_ctx_train": 262144}}]},
            "http://localhost:8080/props": {"model_path": "/m/Qwen3.6.gguf", "n_ctx": 16384},
        },
    )
    assert run_async(f._probe_context("qwen3.6")) == (16384, "qwen3.6")


def test_probe_context_llamacpp_unmatched_props_kept_when_listing_fails(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(
        monkeypatch,
        mod,
        {
            "http://localhost:8080/v1/models": RuntimeError("404"),
            "http://localhost:8080/props": {"model_alias": "other", "n_ctx": 4096},
        },
    )
    assert run_async(f._probe_context("qwen3.6")) == (4096, None)


def test_probe_context_llamacpp_unmatched_listing_kept_when_props_fails(
    usage_display_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = usage_display_module.Filter()
    mod = usage_display_module
    f.valves.llamacpp_url = "http://localhost:8080"
    install_fake_aiohttp(
        monkeypatch,
        mod,
        {
            "http://localhost:8080/v1/models": {"data": [{"id": "other", "meta": {"n_ctx": 4096}}]},
            "http://localhost:8080/props": RuntimeError("props down"),
        },
    )
    assert run_async(f._probe_context("qwen3.6")) == (4096, None)


def test_probe_context_llama_swap_unmatched_row_yields_to_matched_llamacpp(
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
            "http://localhost:8090/running": {"running": [{"model": "other", "cmd": "--ctx-size 1024"}]},
            "http://localhost:8080/v1/models": {"data": [{"id": "qwen3.6", "meta": {"n_ctx": 16384}}]},
        },
    )
    assert run_async(f._probe_context("qwen3.6")) == (16384, "qwen3.6")
    # Neither matches: the swap row (asked first) is returned, flagged as unmatched.
    assert run_async(f._probe_context("gpt-4o")) == (1024, None)


def test_probe_context_llama_swap_matched_row_skips_llamacpp(
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
            "http://localhost:8090/running": {"running": [{"model": "qwen3.6", "cmd": "--ctx-size 16384"}]},
            "http://localhost:8080/v1/models": RuntimeError("must not be asked"),
            "http://localhost:8080/props": RuntimeError("must not be asked"),
        },
    )
    assert run_async(f._probe_context("qwen3.6")) == (16384, "qwen3.6")


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


# --- native prefill timing -----------------------------------------------------


def _prefill_packet(*, prompt: int = 1000, cached: int = 200, milliseconds: object = 400) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "choices": [],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": 40,
            "total_tokens": prompt + 40,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
        "metrics": {
            "time_to_first_token_ms": milliseconds,
            "queue_time_ms": 3000,
            "generation_time_ms": 1000,
            "mean_itl_ms": 25,
        },
    }


def test_prefill_stream_to_outlet_excludes_cached_tokens_and_queue(
    usage_display_module: ModuleType,
) -> None:
    f = usage_display_module.Filter()
    f.valves.show_context_window = False
    f.valves.cost_mode = "off"
    f.valves.fallback_to_tiktoken = False
    body = make_body([make_message("hello", role="user")])
    metadata = {"chat_id": "test", "message_id": "assistant"}
    run_async(f.inlet(body, __metadata__=metadata))
    run_async(f.request(body, __metadata__=metadata))
    packet = run_async(f.stream(_prefill_packet()))
    body["messages"].append(make_message("hello back", usage=packet["usage"]))
    emitter = CapturingEmitter()
    run_async(f.outlet(body, __metadata__=metadata, __event_emitter__=emitter))
    line = emitter.events[-1]["data"]["description"]
    assert "⏩ prefill 2,000.0 t/s" in line
    assert "⚡" in line
    assert packet["metrics"]["queue_time_ms"] == 3000


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_eval_count": 500, "prompt_eval_duration": 2_000_000_000}, 250),
        ({"prompt_n": 1000, "prompt_ms": 4000}, 250),
        ({"prompt_per_second": 250}, 250),
        ({"prompt_token/s": 250}, 250),
        ({"prompt_eval_duration": 2_000_000_000, "prompt_token/s": 250}, 250),
        ({"tud_prefill_ms": 200, "tud_prefill_tokens": 400, "prompt_tokens": 9000}, 2000),
    ],
)
def test_prefill_provider_units_and_last_tool_call_count(
    usage_display_module: ModuleType, usage: dict[str, Any], expected: float
) -> None:
    timing = usage_display_module.Filter._resolve_prefill_timing(usage)
    assert timing["prefill_tps"] == expected


@pytest.mark.parametrize("usage", [None, {}, {"prompt_ms": 0}, {"prompt_n": -1, "prompt_ms": 10}])
def test_prefill_missing_or_invalid_native_timing_is_hidden(
    usage_display_module: ModuleType, usage: dict[str, Any] | None
) -> None:
    assert usage_display_module.Filter._resolve_prefill_timing(usage)["prefill_tps"] is None


def test_prefill_missing_cache_detail_is_not_assumed_zero(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    event = _prefill_packet()
    del event["usage"]["prompt_tokens_details"]
    run_async(f.stream(event))
    assert f._resolve_prefill_timing(event["usage"])["prefill_tps"] is None


def test_prefill_responses_style_cache_detail(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    event = _prefill_packet()
    event["usage"] = {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200}}
    run_async(f.stream(event))
    assert f._resolve_prefill_timing(event["usage"])["prefill_tps"] == 2000


@pytest.mark.parametrize(
    ("milliseconds", "cached"),
    [(0, 200), (-1, 200), (float("nan"), 200), (True, 200), (400, 1001), (400, -1), (400, 1000)],
)
def test_prefill_invalid_or_fully_cached_measurement_is_hidden(
    usage_display_module: ModuleType, milliseconds: object, cached: int
) -> None:
    f = usage_display_module.Filter()
    event = run_async(f.stream(_prefill_packet(milliseconds=milliseconds, cached=cached)))
    assert f._resolve_prefill_timing(event["usage"])["prefill_tps"] is None


def test_prefill_is_never_estimated_from_full_response_time(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    timing = f._resolve_timing({"prompt_tokens": 15000, "completion_tokens": 1000}, 20, 1000)
    assert timing["prefill_tps"] is None
    assert timing["tps"] == 50


def test_prefill_disabled_valve_and_unknown_ttft_schema(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    f.valves.show_prefill_tokens_per_second = False
    event = _prefill_packet()
    run_async(f.stream(event))
    assert "tud_prefill_ms" not in event["usage"]
    f.valves.show_prefill_tokens_per_second = True
    del event["metrics"]["queue_time_ms"]
    run_async(f.stream(event))
    assert event["usage"]["tud_prefill_ms"] is None


@pytest.mark.parametrize("event", [{}, {"usage": {}}, {"usage": "bad"}])
def test_prefill_stream_ignores_events_without_usage(usage_display_module: ModuleType, event: dict[str, Any]) -> None:
    f = usage_display_module.Filter()
    before = dict(event)
    assert run_async(f.stream(event)) == before


def test_prefill_later_tool_call_without_metrics_clears_previous_rate(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    earlier = run_async(f.stream(_prefill_packet()))
    later = run_async(f.stream({"usage": {"prompt_tokens": 1200, "completion_tokens": 20}}))
    merged = {**earlier["usage"], **later["usage"]}
    assert f._resolve_prefill_timing(merged)["prefill_tps"] is None


def test_prefill_single_output_token_has_no_inter_token_latency(usage_display_module: ModuleType) -> None:
    f = usage_display_module.Filter()
    event = _prefill_packet()
    del event["metrics"]["mean_itl_ms"]
    event["metrics"]["generation_time_ms"] = 0
    run_async(f.stream(event))
    assert f._resolve_prefill_timing(event["usage"])["prefill_tps"] == 2000


@pytest.mark.parametrize(("style", "prefix"), [("emoji", "⏩ "), ("simple", "⇥ "), ("off", "")])
def test_render_prefill_visibility_and_icon_styles(
    usage_display_module: ModuleType, valves: Any, style: str, prefix: str
) -> None:
    render = renderer(usage_display_module, "_render_prefill")
    valves.icon_style = style
    bags = [valves, make_tokens(), make_timing(prefill_tps=250), make_ctx(), make_cost(), None]
    assert render(*bags) == f"{prefix}prefill 250.0 t/s"
    valves.show_prefill_tokens_per_second = False
    assert render(*bags) is None
    valves.show_prefill_tokens_per_second = True
    bags[2] = make_timing(prefill_tps=None)
    assert render(*bags) is None
