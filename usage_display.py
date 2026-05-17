"""
title: Token Usage Display
author: smetdenis
version: 1.1.0
description: Displays input/output/total token counts and generation time below each AI response. Reads API-reported usage when available, falls back to tiktoken estimation. Compatible with Open WebUI 0.8.x and 0.9.x (multi-source token extraction, multi-level timing fallback, Anthropic Messages format).
required_open_webui_version: 0.8.0
requirements: tiktoken
"""

import json
import time
from collections.abc import Awaitable, Callable

import tiktoken
from pydantic import BaseModel, Field

# Thread-safe storage for request timing keyed by chat context
_request_timings: dict = {}


def _get_last_assistant_message_obj(messages: list) -> dict:
    """Return the last assistant message dict from the message list."""
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return message
    return {}


def _extract_text_content(message: dict) -> str:
    """Extract text from a message, handling multimodal content arrays."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return str(content) if content else ""


def _count_tokens_tiktoken(text: str, model: str = "") -> int:
    """Estimate token count using tiktoken. Falls back to cl100k_base."""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except (KeyError, ValueError):
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


def _format_duration(seconds: float) -> str:
    """Format elapsed time in a human-friendly way."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f}ms"
    elif seconds < 60.0:
        return f"{seconds:.1f}s"
    else:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.0f}s"


