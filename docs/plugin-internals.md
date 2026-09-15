# usage_display.py internals (maintainer reference)

How the plugin works end-to-end, why it is designed this way, and the edge-case catalog with the
covering tests. `:NNN` line numbers refer to plugin v2.5.2 (1959 lines) and have drifted since; treat
them as anchors, not gospel. Entries added in v2.6.0 cite function names instead.

## Pipeline

### `inlet()` — `usage_display.py:894-928`

Runs before the LLM call. Computes a timing key `f"{chat_id}:{message_id}"` from `__metadata__`
(falling back to `f"fallback:{id(body)}"` when both ids are absent, line 903), stores
`time.time()` in module-level `_request_timings`, and captures a `num_ctx` hint from
`body["num_ctx"]` / `body["options"]["num_ctx"]` / `body["params"]["num_ctx"]` (the `options` path
is the live one for Ollama; `params` is a dead-path safety net — OWUI pops it before inlet).
The key, start time and hint are mirrored into both `body["metadata"]` and `__metadata__`
(`_tud_timing_key`, `_tud_start`, `_tud_num_ctx`) so outlet can recover them even if the module
dict was lost (server restart, multi-worker).

### `outlet()` — `usage_display.py:932-1009`

Orchestration order (each step degrades gracefully, never raises out):

1. **UserValves kill-switch** (`:940-942`) — `enabled=False` returns the body untouched.
2. **Background-task guard** (`:945-955`) — skips the 7 `metadata["task"]` values
   (`title_generation`, `tags_generation`, `follow_up_generation`, `emoji_generation`,
   `query_generation`, `autocomplete_generation`, `moa_response_generation`). Defensive only: task
   requests never run filter functions at all, through v0.11.1 (see `docs/owui-map.md`). The guard
   list is a subset: OWUI's `TASKS` enum gained `image_prompt_generation` and `function_calling`,
   so it now has 9 members — harmless while the requests never reach filters.
3. **Message gates** (`:957-962`) — empty `messages` or no assistant message → return.
4. `_resolve_wall_clock` (`:966`) → `_extract_tokens` (`:969`) → `_resolve_timing` (`:970`).
5. `_resolve_context` (`:971-974`) and `_resolve_cost` (`:977-980`) — each wrapped in its own
   `try/except Exception` with a sentinel fallback (`source="error"` / all-`None` cost), so a
   resolver bug can never kill the response pipeline.
6. `_build_stats` (`:982`) joins the parts with `valves.separator`; `debug_mode` appends `(debug)`.
7. Emits `{"type": "status", "data": {"description": ..., "done": True}}` (`:986-992`), then in
   debug mode `_emit_debug` (`:994-1007`) emits a `citation` event (copyable modal, persisted) and
   mirrors the pretty JSON to server stdout as `[TUD debug]`.

## The OWUI contract (NOTE block) and its verification status

The NOTE block at `usage_display.py:14-46` is the source-verified contract with OWUI. Re-verified
against OWUI v0.11.0 + a `v0.11.0...v0.11.1` diff check (2026-08):

- Normalized triple guaranteed on every save path — **confirmed** (`utils/response.py:14-51,105`).
- `info` is a redundant mirror of `usage` — **confirmed** (`models/chat_messages.py:370-371`).
- Detail keys differ per API shape, passed through untouched — **confirmed**.
- `merge_usage` sums tokens/cost/details but last-wins for Anthropic top-level cache and Ollama
  durations — **confirmed** (they are in none of the merge key sets).
- `content` is still not **persisted** at 0.11.x for either mode (every save path writes `output`
  only) — **confirmed**. What changed: the **outlet body** now carries a non-empty `content`,
  synthesized per message as `content or get_output_text(output)` while the body is assembled
  (`middleware.py:3479`). The NOTE block was updated accordingly — a content-first reader must
  still fall through to `output`, which is the only source on 0.10.x.
- Only saved chats carry history `usage` into outlet; unsaved chats rebuild history as
  role+content — **confirmed** in v0.11.0 and v0.11.3 (`outlet_filter_handler`, added to the NOTE
  block in v2.6.0 for the chat totals).

Any change to token/cost/context reading must be reconciled against this block AND re-verified
against real OWUI source of the version being targeted — never against memory or docs.

## Valves

Grouped inventory (`Valves` at `:755-884`, `UserValves` at `:886-887`). Defaults in parentheses
when non-obvious:

