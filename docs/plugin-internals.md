# usage_display.py internals (maintainer reference)

How the plugin works end-to-end, why it is designed this way, and the edge-case catalog with the
covering tests. Line numbers refer to plugin v2.5.2 (1959 lines); treat them as anchors, not gospel.

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

The NOTE block at `usage_display.py:14-41` is the source-verified contract with OWUI. Re-verified
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

Any change to token/cost/context reading must be reconciled against this block AND re-verified
against real OWUI source of the version being targeted — never against memory or docs.

## Valves

Grouped inventory (`Valves` at `:755-884`, `UserValves` at `:886-887`). Defaults in parentheses
when non-obvious:

- **Visibility** — `show_input_tokens`, `show_output_tokens`, `show_total_tokens`,
  `show_generation_time`, `show_tokens_per_second`, `show_reasoning_tokens`, `show_cached_tokens`,
  `show_audio_tokens` (False), `show_model_name`, `show_data_source` (False),
  `show_context_window`, `show_cumulative_cost`. Message cost has **no** `show_*` — its visibility
  is `cost_mode` itself.
- **Presentation** — `display_order` (pre-filled full 13-key order), `separator` (`" · "`),
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

`_STATS_RENDERERS` (`:737-751`) maps 13 keys (`input`, `output`, `total`, `reasoning`, `cached`,
`audio`, `context`, `time`, `tps`, `cost`, `cost_total`, `model`, `source`) to `_render_*`
functions sharing a fixed 6-arg signature `(v, tokens, timing, ctx, cost, model)` — the reason
`PLR0913` is suppressed. `_build_stats` (`:1708-1736`) walks the resolved order, drops `None`
results, and suppresses a line consisting solely of `source` parts (`:1734-1735`).

Rules: **`show_*` valves gate visibility; `display_order` only sorts.** `_resolve_display_order`
(`:451-458`) always returns a full 13-key permutation — removed keys move to the end, unknown keys
are ignored (surfaced only in the debug payload). To add a metric: add a `show_*` valve, write a
6-arg `_render_*`, register it in `_STATS_RENDERERS`, append the key to `_DEFAULT_ORDER`, add an
icon to `_ICON_EMOJI`/`_ICON_SIMPLE`.

## Resolution chains

### Context size — `_context_size_for` (`:1251-1307`)

1. `context_size_override` valve → 2. `num_ctx` hint from inlet → 3. user `context_size_map` →
4. `_ctx_size_cache` hit → 5. live models.dev (opt-in) → 6. `_STATIC_CONTEXT_SIZES` →
7. llama.cpp/llama-swap probe (opt-in). Tiers 1–3 return **uncached** on purpose (valve edits take
effect immediately); only 5–7 results are cached (`max(60, context_probe_ttl)`). The user map was
promoted above models.dev in v2.5.1 — an explicit user entry must never lose to a live lookup.

### Cost — `_resolve_cost` (`:1429-1494`)

- `off` — nothing computed, fetched or shown.
- `auto` (default) — **only** `_native_cost(usage)`: the provider/proxy-reported cost that survives
  OWUI's `USAGE_COST_KEYS` merge (OpenRouter, LiteLLM). Never estimates, never touches the network.
- `estimate` — native first, else `_estimate_cost(tokens, price)` marked `≈`.
- **Cumulative** (`💰Σ`): recomputed each turn by summing per-message `usage` over
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
- **tiktoken fallback** (`:1066-1101`): only when no API-reported tokens AND
  `fallback_to_tiktoken` AND tiktoken importable. `_message_text` (`:291-306`) prefers `content`,
  falls through to walking the structured `output` array (`_extract_output_text:268-288` — only
  `type=="message"` items; `reasoning` items excluded so they are not double-counted). Input is
  estimated over all non-assistant messages (or just the last user message).
- **Timing — `_resolve_timing` (`:1198+`)**: provider-reported wins (Ollama
  `eval_duration`/`response_token/s`, llama.cpp `predicted_ms`/`predicted_per_second`), else
  wall-clock marked `~`. Wall-clock includes OWUI-side RAG/web-search time — a platform limitation
  (no hook right before the LLM call).

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
| streaming: no `content`, text in `output` | content-first, fall through to output walk | `:291-306` | `test_message_text_prefers_content_then_output` |
| `output` malformed (non-list, non-dict items, non-str text) | items skipped / coerced | `:268-288` | `test_extract_output_text_*` (4 tests) |
| `reasoning` items in `output` | excluded from token estimate | `:278` | `test_extract_output_text_only_message_output_text` |
| tiktoken missing | fallback silently off | `:42-48, :309-312, :1067` | `test_count_tokens_tiktoken_unavailable` |
| unknown model for tiktoken | `cl100k_base` fallback | `:309-317` | (group A tiktoken tests) |
| aiohttp missing | all 3 fetchers short-circuit | `:50-57, :1317, :1345, :1658` | `test_*_aiohttp_unavailable_*` (3 tests) |
| models.dev fetch fails (ctx/price) | `{}` + short retry TTL `min(300, ttl)` | `:1309-1338, :1650-1671` | `test_modelsdev_*_network_error_*` |
| probe: llama-swap fails | falls back to llama.cpp `/props`; outer except → `None` | `:1340-1374` | `test_probe_context_*` (3 tests) |
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
| cumulative == message cost | `💰Σ` suppressed | `:695-699` | `test_render_cost_total_dedupes_when_equal_to_message` |
| mixed native/estimated history | summed; `≈` if any estimated | `:1459-1479` | `test_resolve_cost_cumulative_mixes_native_and_estimated_messages` |
| malformed models.dev `api.json` | tolerant per-entry parse | `:1673-1704` | `test_parse_prices_malformed_input_yields_empty_map` |
| malformed llama-swap `/running` | regex miss → `None` | `:1376-1391` | `test_parse_llama_swap_malformed_input_returns_none` |
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