class Filter:
    class Valves(BaseModel):
        priority: int = Field(
            default=10,
            description="Filter priority (lower = runs first). Set high (e.g., 10) so this runs after other filters.",
        )
        show_input_tokens: bool = Field(
            default=True,
            description="Display input (prompt) token count.",
        )
        show_output_tokens: bool = Field(
            default=True,
            description="Display output (completion) token count.",
        )
        show_total_tokens: bool = Field(
            default=True,
            description="Display total token count.",
        )
        show_generation_time: bool = Field(
            default=True,
            description="Display generation wall-clock time.",
        )
        show_tokens_per_second: bool = Field(
            default=True,
            description="Display output tokens per second.",
        )
        show_data_source: bool = Field(
            default=False,
            description="Indicate whether token counts are API-reported or estimated.",
        )
        fallback_to_tiktoken: bool = Field(
            default=True,
            description="Estimate tokens with tiktoken when API-reported usage is unavailable.",
        )
        count_all_messages_for_input: bool = Field(
            default=True,
            description="When estimating input tokens, count all messages (not just the last user message).",
        )
        show_reasoning_tokens: bool = Field(
            default=True,
            description="Display reasoning/thinking tokens (for o1/o3/o4/Gemini thinking models).",
        )
        show_cached_tokens: bool = Field(
            default=True,
            description="Display cached prompt tokens (prompt cache hit count).",
        )
        show_audio_tokens: bool = Field(
            default=False,
            description="Display audio tokens (for audio-capable models like gpt-4o-audio).",
        )
        show_model_name: bool = Field(
            default=True,
            description="Display base model name for custom workspace models.",
        )
        debug_mode: bool = Field(
            default=False,
            description="Emit an additional status event with raw payload (info/usage/body/metadata keys). Use to diagnose missing widget on 0.9.x.",
        )

    class UserValves(BaseModel):
        enabled: bool = Field(
            default=True,
            description="Show token usage stats below responses.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def inlet(
        self,
        body: dict,
        __user__: dict | None = None,
        __metadata__: dict | None = None,
    ) -> dict:
        """Record the request start time before the LLM call."""
        # Build a unique key from available identifiers
        chat_id = ""
        if __metadata__:
            chat_id = __metadata__.get("chat_id", "") or ""
        message_id = ""
        if __metadata__:
            message_id = __metadata__.get("message_id", "") or ""
        key = (
            f"{chat_id}:{message_id}"
            if chat_id or message_id
            else f"fallback:{id(body)}"
        )

        start_time = time.time()
        _request_timings[key] = start_time

        if "metadata" not in body:
            body["metadata"] = {}
        body["metadata"]["_tud_timing_key"] = key
        body["metadata"]["_tud_start"] = start_time

        if __metadata__ is not None:
            __metadata__["_tud_timing_key"] = key
            __metadata__["_tud_start"] = start_time

        return body

    async def outlet(
        self,
        body: dict,
        __user__: dict | None = None,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
        __metadata__: dict | None = None,
        __model__: dict | None = None,
    ) -> dict:
        """Extract token usage and emit a status event with the stats."""

        # Check if user has disabled this via UserValves
        if __user__ and __user__.get("valves"):
            user_valves = __user__["valves"]
            if hasattr(user_valves, "enabled") and not user_valves.enabled:
                return body

        # Skip non-chat tasks (title generation, tag generation, etc.)
        task = None
        if __metadata__:
            task = __metadata__.get("task")
        if task and task in (
            "title_generation",
            "tags_generation",
            "follow_up_generation",
            "emoji_generation",
            "query_generation",
            "autocomplete_generation",
        ):
            return body

        messages = body.get("messages", [])
        if not messages:
            return body

        assistant_msg = _get_last_assistant_message_obj(messages)
        if not assistant_msg:
            return body

        # --- Extract timing (multi-level fallback: __metadata__ -> body -> module dict) ---
        elapsed_seconds = None
        start_time = None

        if __metadata__:
            start_time = __metadata__.get("_tud_start")

        if start_time is None:
            body_meta = body.get("metadata", {})
            if isinstance(body_meta, dict):
                start_time = body_meta.get("_tud_start")

        if start_time is None:
            timing_key = None
            if __metadata__:
                timing_key = __metadata__.get("_tud_timing_key")
            if not timing_key:
                body_meta = body.get("metadata", {})
                if isinstance(body_meta, dict):
                    timing_key = body_meta.get("_tud_timing_key")
            if not timing_key and __metadata__:
                chat_id = __metadata__.get("chat_id", "") or ""
                message_id = __metadata__.get("message_id", "") or ""
                if chat_id or message_id:
                    timing_key = f"{chat_id}:{message_id}"
            if timing_key and timing_key in _request_timings:
                start_time = _request_timings.pop(timing_key)

        if start_time is not None:
            elapsed_seconds = time.time() - start_time
        else:
            cutoff = time.time() - 600
            stale_keys = [k for k, v in _request_timings.items() if v < cutoff]
            for k in stale_keys:
                _request_timings.pop(k, None)

        # --- Extract token usage ---
        input_tokens = None
        output_tokens = None
        is_api_reported = False

        body_usage = body.get("usage") if isinstance(body.get("usage"), dict) else None
        body_info = body.get("info") if isinstance(body.get("info"), dict) else None

        def _pick_input(src: dict) -> int | None:
            return (
                src.get("prompt_eval_count")
                or src.get("prompt_tokens")
                or src.get("input_tokens")
            )

        def _pick_output(src: dict) -> int | None:
            return (
                src.get("eval_count")
                or src.get("completion_tokens")
                or src.get("output_tokens")
            )

        # Method 1: Read API-reported usage from assistant message "info" dict
        info = assistant_msg.get("info", {})
        if isinstance(info, dict) and info:
            input_tokens = _pick_input(info)
            output_tokens = _pick_output(info)
            if input_tokens is not None or output_tokens is not None:
                is_api_reported = True
                input_tokens = input_tokens or 0
                output_tokens = output_tokens or 0

        # Method 2: Check "usage" key directly on the message
        if not is_api_reported:
            usage = assistant_msg.get("usage", {})
            if isinstance(usage, dict) and usage:
                input_tokens = _pick_input(usage)
                output_tokens = _pick_output(usage)
                if input_tokens is not None or output_tokens is not None:
                    is_api_reported = True
                    input_tokens = input_tokens or 0
                    output_tokens = output_tokens or 0

        # Method 2b: Body-level usage/info (0.9.x may surface tokens at body root)
        if not is_api_reported:
            for src in (body_usage, body_info):
                if isinstance(src, dict) and src:
                    input_tokens = _pick_input(src)
                    output_tokens = _pick_output(src)
                    if input_tokens is not None or output_tokens is not None:
                        is_api_reported = True
                        input_tokens = input_tokens or 0
                        output_tokens = output_tokens or 0
                        break

        # Method 3: Fallback to tiktoken estimation
        if not is_api_reported and self.valves.fallback_to_tiktoken:
            model_id = ""
            if __model__ and isinstance(__model__, dict):
                model_id = __model__.get("id", "")
            elif body.get("model"):
                model_id = body["model"]

            # Estimate output tokens from assistant response
            response_text = _extract_text_content(assistant_msg)
            if response_text:
                output_tokens = _count_tokens_tiktoken(response_text, model_id)

            # Estimate input tokens from conversation messages
            if self.valves.count_all_messages_for_input:
                input_text_parts = []
                for msg in messages:
                    if msg.get("role") != "assistant" or msg is not assistant_msg:
                        if msg is not assistant_msg:
                            input_text_parts.append(_extract_text_content(msg))
                input_tokens = _count_tokens_tiktoken(
                    " ".join(input_text_parts), model_id
                )
            else:
                # Just count the last user message
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        input_tokens = _count_tokens_tiktoken(
                            _extract_text_content(msg), model_id
                        )
                        break

        # --- Extract detailed token breakdown ---
        reasoning_tokens = None
        cached_tokens = None
        audio_tokens_in = None
        audio_tokens_out = None

        for source in (
            assistant_msg.get("info", {}),
            assistant_msg.get("usage", {}),
            body_info or {},
            body_usage or {},
        ):
            if not isinstance(source, dict) or not source:
                continue

            # completion_tokens_details
            comp_details = source.get("completion_tokens_details")
            if isinstance(comp_details, dict):
                if reasoning_tokens is None:
                    val = comp_details.get("reasoning_tokens")
                    if isinstance(val, (int, float)) and val > 0:
                        reasoning_tokens = int(val)
                if audio_tokens_out is None:
                    val = comp_details.get("audio_tokens")
                    if isinstance(val, (int, float)) and val > 0:
                        audio_tokens_out = int(val)

            # prompt_tokens_details
            prompt_details = source.get("prompt_tokens_details")
            if isinstance(prompt_details, dict):
                if cached_tokens is None:
                    val = prompt_details.get("cached_tokens")
                    if isinstance(val, (int, float)) and val > 0:
                        cached_tokens = int(val)
                if audio_tokens_in is None:
                    val = prompt_details.get("audio_tokens")
                    if isinstance(val, (int, float)) and val > 0:
                        audio_tokens_in = int(val)

            # Anthropic-style cache fields at top level (read + write)
            if cached_tokens is None:
                val = source.get("cache_read_input_tokens")
                if isinstance(val, (int, float)) and val > 0:
                    cached_tokens = int(val)
            if cached_tokens is None:
                val = source.get("cache_creation_input_tokens")
                if isinstance(val, (int, float)) and val > 0:
                    cached_tokens = int(val)

        # --- Extract base model name for custom workspace models ---
        base_model_name = None
        if self.valves.show_model_name and __model__ and isinstance(__model__, dict):
            info = __model__.get("info")
            if isinstance(info, dict):
                base_model_id = info.get("base_model_id")
                if base_model_id:
                    base_model_name = base_model_id

        # --- Build stats display ---
        stats_parts = []

        if self.valves.show_input_tokens and input_tokens is not None:
            stats_parts.append(f"⬆︎ {input_tokens:,}")

        if self.valves.show_output_tokens and output_tokens is not None:
            stats_parts.append(f"⬇︎ {output_tokens:,}")

        if (
            self.valves.show_total_tokens
            and input_tokens is not None
            and output_tokens is not None
        ):
            total = input_tokens + output_tokens
            stats_parts.append(f"Σ {total:,}")

        if self.valves.show_reasoning_tokens and reasoning_tokens is not None:
            stats_parts.append(f"🧠 {reasoning_tokens:,}")

        if self.valves.show_cached_tokens and cached_tokens is not None:
            stats_parts.append(f"💾 {cached_tokens:,}")

        if self.valves.show_audio_tokens:
            total_audio = (audio_tokens_in or 0) + (audio_tokens_out or 0)
            if total_audio > 0:
                stats_parts.append(f"🔊 {total_audio:,}")

        if self.valves.show_generation_time and elapsed_seconds is not None:
            stats_parts.append(f"⏱ {_format_duration(elapsed_seconds)}")

        if (
            self.valves.show_tokens_per_second
            and output_tokens
            and elapsed_seconds
            and elapsed_seconds > 0
        ):
            tps = output_tokens / elapsed_seconds
            stats_parts.append(f"⚡ {tps:.1f} t/s")

        if self.valves.show_model_name and base_model_name:
            stats_parts.append(f"🤖 {base_model_name}")

        if self.valves.show_data_source and stats_parts:
            label = "API" if is_api_reported else "est."
            stats_parts.append(f"[{label}]")

        # --- Emit status event ---
        if stats_parts and __event_emitter__:
            stats_string = " · ".join(stats_parts)
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": stats_string,
                        "done": True,
                    },
                }
            )

        if self.valves.debug_mode and __event_emitter__:
            debug_payload = {
                "task": task,
                "messages_count": len(messages),
                "assistant_keys": sorted(assistant_msg.keys()),
                "assistant_info": assistant_msg.get("info"),
                "assistant_usage": assistant_msg.get("usage"),
                "body_keys": sorted(body.keys()),
                "body_usage": body_usage,
                "body_info": body_info,
                "metadata_keys": sorted(__metadata__.keys()) if __metadata__ else [],
                "model_id": (__model__ or {}).get("id"),
                "is_api_reported": is_api_reported,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cached_tokens": cached_tokens,
                "elapsed_seconds": elapsed_seconds,
            }
            try:
                debug_str = json.dumps(debug_payload, default=str, ensure_ascii=False)
            except Exception as exc:
                debug_str = f"serialization error: {exc}"
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"[TUD debug] {debug_str}",
                        "done": True,
                    },
                }
            )

        return body
