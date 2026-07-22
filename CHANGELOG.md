# Changelog

All notable changes to this project are documented here. The format loosely follows
[Keep a Changelog](https://keepachangelog.com/). The authoritative version is the
`version:` field in the `usage_display.py` docstring.


## [2.5.1]

- **`context_size_map` now takes precedence over the live models.dev fetch.** It was previously merged into the built-in table, which is consulted *after* the models.dev lookup — so with `fetch_context_from_modelsdev` enabled, an automatic remote match silently overrode your explicit manual entry. It's now resolved in its own tier, right after `override`/`num_ctx` and **before** models.dev and the static table (source `user_map` in the debug payload), matching how `price_map` already beats models.dev for cost. Consequences: a map entry now wins outright on any substring match (even against a longer static/models.dev key — the same semantics as `price_map`); a falsy size (`0`) is skipped and falls through; parsing is per-entry tolerant (one bad value drops only that entry). It's returned uncached, so edits take effect immediately.

## [2.5.0]

- **Workspace/custom models now infer context size and cost from their base model.** Context-window and cost matching previously keyed on the top-level model id, which for a custom model / "agent" is an arbitrary label (e.g. `research`) that matches no context/price table — so context % and estimated/native cost silently went blank unless you mapped the agent id by hand. Both now resolve the model's **`base_model_id`** (the real underlying LLM — the same id the model-name metric shows and the one Open WebUI itself calls) and fall back to the top-level id only when there is no base. A single `context_size_map` / `price_map` entry keyed on the base model now covers every agent built on it; direct base models are unaffected (no `base_model_id` → their own id is used). The `debug_mode` payload's `model.resolved_id` and provider guess now reflect the id actually used for matching.

## [2.4.0]

- **`debug_mode`: provider `cost_details`.** When the provider reports a cost breakdown next to the total (OpenRouter's `usage.cost_details` — `upstream_inference_cost`, `cache_discount`), it's now surfaced under the `cost_debug` block. The displayed cost still uses the authoritative top-level `cost`; this is diagnostics only (the provider-vs-upstream split and any cache discount).
- **`debug_mode`: web-search usage hint.** A new `web_search` block flags whether the response carried server-side web-search citations (`url_citation` → OWUI `sources`), with a citation count and the domains. It's a heads-up that a provider-side search/tool surcharge may not be fully reflected in `usage.cost` (and BYOK engines bill outside the gateway entirely) — cross-check the Activity page. Detection only; the visible stats line is byte-identical.
- **Docs:** added a "Does the cost include tool / function / MCP calls?" section (Open-WebUI-orchestrated tool turns are summed across rounds into the shown cost; only a provider's own server-side tool surcharge outside `usage.cost` is invisible), and strengthened the `estimate`-mode caveat — a token×price estimate can drift several-fold under a gateway's routing / tier / cache pricing, so `auto` (native, authoritative) is preferred.

## [2.3.0]

- **Configurable metric order.** New admin `display_order` valve sets the order of the stats line via a comma-separated list of metric keys (`input, output, total, reasoning, cached, audio, context, time, tps, cost, cost_total, model, source`). The field is **pre-filled with the full default order** for discoverability — reorder or trim it (empty also means default, so it stays fully backward-compatible). Ordering only — the existing `show_*` toggles still decide visibility (removing a key does not hide it, it moves to the end), and enabled-but-unlisted metrics are appended in the default order. Unknown / misspelled keys are ignored and surfaced under a new `display_order` block in the `debug_mode` payload.
- **Configurable line appearance.** New admin valves: `separator` (string between items, default ` · `); `icon_style` — `emoji` (default), `simple` (monochrome unicode, e.g. `↑ ↓ Σ`, context severity `○ ◐ ●`), or `off` (bare values, no icons); and `compact_numbers` (abbreviate the six token counters as `k`/`M`, matching the context-window style). All default to the previous behavior, so the line is byte-identical out of the box.
- **Cost display threshold.** New `cost_min_display` valve hides message/cumulative cost below a USD threshold (default `0.0` shows everything, including `$0.00`).
- Metric order is now **admin-only** — the earlier per-user override was dropped as unnecessary; `UserValves.enabled` (hide the whole line for yourself) is unchanged.

## [2.2.0]

- **Fixed Anthropic cache double-counting behind a proxy (LiteLLM, etc.).** Some proxies fold Anthropic's cache tokens *into* `prompt_tokens` while still reporting the top-level `cache_read_input_tokens` / `cache_creation_input_tokens`; the plugin previously treated those as *additional* to input, inflating both the total token count and the estimated cost (roughly doubling the cached portion). Cache accounting is now unified around an uncached `fresh_input`: cache is detected as a subset of input (OpenAI / DeepSeek / Gemini / Anthropic-via-proxy) or on top of it (native Anthropic), so totals and estimated cost match regardless of how the same call is routed. Provider-reported (`auto`) cost was never affected.
- **Richer `debug_mode` payload for troubleshooting model/cost issues** (still copyable via the "Token Usage & Cost Display - Debug info" source + server log; the visible stats line is unchanged). Four new blocks:
    - **`model`** — sanitized, safe-to-share identity of the selected model: `id`/`name`/`base_model_id`, `owned_by`, `connection_type`, `provider`, `preset`/`is_pipe`/`has_url_idx`, a best-effort `provider_guess`, the `function_calling` mode and other OWUI params. Uses a strict whitelist, so no prompt text, user/session ids, access grants, base URLs or API keys ever appear. Generation params (temperature/top_p/…) are marked unavailable on purpose — Open WebUI strips them before the filter runs.
    - **`cost_debug`** — why the cost came out the way it did: the `cost_mode`, whether a native provider cost was found (and from which key), the resolved **price source** (`price_map`/`models.dev`/`static`/`none`) with the exact **matched key** (`null` is the tell-tale for an id that matches no price entry), the applied per-1M rates, and a full per-component breakdown (billable input, cached, cache-write, output → USD each).
    - **`context_debug`** — how the context-window size was resolved (`override`/`num_ctx`/`user_map`/`models.dev`/`static_table`/`probe`) and which key matched, plus size/used/percent.
    - **`valves`** — a snapshot of the current admin configuration to reproduce issues from a single payload; the two optional local-backend URLs (`llamacpp_url`, `llama_swap_url`) are masked.

## [2.1.0]

- **Message & chat cost.** New `cost_mode` valve: `auto` (default) shows the provider's own reported cost only (OpenRouter `usage.cost`, LiteLLM, ...) and never fetches or guesses; `estimate` additionally approximates cost from models.dev prices (always marked `≈`), with correct cached/reasoning and Anthropic cache-read/write pricing; `off` disables cost entirely. Shows the per-message cost (`💰`) and a running chat total (`💰Σ`) in USD.
- Optional live **models.dev price fetch** (`api.json`, exact per-provider prices, cached ~24h, on by default in estimate mode) with a built-in offline price table as fallback and a manual `price_map` override that always wins.
- **`debug_mode` is now copyable in-chat:** the stats line just gets a small `(debug)` marker, and the full diagnostic payload is attached as a "Token Usage & Cost Display - Debug info" source whose modal renders it as a JSON block with a Copy button and persists with the message (still mirrored to the server log). The previous status line was clamped to one line and couldn't be selected.
- **Minimum lowered to Open WebUI 0.9.0** (was 0.10.0+): the data contracts the plugin relies on - per-message `usage` in the outlet (since 0.9.0) and normalized usage (since 0.8.0) - predate 0.10, so it runs on 0.9.x with graceful degradation; only provider-reported cost in `auto` mode needs 0.10.0+ (use `estimate` on older versions).
- `debug_mode` now writes the full payload to the server/container logs (copyable, never truncated) and shows only a short summary in the chat status line - the previous status-only output was clamped to one line and couldn't be selected.

## [2.0.0] - full rebuild for Open WebUI 0.10.x (breaking: requires 0.10.0+)

- Reworked around 0.10.x **structured output** and **normalized usage**; fixes the "nothing shows" regression on newer Open WebUI.
- **Responses API** support - reasoning and cached tokens are now read from `output_tokens_details` / `input_tokens_details`, not only the Chat Completions shape.
- Correct **Anthropic cache** semantics (`cache_read` / `cache_creation` counted as additional to input).
- **Context-window utilization** display with warning/critical thresholds.
- **Provider-reported timing** (Ollama / llama.cpp) with a labeled wall-clock fallback.
- Token-estimation fallback now reads text from the structured `output` (fixes empty estimates on streaming responses).
- **`tiktoken` is now optional** (soft import; the `requirements:` line was removed) - fixes install failures under uvx / LXC.
- Internal timing-store cleanup fixed (no more unbounded growth).
- Context-window sizes seeded from **models.dev** with an optional **live fetch** (cached ~24h) for always-current values.
- Added current-generation models to the built-in table (Gemini 3.x, GPT-5.x, Claude Opus 4.6+/Sonnet 5 at 1M, Llama 4, DeepSeek V4, GLM 5.x, Qwen3, Kimi K2, MiniMax M3, etc.).
- llama.cpp / llama-swap context-size probing (opt-in).
