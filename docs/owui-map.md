# Open WebUI source map (maintainer reference)

A navigation map of the Open WebUI codebase covering everything this plugin depends on: the filter
machinery, the request lifecycle, usage normalization, persistence, events, and the frontend pieces
that constrain the plugin's UX. Paths are relative to the Open WebUI repo root.

**Verified against Open WebUI `v0.10.2`** (commit `ecd48e2f7`, released 2026-07-01). OWUI internals
shift between versions — before relying on any claim below, re-check it against a current checkout
(the local clone path and freshness-check commands live in `CLAUDE.local.md`).

## Repo layout

- `backend/open_webui/main.py` — FastAPI app; top-level routes (`/api/chat/completions`,
  `/api/chat/completed`, `/api/tasks/*`); builds the request `metadata` dict.
- `backend/open_webui/utils/` — cross-cutting logic: `middleware.py` (~5380 lines, the request
  lifecycle brain), `filter.py`, `plugin.py`, `chat.py`, `models.py`, `payload.py`, `response.py`.
- `backend/open_webui/routers/` — one file per REST resource: `functions.py`, `models.py`,
  `openai.py`, `ollama.py`, `chats.py`, `tasks.py`, `analytics.py`, ...
- `backend/open_webui/models/` — DB layer: `functions.py`, `chats.py` (legacy JSON blob),
  `chat_messages.py` (normalized message table, dual-written), `models.py`.
- `backend/open_webui/socket/main.py` — Socket.IO server, event emitter/caller.
- `src/lib/components/chat/` — chat UI: `Chat.svelte` (frontend mirror of middleware),
  `Messages/ResponseMessage.svelte` and friends.
- `src/lib/apis/` — thin fetch wrappers per backend resource.

## Filter-plugin machinery

- `utils/plugin.py` — `extract_frontmatter:150` parses the docstring frontmatter, strictly one
  `key: value` per line (regex `^\s*([a-z_]+):\s*(.*)\s*$` — this is why frontmatter lines must
  never be wrapped). `load_function_module_by_id:253` `exec()`s the source into a fresh module;
  a `Filter` class makes it type `filter`; a load error force-deactivates the function (line 301).
  `install_frontmatter_requirements:411` pip-installs anything in a `requirements:` frontmatter
  line — the reason this plugin must never declare one. `get_function_module_from_cache:364`
  caches modules per app state; the `stream` hook uses cache-only loads for performance.
- `utils/filter.py` — `get_sorted_filter_ids:21` merges global filters + the model's
  `info.meta.filterIds`, resolves `Valves.priority`, sorts by `(priority, filter_id)`.
  `process_filter_functions:66` dispatches `inlet`/`outlet`/`stream`: instantiates `Valves` from
  the DB, injects `__user__["valves"]` from `UserValves`, and passes **only the dunder kwargs the
  handler's signature declares** (`inspect.signature`, lines 92–105). Each filter's return value
  becomes the next filter's input.
- `models/functions.py` — `Function` table: `valves` is JSON, encrypted (`utils/valves.py`);
  per-user valves live inside `User.settings.functions.valves[id]`, not a separate table (line 346).
- `routers/functions.py` — admin API: `POST /id/{id}/toggle` (active), `POST /id/{id}/toggle/global`
  (run-on-every-chat). Per-model attachment is stored on the *model* (`info.meta.filterIds`), not
  on the function.
- `required_open_webui_version` is enforced **only client-side** at save time
  (`src/routes/(app)/admin/functions/create/+page.svelte:23`); the backend never checks it.

## Dunder kwargs available to filter hooks

A handler receives only the subset its signature names. Built at `middleware.py:2312-2322` (inlet),
`:3364-3372` (outlet), `:3614-3618` (stream).

