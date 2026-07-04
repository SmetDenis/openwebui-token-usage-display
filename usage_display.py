"""
title: Token Usage Display
author: smetdenis
version: 2.1.0
description: Shows token counts (input/output/total, reasoning, cached, audio), generation time, tokens/sec, context-window utilization and message/chat cost below each AI response. Reads OWUI-normalized usage across providers (OpenAI Chat & Responses API, Anthropic, Gemini, Ollama, llama.cpp), falls back to tiktoken. Cost is native when the provider/proxy reports it (OpenRouter/LiteLLM), or optionally estimated from models.dev prices. Context sizes from a built-in table (seeded from models.dev) with optional live models.dev fetch and llama.cpp/llama-swap probing. Works on Open WebUI 0.9.0+ (built around the 0.10.x structured-output/normalized-usage model; degrades gracefully on 0.9.x). tiktoken is optional (soft import).
required_open_webui_version: 0.9.0
"""

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
#   * merge_usage sums input/output/total + *_tokens_details, but NOT top-level
#     Anthropic cache fields nor Ollama durations -> in multi-round tool turns
#     those reflect the last round only (an OWUI limitation we surface, not fix).
#   * 0.9.0+ compatible: the outlet body carries per-message `usage` since 0.9.0 and
#     normalize_usage exists since 0.8.0, so tokens/context/timing/estimate all work on
#     0.9.x. Only NATIVE provider cost (USAGE_COST_KEYS) needs 0.10.0+ -> on 0.9.x use
#     cost_mode 'estimate'. Verified via git history, not a live 0.9.x run.

# tiktoken is optional: OWUI bundles it, but per-plugin `requirements:` installs
# fail in some sandboxes (uvx/LXC). Soft-import so the plugin always loads.
try:
    import tiktoken

    _TIKTOKEN_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    tiktoken = None
    _TIKTOKEN_AVAILABLE = False

# aiohttp is bundled with OWUI; soft-imported only for the optional context probe.
try:
    import aiohttp

    _AIOHTTP_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    aiohttp = None
    _AIOHTTP_AVAILABLE = False

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, Field

# --- Module-level state --------------------------------------------------------

# Request start times keyed by chat context (wall-clock fallback timing).
_request_timings: dict = {}

# Context-window size cache: model_key -> (size, expiry_epoch).
_ctx_size_cache: dict = {}

# Cached models.dev lookup table (id -> context tokens): {"map": dict|None, "expiry": epoch}.
_modelsdev_cache: dict = {"map": None, "expiry": 0.0}

# Cached models.dev price map (id -> {input,output,cache_read,cache_write} USD/1M): {"map": dict|None, "expiry": epoch}.
_modelsdev_prices_cache: dict = {"map": None, "expiry": 0.0}

