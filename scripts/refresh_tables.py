"""Refresh the plugin's built-in model tables from models.dev — a step of every release.

`_STATIC_PRICES` in usage_display.py is generated: this script rewrites the block between its
`# BEGIN generated static prices` / `# END generated static prices` lines with the models the vendors
themselves sell (VENDORS, first-party providers of models.dev) that came out in the last MONTHS months
and are not deprecated. The plugin matches it by exact id (`_price_key`), so it only has to hold whole
model ids, never families.

`_STATIC_CONTEXT_SIZES` stays hand-written (family keys matched by substring: it is the default source
of the context metric). The script only reports, for every current first-party model, where that table
disagrees with models.dev or has no key at all; a maintainer edits the table from the report.

Stdlib only, like scripts/release.py. Nothing is written when the fetch fails or selects no model.

    python3 scripts/refresh_tables.py                  # fetch models.dev, rewrite prices, print the context report
    python3 scripts/refresh_tables.py --source api.json  # the same from a saved copy of api.json
    python3 scripts/refresh_tables.py --dry-run        # print the new price block instead of writing it
"""

from __future__ import annotations

import argparse
import ast
import calendar
import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "usage_display.py"
API_URL = "https://models.dev/api.json"
# First-party providers of models.dev. Model owners come first: an id several of them list (Alibaba also
# sells kimi-k3) keeps the first vendor's price.
VENDORS = (
    "openai",
    "anthropic",
    "google",
    "deepseek",
    "xai",
    "moonshotai",
    "zai",
    "minimax",
    "cohere",
    "mistral",
    "alibaba",
)
MONTHS = 4
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")  # the rates the plugin reads (_PRICE_FIELDS)
BEGIN = "# BEGIN generated static prices"
END = "# END generated static prices"
TIMEOUT_S = 60


def months_before(day: dt.date, months: int) -> dt.date:
    """The same day `months` calendar months earlier, clamped to the end of a shorter month."""
    total = day.year * 12 + day.month - 1 - months
    year, month = divmod(total, 12)
    month += 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def current_models(data: dict[str, Any], vendor: str) -> list[dict[str, Any]]:
    """A vendor's priced, not deprecated chat models with a context window (embeddings and images drop out)."""
    provider = data.get(vendor)
    models = provider.get("models") if isinstance(provider, dict) else None
    out = []
    for key, entry in (models if isinstance(models, dict) else {}).items():
        if not isinstance(entry, dict) or entry.get("status") == "deprecated":
            continue
        cost = entry.get("cost")
        limit = entry.get("limit")
        if not isinstance(cost, dict) or _positive(cost.get("input")) is None or _positive(cost.get("output")) is None:
            continue
        if not isinstance(limit, dict) or _positive(limit.get("context")) is None:
            continue
        out.append({**entry, "id": str(entry.get("id", key)).lower()})
    return out


def select_prices(data: dict[str, Any], since: dt.date) -> dict[str, dict[str, dict[str, float]]]:
    """{vendor: {model id: rates}} of the models released on or after `since`; first vendor wins an id."""
    taken: set[str] = set()
    out: dict[str, dict[str, dict[str, float]]] = {}
    for vendor in VENDORS:
        rows: dict[str, dict[str, float]] = {}
        for entry in sorted(current_models(data, vendor), key=lambda e: e["id"]):
            released = entry.get("release_date")
            if entry["id"] in taken or not isinstance(released, str) or released < since.isoformat():
                continue
            cost = entry["cost"]
            rows[entry["id"]] = {f: float(cost[f]) for f in PRICE_FIELDS if _positive(cost.get(f)) is not None}
            taken.add(entry["id"])
        if rows:
            out[vendor] = rows
    return out


def render_block(prices: dict[str, dict[str, dict[str, float]]], today: dt.date, since: dt.date) -> str:
    """The generated block, markers included, in the layout ruff format keeps."""
    count = sum(len(rows) for rows in prices.values())
    lines = [
        BEGIN,
        f"# models.dev {API_URL} of {today.isoformat()}: {count} models released on or after {since.isoformat()}.",
        "_STATIC_PRICES: dict[str, dict[str, float]] = {",
    ]
    for vendor, rows in prices.items():
        lines.append(f"    # --- {vendor} ---")
        lines.extend(f"    {json.dumps(model_id)}: {_render_rates(rates)}," for model_id, rates in rows.items())
    lines.extend(["}", END])
    return "\n".join(lines)


