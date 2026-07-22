"""
title: Token Usage & Cost Display
author: smetdenis
version: 2.5.1
description: Shows token counts (input/output/total, reasoning, cached, audio), generation time, tokens/sec, context-window utilization and message/chat cost below each AI response. The metric order, separator, icon style (emoji/simple/off), compact number format and a cost-display threshold are admin-configurable. Reads OWUI-normalized usage across providers (OpenAI Chat & Responses API, Anthropic, Gemini, Ollama, llama.cpp), falls back to tiktoken. Cost is native when the provider/proxy reports it (OpenRouter/LiteLLM), or optionally estimated from models.dev prices. Context sizes from a built-in table (seeded from models.dev) with optional live models.dev fetch and llama.cpp/llama-swap probing. Works on Open WebUI 0.9.0+ (built around the 0.10.x structured-output/normalized-usage model; degrades gracefully on 0.9.x). tiktoken is optional (soft import). With debug_mode, the diagnostic payload also carries the selected model/provider (sanitized, safe to share), a full cost breakdown with price provenance (incl. the provider's own cost_details when present), a web-search-usage hint, context-window provenance, and a valves snapshot.
required_open_webui_version: 0.9.0
"""

from __future__ import annotations

# NOTE (0.9.0+ target, built around the 0.10.x model, verified against source):
#   * OWUI runs `normalize_usage`/`merge_usage` on every usage-save path, so a
#     saved `message["usage"]` is GUARANTEED to carry input_tokens/output_tokens/
#     total_tokens, while provider-native keys/detail dicts are preserved.
#     -> primary token read is the normalized triple; provider keys are backup.
#   * `message["content"]` is NOT persisted for streaming chats (text lives in the
#     structured `message["output"]` array); it IS present for non-streaming.
#     -> tiktoken fallback reads content first, then walks `output`.
#   * `message["info"]` is a redundant mirror ({"usage": ...}) of top-level usage
#     on persisted chats -> intentionally ignored to avoid double counting.
#   * Detail keys differ by API: Chat Completions -> prompt_tokens_details /
#     completion_tokens_details; Responses API -> input_tokens_details /
#     output_tokens_details; Anthropic -> top-level cache_read/creation.
#   * merge_usage sums input/output/total + top-level cost (USAGE_COST_KEYS) +
#     *_tokens_details, but NOT top-level Anthropic cache fields nor Ollama
#     durations -> in multi-round tool turns the token totals AND the native cost
#     cover the whole turn (every LLM round-trip is summed), while cache/timing
#     reflect the last round only (an OWUI limitation we surface, not fix).
#     -> the displayed 💰 already covers Open-WebUI-orchestrated tool/MCP calls;
#     what it can't see is a surcharge the provider never puts in usage.cost
#     (e.g. an OpenRouter BYOK web-search engine billed outside the generation).
#   * 0.9.0+ compatible: the outlet body carries per-message `usage` since 0.9.0 and
#     normalize_usage exists since 0.8.0, so tokens/context/timing/estimate all work on
#     0.9.x. Only NATIVE provider cost (USAGE_COST_KEYS) needs 0.10.0+ -> on 0.9.x use
#     cost_mode 'estimate'. Verified via git history, not a live 0.9.x run.

# tiktoken is optional: OWUI bundles it, but per-plugin `requirements:` installs
# fail in some sandboxes (uvx/LXC). Soft-import so the plugin always loads.
try:
    import tiktoken

    _TIKTOKEN_AVAILABLE = True  # pragma: no cover - only reached when tiktoken is installed
except Exception:  # pragma: no cover - environment dependent
    tiktoken = None
    _TIKTOKEN_AVAILABLE = False

# aiohttp is bundled with OWUI; soft-imported only for the optional context probe.
try:
    import aiohttp

    _AIOHTTP_AVAILABLE = True  # pragma: no cover - only reached when aiohttp is installed
except Exception:  # pragma: no cover - environment dependent
    aiohttp = None
    _AIOHTTP_AVAILABLE = False

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- Module-level state --------------------------------------------------------

# Request start times keyed by chat context (wall-clock fallback timing).
_request_timings: dict[str, float] = {}

# Context-window size cache: model_key -> (size, expiry_epoch, provenance).
_ctx_size_cache: dict[str, tuple[int | None, float, dict[str, Any]]] = {}

# Cached models.dev lookup table (id -> context tokens): {"map": dict|None, "expiry": epoch}.
_modelsdev_cache: dict[str, Any] = {"map": None, "expiry": 0.0}

# Cached models.dev price map (id -> {input,output,cache_read,cache_write} USD/1M): {"map": dict|None, "expiry": epoch}.
_modelsdev_prices_cache: dict[str, Any] = {"map": None, "expiry": 0.0}

# Static context-window table — offline default, seeded from models.dev (2026-07).
# Matched as a case-insensitive substring of the model id; the LONGEST matching key
# wins, so specific keys (gpt-4o) override generic ones (gpt-4). Enable the
# `fetch_context_from_modelsdev` valve for always-current sizes across every model.
_STATIC_CONTEXT_SIZES: dict[str, int] = {
    # --- OpenAI ---
    "gpt-5.5": 1050000,
    "gpt-5.4": 1050000,
    "gpt-5": 400000,
    "gpt-4.1": 1047576,
    "gpt-4o": 128000,
    "gpt-4-turbo": 128000,
    "gpt-4": 8192,
    "gpt-3.5": 16385,
    "o4-mini": 200000,
    "o3": 200000,
    "o1": 200000,
    # --- Anthropic (Opus 4.6+/Sonnet 5 moved to 1M) ---
    "claude-opus-4-8": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-opus-4-6": 1000000,
    "claude-sonnet-5": 1000000,
    "claude-sonnet-4-6": 1000000,
    "claude-fable-5": 1000000,
    "claude": 200000,
    # --- Google Gemini (3.x is the current generation) ---
    "gemini-3.5": 1048576,
    "gemini-3.1": 1048576,
    "gemini-3": 1048576,
    "gemini-2.5": 1048576,
    "gemini-2.0": 1048576,
    "gemini-1.5-pro": 2097152,
    "gemini-1.5": 1048576,
    "gemini": 1048576,
    # --- Meta Llama ---
    "llama-4-scout": 3500000,
    "llama-4": 1000000,
    "llama-3.3": 128000,
    "llama-3.1": 128000,
    "llama-3": 128000,
    "llama": 8192,
    # --- DeepSeek ---
    "deepseek-r1": 128000,
    "deepseek-reasoner": 1000000,
    "deepseek-chat": 1000000,
    "deepseek-v4": 1000000,
    "deepseek-flash": 1000000,
    "deepseek": 128000,
    # --- xAI Grok ---
    "grok-4": 1000000,
    "grok-3": 131072,
    "grok": 256000,
    # --- Mistral ---
    "mistral-large": 262144,
    "mistral-medium": 262144,
    "mistral-small": 256000,
    "codestral": 256000,
    "devstral": 262144,
    "magistral": 128000,
    "mistral-nemo": 128000,
    "pixtral": 128000,
    "mistral": 32768,
    # --- Alibaba Qwen ---
    "qwen3-coder": 262144,
    "qwen3-max": 262144,
    "qwen3": 131072,
    "qwq": 131072,
    "qwen2.5": 128000,
    "qwen": 32768,
    # --- Moonshot Kimi ---
    "kimi-k2": 262144,
    "kimi": 200000,
    # --- Zhipu GLM ---
    "glm-5.2": 1000000,
    "glm-5": 204800,
    "glm-4.7": 204800,
    "glm-4.6": 204800,
    "glm-4.5": 131072,
    "glm": 131072,
    # --- MiniMax ---
    "minimax-m3": 512000,
    "minimax": 204800,
    # --- Cohere Command ---
    "command-a": 256000,
    "command-r": 128000,
    "command": 128000,
}