- **Visibility** — `show_input_tokens`, `show_output_tokens`, `show_total_tokens`,
  `show_cumulative_tokens` (True, v2.6.0),
  `show_generation_time`, `show_tokens_per_second`, `show_reasoning_tokens`, `show_cached_tokens`,
  `show_audio_tokens` (False), `show_model_name`, `show_data_source` (False),
  `show_context_window`, `show_cumulative_cost`. Message cost has **no** `show_*` — its visibility
  is `cost_mode` itself.
- **Presentation** — `display_order` (pre-filled full 14-key order), `separator` (`" · "`),
  `icon_style` (`emoji`/`simple`/`off`), `compact_numbers` (False).
- **Estimation** — `fallback_to_tiktoken` (True), `count_all_messages_for_input` (True).
- **Context** — `context_size_override` (0 = auto), `context_size_map` (JSON string),
  `fetch_context_from_modelsdev` (**False** — privacy: no network without consent),
  `modelsdev_url`, `modelsdev_ttl` (86400), `context_warn_percent` (30),
  `context_critical_percent` (70), `llamacpp_url`, `llama_swap_url` (both ""), `context_probe_ttl`
  (600).
- **Cost** — `cost_mode` (`auto`; Literal `off`/`auto`/`estimate`), `cost_min_display` (0.0),
  `price_map` (JSON string), `fetch_prices_from_modelsdev` (True — but only reachable in
  `estimate` mode, which is itself an explicit choice), `modelsdev_api_url`.
- **Misc** — `priority` (10), `debug_mode` (False).
- **UserValves** — `enabled` (True). Per-user display customization was deliberately rolled back:
  everything else is admin-only.

## Renderer dispatch

`_STATS_RENDERERS` (`:737-751`) maps 14 keys (`input`, `output`, `total`, `tokens_total`,
`reasoning`, `cached`, `audio`, `context`, `time`, `tps`, `cost`, `cost_total`, `model`, `source`)
to `_render_*`
functions with one signature `(s: _Stats) -> str | None`. `_Stats` is a frozen dataclass bundling
`valves`, `tokens`, `timing`, `ctx`, `cost`, `model` (v2.6.0; it replaced a fixed 6-positional-arg
signature that forced suppressing `PLR0913`/`PLR0917` and left every renderer with unused
arguments). `_build_stats` walks the resolved order, drops `None` results, and suppresses a line
consisting solely of `source` parts. `_emit_debug` takes the same `_Stats` plus a `_Turn` (task,
messages, assistant message, usage, metadata).

Rules: **`show_*` valves gate visibility; `display_order` only sorts.** `_resolve_display_order`
(`:451-458`) always returns a full 14-key permutation — removed keys move to the end, unknown keys
are ignored (surfaced only in the debug payload). To add a metric: add a `show_*` valve, write a
`_render_*(s: _Stats)`, register it in `_STATS_RENDERERS`, append the key to `_DEFAULT_ORDER`, add
an icon to `_ICON_EMOJI`/`_ICON_SIMPLE`.

## Resolution chains

### Context size — `_context_size_for` (v2.6.0 order)

