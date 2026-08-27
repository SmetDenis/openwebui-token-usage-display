# Open WebUI source map (maintainer reference)

A navigation map of the Open WebUI codebase covering everything this plugin depends on: the filter
machinery, the request lifecycle, usage normalization, persistence, events, and the frontend pieces
that constrain the plugin's UX. Paths are relative to the Open WebUI repo root.

**Verified against Open WebUI `v0.11.0`** (commit `01f4282f1`) plus a diff check of `v0.11.0...v0.11.1`
(released 2026-08-25): none of the mechanisms below changed in `v0.11.1` — its backend diff touches
Socket.IO Redis locks/heartbeats, model tag normalization and error constants only. OWUI internals
shift between versions — before relying on any claim, re-check it against a current checkout (the
local clone path and freshness-check commands live in `CLAUDE.local.md`).

## What changed since v0.10.2 (plugin-relevant)

The five things a maintainer must know before touching token/context/UX code:

1. **`content` is no longer empty in the outlet body.** OWUI now synthesizes it from `output`:
   `middleware.py:3479` builds every outlet message as `m.get('content') or get_output_text(m.get('output'))`.
   The **DB** still stores `output` only — the synthesis happens when the outlet body is assembled.
2. **Temporary chats gained a new prefix.** `local:` is now legacy; the current one is `temporary:`,
   and `channel:` chats are non-persistent too (`utils/chat_id.py`). Prefix checks must use
   `is_saved_chat_id()` semantics, not a bare `startswith('local:')`.
3. **`TASKS` grew two members** — `image_prompt_generation` and `function_calling`
   (`constants.py:129-142`), for a total of nine.
4. **OWUI ships its own context-usage indicator and context compaction**
   (`utils/context_compaction.py`, `Chat.svelte:243,270-305`) — a native feature overlapping this
   plugin's context-window metric, plus a new `context_compaction` event type and a `contextSummary`
   message field that truncates the effective history.
5. **`pip install` of frontmatter `requirements:` is now gated** by
   `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` (default `True`, `env.py:1114`) and `OFFLINE_MODE`.
   The default is still "install", so this plugin's no-`requirements:` rule stands unchanged.

## Repo layout

- `backend/open_webui/main.py` (2878 lines) — FastAPI app; top-level routes (`/api/chat/completions`,
  `/api/chat/completed`, `/api/tasks/*`); builds the request `metadata` dict.
- `backend/open_webui/utils/` — cross-cutting logic: `middleware.py` (~5670 lines, the request
  lifecycle brain), `filter.py`, `plugin.py`, `chat.py`, `chat_id.py`, `models.py`, `payload.py`,
  `response.py`, `context_compaction.py`.
- `backend/open_webui/routers/` — one file per REST resource: `functions.py`, `models.py`,
  `openai.py`, `ollama.py`, `chats.py`, `tasks.py`, `analytics.py`, ...
- `backend/open_webui/models/` — DB layer: `functions.py`, `chats.py` (legacy JSON blob),
  `chat_messages.py` (normalized message table, dual-written), `models.py`.
- `backend/open_webui/socket/main.py` — Socket.IO server, event emitter/caller.
- `src/lib/components/chat/` — chat UI: `Chat.svelte` (frontend mirror of middleware),
  `Messages/ResponseMessage.svelte` and friends.
- `src/lib/apis/` — thin fetch wrappers per backend resource.

## Filter-plugin machinery

- `utils/plugin.py` — `extract_frontmatter:151` parses the docstring frontmatter, strictly one
  `key: value` per line (regex `^\s*([a-z_]+):\s*(.*)\s*$` — this is why frontmatter lines must
  never be wrapped). `load_function_module_by_id:259` `exec()`s the source into a fresh module;
  a `Filter` class makes it type `filter`; a load error force-deactivates the function (`:312`).
  `install_frontmatter_requirements:422` pip-installs anything in a `requirements:` frontmatter
  line — the reason this plugin must never declare one; it is now skipped when
  `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=false` or `OFFLINE_MODE` is set, and it only runs on
  the code path where `content` is passed in explicitly (save/preview), not on a DB reload.
  `get_function_module_from_cache:375` caches modules per app state; the `stream` hook uses
  cache-only loads for performance. **The whole module is inert when `ENABLE_PLUGINS=false`** —
  `load_function_module_by_id` raises and `process_filter_functions` returns the body untouched.