def _render_rates(rates: dict[str, float]) -> str:
    return "{" + ", ".join(f'"{field}": {value!r}' for field, value in rates.items()) + "}"


def replace_block(text: str, block: str) -> str:
    """`text` with the generated block swapped for `block`; the markers must appear once, in order."""
    start, end = text.find(BEGIN), text.find(END)
    if start < 0 or end < start or text.count(BEGIN) != 1 or text.count(END) != 1:
        msg = f"{PLUGIN.name}: expected one `{BEGIN}` line followed by one `{END}` line"
        raise ValueError(msg)
    return text[:start] + block + text[end + len(END) :]


def static_context_sizes(text: str) -> dict[str, int]:
    """`_STATIC_CONTEXT_SIZES` as written in the plugin source (read without importing the plugin)."""
    for node in ast.parse(text).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "_STATIC_CONTEXT_SIZES":
            value: dict[str, int] = ast.literal_eval(node.value) if node.value is not None else {}
            return value
    msg = f"{PLUGIN.name}: no _STATIC_CONTEXT_SIZES table"
    raise ValueError(msg)


def longest_key(table: dict[str, int], model_id: str) -> str | None:
    """The plugin's _longest_key_match: the longest key contained in the id."""
    return next((key for key in sorted(table, key=len, reverse=True) if key and key in model_id), None)


def context_report(data: dict[str, Any], table: dict[str, int]) -> list[str]:
    """Current first-party models whose context the hand-written table gets wrong or misses.

    Not limited to the price window: the context table keeps older families too.
    """
    lines = []
    seen: set[str] = set()
    for vendor in VENDORS:
        for entry in sorted(current_models(data, vendor), key=lambda e: e["id"]):
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            want = int(entry["limit"]["context"])
            key = longest_key(table, entry["id"])
            if key is None:
                lines.append(f"  missing  {vendor}/{entry['id']}: models.dev {want}, no key matches")
            elif table[key] != want:
                lines.append(f"  differs  {vendor}/{entry['id']}: models.dev {want}, table {table[key]} via {key!r}")
    return lines


def load_api(source: str | None) -> dict[str, Any]:
    """models.dev api.json from the network, or from a saved file."""
    if source:
        raw = Path(source).read_bytes()
    else:
        request = urllib.request.Request(API_URL, headers={"User-Agent": "openwebui-token-usage-display/refresh"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:  # noqa: S310 - fixed https URL
            raw = resp.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        msg = "models.dev api.json is not a JSON object"
        raise TypeError(msg)
    return data


def main(argv: list[str] | None = None) -> int:
    """Rewrite the price block and print the context report; returns the exit status."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", help="a saved models.dev api.json instead of the live one")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.datetime.now(tz=dt.UTC).date())
    parser.add_argument("--dry-run", action="store_true", help="print the price block, write nothing")
    args = parser.parse_args(argv)

    data = load_api(args.source)
    since = months_before(args.today, MONTHS)
    prices = select_prices(data, since)
    if not prices:
        print(f"refresh: no model released since {since} - models.dev changed shape? Nothing written.", file=sys.stderr)  # noqa: T201 - CLI output
        return 1
    text = PLUGIN.read_text(encoding="utf-8")
    block = render_block(prices, args.today, since)
    if args.dry_run:
        print(block)  # noqa: T201 - CLI output
    else:
        PLUGIN.write_text(replace_block(text, block), encoding="utf-8")
        count = sum(len(rows) for rows in prices.values())
        print(f"_STATIC_PRICES: {count} models released since {since} written to {PLUGIN.name}")  # noqa: T201 - CLI output

    report = context_report(data, static_context_sizes(text))
    print(f"_STATIC_CONTEXT_SIZES vs models.dev: {len(report) or 'no'} finding(s)")  # noqa: T201 - CLI output
    print("\n".join(report))  # noqa: T201 - CLI output
    return 0


if __name__ == "__main__":
    sys.exit(main())