1. `context_size_override` valve → 2. `num_ctx` hint from inlet → 3. user `context_size_map` →
4. **live window through the OWUI connection** (`_connection_context_size`, v2.6.0) →
5. **backend-advertised size** (`_backend_context_size`) → `_ctx_size_cache` hit →
6. probe row **matched** to the called model → 7. live models.dev (opt-in) → 8. `_STATIC_CONTEXT_SIZES` →
9. probe row **unmatched** (a backend's only running model under another id). Tiers 1–3 and 5 return
**uncached** on purpose (valve edits and a relisted model take effect immediately); tier 4 caches only hits
under its own key `connection:{urlIdx}:{listed id}`, and 6–9 results are cached under the model id (both
`max(60, context_probe_ttl)`). The user map was promoted above models.dev in v2.5.1 — an explicit user entry
must never lose to a live lookup. Tiers 1–3 live in `_explicit_context_size`, 6–9 in
`_automatic_context_size`.

**Tier 4 — the live window, asked through the model's OWUI connection (v2.6.0, issue #14).**
`_live_backend` reads the raw listing row under `__model__["openai"]` (a workspace model's comes from its base
entry in `app.state.MODELS`, `_base_model_entry`): `owned_by: llama-swap` (alias rows name their real model in
`meta.llamaswap.modelID`; `peer`/selector rows are skipped) or `owned_by: llamacpp` **with** a `status` block (a
llama.cpp router; a single llama-server has no `status` and already lists its live `meta.n_ctx`, tier 5).
`_owui_connection(urlIdx)` returns the base URL without `/v1`, a bearer header (only when the key is set and
`auth_type` is empty or `bearer`) and `prefix_id`, via `open_webui.routers.openai.get_openai_connection` (OWUI
0.10+) or `request.app.state.config.OPENAI_API_*` (0.9.x). `_ask_live_backend` then asks: llama-swap —
`_probe_llama_swap` (`/running` → `_llama_swap_ready_id` → `/props?model=`, else `--ctx-size`); router —
`/props?model=<id>&autoload=false`. `_props_n_ctx` reads `n_ctx` / `default_generation_settings.n_ctx` (the
per-slot window, the same number as `meta.n_ctx`). Every failure returns `None` → tier 5.

**Tier 5 — what the backend advertises.** OWUI keeps each OpenAI-connection model's raw `/v1/models` row
under `__model__["openai"]` *and* spreads it at the top level of the model dict (`routers/openai.py`
`get_all_models`: `{**model, 'openai': model, 'urlIdx': idx}`). `_advertised_context` reads
`meta.n_ctx` (llama.cpp since PR ggml-org/llama.cpp#22683, 2026-05-08 — the per-slot window
`n_ctx_slot()`, capped by `n_ctx_train`; llama-swap when `capabilities.context` is configured,
`internal/server/api.go`) and `max_model_len` (vLLM `ModelCard`). Non-positive values are ignored (llama.cpp
router mode lists `n_ctx: 0` for an unloaded model). A workspace/preset model has no row of its own, so its
`info.base_model_id` row is read from `__request__.app.state.MODELS` (a dict or OWUI's `RedisDict`, both
with `.get`), guarded by `except Exception`. The value reflects OWUI's model list at the time it was fetched
(base-model cache), not a live query.

**The probe** (`_probe_context`) asks llama-swap `/running` first (and, since v2.6.0, `/props?model=` for the
called model when its row is `ready`); unless that yields a matched row, it asks
llama.cpp `/v1/models` (id or `aliases` → `meta.n_ctx`) and, unless *that* matches, `/props` (older builds;
named by `model_alias`, `model_path` or the GGUF file name). Each returns `(size, matched_id | None)` via
`_match_running`: a row matches when its id equals the called id or ends it after `.` (OWUI connection
`prefix_id`) or `/`; a single unmatched row is returned with `None`, several unmatched rows return nothing. A
matched row ranks above models.dev/static (tier 6), an unmatched one below them (tier 9).

### Design decisions (v2.6.0, issue #6)

Issues #6 and #14 were built as 2.7.0 and 2.8.0 but never released separately: both were folded into the
re-cut v2.6.0 (see the release note in `CLAUDE.md`). The two sections below keep their order of design.

- **Root cause, not symptom:** the probe was below the static table, so for every family the table knows it
  never ran; the fix reorders by *what the number means* — a running server's window beats a model's trained
  maximum — instead of adding a per-model map entry.
- **Backend-advertised size (tier 4) ranks above the probe:** it arrives with the request (no network) and is
  tied to the exact model OWUI called, so it has neither the probe's cost nor its identity ambiguity.
- **Fields:** `meta.n_ctx` + `max_model_len` only. Rejected: `context_length` (OpenRouter lists the
  provider's model maximum there, not a served setting — its meaning is too loose to outrank the tables).
- **Workspace models read `__request__.app.state.MODELS`** (OWUI internals, guarded). Accepted risk: the
  attribute may move in a future OWUI; the failure mode is falling back to the tables, never an error.
- **The probe is split by match, not moved wholesale:** `/props` and a single llama-swap row describe *a*
  loaded model, not necessarily the called one. Moving an unmatched result above the tables would stamp a
  local window on cloud models (GPT-4o showing 16k). Unmatched rows keep their pre-2.6.0 rank (last), so an
  alias setup that worked before still works.
- **Probe id matching tightened** (exact / `prefix.id` / `org/id`, no substring): once a match outranks the
  tables, a substring match (`qwen` in `qwen3-max`) becomes a wrong-number bug instead of a harmless fallback.
- **Superseded by issue #14 (same release):** "no auto-discovery of the backend URL from the OWUI connection"
  (`urlIdx`) — it would have silently added network calls to every configured provider. That reason is removed
  by detecting the backend from the listing row; see below.
- **Not fixed here:** the screenshot's `211ms · 23.7 t/s` next to 645 output tokens (5 tokens / 0.211 s =
  23.7 t/s) suggests the llama.cpp timings cover only the last round or chunk — OWUI's `merge_usage` keeps
  the last `predicted_ms`/`predicted_per_second` while summing tokens. Needs a debug payload to confirm.

### Design decisions (v2.6.0, issue #14)

- **Root cause:** llama.cpp's `--fit` (default on) picks the window at load time, so neither llama-swap's
  `/running` command (`--ctx-size` absent) nor its listing (`capabilities.context`, a config number) carries it;
  only the running llama-server knows, and llama-swap reaches it only through the model-dispatched
  `/props?model=` (a bare `/props` is refused). The reporter's patch (`/props?model=` on the valve URL) was the
  right endpoint but broke on an OWUI `prefix_id` and still needed the API key in a valve.
- **URL and key come from the OWUI connection** (owner's choice: reuse, don't duplicate). The URL valves stay
  as a manual fallback for backends not reached through a connection. Accepted risk: `get_openai_connection` /
  `app.state.config` are OWUI internals; failure only means falling back to the next tier.
- **On by default, but only for detected llama-swap / llama.cpp-router rows** (owner's choice). Detection from
  `owned_by` means cloud connections are never contacted — the privacy reason behind the issue #6 "no
  auto-discovery" decision does not apply. Rejected: an opt-in toggle (the reporter would have had to find it).
  Known gap: a connection with hand-typed `model_ids` has no listing row (`openai: {id}` only) → not detected.
- **Live beats listed** (owner's choice): tier 4 sits above tier 5, so a live `/props` answer wins over
  llama-swap's `capabilities.context`. A plain llama-server is not probed — its listing already is live.
- **Never trigger a load.** A model-dispatched request makes llama-swap start (and evict for) a model that is
  not running, and a llama.cpp router autoloads by default. llama-swap is asked `/props?model=` only when
  `/running` shows the model `ready`; the router is asked with `autoload=false`. Accepted residual race: a swap
  between the two requests (milliseconds, right after the model answered).
- **Key handling:** bearer only, and only for `auth_type` empty/`bearer` — session/OAuth/Entra tokens are
  per-user and are not reused; custom connection `headers` are not replayed. The URL and key never enter the
  debug payload; `model.live_backend` shows only the detected kind and listed id.
- **Cache hits only** (`connection:{urlIdx}:{id}`): a miss (model swapped out, auth refused) is asked again on
  the next response instead of pinning a wrong fallback for `context_probe_ttl`. Keyed by connection index so
  the same id on two connections cannot share an entry.
- **Out of scope:** Ollama connections (they send `num_ctx`, tier 2), LM Studio, and `/props?model=` for the
  `llamacpp_url` valve on a router (its `/v1/models` already lists a loaded model's `meta.n_ctx`).

### Cost — `_resolve_cost` (`:1429-1494`)

- `off` — nothing computed, fetched or shown.
- `auto` (default) — **only** `_native_cost(usage)`: the provider/proxy-reported cost that survives
  OWUI's `USAGE_COST_KEYS` merge (OpenRouter, LiteLLM). Never estimates, never touches the network.
- `estimate` — native first, else `_estimate_cost(tokens, price)` marked `≈`.
- **Cumulative** (`💰Σ`, `_cumulative_cost`): recomputed each turn by summing per-message `usage` over
  `body["messages"]` — deliberately NOT a module-level accumulator (survives restarts, follows the
  active branch on regenerate/edit). Suppressed when equal to the message cost. Caveat: historical
  messages without native cost are estimated at the *current* model's price (history carries no
  per-message model), hence the `≈`.
- Cost math honors cache semantics: OpenAI-style cache is a **subset** of input
  (`(input−cached)·in + cached·cache_read`), Anthropic-native cache is **additive on top**
  (`input·in + cache_read·rate + cache_write·rate`). See `_cache_and_fresh` below.

### Price — `_resolve_price` (`:1617-1635`)

`price_map` valve → live models.dev `api.json` (per-provider prices) → `_STATIC_PRICES`. The
`llama` family is deliberately absent from `_STATIC_PRICES`: Meta's own API is free but the same
ids are paid on Groq/Together/Fireworks — hardcoding $0 would lie for paid hosts.

### Model identity — `_resolve_model_id` (`:387-407`)

`model["info"]["base_model_id"]` → `model["id"]` → `body["model"]`. Workspace/"agent" models thus
resolve context and price via the real underlying LLM, so one map entry covers every agent on it.

### Matching semantics

`_longest_key_match` (`:357-371`): case-insensitive substring, longest key wins (`gpt-4o` beats
`gpt-4`) — used for all static tables and user maps. `_modelsdev_match` (`:374-384`): exact id →
bare last path segment (`openai/gpt-4o` → `gpt-4o`) → substring — used only for the two live
models.dev tables. Classic failure: `claude-opus-4.8` (dot) does not substring-match key
`claude-opus-4-8` (dash); `matched_key: null` in the debug payload is the primary signal for this.

## Token extraction — `_extract_tokens` (`:1013-1072`)

- Key priority: `input_tokens` → `prompt_tokens` → `prompt_eval_count` (Ollama) → `prompt_n`
  (llama.cpp); analogous for output. Reasoning: `completion_tokens_details.reasoning_tokens` (Chat
  Completions) → `output_tokens_details.reasoning_tokens` (Responses API). Audio: sum of the
  in/out `audio_tokens` detail keys.
- **Cache — `_cache_and_fresh` (`:1103-1142`), the subtlest logic.** `native_anthropic` = top-level
  `cache_read_input_tokens`/`cache_creation_input_tokens` present AND no `*_tokens_details` cache
  keys → cache is *on top* of reported input. Otherwise (OpenAI, or a proxy that folded Anthropic
  cache into details) cache is a *subset*: `fresh_input = max(input − cached − cache_write, 0)`.
  Getting this wrong double-counts behind LiteLLM (fixed in v2.2.0).
- `_compute_total` (`:1144-1158`): `fresh_input + cached + cache_write + output`.
- **Running chat total — `_cumulative_tokens`** (v2.6.0, called at the end of `_extract_tokens`):
  fills `tokens["cumulative"]` / `tokens["cumulative_est"]`, rendered as `🧮` by
  `_render_tokens_total`. Past assistant turns: `_compute_total(_usage_token_bag(usage))`, i.e. the
  same cache-aware `Σ` each of them displayed; the current turn: the already-resolved `total` (so a
  tiktoken-estimated `Σ` counts as shown and sets `cumulative_est` → `≈`). Past turns without usage
  (and a current turn with no `total`) are counted in `messages_skipped_no_usage`, never re-estimated,
  so `messages_counted + messages_skipped_no_usage` equals the number of assistant messages. Also re-run by `_emit_debug` as
  `cumulative_tokens_debug`, so the debug provenance cannot drift from the displayed number.
- **tiktoken fallback** (`:1066-1101`): only when no API-reported tokens AND
  `fallback_to_tiktoken` AND tiktoken importable. `_message_text` (`:291-306`) prefers `content`,
  falls through to walking the structured `output` array (`_extract_output_text:268-288` — only
  `type=="message"` items; `reasoning` items excluded so they are not double-counted). Input is
  estimated over all non-assistant messages (or just the last user message).
- **Timing — `_resolve_timing` (`:1198+`)**: provider-reported wins (Ollama
  `eval_duration`/`response_token/s`, llama.cpp `predicted_ms`/`predicted_per_second`), else
  wall-clock marked `~`. Wall-clock includes OWUI-side RAG/web-search time — a platform limitation
  (no hook right before the LLM call).

### Chat token total — design decisions (v2.6.0)

Ported from the idea in fork `ArtyCooL/openwebui-token-usage-display@f73c618` and redesigned:

- **What is summed: the per-response `Σ` (cache included), not fresh-only input or output only.**
  The number is then verifiable by hand against the lines already on screen, and it matches what
  the provider processed. Rejected: excluding cache reads (no longer equals the sum of visible `Σ`,
  confusing); output only (says nothing about load/context).
- **It measures processed tokens, not chat size.** Every turn re-reads the history, so it grows
  roughly quadratically; the valve description says so to avoid confusion with `📐`.
- **On by default**, like `show_cumulative_cost` — the owner chose discoverability over an
  unchanged line (accepted risk: the `line-clamp-1` status line gets one item longer after the
  update).
- **Deduped against the message `Σ`**, like `💰Σ` against `💰`: no repeated number on the first turn
  or in unsaved chats.
- **Placed right after `total`** so all token counters stay grouped. Saved custom orders get it at
  the end (standard `display_order` semantics), noted in the CHANGELOG.
- **Key `tokens_total`, valve `show_cumulative_tokens`, bag keys `cumulative`/`cumulative_est`** —
  mirrors `cost_total` / `show_cumulative_cost` / `cost["cumulative"]`.
- **No re-estimation of history without usage**: unsaved chats carry no history usage at all, and
  re-tokenizing the whole history on every response would be quadratic.
- **The current turn comes from the resolved bag, not from `usage`** — the fork read only
  persisted usage, so a tiktoken-estimated current turn silently fell out of the sum.
- **Computed unconditionally** (not gated on the valve, unlike cumulative cost, whose gate exists to
  avoid a price fetch): it is a pure O(n) walk and the debug block needs it regardless.

## Module state

| Name | TTL | Purpose |
|---|---|---|
| `_request_timings` | self-cleaning | inlet→outlet wall clock; used entry popped, stale (>600s) purged on every call (`:1186-1194`) |
| `_ctx_size_cache` | `max(60, context_probe_ttl)` | resolved sizes for the modelsdev/static/probe tiers only |
| `_modelsdev_cache` | `modelsdev_ttl` (24h); `min(300, ttl)` on failure | live context table |
| `_modelsdev_prices_cache` | same | live price table |

`_STATIC_CONTEXT_SIZES` (`:85-167`) and `_STATIC_PRICES` (`:178-224`) are hand-maintained offline
seed tables (seeded from models.dev, 2026-07) — **they go stale and need periodic refreshing**
against models.dev. All three aiohttp fetchers catch broad `Exception` and degrade to `{}`/`None`.

## Debug payload — `_emit_debug` (`:1903+`)

- Ships as a `citation` event (copyable Markdown modal, persisted with the message) + stdout
  mirror, because the status line is `line-clamp-1` plain text inside a toggle `<button>` and
  `content` is invisible when `output` is non-empty (see `docs/owui-map.md`, frontend constraints).
- **Provenance-first**: `source`/`matched_key`/rates come from the real resolvers
  (`_resolve_price`, `_context_size_for` return provenance), so debug can never drift from the
  actual logic. `matched_key: null` = table-matching miss, the #1 support diagnosis.
- **Sanitization is a whitelist, not a blacklist** (`_sanitize_model:1784-1823`): `__metadata__`
  carries the raw prompt (`user_message`), `user_id`, `session_id`, `chat_id` — raw dicts are
  never included. `llamacpp_url`/`llama_swap_url` are masked `******` in the valves snapshot.
- Includes a `web_search` hint (server-side web citations detected via http(s)-host URL check) —
  flags that a provider's search surcharge may be billed outside `usage.cost`.

## Edge-case catalog

Every guard, with its trigger, handling and covering test (`tests/test_usage_display.py`).

| Trigger | Handling | Code | Test |
|---|---|---|---|
| `UserValves.enabled=False` | outlet no-op | `:940-942` | `test_outlet_skips_when_user_disabled` |
| any of the 7 background tasks | early return | `:945-955` | `test_outlet_skips_every_background_task_string` |
| empty `messages` / no assistant msg | early return | `:957-962` | `test_outlet_no_messages_or_no_assistant` |
| `usage` `None`/`{}`/non-numeric/negative | all-`None` bag, no crash | `:1036`, `:230-247` | `test_outlet_survives_malformed_usage` |
| `NaN`/`±inf` in current or past `usage` (v2.6.0) | rejected by `_num` → field omitted, no `int()` crash | `_num` | `test_outlet_survives_non_finite_usage_in_history_and_current`, `test_num_rejects_bool_and_nonnumbers` |
| `Infinity` value in `context_size_map` | entry skipped (`OverflowError` caught) | `_context_size_map_table` | `test_context_size_map_table_skips_infinite_entry` |
| streaming: no `content`, text in `output` | content-first, fall through to output walk | `:291-306` | `test_message_text_prefers_content_then_output` |
| `output` malformed (non-list, non-dict items, non-str text) | items skipped / coerced | `:268-288` | `test_extract_output_text_*` (4 tests) |
| `reasoning` items in `output` | excluded from token estimate | `:278` | `test_extract_output_text_only_message_output_text` |
| tiktoken missing | fallback silently off | `:42-48, :309-312, :1067` | `test_count_tokens_tiktoken_unavailable` |
| unknown model for tiktoken | `cl100k_base` fallback | `:309-317` | (group A tiktoken tests) |
| aiohttp missing | all 3 fetchers short-circuit | `:50-57, :1317, :1345, :1658` | `test_*_aiohttp_unavailable_*` (3 tests) |
| models.dev fetch fails (ctx/price) | `{}` + short retry TTL `min(300, ttl)` | `:1309-1338, :1650-1671` | `test_modelsdev_*_network_error_*` |
| probe: llama-swap fails | falls back to llama.cpp `/v1/models` → `/props`; outer except → `None` | `_probe_context` | `test_probe_context_*` |
| `_resolve_context`/`_resolve_cost` raise | sentinel dicts, response survives | `:971-980` | `test_outlet_survives_network_resolver_exception` |
| malformed JSON in `context_size_map`/`price_map` | `{}`; bad entries dropped per-entry | `:1393-1416, :1637-1648` | `test_context_size_map_table_parsing`, `test_price_map_table_*` |
| map entry value `0` | skipped, falls through to auto sources | `:1274-1275` | `test_context_size_map_zero_value_falls_through` |
| user map vs live models.dev both match | user map wins (own tier, v2.5.1) | `:1268-1275` | `test_context_size_map_beats_modelsdev` |
| `num_ctx` non-positive / non-int | rejected | `:1265-1267` | `test_context_num_ctx_hint_rejected_when_not_positive` |
| workspace model | tables keyed on `base_model_id` | `:387-407` | `test_context_workspace_model_matches_via_base_model_id` |
| no `__model__` at all | falls back to `body["model"]` | `:405-406` | `test_outlet_falls_back_to_body_model_when_no_model_dict` |
| no ids for timing key | `fallback:{id(body)}` key | `:903` | `test_inlet_no_metadata_falls_back_to_id_keyed_entry` |
| module dict lost (restart) | key rebuilt from `chat_id:message_id`; popped after use | `:1181-1190` | `test_resolve_wall_clock_reconstructs_key_from_chat_and_message_id` |
| stale timing entries | purged >600s on every call | `:1191-1194` | `test_resolve_wall_clock_purges_stale_entries` |
| `wall_seconds == 0.0` | no div-by-zero (falsy guard) | `:1225-1226` | `test_resolve_timing_zero_wall_seconds_no_divide_by_zero` |
| provider-reported tps present | beats computed `output/seconds` | `:1214-1221` | `test_resolve_timing_ollama_reported_tps` |
| proxy folds Anthropic cache into details | treated as subset (no double count) | `:1127-1134` | `test_cache_and_fresh_openai_subset` / `_anthropic_native_on_top` |
| `auto` mode, no native cost key | never estimates/fetches | `:1442-1446` | `test_resolve_cost_auto_no_native_key_never_estimates` |
| zero-value token bag | cost `None` | `:1583-1584` | `test_cost_components_zero_token_bag_returns_none` |
| price missing `input`+`output` rates | cost `None` | `:1561-1564` | `test_cost_components_none_price_or_no_rates` |
| cache rates missing | default to `input` rate | `:1567-1572` | `test_cost_components_cache_defaults_to_input_rate` |
| cost below `cost_min_display` | hidden | `:681, :698` | `test_render_cost_estimate_marker_and_threshold` |
| chat token total == message `Σ` | `🧮` suppressed | `_render_tokens_total` | `test_render_tokens_total_dedupes_and_marks_estimate` |
| past turns without/with malformed usage (unsaved chats) | skipped + counted in debug | `_cumulative_tokens` | `test_cumulative_tokens_skips_history_without_usage` |
| current turn tiktoken-estimated | included, `🧮 ≈` | `_cumulative_tokens` | `test_cumulative_tokens_estimated_current_turn_and_empty`, `test_outlet_chat_token_total_marks_estimated_current_turn` |
| cumulative == message cost | `💰Σ` suppressed | `:695-699` | `test_render_cost_total_dedupes_when_equal_to_message` |
| mixed native/estimated history | summed; `≈` if any estimated | `:1459-1479` | `test_resolve_cost_cumulative_mixes_native_and_estimated_messages` |
| malformed models.dev `api.json` | tolerant per-entry parse | `:1673-1704` | `test_parse_prices_malformed_input_yields_empty_map` |
| malformed llama-swap `/running` | regex miss → `None` | `:1376-1391` | `test_parse_llama_swap_malformed_input_returns_none` |
| several llama-swap models running (v2.6.0) | row matched to the called model id; no match → `None` → llama.cpp | `_parse_llama_swap` | `test_parse_llama_swap_picks_the_called_model_among_several`, `test_probe_context_llama_swap_unmatched_model_falls_back_to_llamacpp` |
| one llama-swap model, id differs (alias) | that row is used, flagged unmatched → ranked after the tables | `_parse_llama_swap` | `test_parse_llama_swap_single_row_is_used_despite_id_mismatch` |
| backend lists `meta.n_ctx` / `max_model_len` (v2.6.0) | beats models.dev/static/probe; uncached | `_backend_context_size` | `test_context_backend_n_ctx_beats_static_table`, `test_context_backend_vllm_max_model_len` |
| backend lists `n_ctx: 0` (llama.cpp router, unloaded) or malformed fields | ignored → next tier | `_advertised_context` | `test_context_backend_ignores_non_positive_and_malformed` |
| workspace model on a llama.cpp base | base row read from `__request__.app.state.MODELS` | `_backend_context_size` | `test_context_workspace_model_reads_backend_row_of_its_base_model`, `test_outlet_passes_request_for_workspace_backend_context` |
| model registry missing/raising | guarded → tables | `_backend_context_size` | `test_context_workspace_model_registry_failure_falls_through` |
| llama-swap model started with `--fit` (no `--ctx-size`) (v2.6.0) | `/props?model=` of the `ready` row | `_probe_llama_swap` | `test_probe_llama_swap_reads_fit_window_from_props_of_ready_model` |
| llama-swap row not `ready` | `/props` never asked (it would load the model); `--ctx-size` only | `_llama_swap_ready_id` | `test_probe_llama_swap_never_asks_props_for_a_model_that_is_not_ready`, `test_llama_swap_ready_id_needs_a_ready_matched_row` |
| llama-swap upstream without `/props` (vLLM) / `/props` error | falls back to `--ctx-size` | `_probe_llama_swap` | `test_probe_llama_swap_props_failure_falls_back_to_ctx_size` |
| model served by llama-swap via an OWUI connection (issue #14) | connection URL/key/prefix reused; live beats listed; hit cached, user map still wins | `_connection_context_size` | `test_connection_probe_llama_swap_uses_owui_url_key_and_prefix` |
| connection probe miss (model swapped out) | next tier; not cached | `_connection_context_size` | `test_connection_probe_miss_falls_through_to_listing_and_is_not_cached` |
| llama.cpp router, model not loaded | `autoload=false` refusal → next tier | `_ask_live_backend` | `test_connection_probe_llamacpp_router_never_autoloads` |
| workspace model / OWUI 0.9.x config / non-bearer auth | base entry; `app.state.config`; no key sent | `_owui_connection` | `test_connection_probe_workspace_model_on_owui_09_config_without_bearer` |
| connection lookup raises/junk, cloud row, bool `urlIdx`, aiohttp missing | no request at all / next tier | `_live_backend`, `_connection_context_size` | `test_connection_probe_degrades_silently`, `test_live_backend_detects_only_llama_swap_and_llamacpp_router` |
| probe row matched vs unmatched | matched outranks the tables, unmatched ranks after them | `_automatic_context_size` | `test_context_matched_probe_beats_tables_unmatched_loses` |
| short local id vs longer cloud id (`qwen` / `qwen3-max`) | no match (no substring) | `_match_running` | `test_parse_llama_swap_no_loose_substring_match` |
| llama.cpp `/v1/models` lacks `meta.n_ctx` (pre-May-2026) | `/props`, matched by alias / GGUF name | `_probe_llamacpp` | `test_probe_context_llamacpp_old_build_matches_via_props`, `test_parse_llamacpp_props_names_the_loaded_model` |
| llama.cpp `/v1/models` or `/props` down | the other endpoint's result is kept | `_probe_llamacpp` | `test_probe_context_llamacpp_unmatched_*` |
| `display_order` unknown/dup/empty | ignored+debug / dedup / default | `:432-458` | `test_normalize_order_dedups_and_flags_unknown` |
| reasoning/cached/audio == 0 | hidden (truthiness); context `used=0` still renders | `:597-636` | `test_render_reasoning_hidden_when_zero_or_none`, `test_render_context` |
| only `source` parts rendered | whole line suppressed | `:1734-1735` | (build_stats tests) |
| `valves.model_dump()` raises | `{}` snapshot | `:1825-1830` | `test_valves_snapshot_returns_empty_dict_on_dump_failure` |
| debug `json.dumps` raises | error-string fallback | `:1940-1943` | `test_emit_debug_json_dumps_failure_falls_back_to_error_string` |
| secret-bearing model/metadata dicts | whitelist copy-out only | `:1784-1823` | `test_sanitize_model_whitelist_only_no_secrets` |
| RAG/file sources in web-search detect | non-http sources excluded | `:1836-1878` | `test_web_search_debug_ignores_non_web_sources` |

Not handled by design: non-persistent chats (`temporary:` at 0.11.x, legacy `local:`, and
`channel:`) are not branched on — the plugin works purely on what inlet/outlet receive, persistence
differences don't affect it.

**Not detectable from inside the plugin's own logic — the #1 "it runs but nothing shows" cause.**
OWUI's frontend gates the whole status line on the model's **Status Updates** capability and the
debug citation on **Citations** (`ResponseMessage.svelte:683,882`). With either off, the plugin
emits normally, the backend persists normally, and the user sees nothing — only the stdout
`[TUD debug]` mirror survives. Workspace/preset models are the ones affected (they store an
explicit `info.meta.capabilities`; plain connection models have no `info` and default to on).
`__model__["info"]["meta"]["capabilities"]` **is** readable in outlet, so the debug payload could
surface this directly — not implemented yet, see `docs/owui-map.md` → frontend constraints.