# Static context-window table — offline default, seeded from models.dev (2026-07).
# Matched as a case-insensitive substring of the model id; the LONGEST matching key
# wins, so specific keys (gpt-4o) override generic ones (gpt-4). Enable the
# `fetch_context_from_modelsdev` valve for always-current sizes across every model.
_STATIC_CONTEXT_SIZES: dict = {
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
_STATIC_PRICES: dict = {
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


def _num(value) -> int | float | None:
    """Return the value if it is a real (non-bool) number, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _first_num(src: dict, *keys: str) -> int | float | None:
    """First present numeric value among keys (0 is valid, unlike `or`-chains)."""
    if not isinstance(src, dict):
        return None
    for key in keys:
        val = _num(src.get(key))
        if val is not None:
            return val
    return None


def _detail_num(usage: dict, group: str, key: str) -> int | float | None:
    """Read usage[group][key] as a number (e.g. completion_tokens_details.reasoning_tokens)."""
    if not isinstance(usage, dict):
        return None
    group_dict = usage.get(group)
    if isinstance(group_dict, dict):
        return _num(group_dict.get(key))
    return None


def _get_last_assistant_message_obj(messages: list) -> dict:
    """Return the last assistant message dict from the message list."""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            return message
    return {}


def _extract_output_text(output: list) -> str:
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


def _message_text(message: dict) -> str:
    """Best-effort visible text of a message: content if present, else output."""
    content = message.get("content", "")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        chunks = []
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
        show_data_source: bool = Field(
            default=False, description="Append [API]/[est.] to indicate token count source."
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
            description='JSON object of {"model-substring": context_tokens} merged over the built-in table.',
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
        context_warn_percent: int = Field(default=25, description="Context %% at which the icon turns orange.")
        context_critical_percent: int = Field(default=70, description="Context %% at which the icon turns red.")
        llamacpp_url: str = Field(
            default="",
            description="Optional llama.cpp base URL (e.g. http://127.0.0.1:8080) for /props n_ctx probe. Empty=off.",
        )
        llama_swap_url: str = Field(
            default="",
            description="Optional llama-swap base URL to probe /running for --ctx-size. Empty = off.",
        )
        context_probe_ttl: int = Field(
            default=600, description="Seconds to cache a probed/resolved context size."
        )
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
        price_map: str = Field(
            default="",
            description=(
                'JSON of {"model-substring": {"input": USD_per_1M, "output": USD_per_1M, '
                '"cache_read": USD_per_1M, "cache_write": USD_per_1M}}; highest priority in estimate mode.'
            ),
        )
        fetch_prices_from_modelsdev: bool = Field(
            default=True,
            description="In estimate mode, fetch live per-provider prices from models.dev (cached ~24h). Off = static table only.",
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

    def __init__(self):
        self.valves = self.Valves()

    # --- inlet: record start time + capture context hints ----------------------

    async def inlet(
        self,
        body: dict,
        __user__: dict | None = None,
        __metadata__: dict | None = None,
    ) -> dict:
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
        body: dict,
        __user__: dict | None = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
        __metadata__: dict | None = None,
        __model__: dict | None = None,
    ) -> dict:
        if __user__ and __user__.get("valves"):
            user_valves = __user__["valves"]
            if hasattr(user_valves, "enabled") and not user_valves.enabled:
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
        ctx = await self._resolve_context(body, __metadata__, __model__, tokens)

        model_id = ""
        if isinstance(__model__, dict):
            model_id = __model__.get("id", "") or ""
        if not model_id:
            model_id = body.get("model", "") or ""
        cost = await self._resolve_cost(usage, tokens, messages, model_id)

        stats_parts = self._build_stats(v, tokens, timing, ctx, cost, __model__)
        if v.debug_mode:
            stats_parts.append("(debug)")

        if stats_parts and __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": " · ".join(stats_parts), "done": True},
                }
            )

        if v.debug_mode and __event_emitter__:
            await self._emit_debug(__event_emitter__, task, messages, assistant_msg, usage, tokens, timing, ctx, cost)

        return body

    # --- token extraction ------------------------------------------------------

    def _extract_tokens(
        self,
        usage: dict | None,
        assistant_msg: dict,
        messages: list,
        body: dict,
        model: dict | None,
    ) -> dict:
        """Return a normalized bag of token counts across all providers/APIs."""
        result = {
            "input": None,
            "output": None,
            "total": None,
            "reasoning": None,
            "cached": None,
            "cache_write": None,
            "audio": None,
            "is_api": False,
            "is_anthropic": False,
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

            # Cached prompt tokens: OpenAI (subset of input) vs Anthropic (extra).
            openai_cached = _detail_num(usage, "prompt_tokens_details", "cached_tokens") or _detail_num(
                usage, "input_tokens_details", "cached_tokens"
            )
            anth_read = _num(usage.get("cache_read_input_tokens"))
            anth_write = _num(usage.get("cache_creation_input_tokens"))
            result["is_anthropic"] = anth_read is not None or anth_write is not None
            result["cached"] = anth_read if result["is_anthropic"] else openai_cached
            result["cache_write"] = anth_write

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

        # Derived total (Anthropic cache is additional to input; OpenAI already inside it).
        result["total"] = self._compute_total(usage, result)
        return result

    def _estimate_tokens(
        self, result: dict, assistant_msg: dict, messages: list, body: dict, model: dict | None
    ) -> None:
        """tiktoken estimate of input/output tokens when the provider reported no usage."""
        model_id = ""
        if isinstance(model, dict):
            model_id = model.get("id", "") or ""
        if not model_id:
            model_id = body.get("model", "") or ""

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

    def _compute_total(self, usage: dict | None, result: dict) -> int | None:
        inp = result["input"]
        out = result["output"]
        if result["is_anthropic"]:
            eff = (inp or 0) + (result["cached"] or 0) + (result["cache_write"] or 0)
            if inp is None and out is None:
                return None
            return eff + (out or 0)
        if usage:
            total = _num(usage.get("total_tokens"))
            if total is not None:
                return int(total)
        if inp is None and out is None:
            return None
        return (inp or 0) + (out or 0)

    # --- timing ----------------------------------------------------------------

    def _resolve_wall_clock(self, body: dict, metadata: dict | None) -> float | None:
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

    def _resolve_timing(self, usage: dict | None, wall_seconds: float | None, output_tokens) -> dict:
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

    async def _resolve_context(self, body, metadata, model, tokens) -> dict:
        if not self.valves.show_context_window:
            return {"size": None, "used": None}

        used = tokens["total"]
        if used is None:
            used = (tokens["input"] or 0) + (tokens["output"] or 0) or None

        model_id = ""
        if isinstance(model, dict):
            model_id = model.get("id", "") or ""
        if not model_id:
            model_id = body.get("model", "") or ""

        size = await self._context_size_for(model_id, metadata)
        return {"size": size, "used": used}

    async def _context_size_for(self, model_id: str, metadata: dict | None) -> int | None:
        v = self.valves
        # 1) Explicit override.
        if v.context_size_override and v.context_size_override > 0:
            return int(v.context_size_override)
        # 2) num_ctx captured at inlet (Ollama/local models).
        hint = (metadata or {}).get("_tud_num_ctx")
        if isinstance(hint, int) and hint > 0:
            return hint

        cache_key = model_id.lower()
        cached = _ctx_size_cache.get(cache_key)
        if cached and cached[1] > time.time():
            return cached[0]

        size = None
        # 3) Live models.dev lookup (opt-in; cached ~24h).
        if v.fetch_context_from_modelsdev:
            size = await self._modelsdev_lookup(model_id)
        # 4) Static table (+ user map override), matched by substring.
        if size is None:
            size = self._table_lookup(model_id)
        # 5) Optional endpoint probe for local backends (opt-in via valve URL).
        if size is None and (v.llamacpp_url or v.llama_swap_url):
            size = await self._probe_context(model_id)

        if size is not None:
            _ctx_size_cache[cache_key] = (size, time.time() + max(60, v.context_probe_ttl))
        return size

    async def _modelsdev_map(self) -> dict:
        """Fetch and cache {model_id -> context_tokens} from models.dev. Non-fatal."""
        now = time.time()
        if _modelsdev_cache.get("map") is not None and _modelsdev_cache.get("expiry", 0) > now:
            return _modelsdev_cache["map"]

        result: dict = {}
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

    async def _modelsdev_lookup(self, model_id: str) -> int | None:
        table = await self._modelsdev_map()
        if not table:
            return None
        mid = (model_id or "").lower()
        if mid in table:
            return table[mid]
        bare = mid.split("/")[-1]
        if bare in table:
            return table[bare]
        for key in sorted(table, key=len, reverse=True):
            if key in mid:
                return table[key]
        return None

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
    def _parse_llama_swap(data) -> int | None:
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

    def _table_lookup(self, model_id: str) -> int | None:
        table = dict(_STATIC_CONTEXT_SIZES)
        if self.valves.context_size_map:
            try:
                user_map = json.loads(self.valves.context_size_map)
                if isinstance(user_map, dict):
                    table.update({str(k).lower(): int(val) for k, val in user_map.items()})
            except Exception:
                pass
        mid = (model_id or "").lower()
        # Longest key first so specific matches (gpt-4o) beat generic (gpt-4).
        for key in sorted(table, key=len, reverse=True):
            if key in mid:
                return table[key]
        return None

    # --- cost ------------------------------------------------------------------

    async def _resolve_cost(self, usage: dict | None, tokens: dict, messages: list, model_id: str) -> dict:
        """Cost of this message + running chat total. Native (provider) first, else estimate.

        cost_mode: off -> nothing; auto -> native only (no fetch/estimate);
        estimate -> native first, else approximate from models.dev prices (marked ≈).
        """
        result = {"message": None, "message_est": False, "cumulative": None, "cumulative_est": False}
        mode = self.valves.cost_mode
        if mode == "off":
            return result

        native = self._native_cost(usage)
        price = None
        if native is not None:
            result["message"] = native
        elif mode == "estimate":
            price = await self._resolve_price(model_id)
            est = self._estimate_cost(tokens, price)
            if est is not None:
                result["message"] = est
                result["message_est"] = True

        if self.valves.show_cumulative_cost:
            if price is None and mode == "estimate":
                price = await self._resolve_price(model_id)
            total = 0.0
            seen = False
            any_est = False
            for m in messages:
                if not (isinstance(m, dict) and m.get("role") == "assistant"):
                    continue
                u = m.get("usage")
                if not isinstance(u, dict):
                    continue
                n = self._native_cost(u)
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
        return result

    @staticmethod
    def _native_cost(usage: dict | None) -> float | None:
        """Provider/proxy-reported cost from the usage object (OpenRouter, LiteLLM, ...)."""
        if not isinstance(usage, dict):
            return None
        total = _first_num(usage, "cost", "total_cost")
        if total is not None:
            return float(total)
        inp = _first_num(usage, "input_cost", "prompt_cost")
        out = _first_num(usage, "output_cost", "completion_cost")
        if inp is not None or out is not None:
            return float((inp or 0) + (out or 0))
        return None

    @staticmethod
    def _usage_token_bag(usage: dict) -> dict:
        """Minimal token counts from a usage dict (for cost of historical messages)."""
        bag = {"input": None, "output": None, "cached": None, "cache_write": None, "is_anthropic": False}
        if not isinstance(usage, dict):
            return bag
        bag["input"] = _first_num(usage, "input_tokens", "prompt_tokens", "prompt_eval_count", "prompt_n")
        bag["output"] = _first_num(usage, "output_tokens", "completion_tokens", "eval_count", "predicted_n")
        openai_cached = _detail_num(usage, "prompt_tokens_details", "cached_tokens") or _detail_num(
            usage, "input_tokens_details", "cached_tokens"
        )
        anth_read = _num(usage.get("cache_read_input_tokens"))
        anth_write = _num(usage.get("cache_creation_input_tokens"))
        bag["is_anthropic"] = anth_read is not None or anth_write is not None
        bag["cached"] = anth_read if bag["is_anthropic"] else openai_cached
        bag["cache_write"] = anth_write
        return bag

    @staticmethod
    def _estimate_cost(bag: dict, price: dict | None) -> float | None:
        """Estimate USD from a token bag and a per-1M price dict. None if not computable."""
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

        inp = bag.get("input") or 0
        out = bag.get("output") or 0
        cached = bag.get("cached") or 0
        cache_write = bag.get("cache_write") or 0
        if not inp and not out:
            return None

        if bag.get("is_anthropic"):
            # Anthropic: cache read/write are billed ON TOP of fresh input tokens.
            p_cache_write = _num(price.get("cache_write"))
            if p_cache_write is None:
                p_cache_write = p_in
            tokens_cost = inp * p_in + cached * p_cache_read + cache_write * p_cache_write + out * p_out
        else:
            # OpenAI-style: cached tokens are a discounted SUBSET of the input count.
            billable_in = max(inp - cached, 0)
            tokens_cost = billable_in * p_in + cached * p_cache_read + out * p_out
        return tokens_cost / 1_000_000.0

    async def _resolve_price(self, model_id: str) -> dict | None:
        """Per-1M price for the model: manual map -> live models.dev -> static table."""
        price = self._price_map_lookup(model_id)
        if price:
            return price
        if self.valves.fetch_prices_from_modelsdev:
            price = await self._modelsdev_price_lookup(model_id)
            if price:
                return price
        return self._static_price_lookup(model_id)

    def _price_map_lookup(self, model_id: str) -> dict | None:
        raw = self.valves.price_map
        if not raw:
            return None
        try:
            user_map = json.loads(raw)
        except Exception:
            return None
        if not isinstance(user_map, dict):
            return None
        table = {str(k).lower(): v for k, v in user_map.items() if isinstance(v, dict)}
        mid = (model_id or "").lower()
        for key in sorted(table, key=len, reverse=True):
            if key in mid:
                return table[key]
        return None

    @staticmethod
    def _static_price_lookup(model_id: str) -> dict | None:
        mid = (model_id or "").lower()
        for key in sorted(_STATIC_PRICES, key=len, reverse=True):
            if key in mid:
                return _STATIC_PRICES[key]
        return None

    async def _modelsdev_prices_map(self) -> dict:
        """Fetch and cache {model_id -> price dict} from models.dev api.json. Non-fatal."""
        now = time.time()
        if _modelsdev_prices_cache.get("map") is not None and _modelsdev_prices_cache.get("expiry", 0) > now:
            return _modelsdev_prices_cache["map"]

        result: dict = {}
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
    def _parse_prices(data) -> dict:
        """Flatten models.dev api.json to {model_id(lower) -> price}. Tolerant of shape.

        Expected: {provider: {"models": {model_id: {..., "cost": {...}}}}}; also accepts a
        flat {model_id: {..., "cost": {...}}}. Prices are USD per 1M tokens.
        """
        out: dict = {}

        def add(model_id, cost) -> None:
            if not isinstance(cost, dict):
                return
            price = {}
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

    async def _modelsdev_price_lookup(self, model_id: str) -> dict | None:
        table = await self._modelsdev_prices_map()
        if not table:
            return None
        mid = (model_id or "").lower()
        if mid in table:
            return table[mid]
        bare = mid.split("/")[-1]
        if bare in table:
            return table[bare]
        for key in sorted(table, key=len, reverse=True):
            if key and key in mid:
                return table[key]
        return None

    # --- display ---------------------------------------------------------------

    def _build_stats(self, v, tokens, timing, ctx, cost, model) -> list:
        parts: list[str] = []

        if v.show_input_tokens and tokens["input"] is not None:
            parts.append(f"⬆︎ {int(tokens['input']):,}")
        if v.show_output_tokens and tokens["output"] is not None:
            parts.append(f"⬇︎ {int(tokens['output']):,}")
        if v.show_total_tokens and tokens["total"] is not None:
            parts.append(f"Σ {int(tokens['total']):,}")
        if v.show_reasoning_tokens and tokens["reasoning"]:
            parts.append(f"🧠 {int(tokens['reasoning']):,}")
        if v.show_cached_tokens and tokens["cached"]:
            parts.append(f"💾 {int(tokens['cached']):,}")
        if v.show_audio_tokens and tokens["audio"]:
            parts.append(f"🔊 {int(tokens['audio']):,}")

        if v.show_context_window and ctx["size"] and ctx["used"] is not None:
            pct = (ctx["used"] / ctx["size"]) * 100 if ctx["size"] else 0
            icon = "📐"
            if pct >= v.context_critical_percent:
                icon = "🔴"
            elif pct >= v.context_warn_percent:
                icon = "🟠"
            parts.append(f"{icon} {_format_k(ctx['used'])}/{_format_k(ctx['size'])} ({pct:.0f}%)")

        if v.show_generation_time and timing["seconds"] is not None:
            prefix = "~" if timing["source"] == "wall" else ""
            parts.append(f"⏱ {prefix}{_format_duration(timing['seconds'])}")
        if v.show_tokens_per_second and timing["tps"]:
            parts.append(f"⚡ {timing['tps']:.1f} t/s")

        if cost["message"] is not None:
            prefix = "≈" if cost["message_est"] else ""
            parts.append(f"💰 {prefix}{_format_cost(cost['message'])}")
        # Chat total — only when it adds information over the single-message figure.
        if (
            cost["cumulative"] is not None
            and v.show_cumulative_cost
            and (cost["message"] is None or abs(cost["cumulative"] - (cost["message"] or 0)) > 1e-9)
        ):
            prefix = "≈" if cost["cumulative_est"] else ""
            parts.append(f"💰Σ {prefix}{_format_cost(cost['cumulative'])}")

        if v.show_model_name and isinstance(model, dict):
            info = model.get("info")
            base = info.get("base_model_id") if isinstance(info, dict) else None
            if base:
                parts.append(f"🤖 {base}")

        if v.show_data_source and parts:
            parts.append(f"[{'API' if tokens['is_api'] else 'est.'}]")

        return parts

    async def _emit_debug(self, emit, task, messages, assistant_msg, usage, tokens, timing, ctx, cost) -> None:
        """Diagnostics for troubleshooting, in a copyable and untruncated form.

        A chat status line is clamped to one line and not selectable, so it cannot show the raw
        JSON. The stats line only carries a small `(debug)` marker; the full payload is emitted
        two ways:
          1. a `citation` event -> a "Token Usage Display - Debug info" source whose modal renders
             the JSON in a ```json code block WITH a Copy button (persisted with the message);
          2. the server/container stdout (`docker logs`) as a copyable fallback.
        """
        content = assistant_msg.get("content")
        output = assistant_msg.get("output")
        payload = {
            "task": task,
            "messages_count": len(messages),
            "assistant_keys": sorted(assistant_msg.keys()),
            "usage": usage,
            "output_types": (
                [item.get("type") for item in output if isinstance(item, dict)] if isinstance(output, list) else None
            ),
            "content_len": len(content) if isinstance(content, str) else -1,
            "tokens": tokens,
            "timing": timing,
            "ctx": ctx,
            "cost": cost,
            "tiktoken": _TIKTOKEN_AVAILABLE,
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
                    "source": {"name": "Token Usage Display - Debug info"},
                    "document": [f"```json\n{pretty}\n```"],
                    "metadata": [{"source": "Token Usage Display - Debug info"}],
                },
            }
        )
        # 2) Copyable fallback -> container stdout (docker logs / uvicorn console).
        print(f"[TUD debug]\n{pretty}", flush=True)  # noqa: T201 - intentional debug channel
