# Token Usage & Cost Display

> Copy the **Description** into the Open WebUI "Description" field, and the **Full text** into the post body.

Community page, comments and feedback: https://openwebui.com/posts/token_usage_display_a94ea72f

---

## Description

Shows token counts, reasoning/cached breakdowns, context-window utilization, generation time, tokens/sec and message/chat cost below each AI response. Reads provider-reported usage across OpenAI (Chat & Responses API), Anthropic, Gemini, Ollama and llama.cpp, with an optional tiktoken fallback. Cost is shown when your provider/proxy reports it (OpenRouter, LiteLLM) and can optionally be estimated from models.dev prices.

---

## Full text

> **GitHub:** [github.com/SmetDenis/openwebui-token-usage-display](https://github.com/SmetDenis/openwebui-token-usage-display) - source code, full documentation, changelog and issue tracker. Please, star it.

**Token Usage & Cost Display** is a filter plugin for Open WebUI that shows detailed token usage statistics below each AI response - rebuilt around Open WebUI's **0.10.x** data model (structured output + normalized usage), compatible back to **0.9.0**, and covering both of OpenAI's APIs, Anthropic, Gemini, Ollama and llama.cpp.

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

**Found a bug, or none of the above helped?** Please [open an issue on GitHub](https://github.com/SmetDenis/openwebui-token-usage-display/issues) and attach the `debug_mode` payload — it is sanitized (no prompts, ids or secrets) and names the exact broken link, which makes most reports fixable from a single paste.

### Notes & limitations

- For providers that **don't** report a generation duration (OpenAI, Anthropic), time and t/s are measured as wall-clock and marked `~`; with web search / RAG / tool calls enabled, that figure includes the pre-model work. Local providers (Ollama, llama.cpp) report real generation time and are unaffected.
- In multi-round tool turns, Open WebUI only carries the last round's provider-specific cache/timing fields, so those can reflect the final round rather than the whole turn.
- Estimated cost (`≈`) is a ballpark from published models.dev prices, not your invoice; long-context tier pricing isn't modelled and, when the model can't be mapped to a provider, no estimate is shown. Provider-reported cost (`auto` mode) is exact.

### Changelog

The full version history is maintained on GitHub: [CHANGELOG.md](https://github.com/SmetDenis/openwebui-token-usage-display/blob/main/CHANGELOG.md)