- `utils/filter.py` — restructured in 0.11 around `resolve_filter_pipeline:56`, which merges global
  filters + the model's `info.meta.filterIds` (`get_model_filter_ids:47`), keeps only filters that
  are active (a `toggle` filter must additionally be enabled for the request), reads
  `Valves.priority` and sorts by `(priority, filter_id)`. `get_sorted_filter_ids:97` and
  `get_filter_functions:103` are thin wrappers over it.
  `process_filter_functions:197` → `process_filter_function:153` dispatches `inlet`/`outlet`/`stream`:
  instantiates `Valves` from the DB (`apply_filter_valves:108`), injects `__user__["valves"]` from
  `UserValves` (`apply_user_valves:135`), and passes **only the dunder kwargs the handler's
  signature declares** (`get_filter_params:123`, `inspect.signature`). Each filter's return value
  becomes the next filter's input. `FilterContext:11` is a new per-request memo for valve
  instantiation (passed as `None` from the chat paths, so valves are still built per filter call).
- `models/functions.py` — `Function` table: `valves` is JSON, encrypted (`utils/valves.py`);
  per-user valves live inside `User.settings.functions.valves[id]`, not a separate table
  (`get_user_valves_by_id_and_user_id:354`).
- `routers/functions.py` — admin API: `POST /id/{id}/toggle:287` (active),
  `POST /id/{id}/toggle/global:341` (run-on-every-chat). Per-model attachment is stored on the
  *model* (`info.meta.filterIds`), not on the function.
- `required_open_webui_version` is enforced **only client-side** at save time
  (`src/routes/(app)/admin/functions/create/+page.svelte:23` and `edit/+page.svelte:24`); the
  backend never checks it.

## Dunder kwargs available to filter hooks

A handler receives only the subset its signature names. Built at `middleware.py:2401-2411` (inlet),
`:3505-3512` (outlet), `:3766-3774` (stream).

| kwarg | inlet | outlet | stream | notes |
|---|---|---|---|---|
| `__event_emitter__`, `__event_call__`, `__user__`, `__metadata__`, `__request__`, `__model__` | yes | yes | yes | `__metadata__` and `body["metadata"]` are the same dict object |
| `__oauth_token__` | yes | no | yes | |
| `__chat_id__`, `__message_id__` | yes | no | no | duplicated inside `__metadata__` anyway |
| `__id__` | yes | yes | yes | the filter's own function id (`filter.py:123-133`) |
| `__body__` | no | no | yes | full form_data alongside the chunk (`middleware.py:4218`) |