# Static price table — offline fallback for cost estimation (USD per 1M tokens),
# seeded from models.dev (2026-07). Matched like the context table: case-insensitive
# substring of the model id, LONGEST key wins. `cache_read` is the discounted price
# for cached-prompt tokens; `cache_write` (Anthropic only) is the cache-write premium.
# Prices are provider-specific — these use each family's first-party ("canonical")
# provider, so an estimate is only ever an approximation. Enable
# `fetch_prices_from_modelsdev` for exact, always-current per-provider prices.
# NOTE: local/free backends (Ollama, llama.cpp, Meta's free Llama API) are omitted on
# purpose — their price is $0 or varies by host, so estimating would mislead.
_STATIC_PRICES: dict[str, dict[str, float]] = {
    # --- OpenAI ---
    "gpt-5.5": {"input": 5.00, "output": 30.00, "cache_read": 0.50},
    "gpt-5-mini": {"input": 0.25, "output": 2.00, "cache_read": 0.025},
    "gpt-5": {"input": 1.25, "output": 10.00, "cache_read": 0.125},
    "gpt-4.1": {"input": 2.00, "output": 8.00, "cache_read": 0.50},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075},
    "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25},
    "o4-mini": {"input": 1.10, "output": 4.40, "cache_read": 0.275},
    "o3": {"input": 2.00, "output": 8.00, "cache_read": 0.50},
    # --- Anthropic (cache_write = cache-write premium) ---
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    # --- Google Gemini ---
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00, "cache_read": 0.15},
    "gemini-3-pro": {"input": 2.00, "output": 12.00, "cache_read": 0.20},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cache_read": 0.125},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03},
    # --- DeepSeek ---
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87, "cache_read": 0.003625},
    "deepseek-v4": {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
    "deepseek-reasoner": {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
    "deepseek-chat": {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
    # --- xAI Grok ---
    "grok-4": {"input": 1.25, "output": 2.50, "cache_read": 0.20},
    # --- Mistral ---
    "mistral-large": {"input": 0.50, "output": 1.50},
    "mistral-medium": {"input": 0.40, "output": 2.00},
    "mistral-small": {"input": 0.15, "output": 0.60},
    "pixtral": {"input": 2.00, "output": 6.00},
    # --- Alibaba Qwen ---
    "qwen3-coder": {"input": 1.00, "output": 5.00},
    "qwen3-max": {"input": 1.20, "output": 6.00},
    # --- Moonshot Kimi ---
    "kimi-k2.6": {"input": 0.95, "output": 4.00, "cache_read": 0.16},
    "kimi-k2": {"input": 0.60, "output": 2.50, "cache_read": 0.15},
    # --- Zhipu GLM ---
    "glm-5.2": {"input": 1.40, "output": 4.40, "cache_read": 0.26},
    "glm-5": {"input": 1.00, "output": 3.20, "cache_read": 0.20},
    "glm-4.6": {"input": 0.60, "output": 2.20, "cache_read": 0.11},
    # --- MiniMax ---
    "minimax-m3": {"input": 0.30, "output": 1.20, "cache_read": 0.06},
    # --- Cohere Command ---
    "command-a": {"input": 2.50, "output": 10.00},
    "command-r": {"input": 0.15, "output": 0.60},
}


# --- Helpers -------------------------------------------------------------------


def _num(value: Any) -> int | float | None:
    """Return the value if it is a real (non-bool) number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _first_num(src: Any, *keys: str) -> int | float | None:
    """First present numeric value among keys (0 is valid, unlike `or`-chains)."""
    if not isinstance(src, dict):
        return None
    for key in keys:
        val = _num(src.get(key))
        if val is not None:
            return val
    return None


def _detail_num(usage: Any, group: str, key: str) -> int | float | None:
    """Read usage[group][key] as a number (e.g. completion_tokens_details.reasoning_tokens)."""
    if not isinstance(usage, dict):
        return None
    group_dict = usage.get(group)
    if isinstance(group_dict, dict):
        return _num(group_dict.get(key))
    return None


def _get_last_assistant_message_obj(messages: list[Any]) -> dict[str, Any]:
    """Return the last assistant message dict from the message list."""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            return message
    return {}


def _extract_output_text(output: Any) -> str:
    """Concatenate visible assistant text from a structured `output` array.

    Mirrors OWUI's own convert_output_to_messages: only `message` items and their
    `output_text` parts are visible text. Reasoning items are excluded (they are
    counted separately as reasoning tokens and are often hidden by the provider).
    """
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text", "")
                if text:
                    parts.append(text if isinstance(text, str) else str(text))
    return "".join(parts)


def _message_text(message: dict[str, Any]) -> str:
    """Best-effort visible text of a message: content if present, else output."""
    content = message.get("content", "")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(item.get("text", ""))
            elif isinstance(item, str):
                chunks.append(item)
        if chunks:
            return " ".join(c for c in chunks if c)
    # Streaming assistant messages keep their text only in `output`.
    return _extract_output_text(message.get("output", []))


def _count_tokens_tiktoken(text: str, model: str = "") -> int | None:
    """Estimate token count with tiktoken. Returns None if tiktoken is unavailable."""
    if not _TIKTOKEN_AVAILABLE or not text:
        return None if not _TIKTOKEN_AVAILABLE else 0
    try:
        encoding = tiktoken.encoding_for_model(model)
    except (KeyError, ValueError):
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


def _format_duration(seconds: float) -> str:
    """Human-friendly elapsed time."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"


def _format_k(n: float) -> str:
    """Compact k/M token formatting for context display (e.g. 3.5k, 1.0M)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n)}"


def _format_cost(usd: float) -> str:
    """USD with precision scaled to magnitude (e.g. $1.23, $0.0123, <$0.0001)."""
    if usd <= 0:
        return "$0.00"
    if usd < 0.0001:
        return "<$0.0001"
    if usd < 1:
        return f"${usd:.4f}"
    return f"${usd:,.2f}"


def _url_host(url: str) -> str:
    """Best-effort lowercased host from an http(s) URL (stdlib `re`, no extra import)."""
    m = re.match(r"https?://([^/?#]+)", url.strip())
    return m.group(1).lower() if m else ""


def _longest_key_match(table: dict[str, Any], model_id: str) -> str | None:
    """Longest case-insensitive substring key of `table` contained in `model_id`, else None.

    Single source of truth for the substring lookup used by both context-size and price
    resolution: specific keys (gpt-4o) beat generic ones (gpt-4). Empty keys are ignored.
    """
    if not isinstance(table, dict) or not table:
        return None
    mid = (model_id or "").lower()
    if not mid:
        return None
    for key in sorted(table, key=len, reverse=True):
        if key and key in mid:
            return key
    return None


def _modelsdev_match(table: dict[str, Any], model_id: str) -> str | None:
    """models.dev key matching `model_id`: exact -> bare (last path segment) -> longest substring."""
    if not isinstance(table, dict) or not table:
        return None
    mid = (model_id or "").lower()
    if mid in table:
        return mid
    bare = mid.split("/")[-1]
    if bare in table:
        return bare
    return _longest_key_match(table, model_id)


def _resolve_model_id(model: dict[str, Any] | None, body: dict[str, Any] | None = None) -> str:
    """Model id used for provider-table matching (context size, price, tiktoken).

    Prefers a workspace model's `info.base_model_id` — the real underlying LLM — over the
    top-level `id`, which for a custom/agent model is an arbitrary user label (e.g. "research")
    that carries no provider token and so never matches the context/price tables. This mirrors
    what OWUI itself does (it swaps in base_model_id for the actual LLM call) and what the
    model-name metric already displays, so one map entry keyed on the base model covers every
    agent built on it. Falls back to the top-level id, then body["model"], then "".
    """
    if isinstance(model, dict):
        info = model.get("info")
        base = info.get("base_model_id") if isinstance(info, dict) else None
        if base:
            return str(base)
        mid = model.get("id", "") or ""
        if mid:
            return str(mid)
    if isinstance(body, dict):
        return body.get("model", "") or ""
    return ""


# --- stats-line order (see configurable-order design doc) ----------------------

# The 13 metric keys in their default display order (identical to the historical
# hard-coded order of _build_stats). Registry populated in the renderers section.
_DEFAULT_ORDER: list[str] = [
    "input",
    "output",
    "total",
    "reasoning",
    "cached",
    "audio",
    "context",
    "time",
    "tps",
    "cost",
    "cost_total",
    "model",
    "source",
]
_STATS_KEYS: frozenset[str] = frozenset(_DEFAULT_ORDER)


def _normalize_order(order_str: str) -> tuple[list[str], list[str]]:
    """Split a comma-separated order string into (valid_keys_deduped, ignored_unknown).

    Case-insensitive, whitespace-tolerant, order-preserving; blanks are skipped.
    """
    valid: list[str] = []
    unknown: list[str] = []
    for raw in (order_str or "").split(","):
        key = raw.strip().lower()
        if not key:
            continue
        if key in _STATS_KEYS:
            if key not in valid:
                valid.append(key)
        elif key not in unknown:
            unknown.append(key)
    return valid, unknown


def _resolve_display_order(admin_order: str) -> list[str]:
    """Resolve the admin-configured metric order, else the default.

    Returns a permutation of all 13 keys: the parsed (valid) keys first, then the
    remaining keys in default order. Empty input -> _DEFAULT_ORDER unchanged.
    """
    valid, _ = _normalize_order(admin_order)
    return valid + [k for k in _DEFAULT_ORDER if k not in valid]


def _display_order_debug(admin_order: str) -> dict[str, Any]:
    """Provenance of the resolved order for the debug payload (debug_mode only)."""
    au = (admin_order or "").strip()
    source, order_str = ("admin", au) if au else ("default", "")
    valid, unknown = _normalize_order(order_str)
    resolved = valid + [k for k in _DEFAULT_ORDER if k not in valid]
    return {
        "source": source,
        "admin_order": admin_order,
        "parsed": valid,
        "ignored_unknown": unknown,
        "resolved": resolved,
    }


# --- icons & number formatting (icon_style / compact_numbers) ------------------
# Icons are kept OUT of the renderer bodies so a single icon_style switch can swap
# the whole set. `off` yields "" (bare value); `simple` is monochrome unicode.
_ICON_EMOJI: dict[str, str] = {
    "input": "⬆︎",
    "output": "⬇︎",
    "total": "Σ",
    "reasoning": "🧠",
    "cached": "💾",
    "audio": "🔊",
    "time": "⏱",
    "tps": "⚡",
    "cost": "💰",
    "cost_total": "💰Σ",
    "model": "🤖",
}
_ICON_SIMPLE: dict[str, str] = {
    "input": "↑",
    "output": "↓",
    "total": "Σ",
    "reasoning": "∴",
    "cached": "≡",
    "audio": "♪",
    "time": "◷",
    "tps": "»",
    "cost": "",
    "cost_total": "Σ",
    "model": "◇",  # cost value already carries "$"
}
# context has a severity triplet (normal / warn / critical) instead of a flat icon.
_CONTEXT_ICONS: dict[str, tuple[str, str, str]] = {
    "emoji": ("📐", "🟠", "🔴"),
    "simple": ("○", "◐", "●"),
    "off": ("", "", ""),
}


def _icon(v: Filter.Valves, key: str) -> str:
    """Icon glyph for a metric key under the current icon_style ('' when off/none)."""
    style = getattr(v, "icon_style", "emoji")
    if style == "off":
        return ""
    table = _ICON_SIMPLE if style == "simple" else _ICON_EMOJI
    return table.get(key, "")


def _with_icon(icon: str, body: str) -> str:
    """Prefix a value with its icon, or return the bare value when there is no icon."""
    return f"{icon} {body}" if icon else body


def _context_icon(v: Filter.Valves, pct: float) -> str:
    """Severity icon for context utilization under the current icon_style."""
    style = getattr(v, "icon_style", "emoji")
    normal, warn, crit = _CONTEXT_ICONS.get(style, _CONTEXT_ICONS["emoji"])
    if pct >= v.context_critical_percent:
        return crit
    if pct >= v.context_warn_percent:
        return warn
    return normal


def _fmt_count(v: Filter.Valves, n: Any) -> str:
    """Format a token counter: compact k/M when compact_numbers, else grouped digits."""
    if getattr(v, "compact_numbers", False):
        return _format_k(n)
    return f"{int(n):,}"


# --- stats-line renderers (one per metric key; None = omitted) ------------------
# show_* gating lives here, so a disabled or data-less metric returns None and simply
# does not appear. Icons come from _icon/_context_icon; counters from _fmt_count.


def _render_input(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_input_tokens and tokens["input"] is not None:
        return _with_icon(_icon(v, "input"), _fmt_count(v, tokens["input"]))
    return None


def _render_output(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_output_tokens and tokens["output"] is not None:
        return _with_icon(_icon(v, "output"), _fmt_count(v, tokens["output"]))
    return None


def _render_total(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_total_tokens and tokens["total"] is not None:
        return _with_icon(_icon(v, "total"), _fmt_count(v, tokens["total"]))
    return None


def _render_reasoning(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_reasoning_tokens and tokens["reasoning"]:
        return _with_icon(_icon(v, "reasoning"), _fmt_count(v, tokens["reasoning"]))
    return None


def _render_cached(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_cached_tokens and tokens["cached"]:
        return _with_icon(_icon(v, "cached"), _fmt_count(v, tokens["cached"]))
    return None


def _render_audio(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_audio_tokens and tokens["audio"]:
        return _with_icon(_icon(v, "audio"), _fmt_count(v, tokens["audio"]))
    return None


def _render_context(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_context_window and ctx["size"] and ctx["used"] is not None:
        pct = (ctx["used"] / ctx["size"]) * 100 if ctx["size"] else 0
        body = f"{_format_k(ctx['used'])}/{_format_k(ctx['size'])} ({pct:.0f}%)"
        return _with_icon(_context_icon(v, pct), body)
    return None


def _render_time(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_generation_time and timing["seconds"] is not None:
        prefix = "~" if timing["source"] == "wall" else ""
        return _with_icon(_icon(v, "time"), f"{prefix}{_format_duration(timing['seconds'])}")
    return None


def _render_tps(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_tokens_per_second and timing["tps"]:
        return _with_icon(_icon(v, "tps"), f"{timing['tps']:.1f} t/s")
    return None


def _render_cost(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    # No dedicated show_* flag: visibility is driven by cost_mode via the precomputed
    # cost dict (message is None when cost_mode == off or no price). cost_min_display
    # additionally hides negligible amounts (default 0.0 hides nothing).
    if cost["message"] is not None and cost["message"] >= v.cost_min_display:
        prefix = "≈" if cost["message_est"] else ""
        return _with_icon(_icon(v, "cost"), f"{prefix}{_format_cost(cost['message'])}")
    return None


def _render_cost_total(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if (
        cost["cumulative"] is not None
        and v.show_cumulative_cost
        and cost["cumulative"] >= v.cost_min_display
        and (cost["message"] is None or abs(cost["cumulative"] - (cost["message"] or 0)) > 1e-9)
    ):
        prefix = "≈" if cost["cumulative_est"] else ""
        return _with_icon(_icon(v, "cost_total"), f"{prefix}{_format_cost(cost['cumulative'])}")
    return None


def _render_model(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    if v.show_model_name and isinstance(model, dict):
        info = model.get("info")
        base = info.get("base_model_id") if isinstance(info, dict) else None
        if base:
            return _with_icon(_icon(v, "model"), base)
    return None


def _render_source(
    v: Filter.Valves,
    tokens: dict[str, Any],
    timing: dict[str, Any],
    ctx: dict[str, Any],
    cost: dict[str, Any],
    model: dict[str, Any] | None,
) -> str | None:
    # The "not shown on its own" guard lives in _build_stats, not here. No icon in any
    # style: the [API]/[est.] brackets are self-labeling.
    if v.show_data_source:
        return f"[{'API' if tokens['is_api'] else 'est.'}]"
    return None


_STATS_RENDERERS: dict[str, Any] = {
    "input": _render_input,
    "output": _render_output,
    "total": _render_total,
    "reasoning": _render_reasoning,
    "cached": _render_cached,
    "audio": _render_audio,
    "context": _render_context,
    "time": _render_time,
    "tps": _render_tps,
    "cost": _render_cost,
    "cost_total": _render_cost_total,
    "model": _render_model,
    "source": _render_source,
}


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=10,
            description="Filter priority (lower runs first). Keep high so this runs after other filters.",
        )
        show_input_tokens: bool = Field(default=True, description="Display input (prompt) token count.")
        show_output_tokens: bool = Field(default=True, description="Display output (completion) token count.")
        show_total_tokens: bool = Field(default=True, description="Display total token count.")
        show_generation_time: bool = Field(default=True, description="Display generation time.")
        show_tokens_per_second: bool = Field(default=True, description="Display output tokens per second.")
        show_reasoning_tokens: bool = Field(
            default=True, description="Display reasoning/thinking tokens (o1/o3/o4, Gemini thinking)."
        )
        show_cached_tokens: bool = Field(default=True, description="Display cached prompt tokens (cache hits).")
        show_audio_tokens: bool = Field(default=False, description="Display audio tokens (gpt-4o-audio etc.).")
        show_model_name: bool = Field(default=True, description="Display base model name for workspace models.")
        show_data_source: bool = Field(default=False, description="Append [API]/[est.] to indicate token count source.")
        display_order: str = Field(
            default=", ".join(_DEFAULT_ORDER),
            description=(
                "Comma-separated metric order, pre-filled with the default order — reorder or trim "
                "to taste (empty also means default). Keys: input, output, total, reasoning, cached, "
                "audio, context, time, tps, cost, cost_total, model, source. This only sorts: "
                "visibility is governed by the show_* toggles, and removing a key here does NOT hide "
                "it (use show_* for that) — it just moves to the end, like any enabled-but-unlisted metric."
            ),
        )
        separator: str = Field(
            default=" · ",
            description="String placed between stats items in the line (default ' · ').",
        )
        icon_style: Literal["emoji", "simple", "off"] = Field(
            default="emoji",
            description=(
                "Icon rendering: 'emoji' (colorful, default), 'simple' (monochrome unicode symbols), "
                "'off' (no icons — bare values; pure counters like input/output/total become ambiguous, "
                "while cost/context/time/tps/source stay self-labeled via $, %, s, t/s, [API])."
            ),
        )
        compact_numbers: bool = Field(
            default=False,
            description=(
                "Abbreviate token counters as k/M (e.g. 12,345 -> 12.3k, 1,234,567 -> 1.2M), matching "
                "the context-window style. Off = full numbers with thousands separators. Affects only "
                "the six counters (input, output, total, reasoning, cached, audio)."
            ),
        )
        fallback_to_tiktoken: bool = Field(
            default=True, description="Estimate tokens with tiktoken when the API reports no usage (needs tiktoken)."
        )
        count_all_messages_for_input: bool = Field(
            default=True,
            description="When estimating input tokens, count the whole conversation, not just the last turn.",
        )
        # --- Context-window utilization ---
        show_context_window: bool = Field(
            default=True, description="Display context-window utilization (used/available + %)."
        )
        context_size_override: int = Field(
            default=0, description="Force a context-window size (tokens). 0 = auto-detect. Highest priority."
        )
        context_size_map: str = Field(
            default="",
            description=(
                'JSON object of {"model-substring": context_tokens}. An explicit override: checked '
                "BEFORE the models.dev fetch and the built-in table, so it always wins on a match."
            ),
        )
        fetch_context_from_modelsdev: bool = Field(
            default=False,
            description="Live context sizes from models.dev (opt-in, cached). Overrides the static table on match.",
        )
        modelsdev_url: str = Field(
            default="https://models.dev/models.json",
            description="models.dev endpoint used when fetch_context_from_modelsdev is on.",
        )
        modelsdev_ttl: int = Field(
            default=86400, description="Seconds to cache the fetched models.dev context table (default 24h)."
        )
        context_warn_percent: int = Field(default=30, description="Context %% at which the icon turns orange.")
        context_critical_percent: int = Field(default=70, description="Context %% at which the icon turns red.")
        llamacpp_url: str = Field(
            default="",
            description="Optional llama.cpp base URL (e.g. http://127.0.0.1:8080) for /props n_ctx probe. Empty=off.",
        )
        llama_swap_url: str = Field(
            default="",
            description="Optional llama-swap base URL to probe /running for --ctx-size. Empty = off.",
        )
        context_probe_ttl: int = Field(default=600, description="Seconds to cache a probed/resolved context size.")
        # --- Cost ---
        cost_mode: Literal["off", "auto", "estimate"] = Field(
            default="auto",
            description=(
                "off = never show/compute cost; auto = show only the provider's own cost "
                "(OpenRouter/LiteLLM), never estimate or fetch; estimate = also approximate cost "
                "from models.dev prices when the provider reports none (marked ≈)."
            ),
        )
        show_cumulative_cost: bool = Field(
            default=True, description="Also show the running total cost for the whole chat."
        )
        cost_min_display: float = Field(
            default=0.0,
            description=(
                "Hide message/cumulative cost when its USD value is below this threshold. Default 0.0 "
                "shows everything (including $0.00). E.g. 0.0001 drops negligible sub-$0.0001 costs."
            ),
        )
        price_map: str = Field(
            default="",
            description=(
                'JSON of {"model-substring": {"input": USD_per_1M, "output": USD_per_1M, '
                '"cache_read": USD_per_1M, "cache_write": USD_per_1M}}; highest priority in estimate mode.'
            ),
        )
        fetch_prices_from_modelsdev: bool = Field(
            default=True,
            description=(
                "In estimate mode, fetch live per-provider prices from models.dev (cached ~24h). "
                "Off = static table only."
            ),
        )
        modelsdev_api_url: str = Field(
            default="https://models.dev/api.json",
            description="models.dev price endpoint used when fetch_prices_from_modelsdev is on.",
        )
        debug_mode: bool = Field(
            default=False, description="Emit an extra status event with the raw payload for diagnostics."
        )

    class UserValves(BaseModel):
        enabled: bool = Field(default=True, description="Show token usage stats below responses.")

    def __init__(self) -> None:
        self.valves = self.Valves()

    # --- inlet: record start time + capture context hints ----------------------

    async def inlet(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stash a start timestamp and any context-size hint for the outlet."""
        chat_id = (__metadata__ or {}).get("chat_id", "") or ""
        message_id = (__metadata__ or {}).get("message_id", "") or ""
        key = f"{chat_id}:{message_id}" if (chat_id or message_id) else f"fallback:{id(body)}"

        start_time = time.time()
        _request_timings[key] = start_time

        # num_ctx is only visible in the inbound body (OWUI strips model.info.params).
        num_ctx = (
            _first_num(body, "num_ctx")
            or _first_num(body.get("options", {}) if isinstance(body.get("options"), dict) else {}, "num_ctx")
            or _first_num(body.get("params", {}) if isinstance(body.get("params"), dict) else {}, "num_ctx")
        )

        if "metadata" not in body or not isinstance(body.get("metadata"), dict):
            body["metadata"] = {}
        body["metadata"]["_tud_timing_key"] = key
        body["metadata"]["_tud_start"] = start_time
        if num_ctx:
            body["metadata"]["_tud_num_ctx"] = int(num_ctx)

        if __metadata__ is not None:
            __metadata__["_tud_timing_key"] = key
            __metadata__["_tud_start"] = start_time
            if num_ctx:
                __metadata__["_tud_num_ctx"] = int(num_ctx)

        return body

    # --- outlet: build and emit the stats line ---------------------------------

    async def outlet(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        __metadata__: dict[str, Any] | None = None,
        __model__: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_valves = __user__.get("valves") if __user__ else None
        if user_valves is not None and hasattr(user_valves, "enabled") and not user_valves.enabled:
            return body

        # Skip non-chat background tasks (title/tag/query/etc.).
        task = (__metadata__ or {}).get("task")
        if task in (
            "title_generation",
            "tags_generation",
            "follow_up_generation",
            "emoji_generation",
            "query_generation",
            "autocomplete_generation",
            "moa_response_generation",
        ):
            return body

        messages = body.get("messages", [])
        if not messages:
            return body
        assistant_msg = _get_last_assistant_message_obj(messages)
        if not assistant_msg:
            return body

        v = self.valves

        elapsed_seconds = self._resolve_wall_clock(body, __metadata__)
        usage = assistant_msg.get("usage") if isinstance(assistant_msg.get("usage"), dict) else None

        tokens = self._extract_tokens(usage, assistant_msg, messages, body, __model__)
        timing = self._resolve_timing(usage, elapsed_seconds, tokens["output"])
        try:
            ctx = await self._resolve_context(body, __metadata__, __model__, tokens)
        except Exception:
            ctx = {"size": None, "used": None, "source": "error", "matched_key": None}

        model_id = _resolve_model_id(__model__, body)
        try:
            cost = await self._resolve_cost(usage, tokens, messages, model_id)
        except Exception:
            cost = {"message": None, "message_est": False, "cumulative": None, "cumulative_est": False}

        stats_parts = self._build_stats(v, tokens, timing, ctx, cost, __model__)
        if v.debug_mode:
            stats_parts.append("(debug)")

        if stats_parts and __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": v.separator.join(stats_parts), "done": True},
                }
            )

        if v.debug_mode and __event_emitter__:
            await self._emit_debug(
                __event_emitter__,
                task,
                messages,
                assistant_msg,
                usage,
                tokens,
                timing,
                ctx,
                cost,
                __model__,
                __metadata__,
            )

        return body

    # --- token extraction ------------------------------------------------------

    def _extract_tokens(
        self,
        usage: dict[str, Any] | None,
        assistant_msg: dict[str, Any],
        messages: list[Any],
        body: dict[str, Any],
        model: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Return a normalized bag of token counts across all providers/APIs."""
        result: dict[str, Any] = {
            "input": None,
            "output": None,
            "total": None,
            "reasoning": None,
            "cached": None,
            "cache_write": None,
            "audio": None,
            "is_api": False,
            "is_anthropic": False,
            "input_has_cache": False,
            "fresh_input": None,
        }

        if usage:
            # Primary: OWUI-normalized triple; provider keys as backup.
            result["input"] = _first_num(usage, "input_tokens", "prompt_tokens", "prompt_eval_count", "prompt_n")
            result["output"] = _first_num(usage, "output_tokens", "completion_tokens", "eval_count", "predicted_n")
            if result["input"] is not None or result["output"] is not None:
                result["is_api"] = True

            # Reasoning: Chat Completions vs Responses API naming.
            result["reasoning"] = _detail_num(usage, "completion_tokens_details", "reasoning_tokens") or _detail_num(
                usage, "output_tokens_details", "reasoning_tokens"
            )

            # Cache tokens + cache semantics (subset-of-input vs on-top) — see _cache_and_fresh.
            cache = self._cache_and_fresh(usage, result["input"])
            result["cached"] = cache["cached"]
            result["cache_write"] = cache["cache_write"]
            result["is_anthropic"] = cache["is_anthropic"]
            result["input_has_cache"] = cache["input_has_cache"]
            result["fresh_input"] = cache["fresh_input"]

            # Audio tokens (input+output), either naming.
            audio_out = _detail_num(usage, "completion_tokens_details", "audio_tokens") or _detail_num(
                usage, "output_tokens_details", "audio_tokens"
            )
            audio_in = _detail_num(usage, "prompt_tokens_details", "audio_tokens") or _detail_num(
                usage, "input_tokens_details", "audio_tokens"
            )
            total_audio = (audio_in or 0) + (audio_out or 0)
            result["audio"] = total_audio or None

        # Fallback estimation when the provider reported no usage.
        if not result["is_api"] and self.valves.fallback_to_tiktoken and _TIKTOKEN_AVAILABLE:
            self._estimate_tokens(result, assistant_msg, messages, body, model)

        # Derived total: fresh input + cache (read+write) + output — correct for every shape.
        result["total"] = self._compute_total(result)
        return result

    def _estimate_tokens(
        self,
        result: dict[str, Any],
        assistant_msg: dict[str, Any],
        messages: list[Any],
        body: dict[str, Any],
        model: dict[str, Any] | None,
    ) -> None:
        """tiktoken estimate of input/output tokens when the provider reported no usage."""
        model_id = _resolve_model_id(model, body)

        response_text = _message_text(assistant_msg)
        if response_text:
            est_out = _count_tokens_tiktoken(response_text, model_id)
            if est_out is not None:
                result["output"] = est_out

        if self.valves.count_all_messages_for_input:
            parts = [_message_text(m) for m in messages if m is not assistant_msg]
            est_in = _count_tokens_tiktoken(" ".join(p for p in parts if p), model_id)
        else:
            est_in = None
            for m in reversed(messages):
                if isinstance(m, dict) and m.get("role") == "user":
                    est_in = _count_tokens_tiktoken(_message_text(m), model_id)
                    break
        if est_in is not None:
            result["input"] = est_in

    @staticmethod
    def _cache_and_fresh(usage: dict[str, Any], reported_input: Any) -> dict[str, Any]:
        """Cache read/write tokens + whether the reported input already includes them.

        Cache is reported with two different semantics depending on provider/proxy shape:
          * OpenAI / DeepSeek / Gemini / Anthropic-*via-LiteLLM* put cache inside
            `*_tokens_details` -> cache is a SUBSET of the reported prompt/input tokens
            (LiteLLM folds Anthropic cache INTO prompt_tokens, so it must be subtracted back out).
          * Anthropic *native* reports only top-level cache_read/creation_input_tokens and its
            input EXCLUDES cache -> cache is billed ON TOP.
        `fresh_input` is the full-price (uncached) input for BOTH shapes, so callers price and
        total uniformly without a per-provider branch.
        """
        pt_cached = _detail_num(usage, "prompt_tokens_details", "cached_tokens") or _detail_num(
            usage, "input_tokens_details", "cached_tokens"
        )
        pt_cache_creation = _detail_num(usage, "prompt_tokens_details", "cache_creation_tokens")
        anth_read = _num(usage.get("cache_read_input_tokens"))
        anth_write = _num(usage.get("cache_creation_input_tokens"))
        has_toplevel_anth = anth_read is not None or anth_write is not None

        cached = anth_read if has_toplevel_anth else pt_cached
        cache_write = anth_write if has_toplevel_anth else pt_cache_creation

        # Pure Anthropic-native = top-level cache keys but NO OpenAI-style detail dict. Anything
        # carrying *_tokens_details (incl. LiteLLM's Anthropic mapping) already folds cache into input.
        native_anthropic = has_toplevel_anth and pt_cached is None and pt_cache_creation is None
        input_has_cache = not native_anthropic

        fresh_input = reported_input
        if reported_input is not None and input_has_cache:
            fresh_input = max(reported_input - (cached or 0) - (cache_write or 0), 0)

        return {
            "cached": cached,
            "cache_write": cache_write,
            "is_anthropic": has_toplevel_anth,
            "input_has_cache": input_has_cache,
            "fresh_input": fresh_input,
        }

    def _compute_total(self, result: dict[str, Any]) -> int | None:
        """Total tokens = fresh input + cache (read + write) + output.

        Equals the provider's total for subset-cache shapes (OpenAI/DeepSeek/Gemini/LiteLLM),
        and correctly adds cache for Anthropic-native (whose reported input omits it). fresh_input
        falls back to the raw input on the tiktoken-estimate path (no cache there).
        """
        inp = result["input"]
        out = result["output"]
        if inp is None and out is None:
            return None
        fresh = result.get("fresh_input")
        if fresh is None:
            fresh = inp
        return int((fresh or 0) + (result["cached"] or 0) + (result["cache_write"] or 0) + (out or 0))

    # --- timing ----------------------------------------------------------------

    def _resolve_wall_clock(self, body: dict[str, Any], metadata: dict[str, Any] | None) -> float | None:
        """Wall-clock seconds from inlet->outlet (multi-level fallback + leak cleanup)."""
        start_time = None
        if metadata:
            start_time = metadata.get("_tud_start")
        if start_time is None:
            body_meta = body.get("metadata", {})
            if isinstance(body_meta, dict):
                start_time = body_meta.get("_tud_start")

        # Reconstruct the module-dict key and ALWAYS pop it (fixes the leak where
        # the metadata path resolved timing but never released the module entry).
        timing_key = None
        if metadata:
            timing_key = metadata.get("_tud_timing_key")
        if not timing_key:
            body_meta = body.get("metadata", {})
            if isinstance(body_meta, dict):
                timing_key = body_meta.get("_tud_timing_key")
        if not timing_key and metadata:
            chat_id = metadata.get("chat_id", "") or ""
            message_id = metadata.get("message_id", "") or ""
            if chat_id or message_id:
                timing_key = f"{chat_id}:{message_id}"
        if timing_key:
            popped = _request_timings.pop(timing_key, None)
            if start_time is None:
                start_time = popped

        # Drop stale entries so the module dict cannot grow unbounded.
        cutoff = time.time() - 600
        for k in [k for k, ts in _request_timings.items() if ts < cutoff]:
            _request_timings.pop(k, None)

        return (time.time() - start_time) if start_time is not None else None

    def _resolve_timing(
        self, usage: dict[str, Any] | None, wall_seconds: float | None, output_tokens: Any
    ) -> dict[str, Any]:
        """Prefer provider-reported generation time/tps; else labeled wall-clock."""
        gen_seconds = None
        tps = None
        source = None

        if usage:
            eval_dur = _num(usage.get("eval_duration"))  # Ollama, nanoseconds
            predicted_ms = _num(usage.get("predicted_ms"))  # llama.cpp
            if eval_dur and eval_dur > 0:
                gen_seconds, source = eval_dur / 1e9, "provider"
            elif predicted_ms and predicted_ms > 0:
                gen_seconds, source = predicted_ms / 1000.0, "provider"

            ollama_tps = usage.get("response_token/s")
            llama_tps = _num(usage.get("predicted_per_second"))
            if _num(ollama_tps) is not None:
                tps = _num(ollama_tps)
            elif llama_tps is not None:
                tps = llama_tps
            elif source == "provider" and output_tokens and gen_seconds:
                tps = output_tokens / gen_seconds

        if gen_seconds is None and wall_seconds is not None:
            gen_seconds, source = wall_seconds, "wall"
        if tps is None and output_tokens and wall_seconds and wall_seconds > 0:
            tps = output_tokens / wall_seconds  # approximate; wall-clock includes pre-processing

        return {"seconds": gen_seconds, "tps": tps, "source": source}

    # --- context window --------------------------------------------------------

    async def _resolve_context(
        self,
        body: dict[str, Any],
        metadata: dict[str, Any] | None,
        model: dict[str, Any] | None,
        tokens: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.valves.show_context_window:
            return {"size": None, "used": None, "source": "disabled", "matched_key": None}

        used = tokens["total"]
        if used is None:
            used = (tokens["input"] or 0) + (tokens["output"] or 0) or None

        model_id = _resolve_model_id(model, body)

        size, prov = await self._context_size_for(model_id, metadata)
        return {"size": size, "used": used, "source": prov["source"], "matched_key": prov["matched_key"]}

    async def _context_size_for(
        self, model_id: str, metadata: dict[str, Any] | None
    ) -> tuple[int | None, dict[str, Any]]:
        """Resolve context size + provenance ({source, matched_key}). Provenance is debug-only.

        Resolution order: override -> num_ctx hint -> user context_size_map -> live models.dev ->
        static table -> local-backend probe. The user's context_size_map is an explicit override,
        so it beats the automatic models.dev fetch (mirrors how price_map wins over models.dev).
        """
        v = self.valves
        # 1) Explicit override.
        if v.context_size_override and v.context_size_override > 0:
            return int(v.context_size_override), {"source": "override", "matched_key": None}
        # 2) num_ctx captured at inlet (Ollama/local models — the actual running window, ground truth).
        hint = (metadata or {}).get("_tud_num_ctx")
        if isinstance(hint, int) and hint > 0:
            return hint, {"source": "num_ctx", "matched_key": None}
        # 3) User context_size_map — an explicit manual override, checked BEFORE the live models.dev
        #    fetch (mirrors how price_map beats models.dev for cost). Returned uncached, like
        #    override/num_ctx, so valve edits take effect at once and are never masked by a stale
        #    _ctx_size_cache entry. Falsy sizes (e.g. 0) fall through to the automatic sources.
        user_map = self._context_size_map_table()
        umkey = _longest_key_match(user_map, model_id)
        if umkey is not None and user_map[umkey]:
            return int(user_map[umkey]), {"source": "user_map", "matched_key": umkey}

        cache_key = model_id.lower()
        cached = _ctx_size_cache.get(cache_key)
        if cached and cached[1] > time.time():
            prov = cached[2] if len(cached) > 2 else {"source": "cache", "matched_key": None}
            return cached[0], prov

        size = None
        prov = {"source": "none", "matched_key": None}
        # 4) Live models.dev lookup (opt-in; cached ~24h).
        if v.fetch_context_from_modelsdev:
            table = await self._modelsdev_map()
            key = _modelsdev_match(table, model_id)
            if key is not None:
                size = table[key]
                prov = {"source": "modelsdev", "matched_key": key}
        # 5) Static table, matched by substring.
        if size is None:
            table = self._context_table()
            key = _longest_key_match(table, model_id)
            if key is not None:
                size = table[key]
                prov = {"source": "static_table", "matched_key": key}
        # 6) Optional endpoint probe for local backends (opt-in via valve URL).
        if size is None and (v.llamacpp_url or v.llama_swap_url):
            size = await self._probe_context(model_id)
            if size is not None:
                prov = {"source": "probe", "matched_key": None}

        if size is not None:
            _ctx_size_cache[cache_key] = (size, time.time() + max(60, v.context_probe_ttl), prov)
        return size, prov

    async def _modelsdev_map(self) -> dict[str, Any]:
        """Fetch and cache {model_id -> context_tokens} from models.dev. Non-fatal."""
        now = time.time()
        cached_map = _modelsdev_cache.get("map")
        if cached_map is not None and _modelsdev_cache.get("expiry", 0) > now:
            return cached_map  # type: ignore[no-any-return]

        result: dict[str, Any] = {}
        if _AIOHTTP_AVAILABLE:
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(self.valves.modelsdev_url) as resp:
                        data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    for key, entry in data.items():
                        limit = entry.get("limit") if isinstance(entry, dict) else None
                        ctx = _num(limit.get("context")) if isinstance(limit, dict) else None
                        if ctx:
                            full = str(key).lower()
                            result[full] = int(ctx)
                            result.setdefault(full.split("/")[-1], int(ctx))
            except Exception:
                result = {}

        # Cache success for the full TTL; a failure only briefly so it retries soon.
        ttl = self.valves.modelsdev_ttl if result else min(300, self.valves.modelsdev_ttl)
        _modelsdev_cache["map"] = result
        _modelsdev_cache["expiry"] = now + max(60, ttl)
        return result

    async def _probe_context(self, model_id: str) -> int | None:
        """Best-effort probe of a local backend for its running context size.

        Fully isolated: any failure returns None and never affects the stats line.
        """
        if not _AIOHTTP_AVAILABLE:
            return None
        v = self.valves
        try:
            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # llama-swap: /running lists models with their launch command (--ctx-size).
                if v.llama_swap_url:
                    try:
                        async with session.get(v.llama_swap_url.rstrip("/") + "/running") as resp:
                            data = await resp.json(content_type=None)
                        size = self._parse_llama_swap(data)
                        if size:
                            return size
                    except Exception:
                        pass
                # llama.cpp: /props reports the loaded model's n_ctx.
                if v.llamacpp_url:
                    try:
                        async with session.get(v.llamacpp_url.rstrip("/") + "/props") as resp:
                            data = await resp.json(content_type=None)
                        gen = data.get("default_generation_settings") if isinstance(data, dict) else None
                        n_ctx = _first_num(data, "n_ctx") or _first_num(gen if isinstance(gen, dict) else {}, "n_ctx")
                        if n_ctx:
                            return int(n_ctx)
                    except Exception:
                        pass
        except Exception:
            return None
        return None

    @staticmethod
    def _parse_llama_swap(data: Any) -> int | None:
        """Extract --ctx-size from a llama-swap /running payload."""
        rows = data.get("running", []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            cmd = row.get("cmd") or row.get("command") or ""
            if isinstance(cmd, list):
                cmd = " ".join(str(c) for c in cmd)
            match = re.search(r"--ctx-size[= ]+(\d+)", str(cmd))
            if match:
                return int(match.group(1))
        return None

    def _context_size_map_table(self) -> dict[str, int]:
        """Parse the user's context_size_map valve into {model-substring(lower) -> tokens}. Non-fatal.

        Mirrors _price_map_table: a per-entry-tolerant parse (one bad value drops only that entry,
        not the whole map). Applied as its own resolution tier ABOVE models.dev in
        _context_size_for, so an explicit user override beats the automatic remote lookup — exactly
        like price_map wins over models.dev for cost.
        """
        raw = self.valves.context_size_map
        if not raw:
            return {}
        try:
            user_map = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(user_map, dict):
            return {}
        result: dict[str, int] = {}
        for key, val in user_map.items():
            try:
                result[str(key).lower()] = int(val)
            except (TypeError, ValueError):
                continue
        return result

    def _context_table(self) -> dict[str, Any]:
        """Static context table (offline default).

        The user's context_size_map is NO longer merged here — it is resolved as a higher-priority
        tier in _context_size_for (above models.dev), so merging it into the static fallback would
        be dead code. A copy is returned so callers can't mutate the module-level table.
        """
        return dict(_STATIC_CONTEXT_SIZES)

    # --- cost ------------------------------------------------------------------

    async def _resolve_cost(
        self, usage: dict[str, Any] | None, tokens: dict[str, Any], messages: list[Any], model_id: str
    ) -> dict[str, Any]:
        """Cost of this message + running chat total. Native (provider) first, else estimate.

        cost_mode: off -> nothing; auto -> native only (no fetch/estimate);
        estimate -> native first, else approximate from models.dev prices (marked ≈).
        """
        result: dict[str, Any] = {"message": None, "message_est": False, "cumulative": None, "cumulative_est": False}
        mode = self.valves.cost_mode
        if mode == "off":
            return result

        native, native_key = self._native_cost(usage)
        price = None
        price_prov = {"source": "none", "matched_key": None}
        if native is not None:
            result["message"] = native
        elif mode == "estimate":
            price, price_prov = await self._resolve_price(model_id)
            est = self._estimate_cost(tokens, price)
            if est is not None:
                result["message"] = est
                result["message_est"] = True

        if self.valves.show_cumulative_cost:
            if price is None and mode == "estimate":
                price, price_prov = await self._resolve_price(model_id)
            total = 0.0
            seen = False
            any_est = False
            for m in messages:
                if not (isinstance(m, dict) and m.get("role") == "assistant"):
                    continue
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                n, _ = self._native_cost(u)
                if n is not None:
                    total += n
                    seen = True
                    continue
                if mode == "estimate" and price:
                    est = self._estimate_cost(self._usage_token_bag(u), price)
                    if est is not None:
                        total += est
                        seen = True
                        any_est = True
            if seen:
                result["cumulative"] = total
                result["cumulative_est"] = any_est

        # Debug-only provenance/breakdown; kept off the hot path when debug is disabled.
        if self.valves.debug_mode:
            result["debug"] = {
                "mode": mode,
                "native": {"found": native is not None, "value": native, "source_key": native_key},
                "cost_details": self._native_cost_details(usage),
                "price": {
                    "source": price_prov["source"],
                    "matched_key": price_prov["matched_key"],
                    "rates_per_1m": price,
                },
                "breakdown": self._cost_components(tokens, price),
            }
        return result

    @staticmethod
    def _native_cost_details(usage: dict[str, Any] | None) -> dict[str, Any] | None:
        """Provider cost breakdown when present, e.g. OpenRouter `cost_details`.

        OpenRouter reports `usage.cost_details` alongside `usage.cost` — typically
        `upstream_inference_cost` (what the upstream provider charged) and `cache_discount`.
        The displayed cost uses the authoritative top-level `cost`; this is diagnostics only,
        showing the provider vs upstream split. Numeric values only, so it stays serializable.
        """
        if not isinstance(usage, dict):
            return None
        details = usage.get("cost_details")
        if not isinstance(details, dict):
            return None
        cleaned = {k: v for k, v in details.items() if _num(v) is not None}
        return cleaned or None

    @staticmethod
    def _native_cost(usage: dict[str, Any] | None) -> tuple[float | None, str | None]:
        """Provider/proxy-reported cost + the key it came from (OpenRouter, LiteLLM, ...)."""
        if not isinstance(usage, dict):
            return None, None
        for key in ("cost", "total_cost"):
            val = _num(usage.get(key))
            if val is not None:
                return float(val), key
        inp = _first_num(usage, "input_cost", "prompt_cost")
        out = _first_num(usage, "output_cost", "completion_cost")
        if inp is not None or out is not None:
            return float((inp or 0) + (out or 0)), "input_cost+output_cost"
        return None, None

    @staticmethod
    def _usage_token_bag(usage: Any) -> dict[str, Any]:
        """Minimal cache-aware token bag from a usage dict (for cost of historical messages)."""
        bag: dict[str, Any] = {
            "input": None,
            "output": None,
            "cached": None,
            "cache_write": None,
            "is_anthropic": False,
            "input_has_cache": False,
            "fresh_input": None,
        }
        if not isinstance(usage, dict):
            return bag
        bag["input"] = _first_num(usage, "input_tokens", "prompt_tokens", "prompt_eval_count", "prompt_n")
        bag["output"] = _first_num(usage, "output_tokens", "completion_tokens", "eval_count", "predicted_n")
        cache = Filter._cache_and_fresh(usage, bag["input"])
        bag["cached"] = cache["cached"]
        bag["cache_write"] = cache["cache_write"]
        bag["is_anthropic"] = cache["is_anthropic"]
        bag["input_has_cache"] = cache["input_has_cache"]
        bag["fresh_input"] = cache["fresh_input"]
        return bag

    @staticmethod
    def _cost_components(bag: dict[str, Any], price: dict[str, Any] | None) -> dict[str, Any] | None:
        """Per-component token counts + USD from a token bag and per-1M price dict.

        Single source of truth for the cost math: `_estimate_cost` returns its `total_usd`,
        and the debug breakdown shows its parts. None if the cost is not computable.
        """
        if not price:
            return None
        p_in = _num(price.get("input"))
        p_out = _num(price.get("output"))
        if p_in is None and p_out is None:
            return None
        p_in = p_in or 0.0
        p_out = p_out or 0.0
        p_cache_read = _num(price.get("cache_read"))
        if p_cache_read is None:
            p_cache_read = p_in
        p_cache_write = _num(price.get("cache_write"))
        if p_cache_write is None:
            p_cache_write = p_in

        # fresh_input = uncached, full-price input (cache already subtracted for subset shapes).
        # Uniform formula: cache read/write always priced at their own rates on top of fresh input.
        fresh = bag.get("fresh_input")
        if fresh is None:
            fresh = bag.get("input")
        fresh = fresh or 0
        out = bag.get("output") or 0
        cached = bag.get("cached") or 0
        cache_write = bag.get("cache_write") or 0
        if not (fresh or cached or cache_write or out):
            return None

        raw_in = fresh * p_in
        raw_cached = cached * p_cache_read
        raw_cache_write = cache_write * p_cache_write
        raw_out = out * p_out
        total = (raw_in + raw_cached + raw_cache_write + raw_out) / 1_000_000.0

        return {
            "is_anthropic": bool(bag.get("is_anthropic")),
            "input_has_cache": bool(bag.get("input_has_cache")),
            "tokens": {
                "input": bag.get("input"),
                "billable_in": fresh,
                "cached": cached,
                "cache_write": cache_write,
                "output": out,
            },
            "usd_per_component": {
                "input": round(raw_in / 1_000_000.0, 8),
                "cached": round(raw_cached / 1_000_000.0, 8),
                "cache_write": round(raw_cache_write / 1_000_000.0, 8),
                "output": round(raw_out / 1_000_000.0, 8),
            },
            "total_usd": total,
        }

    @staticmethod
    def _estimate_cost(bag: dict[str, Any], price: dict[str, Any] | None) -> float | None:
        """Estimate USD from a token bag and a per-1M price dict. None if not computable."""
        components = Filter._cost_components(bag, price)
        return components["total_usd"] if components else None

    async def _resolve_price(self, model_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Per-1M price + provenance ({source, matched_key}): manual map -> models.dev -> static.

        Provenance is debug-only; the returned price is what the hot path uses. Order and
        matching semantics are unchanged from the previous per-source lookups.
        """
        table = self._price_map_table()
        key = _longest_key_match(table, model_id)
        if key is not None and table[key]:
            return table[key], {"source": "price_map", "matched_key": key}
        if self.valves.fetch_prices_from_modelsdev:
            mtable = await self._modelsdev_prices_map()
            mkey = _modelsdev_match(mtable, model_id)
            if mkey is not None and mtable[mkey]:
                return mtable[mkey], {"source": "modelsdev", "matched_key": mkey}
        skey = _longest_key_match(_STATIC_PRICES, model_id)
        if skey is not None:
            return _STATIC_PRICES[skey], {"source": "static", "matched_key": skey}
        return None, {"source": "none", "matched_key": None}

    def _price_map_table(self) -> dict[str, Any]:
        """Parse the user's price_map valve into {model-substring(lower) -> price dict}. Non-fatal."""
        raw = self.valves.price_map
        if not raw:
            return {}
        try:
            user_map = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(user_map, dict):
            return {}
        return {str(k).lower(): v for k, v in user_map.items() if isinstance(v, dict)}

    async def _modelsdev_prices_map(self) -> dict[str, Any]:
        """Fetch and cache {model_id -> price dict} from models.dev api.json. Non-fatal."""
        now = time.time()
        cached_map = _modelsdev_prices_cache.get("map")
        if cached_map is not None and _modelsdev_prices_cache.get("expiry", 0) > now:
            return cached_map  # type: ignore[no-any-return]

        result: dict[str, Any] = {}
        if _AIOHTTP_AVAILABLE:
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(self.valves.modelsdev_api_url) as resp:
                        data = await resp.json(content_type=None)
                result = self._parse_prices(data)
            except Exception:
                result = {}

        ttl = self.valves.modelsdev_ttl if result else min(300, self.valves.modelsdev_ttl)
        _modelsdev_prices_cache["map"] = result
        _modelsdev_prices_cache["expiry"] = now + max(60, ttl)
        return result

    @staticmethod
    def _parse_prices(data: Any) -> dict[str, Any]:
        """Flatten models.dev api.json to {model_id(lower) -> price}. Tolerant of shape.

        Expected: {provider: {"models": {model_id: {..., "cost": {...}}}}}; also accepts a
        flat {model_id: {..., "cost": {...}}}. Prices are USD per 1M tokens.
        """
        out: dict[str, Any] = {}

        def add(model_id: Any, cost: Any) -> None:
            if not isinstance(cost, dict):
                return
            price: dict[str, Any] = {}
            for field in ("input", "output", "cache_read", "cache_write"):
                val = _num(cost.get(field))
                if val is not None:
                    price[field] = val
            if price.get("input") is None and price.get("output") is None:
                return
            key = str(model_id).lower()
            out.setdefault(key, price)
            out.setdefault(key.split("/")[-1], price)

        if isinstance(data, dict):
            for pid, prov in data.items():
                if isinstance(prov, dict) and isinstance(prov.get("models"), dict):
                    for mid, entry in prov["models"].items():
                        if isinstance(entry, dict):
                            add(entry.get("id", mid), entry.get("cost"))
                elif isinstance(prov, dict) and "cost" in prov:
                    add(prov.get("id", pid), prov.get("cost"))
        return out

    # --- display ---------------------------------------------------------------

    def _build_stats(
        self,
        v: Filter.Valves,
        tokens: dict[str, Any],
        timing: dict[str, Any],
        ctx: dict[str, Any],
        cost: dict[str, Any],
        model: dict[str, Any] | None,
    ) -> list[str]:
        """Assemble the stats parts in the resolved metric order.

        show_* still gates visibility (each renderer returns None when off/no-data);
        display_order only reorders. Empty order -> byte-identical to the old output.
        """
        order = _resolve_display_order(getattr(v, "display_order", "") or "")

        rendered: list[tuple[str, str]] = []  # (key, part) — key kept for the source guard
        for key in order:
            render = _STATS_RENDERERS.get(key)
            if render is None:
                continue
            part = render(v, tokens, timing, ctx, cost, model)
            if part is not None:
                rendered.append((key, part))

        # Preserve the old guard: the data-source suffix is not shown on its own.
        if all(key == "source" for key, _ in rendered):
            rendered = [(k, p) for k, p in rendered if k != "source"]
        return [part for _, part in rendered]

    @staticmethod
    def _provider_guess(model: dict[str, Any] | None, model_id: str) -> str:
        """Best-effort provider label from outlet-visible fields (no base_url/api-key exist here).

        Local backends and an admin-set `provider` are authoritative; otherwise the family is
        inferred from the model-id substring — the same thing that drives cost/context matching.
        """
        m = model if isinstance(model, dict) else {}
        owned = (m.get("owned_by") or "").lower()
        conn = (m.get("connection_type") or "").lower()
        provider = m.get("provider") or ""
        if owned == "ollama" or conn == "local":
            return "ollama/local"
        if owned == "arena":
            return "arena"
        if provider:
            return str(provider)
        mid = (model_id or "").lower()
        families = [
            ("openrouter", "openrouter"),
            ("litellm", "litellm"),
            ("anthropic", "anthropic"),
            ("claude", "anthropic"),
            ("gemini", "google"),
            ("deepseek", "deepseek"),
            ("grok", "xai"),
            ("codestral", "mistral"),
            ("devstral", "mistral"),
            ("magistral", "mistral"),
            ("pixtral", "mistral"),
            ("mistral", "mistral"),
            ("qwen", "qwen"),
            ("qwq", "qwen"),
            ("kimi", "moonshot"),
            ("glm", "zhipu"),
            ("minimax", "minimax"),
            ("command", "cohere"),
            ("llama", "meta"),
            ("gpt", "openai"),
            ("openai", "openai"),
        ]
        for needle, label in families:
            if needle in mid:
                return label
        return owned or "unknown"

    def _sanitize_model(
        self, model: dict[str, Any] | None, metadata: dict[str, Any] | None, resolved_id: str
    ) -> dict[str, Any]:
        """Whitelist of shareable model/provider fields — NO secrets, prompt, user ids, or grants.

        Only known-safe keys are copied out; raw __model__/__metadata__ (which carry user_message,
        user_id, session_id, access_grants, the full ollama/openai model dict, ...) never leak here.
        OWUI strips info.params before outlet, so generation params are unavailable — surfaced
        explicitly via gen_params_available rather than silently omitted.
        """
        m = model if isinstance(model, dict) else {}
        info_raw = m.get("info")
        info: dict[str, Any] = info_raw if isinstance(info_raw, dict) else {}
        params: dict[str, Any] = {}
        function_calling = None
        if isinstance(metadata, dict) and isinstance(metadata.get("params"), dict):
            mp = metadata["params"]
            params = {
                "reasoning_tags": mp.get("reasoning_tags"),
                "compact_token_threshold": mp.get("compact_token_threshold"),
                "stream_delta_chunk_size": mp.get("stream_delta_chunk_size"),
            }
            function_calling = mp.get("function_calling")
        return {
            "resolved_id": resolved_id or None,
            "id": m.get("id"),
            "name": m.get("name"),
            "base_model_id": info.get("base_model_id"),
            "owned_by": m.get("owned_by"),
            "connection_type": m.get("connection_type"),
            "provider": m.get("provider"),
            "preset": bool(m.get("preset")) if "preset" in m else None,
            "is_pipe": bool(m.get("pipe")),
            "has_url_idx": "urlIdx" in m,
            "provider_guess": self._provider_guess(m, resolved_id),
            "function_calling": function_calling,
            "owui_params": params,
            "gen_params_available": False,
            "note": "generation params (temperature/top_p/max_tokens/system/reasoning) removed by OWUI before outlet",
        }

    def _valves_snapshot(self) -> dict[str, Any]:
        """Full valve dump for reproducing issues; the two local-backend URLs are masked."""
        try:
            data = self.valves.model_dump()
        except Exception:
            return {}
        for key in ("llamacpp_url", "llama_swap_url"):
            if data.get(key):
                data[key] = "******"
        return data

    @staticmethod
    def _web_search_debug(assistant_msg: dict[str, Any]) -> dict[str, Any]:
        """Hint whether the response used server-side web search (url_citation -> OWUI `sources`).

        Why it matters for cost: Open-WebUI-orchestrated tool/MCP calls are pure tokens and already
        summed into `usage.cost`. But a provider's OWN server-side web search / tools (OpenRouter's
        `web` plugin / `openrouter:web_search`, `:online`) can add a surcharge on top of tokens, and a
        BYOK engine (e.g. Firecrawl) bills entirely outside the OpenRouter generation. So when a
        response carries web citations, the shown cost may be incomplete — cross-check the provider's
        Activity page. This is a diagnostic flag only; it never changes the displayed number.

        Detection is conservative: OWUI turns `url_citation` annotations into `sources` whose entries
        carry an http(s) URL (RAG/file sources do not), so an http host is the tell-tale of web search.
        """
        sources = assistant_msg.get("sources")
        hosts: list[str] = []
        if isinstance(sources, list):
            for src in sources:
                if not isinstance(src, dict):
                    continue
                candidates: list[Any] = []
                inner = src.get("source")
                if isinstance(inner, dict):
                    candidates.append(inner.get("url"))
                meta = src.get("metadata")
                if isinstance(meta, list):
                    for m in meta:
                        if isinstance(m, dict):
                            candidates.extend((m.get("source"), m.get("url")))
                host = next((h for h in (_url_host(c) for c in candidates if isinstance(c, str)) if h), "")
                if host:
                    hosts.append(host)
        return {
            "detected": bool(hosts),
            "citation_count": len(hosts),
            "domains": sorted(set(hosts))[:10],
            "note": (
                "Response has web-search citations; a provider-side search/tool surcharge may not be "
                "fully in usage.cost (BYOK engines bill outside OpenRouter). Cross-check the Activity page."
                if hosts
                else None
            ),
        }

    async def _emit_debug(
        self,
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        task: Any,
        messages: list[Any],
        assistant_msg: dict[str, Any],
        usage: dict[str, Any] | None,
        tokens: dict[str, Any],
        timing: dict[str, Any],
        ctx: dict[str, Any],
        cost: dict[str, Any],
        model: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        """Diagnostics for troubleshooting, in a copyable and untruncated form.

        A chat status line is clamped to one line and not selectable, so it cannot show the raw
        JSON. The stats line only carries a small `(debug)` marker; the full payload is emitted
        two ways:
          1. a `citation` event -> a "Token Usage & Cost Display - Debug info" source whose modal renders
             the JSON in a ```json code block WITH a Copy button (persisted with the message);
          2. the server/container stdout (`docker logs`) as a copyable fallback.
        """
        content = assistant_msg.get("content")
        output = assistant_msg.get("output")
        resolved_id = _resolve_model_id(model)

        size = ctx.get("size")
        used = ctx.get("used")
        percent = round((used / size) * 100, 2) if (size and used is not None) else None
        context_debug = {
            "source": ctx.get("source"),
            "matched_key": ctx.get("matched_key"),
            "size": size,
            "used": used,
            "percent": percent,
        }

        display_order_debug = _display_order_debug(self.valves.display_order or "")

        payload = {
            "task": task,
            "messages_count": len(messages),
            "assistant_keys": sorted(assistant_msg.keys()),
            "usage": usage,
            "output_types": (
                [item.get("type") for item in output if isinstance(item, dict)] if isinstance(output, list) else None
            ),
            "content_len": len(content) if isinstance(content, str) else -1,
            "model": self._sanitize_model(model, metadata, resolved_id),
            "tokens": tokens,
            "timing": timing,
            "cost": {k: val for k, val in cost.items() if k != "debug"},
            "cost_debug": cost.get("debug"),
            "web_search": self._web_search_debug(assistant_msg),
            "context_debug": context_debug,
            "display_order": display_order_debug,
            "tiktoken": _TIKTOKEN_AVAILABLE,
            "valves": self._valves_snapshot(),
        }
        try:
            pretty = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        except Exception as exc:
            pretty = f"serialization error: {exc}"

        # 1) In-chat, copyable, untruncated: a citation renders the JSON in a modal (titled from
        #    source.name) with a Copy button. The payload MUST NOT carry a top-level `type` key or
        #    the backend drops it.
        await emit(
            {
                "type": "citation",
                "data": {
                    "source": {"name": "Token Usage & Cost Display - Debug info"},
                    "document": [f"```json\n{pretty}\n```"],
                    "metadata": [{"source": "Token Usage & Cost Display - Debug info"}],
                },
            }
        )
        # 2) Copyable fallback -> container stdout (docker logs / uvicorn console).
        print(f"[TUD debug]\n{pretty}", flush=True)  # noqa: T201 - intentional debug channel
