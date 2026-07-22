# Token Usage & Cost Display

> Copy the **Description** into the Open WebUI "Description" field, and the **Full text** into the post body.

Community page, comments and feedback: https://openwebui.com/posts/a94ea72f-8a84-4686-ad36-dd51342cc45d

---

## Description

Shows token counts, reasoning/cached breakdowns, context-window utilization, generation time, tokens/sec and message/chat cost below each AI response. Reads provider-reported usage across OpenAI (Chat & Responses API), Anthropic, Gemini, Ollama and llama.cpp, with an optional tiktoken fallback. Cost is shown when your provider/proxy reports it (OpenRouter, LiteLLM) and can optionally be estimated from models.dev prices.

---

## Full text

**Token Usage & Cost Display** is a filter plugin for Open WebUI that shows detailed token usage statistics below each AI response - rebuilt around Open WebUI's **0.10.x** data model (structured output + normalized usage), compatible back to **0.9.0**, and covering both of OpenAI's APIs, Anthropic, Gemini, Ollama and llama.cpp.

> **Works on Open WebUI 0.9.0+.** It's built for the 0.10.x data model but degrades gracefully on 0.9.x - the only limitation there is provider-reported cost in `auto` mode, which needs 0.10.0+ (use `estimate` on older versions). `tiktoken` is **optional** - the plugin loads fine without it (only used for estimation when a provider reports no usage).

### What it displays

- **Input / output / total** token counts
- **Reasoning / thinking** tokens - OpenAI o3/o4-mini & GPT-5, Gemini 2.5/3 thinking, DeepSeek R1, Claude thinking, etc. (read from both the Chat Completions and the newer Responses API shapes)
- **Cached prompt** tokens - OpenAI prompt cache and Anthropic `cache_read` / `cache_creation` (Anthropic cache is correctly counted as *additional* to input)
- **Context-window utilization** - how much of the model's window you've used, e.g. `8.8k/200k (4%)`; the icon turns 🟠 at 30% and 🔴 at 70% by default (configurable via `context_warn_percent` / `context_critical_percent`)
- **Generation time** and **tokens/second**
- **Cost** - the cost of the message and a running total for the chat: the provider's own reported cost when available (OpenRouter, LiteLLM, ...), or an optional estimate from models.dev prices (always marked `≈`)
- **Audio** tokens (off by default) and the **base model name** for workspace models

**Example output:**

`⬆︎ 8,204 · ⬇︎ 586 · Σ 8,790 · 🧠 412 · 💾 4,096 · 📐 8.8k/200k (4%) · ⏱ 3.1s · ⚡ 189.0 t/s · 💰 $0.0284 · 💰Σ $0.1120`

Each metric only appears when it has a value, so the line stays clean for models that don't report detailed breakdowns.

### How it works

The filter reads Open WebUI's **normalized usage** attached to the response, so it works consistently across providers and response types:

- **Providers:** OpenAI (Chat Completions **and** Responses API), Anthropic, Google Gemini, Ollama, llama.cpp, and any OpenAI-compatible endpoint.
- **Token counts** come straight from the reported usage. When a provider reports no usage, the plugin falls back to **tiktoken** estimation over the response text (extracted from the 0.10.x structured `output`).
- **Timing / tokens-per-second** prefer the provider's own numbers where available (Ollama `eval_duration`, llama.cpp `timings`). Otherwise a wall-clock measurement is shown, prefixed with `~` to indicate it's approximate - note that wall-clock can include time spent on web search, RAG or tool calls, since there is no hook immediately before the model call.

### Context-window detection

The context window size is resolved in this order:

1. **Manual override** valve (if set)
2. **`num_ctx`** captured from the request (Ollama / local models)
3. **Your `context_size_map`** - an explicit user override, so it wins over the automatic sources below (mirrors how `price_map` beats models.dev for cost)
4. **Live models.dev lookup** - optional, opt-in; fetches current sizes and caches them (default off, for privacy)
5. **Built-in table** - seeded from [models.dev](https://models.dev), covers the popular OpenAI / Anthropic / Gemini / Llama / DeepSeek / Grok / Mistral / Qwen / Kimi / GLM / MiniMax / Cohere families
6. **llama.cpp / llama-swap probe** - optional, opt-in; queries `/props` or `/running` for the running context size

**Workspace models resolve via their base model.** For a custom model / "agent" built on a base model, matching (context size **and** cost) uses the model's **`base_model_id`** - the real underlying LLM - not your custom model id, which is an arbitrary label (e.g. `research`) that carries no provider token. So a single `context_size_map` entry keyed on the base model (or a models.dev match on it) covers every agent built on it - you don't need to map each custom id separately. Direct base models are unaffected: they have no `base_model_id`, so their own id is used.

Because vendors ship new versions with different context sizes constantly, enable the **live models.dev fetch** valve if you want always-current numbers; the built-in table is the offline default.

### Cost

Cost has three modes, set by the **`cost_mode`** valve:

1. **`auto`** (default) - show cost **only when your provider or proxy reports it** in the usage payload (e.g. OpenRouter's `usage.cost`, a LiteLLM proxy). Nothing is fetched or estimated - this is always the real, billed number.
2. **`estimate`** - additionally *approximate* the cost from [models.dev](https://models.dev) prices whenever the provider reports none. Estimates are always marked `≈`.
3. **`off`** - never show, compute or fetch anything cost-related.

In `estimate` mode the price is resolved as: your manual **`price_map`** valve → live models.dev `api.json` (per-provider prices, cached ~24h, on by default in this mode) → a built-in offline price table. Cached and reasoning tokens are priced correctly (cached input at the discounted cache-read rate; Anthropic cache-read/write counted on top of input).

Both the **message** cost (`💰`) and a running **chat total** (`💰Σ`) are shown; the total is summed from each message's own reported/estimated cost.

> **Estimates are approximate on purpose.** models.dev lists *published* per-provider prices - your real bill can differ, sometimes by a large multiple. A gateway like OpenRouter routes the same model id to different upstream providers (primary / fallback / BYOK) at different prices and applies cache, tier and long-context adjustments a static table can't see, so a token×price estimate can drift several-fold on tier-priced models; matching an arbitrary model id to a provider is also best-effort. When you need the exact figure, use a provider/proxy that reports `cost` in usage - `auto` mode is then the **authoritative billed number** (it can't be reconstructed from tokens), so prefer it over `estimate`; treat any `≈` number as a ballpark.

### Does the cost include tool / function / MCP calls?

**Yes - for the tools Open WebUI runs itself.** A tool / function / MCP call is just extra tokens across several LLM round-trips: the provider bills the tool schema and results as ordinary input/output tokens (there is no separate "function-calling fee"), and Open WebUI **sums the reported `cost` across every round** of a multi-round tool turn. So `💰` and `💰Σ` already cover the whole turn, not just the final call - the line simply mirrors the provider's own `usage.cost`.

What no client-side display can capture is a surcharge the provider charges *on top of* tokens and does **not** put into `usage.cost`. The main example is a gateway's **own server-side** web search / tools - OpenRouter's `web` plugin / `openrouter:web_search`, the `:online` suffix - which add a per-request fee; a BYOK search engine (e.g. Firecrawl) is billed entirely outside the generation. When such a surcharge is folded into the response's `usage.cost`, it's shown; when the provider bills it separately (BYOK, or only on your Activity page), it isn't. Turn on `debug_mode` to see a `web_search` hint (did the response carry web citations?) and the provider's `cost_details` split under `cost_debug`.

### Configuration

- Every metric can be toggled on/off via admin **Valves**.
- Users can disable the display entirely through **UserValves**.
- **Metric order is configurable** via the admin `display_order` valve (comma-separated keys: `input, output, total, reasoning, cached, audio, context, time, tps, cost, cost_total, model, source`). The field comes **pre-filled with the default order** — just reorder or trim it (empty also means default). It only *reorders*: visibility stays governed by the `show_*` toggles, so removing a key does **not** hide it — the metric just moves to the end, like any enabled-but-unlisted one.
- **Line appearance is configurable** (admin): `separator` (string between items, default ` · `), `icon_style` (`emoji` default / `simple` monochrome unicode / `off` bare values), and `compact_numbers` (abbreviate the six token counters as `k`/`M`, e.g. `12,345 → 12.3k`, matching the context style).
- Context detection is configurable: manual size override, a custom `{"model-substring": tokens}` map, the live models.dev fetch toggle, and optional llama.cpp / llama-swap URLs.
- Cost is configurable: `cost_mode` (`off` / `auto` / `estimate`), a running chat-total toggle, a `cost_min_display` threshold that hides negligible amounts (default `0.0` shows everything), a manual `price_map`, and the live models.dev price-fetch toggle.
- Audio tokens are off by default since few models use them.

### Troubleshooting

The fastest way to find out *why* a metric is missing is to turn on the **`debug_mode`** valve and open the **"Token Usage & Cost Display - Debug info"** source under the message (it's also mirrored to the server log). The `cost_debug`, `context_debug`, `model` and `valves` blocks name the exact broken link — the cases below map those fields to a fix.

**Nothing shows below the response at all (no tokens).**
The provider returned no `usage`. In streaming — Open WebUI's default — most OpenAI-compatible endpoints only send a usage chunk when the request explicitly asks for one. Enable the model's **Usage** capability (Workspace → Models → *your model* → Capabilities → **Usage**); it is **off by default** and is what makes Open WebUI send `stream_options: {include_usage: true}`. Behind a LiteLLM proxy you can instead force it globally with `general_settings: { always_include_stream_usage: true }`. Non-streaming responses carry `usage` regardless. Also confirm the filter is active and attached to the model.

**Tokens show but cost is blank, and `cost_mode` is `auto`** (`cost_debug.native.found: false`).
In `auto`, cost appears only when your provider/proxy reports it **inside the response body** `usage`. The important gotcha: **a LiteLLM proxy returns its cost in the `x-litellm-response-cost` HTTP header by default, and Open WebUI does not read that header** — so `auto` never sees it. Move the cost into the body with `litellm_settings: { include_cost_in_streaming_usage: true }`. That is the only flag of its kind and it applies to **streaming only** — a non-streaming `/chat/completions` body never carries cost. Once set, `auto` shows the exact billed number. Otherwise switch to `cost_mode: estimate` for an approximation (always marked `≈`).

**`cost_mode` is `estimate` but cost is still blank** (`cost_debug.price.matched_key: null`).
The model id matched no price entry. This is common when Open WebUI exposes a **display name** (e.g. `Anthropic - Opus`) instead of a canonical id (`claude-opus-4-…`) — there is no recognizable token to substring-match. Add a `price_map` keyed to a substring of that id, e.g. `{"anthropic - opus": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25}}` — or rename the model / set the LiteLLM `model_name` to a canonical id, which makes prices, context sizes **and** models.dev all match automatically. For a **workspace/custom model** the id matched is its `base_model_id` (since v2.5.0), so key the `price_map` on the **base** model, not the agent id.

**Context % is missing or wrong** (`context_debug.matched_key: null`, `size: null`).
Same id-matching problem, for the context table. Add a `context_size_map` (e.g. `{"anthropic - opus": 1000000}`), enable the `fetch_context_from_modelsdev` valve, or use a canonical id. For a **workspace/custom model** matching uses its `base_model_id` (since v2.5.0), so key any map on the **base** model, not the agent id. Note: a connection `prefix_id` (which prepends `prefix.`) does **not** break matching — the original slug is still a substring; only a rename that alters the model-family token (e.g. `claude-opus-4.8` vs a `claude-opus-4-8` table key) does.

**Anthropic cost or token totals look roughly doubled behind a proxy.**
Fixed in **v2.2.0** — upgrade. Some proxies (LiteLLM) fold Anthropic's cache tokens into `prompt_tokens` while still reporting the top-level cache fields, and older versions counted them twice.

### Notes & limitations

- For providers that **don't** report a generation duration (OpenAI, Anthropic), time and t/s are measured as wall-clock and marked `~`; with web search / RAG / tool calls enabled, that figure includes the pre-model work. Local providers (Ollama, llama.cpp) report real generation time and are unaffected.
- In multi-round tool turns, Open WebUI only carries the last round's provider-specific cache/timing fields, so those can reflect the final round rather than the whole turn.
- Estimated cost (`≈`) is a ballpark from published models.dev prices, not your invoice; long-context tier pricing isn't modelled and, when the model can't be mapped to a provider, no estimate is shown. Provider-reported cost (`auto` mode) is exact.

---

## Changelog

### v2.5.1

- **`context_size_map` now takes precedence over the live models.dev fetch.** It was previously merged into the built-in table, which is consulted *after* the models.dev lookup — so with `fetch_context_from_modelsdev` enabled, an automatic remote match silently overrode your explicit manual entry. It's now resolved in its own tier, right after `override`/`num_ctx` and **before** models.dev and the static table (source `user_map` in the debug payload), matching how `price_map` already beats models.dev for cost. Consequences: a map entry now wins outright on any substring match (even against a longer static/models.dev key — the same semantics as `price_map`); a falsy size (`0`) is skipped and falls through; parsing is per-entry tolerant (one bad value drops only that entry). It's returned uncached, so edits take effect immediately.

### v2.5.0

- **Workspace/custom models now infer context size and cost from their base model.** Context-window and cost matching previously keyed on the top-level model id, which for a custom model / "agent" is an arbitrary label (e.g. `research`) that matches no context/price table — so context % and estimated/native cost silently went blank unless you mapped the agent id by hand. Both now resolve the model's **`base_model_id`** (the real underlying LLM — the same id the model-name metric shows and the one Open WebUI itself calls) and fall back to the top-level id only when there is no base. A single `context_size_map` / `price_map` entry keyed on the base model now covers every agent built on it; direct base models are unaffected (no `base_model_id` → their own id is used). The `debug_mode` payload's `model.resolved_id` and provider guess now reflect the id actually used for matching.

### v2.4.0

- **`debug_mode`: provider `cost_details`.** When the provider reports a cost breakdown next to the total (OpenRouter's `usage.cost_details` — `upstream_inference_cost`, `cache_discount`), it's now surfaced under the `cost_debug` block. The displayed cost still uses the authoritative top-level `cost`; this is diagnostics only (the provider-vs-upstream split and any cache discount).
- **`debug_mode`: web-search usage hint.** A new `web_search` block flags whether the response carried server-side web-search citations (`url_citation` → OWUI `sources`), with a citation count and the domains. It's a heads-up that a provider-side search/tool surcharge may not be fully reflected in `usage.cost` (and BYOK engines bill outside the gateway entirely) — cross-check the Activity page. Detection only; the visible stats line is byte-identical.
- **Docs:** added a "Does the cost include tool / function / MCP calls?" section (Open-WebUI-orchestrated tool turns are summed across rounds into the shown cost; only a provider's own server-side tool surcharge outside `usage.cost` is invisible), and strengthened the `estimate`-mode caveat — a token×price estimate can drift several-fold under a gateway's routing / tier / cache pricing, so `auto` (native, authoritative) is preferred.

### v2.3.0

- **Configurable metric order.** New admin `display_order` valve sets the order of the stats line via a comma-separated list of metric keys (`input, output, total, reasoning, cached, audio, context, time, tps, cost, cost_total, model, source`). The field is **pre-filled with the full default order** for discoverability — reorder or trim it (empty also means default, so it stays fully backward-compatible). Ordering only — the existing `show_*` toggles still decide visibility (removing a key does not hide it, it moves to the end), and enabled-but-unlisted metrics are appended in the default order. Unknown / misspelled keys are ignored and surfaced under a new `display_order` block in the `debug_mode` payload.
- **Configurable line appearance.** New admin valves: `separator` (string between items, default ` · `); `icon_style` — `emoji` (default), `simple` (monochrome unicode, e.g. `↑ ↓ Σ`, context severity `○ ◐ ●`), or `off` (bare values, no icons); and `compact_numbers` (abbreviate the six token counters as `k`/`M`, matching the context-window style). All default to the previous behavior, so the line is byte-identical out of the box.
- **Cost display threshold.** New `cost_min_display` valve hides message/cumulative cost below a USD threshold (default `0.0` shows everything, including `$0.00`).
- Metric order is now **admin-only** — the earlier per-user override was dropped as unnecessary; `UserValves.enabled` (hide the whole line for yourself) is unchanged.

### v2.2.0

- **Fixed Anthropic cache double-counting behind a proxy (LiteLLM, etc.).** Some proxies fold Anthropic's cache tokens *into* `prompt_tokens` while still reporting the top-level `cache_read_input_tokens` / `cache_creation_input_tokens`; the plugin previously treated those as *additional* to input, inflating both the total token count and the estimated cost (roughly doubling the cached portion). Cache accounting is now unified around an uncached `fresh_input`: cache is detected as a subset of input (OpenAI / DeepSeek / Gemini / Anthropic-via-proxy) or on top of it (native Anthropic), so totals and estimated cost match regardless of how the same call is routed. Provider-reported (`auto`) cost was never affected.
- **Richer `debug_mode` payload for troubleshooting model/cost issues** (still copyable via the "Token Usage & Cost Display - Debug info" source + server log; the visible stats line is unchanged). Four new blocks:
    - **`model`** — sanitized, safe-to-share identity of the selected model: `id`/`name`/`base_model_id`, `owned_by`, `connection_type`, `provider`, `preset`/`is_pipe`/`has_url_idx`, a best-effort `provider_guess`, the `function_calling` mode and other OWUI params. Uses a strict whitelist, so no prompt text, user/session ids, access grants, base URLs or API keys ever appear. Generation params (temperature/top_p/…) are marked unavailable on purpose — Open WebUI strips them before the filter runs.
    - **`cost_debug`** — why the cost came out the way it did: the `cost_mode`, whether a native provider cost was found (and from which key), the resolved **price source** (`price_map`/`models.dev`/`static`/`none`) with the exact **matched key** (`null` is the tell-tale for an id that matches no price entry), the applied per-1M rates, and a full per-component breakdown (billable input, cached, cache-write, output → USD each).
    - **`context_debug`** — how the context-window size was resolved (`override`/`num_ctx`/`user_map`/`models.dev`/`static_table`/`probe`) and which key matched, plus size/used/percent.
    - **`valves`** — a snapshot of the current admin configuration to reproduce issues from a single payload; the two optional local-backend URLs (`llamacpp_url`, `llama_swap_url`) are masked.

### v2.1.0

- **Message & chat cost.** New `cost_mode` valve: `auto` (default) shows the provider's own reported cost only (OpenRouter `usage.cost`, LiteLLM, ...) and never fetches or guesses; `estimate` additionally approximates cost from models.dev prices (always marked `≈`), with correct cached/reasoning and Anthropic cache-read/write pricing; `off` disables cost entirely. Shows the per-message cost (`💰`) and a running chat total (`💰Σ`) in USD.
- Optional live **models.dev price fetch** (`api.json`, exact per-provider prices, cached ~24h, on by default in estimate mode) with a built-in offline price table as fallback and a manual `price_map` override that always wins.
- **`debug_mode` is now copyable in-chat:** the stats line just gets a small `(debug)` marker, and the full diagnostic payload is attached as a "Token Usage & Cost Display - Debug info" source whose modal renders it as a JSON block with a Copy button and persists with the message (still mirrored to the server log). The previous status line was clamped to one line and couldn't be selected.
- **Minimum lowered to Open WebUI 0.9.0** (was 0.10.0+): the data contracts the plugin relies on - per-message `usage` in the outlet (since 0.9.0) and normalized usage (since 0.8.0) - predate 0.10, so it runs on 0.9.x with graceful degradation; only provider-reported cost in `auto` mode needs 0.10.0+ (use `estimate` on older versions).
- `debug_mode` now writes the full payload to the server/container logs (copyable, never truncated) and shows only a short summary in the chat status line - the previous status-only output was clamped to one line and couldn't be selected.

### v2.0.0 - full rebuild for Open WebUI 0.10.x (breaking: requires 0.10.0+)

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