| kwarg | inlet | outlet | stream | notes |
|---|---|---|---|---|
| `__event_emitter__`, `__event_call__`, `__user__`, `__metadata__`, `__request__`, `__model__` | yes | yes | yes | `__metadata__` and `body["metadata"]` are the same dict object |
| `__oauth_token__` | yes | no | yes | |
| `__chat_id__`, `__message_id__` | yes | no | no | duplicated inside `__metadata__` anyway |
| `__id__` | yes | yes | yes | the filter's own function id (`filter.py:102`) |
| `__body__` | no | no | yes | full form_data alongside the chunk (`middleware.py:4040`) |

**`__model__` caveats:** raw (non-customized) models have **no `info` key** at all
(`utils/models.py:33-51`) — always `.get("info") or {}`. `info.params` (temperature, num_ctx, ...)
is **stripped before filters see the model** (`utils/models.py:163-164, 202-204`, "Remove params to
avoid exposing sensitive info") — generation params and base URLs are unavailable in outlet.

**`__metadata__` is sensitive:** it carries `user_message` (the raw prompt), `user_id`,
`user_agent`, `session_id`, `chat_id`, `variables`, `files` (`main.py:1130-1157`). Never dump it
raw into anything user-shareable — whitelist fields instead.

## Request lifecycle

1. Frontend `Chat.svelte:2548` → `POST /api/chat/completions`. `stream_options.include_usage` is
   sent **only when the model's `capabilities.usage` flag is on** (`Chat.svelte:2604-2610`) — the
   root cause of most "no tokens shown in streaming" reports.
2. `main.py:chat_completion:1009` builds `metadata` (`:1130-1157`), resolves arena/custom models,
   generates ids; `process_chat(...)` (`:1473`) then runs the pipeline.
3. `middleware.py:process_chat_payload:2163` — `apply_params_to_form_data:1872` runs **before**
   inlet filters and, for Ollama-owned models, moves params into `form_data["options"]` (including
   `num_ctx`, `:1903-1905`). Inlet filters run at `:2424-2434`. `features` is popped into
   `extra_params["__features__"]` only *after* inlet (`:2438-2439`).
4. Provider routers substitute `base_model_id` right before the upstream call
   (`routers/openai.py:1134-1139`, `routers/ollama.py:1106-1108` and siblings) and apply model
   params. Ollama usage is converted to OpenAI shape by
   `utils/response.py:convert_ollama_usage_to_openai:156` (`eval_count` → tokens, duration fields).
5. **Outlet filters run inline** in `middleware.py:outlet_filter_handler:3273-3416`: it builds the
   outlet body (`{model, messages, chat_id, session_id, id}`, messages carry per-message `usage`
   and `info`, `:3336-3355`), runs `process_filter_functions(filter_type="outlet")`, persists
   changes and emits `chat:outlet`. Called from both the non-streaming handler (`:3571, :3591`) and
   end-of-stream (`:5278, :5357`); the raw-API path is gated by `ENABLE_API_OUTLET_FILTERS`
   (default on). `POST /api/chat/completed` still exists (`main.py:1732`) but is **deprecated** —
   the frontend no longer calls it.
6. **Task requests never reach filter functions.** Title/tags/follow-up/etc. generation
   (`routers/tasks.py`, marks `metadata["task"]`) calls `generate_chat_completion()` directly
   (`utils/chat.py:152-305`), which runs no Function-filter inlet/outlet at all. The plugin's task
   guard is defensive belt-and-suspenders.
7. **Usage capture:** streaming accumulates `merge_usage(...)` per chunk (`middleware.py:4125`
   Responses API; `:4151-4154` Chat Completions, where llama.cpp `timings` are folded into usage);
   non-streaming normalizes once (`:3535, :3585`). The final `usage` rides the `chat:completion`
   "done" event and is persisted with the message (`:5222-5276`).
8. **Structured `output` arrays** are built by `handle_responses_streaming_event:424` (Responses
   API), inline tag handlers (reasoning/solution tags, `:3637+`), or synthesized from
   `choices[0].message` for non-streaming (`:3488-3521`). `utils/misc.py:convert_output_to_messages:223`
   is the canonical output→text converter.

## Usage normalization (`utils/response.py`)

- `normalize_usage:13` — always derives `input_tokens`/`output_tokens`/`total_tokens` (from
  `prompt_tokens`/`completion_tokens`, Ollama `prompt_eval_count`/`eval_count`, llama.cpp
  `prompt_n`/`predicted_n`), **preserving** all original provider keys alongside.
- `merge_usage:104` — multi-round tool turns: sums `USAGE_TOKEN_KEYS`, `USAGE_COST_KEYS`
  (`cost, total_cost, input_cost, output_cost, prompt_cost, completion_cost` — how proxy-reported
  cost survives normalization) and the four `*_tokens_details` dicts. Top-level Anthropic cache
  fields and Ollama duration fields are in none of those sets → **last round wins** for
  cache/timing. This is the OWUI limitation the plugin documents rather than fixes.

## Events (`socket/main.py`)

`get_event_emitter:919` needs `user_id` + `chat_id` + `message_id`; emits over Socket.IO and — for
persistent chats — writes DB side effects by `data["type"]`:

- `status` → **appended** to `statusHistory` (`models/chats.py:796`); the UI shows only the latest
  entry collapsed, but the full history persists and is expandable.
- `source` / `citation` → appended to `message.sources` (`:1014-1031`), **only when `data.type` is
  absent** — the persistence trick the plugin's debug citation relies on.
- `message` / `replace` → append to / overwrite `message.content`.
- Temporary chats (`chat_id.startswith("local:")` — prefix, not equality) skip all persistence.

`get_event_call:1039` — request/response calls into a live WS session (dialogs, client-side JS).

## Persistence and the message data model

- `models/chats.py:upsert_message_to_chat_by_id_and_message_id:704` — the single message-write
  path; dual-writes the normalized `chat_message` table (`ChatMessages.upsert_message`, non-fatal).
- `models/chat_messages.py` — `usage` is a **first-class column** (`:80-123`).
  `get_messages_map_by_chat_id:303-305` is the `info` mirror writer:
  `if "usage" in msg: msg["info"] = {"usage": msg["usage"]}` — `info` and `usage` are the *same
  data*; reading both double-counts. This is why the plugin ignores `info`.
- **`content` is empty for BOTH streaming and non-streaming at v0.10.2.** The assistant placeholder
  starts as `''` (`main.py:1443`) and every save path writes `output`/`usage` only
  (`middleware.py:5228-5242` streaming, `:3538-3547` non-streaming). All visible text lives in the
  structured `output` array; a content-first reader must always fall through to `output`.
- Aggregate token analytics (independent of this plugin):
  `chat_messages.py:get_token_usage_by_model:489`, exposed via `routers/analytics.py`.

## Frontend constraints that shaped the plugin

- `Chat.svelte:chatEventHandler:610` — `status` pushes onto `message.statusHistory` (`:621-626`);
  `chat:completion` sets `message.usage` (`:2013`).
- `Messages/ResponseMessage/StatusHistory/StatusItem.svelte` — status text renders plain-text with
  a hardcoded `line-clamp-1` and is not selectable → **multi-line / markdown / copyable debug output
  in the status line is impossible**.
- `ContentRenderer.svelte:265-312` — `content` renders **only when `output` is empty**; on 0.10.x
  responses `output` is non-empty → appending debug text to `content` is invisible.
- `Citations.svelte` → `CitationModal.svelte` — sources render as Markdown with a Copy button on
  code blocks (`CodeBlock.svelte:518`) → the reason `debug_mode` ships its payload as a `citation`
  event (copyable, persisted) with stdout as fallback.
- `ResponseMessage.svelte:1160-1184` — OWUI's own built-in usage tooltip (pretty-printed
  `message.usage`) — the native UI this plugin complements.