**`__model__` caveats:** raw (non-customized) models have **no `info` key** at all
(`utils/models.py:34-75`) — always `.get("info") or {}`. `info.params` (temperature, num_ctx, ...)
is **stripped before filters see the model** (`utils/models.py:180-181, 221-223`, "Remove params to
avoid exposing sensitive info") — generation params and base URLs are unavailable in outlet.
What *is* visible on a workspace/preset model is `info.meta`, including
**`info.meta.capabilities`** — the frontend render gates (see below) can therefore be read from
inside the plugin.

**`__metadata__` is sensitive:** it carries `user_message` (the raw prompt), `user_id`,
`user_agent`, `session_id`, `chat_id`, `variables`, `chat_variables`, `files`, and the full `model`
dict (`main.py:1180-1209`). Never dump it raw into anything user-shareable — whitelist fields
instead.

## Request lifecycle

1. Frontend `Chat.svelte:2548`-ish → `POST /api/chat/completions`. `stream_options.include_usage` is
   sent **only when the model's `capabilities.usage` flag is on** (`Chat.svelte:3185-3189`) — the
   root cause of most "no tokens shown in streaming" reports.
2. `main.py:chat_completion:1054` builds `metadata` (`:1180-1209`), resolves arena/custom models,
   generates ids and sets `metadata["message_id"]` (`:1784`); `process_chat(...)` then runs the
   pipeline.
3. `middleware.py:process_chat_payload:2248` — `apply_params_to_form_data:1947` runs **before**
   inlet filters and, for Ollama-owned models, moves the whole params dict into
   `form_data["options"]` (`:1980-1982`), `num_ctx` included. Inlet filters run at `:2513-2525`
   (guarded by `ENABLE_PLUGINS`). `features` is popped into `extra_params["__features__"]` only
   *after* inlet (`:2528-2529`).
4. Provider routers substitute `base_model_id` right before the upstream call
   (`routers/openai.py`, `routers/ollama.py` and siblings) and apply model params. Ollama usage is
   converted to OpenAI shape by `utils/response.py:convert_ollama_usage_to_openai:168`
   (`eval_count` → tokens, duration fields, `response_token/s`).
5. **Outlet filters run inline** in `middleware.py:outlet_filter_handler:3412`: for a saved chat it
   reloads the message map from the DB, builds the outlet body
   (`{model, messages, chat_id, session_id, id}`, messages carrying `content` — real **or
   synthesized from `output`** — plus `info`, `output`, `usage`, `sources`, `:3473-3489`), runs
   `process_filter_functions(filter_type="outlet")`, persists any message whose content/output the
   filter actually changed (`:3534-3559`, also writing `originalContent`) and emits `chat:outlet`.
   Called from both the non-streaming handler (`:3703`) and end-of-stream (`:5551`); the raw-API
   path is gated by `ENABLE_API_OUTLET_FILTERS` (`:3735`, default on).
   `POST /api/chat/completed` still exists (`main.py:1976` → `utils/chat.py:chat_completed:313`,
   which runs outlet filters with a minimal `__metadata__` carrying no `task`) but is
   **deprecated** — the frontend no longer calls it.
6. **Task requests never reach filter functions.** Title/tags/follow-up/image-prompt/etc. generation
   (`routers/tasks.py`, marks `metadata["task"]`) calls `generate_chat_completion()` directly
   (`utils/chat.py:151`), which runs no Function-filter inlet/outlet at all. The plugin's task
   guard is defensive belt-and-suspenders. Full enum (`constants.py:129-142`): `title_generation`,
   `follow_up_generation`, `tags_generation`, `emoji_generation`, `query_generation`,
   `image_prompt_generation`, `autocomplete_generation`, `function_calling`,
   `moa_response_generation`.
7. **Usage capture:** streaming accumulates `merge_usage(...)` per chunk (Responses API and Chat
   Completions paths in `middleware.py:4100-4300`, where llama.cpp `timings` are folded into
   usage); non-streaming normalizes once. The final `usage` rides the `chat:completion` "done"
   event and is persisted with the message (`:5505-5545`).
8. **Structured `output` arrays** are built by `handle_responses_streaming_event:478` (Responses
   API), inline tag handlers (reasoning/solution tags), or synthesized from `choices[0].message`
   for non-streaming. `utils/misc.py:get_output_text:183` is the canonical output→text converter
   (**only `type == "message"` items**, their `content[].text` joined); `convert_output_to_messages:257`
   rebuilds full messages.

## Usage normalization (`utils/response.py`)

- `normalize_usage:14` — always derives `input_tokens`/`output_tokens`/`total_tokens` (from
  `prompt_tokens`/`completion_tokens`, Ollama `prompt_eval_count`/`eval_count`, llama.cpp
  `prompt_n`/`predicted_n`), **preserving** all original provider keys alongside.
- `merge_usage:105` — multi-round tool turns: sums `USAGE_SUMMABLE_KEYS` = `USAGE_TOKEN_KEYS:55`
  ∪ `USAGE_COST_KEYS:61` (`cost, total_cost, input_cost, output_cost, prompt_cost, completion_cost`
  — how proxy-reported cost survives normalization) and deep-sums the four `USAGE_DETAIL_KEYS:72`
  `*_tokens_details` dicts. Top-level Anthropic cache fields and Ollama duration fields are in none
  of those sets → **last round wins** for cache/timing. This is the OWUI limitation the plugin
  documents rather than fixes.

## Events (`socket/main.py`)

`get_event_emitter:968` needs `user_id` + `chat_id` + `message_id`; emits over Socket.IO and — when
`is_saved_chat_id(chat_id)` — writes DB side effects by `data["type"]`:

- `status` → **appended** to `statusHistory` (`models/chats.py:add_message_status_to_chat_by_id_and_message_id:1065`);
  the UI shows only the latest entry collapsed, but the full history persists and is expandable.
- `source` / `citation` → appended to `message.sources` (`socket/main.py:1073-1092`), **only when
  `data.type` is absent** — the persistence trick the plugin's debug citation relies on.
- `message` / `replace` → append to / overwrite `message.content`.
- Non-persistent chats — `temporary:` (current), `local:` (legacy) and `channel:` — skip all
  persistence (`utils/chat_id.py:is_saved_chat_id`). A `channel:` chat routes the emitter to
  channel-message updates entirely (`_make_channel_emitter`).

`get_event_call:1100` — request/response calls into a live WS session (dialogs, client-side JS); it
now verifies the target session belongs to the requesting user.

## Persistence and the message data model

- `models/chats.py:upsert_message_to_chat_by_id_and_message_id:969` — the single message-write
  path; dual-writes the normalized `chat_message` table (`ChatMessages.upsert_message`, non-fatal).
- `models/chat_messages.py` — `usage` is a **first-class column**.
  `get_messages_map_by_chat_id:331` reconstructs message dicts from columns (renaming via
  `DB_TO_JSON_KEY_MAP:322`) and is the `info` mirror writer (`:370-371`):
  `if 'usage' in msg: msg['info'] = {'usage': msg['usage']}` — `info` and `usage` are the *same
  data*; reading both double-counts. This is why the plugin ignores `info`.
- **`content` is still not persisted at 0.11.x for either mode.** Every save path writes
  `output`/`usage` only (`middleware.py:5513-5535` streaming, `:3685-3693` non-streaming); the
  assistant placeholder starts as `''`. What changed is that the **outlet body** now fills `content`
  in from `output` on the way out (`:3479`), so an outlet filter sees text in `content` even though
  the DB row has none. A content-first reader must still fall through to `output` — it is the only
  source on 0.9.x/0.10.x, and inside `stream`/other paths.
- Aggregate token analytics (independent of this plugin):
  `chat_messages.py:get_token_usage_by_model:555`, exposed via `routers/analytics.py`.

## Context compaction — OWUI's own context metric (new in 0.11)

`utils/context_compaction.py` gives OWUI a native version of this plugin's context-window metric:

- `get_chat_context_usage:237` walks the branch backwards for the newest message carrying `usage`,
  takes `prompt+completion+cache_n` from it, adds a heuristic estimate for everything after it, and
  returns `{tokens, estimated_tokens, threshold, percent, source: 'estimated'}`
  (`_build_context_usage:290`). Exposed on chat reads (`routers/chats.py:1288,1337`).
- The `threshold` is **not** the model's real context window: it is the compaction threshold
  (`compact_token_threshold` param, capped by admin config), so its percentage and this plugin's
  are different numbers by design.
- When the threshold is crossed OWUI summarizes the older half into a `contextSummary` /
  `context_summary` field on a message and emits a `context_compaction` status action, which the
  frontend renders as a toast rather than a status entry (`Chat.svelte:920,973`). Effective history
  is then truncated at that checkpoint — relevant to the plugin's cumulative-cost sum, which walks
  all of `body["messages"]`.
- The whole feature is behind `config.features.enable_context_compaction` (`Chat.svelte:243`).

## Frontend constraints that shaped the plugin

- **Two per-model render gates decide whether the plugin's output is visible at all**
  (`ResponseMessage.svelte:683` and `:882`):
  ```svelte
  {#if model?.info?.meta?.capabilities?.status_updates ?? true}   <StatusHistory ... />
  {#if (message?.sources || …) && (model?.info?.meta?.capabilities?.citations ?? true)}  <Citations ... />
  ```
  With **Status Updates** off the stats line never renders, and with **Citations** off the
  `debug_mode` payload never renders — while the plugin runs normally and the backend persists both.
  Plain connection models have no `info`, so they fall through to the `?? true` default; a
  **workspace/preset model stores an explicit `info.meta.capabilities` object**, so this is a
  preset-only failure mode. Defaults live in `constants.ts:DEFAULT_CAPABILITIES:100-113`
  (`status_updates: true`, `citations: true`), the toggles in
  `workspace/Models/Capabilities.svelte:50`. The gates date back to v0.6.27, not to 0.11.
- `Chat.svelte:chatEventHandler:949` — `status` pushes onto `message.statusHistory` (`:967-972`);
  `chat:completion` sets `message.usage` (`chatCompletionEventHandler:2377`, `:2431-2432`).
- `Messages/ResponseMessage/StatusHistory/StatusItem.svelte` — status text renders plain-text with
  a hardcoded `line-clamp-1` in every branch, inside a `<button>` that toggles the history →
  **multi-line / markdown / conveniently-copyable debug output in the status line is impossible**.
- `ContentRenderer.svelte:282-302` — `output` wins; `content` renders **only when `output` is
  empty**. On 0.10.x/0.11.x responses `output` is non-empty → appending debug text to `content` is
  invisible.
- `Citations.svelte` → `CitationModal.svelte` — sources render as Markdown with a Copy button on
  code blocks (`CodeBlock.svelte:519`) → the reason `debug_mode` ships its payload as a `citation`
  event (copyable, persisted) with stdout as fallback.
- `ResponseMessage.svelte:1182-1186` — OWUI's own built-in usage tooltip (pretty-printed
  `message.usage`) — the native UI this plugin complements.
