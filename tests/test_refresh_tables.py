"""scripts/refresh_tables.py: which models.dev rows become the built-in price table, and how it is written.

No network: every test feeds a hand-made api.json. The live table in usage_display.py is checked only
for the invariants the plugin relies on, never for particular models (it changes on every release).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

TODAY = dt.date(2026, 11, 6)
SINCE = dt.date(2026, 7, 6)


def _model(model_id: str, released: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": model_id,
        "release_date": released,
        "limit": {"context": 1_000_000},
        "cost": {"input": 1, "output": 5, "cache_read": 0.1},
    }
    row.update(extra)
    return row


def _api() -> dict[str, Any]:
    return {
        "openai": {
            "models": {
                "gpt-new": _model("gpt-new", "2026-10-26", cost={"input": 1, "output": 5, "cache_write": 1.25}),
                "gpt-old": _model("gpt-old", "2026-01-01"),
                "gpt-gone": _model("gpt-gone", "2026-09-01", status="deprecated"),
                "embed": _model("embed", "2026-09-01", cost={"input": 0.1, "output": 0}),
                "image": _model("image", "2026-09-01", limit={"context": 0}),
                "undated": _model("undated", ""),
            }
        },
        "moonshotai": {"models": {"kimi-k3": _model("Kimi-K3", "2026-08-01")}},
        "alibaba": {"models": {"kimi-k3": _model("kimi-k3", "2026-08-01", cost={"input": 9, "output": 9})}},
        "some-reseller": {"models": {"resold": _model("resold", "2026-10-01")}},
    }


def test_months_before_clamps_to_the_end_of_a_shorter_month(refresh_tables_module: ModuleType) -> None:
    fn = refresh_tables_module.months_before
    assert fn(TODAY, 4) == SINCE
    assert fn(dt.date(2026, 6, 30), 4) == dt.date(2026, 2, 28)
    assert fn(dt.date(2026, 2, 15), 4) == dt.date(2025, 10, 15)


def test_select_prices_keeps_recent_current_first_party_models_only(refresh_tables_module: ModuleType) -> None:
    prices = refresh_tables_module.select_prices(_api(), SINCE)
    # old, deprecated, unpriced, windowless and undated rows drop out, and resellers are not vendors
    assert prices == {
        "openai": {"gpt-new": {"input": 1.0, "output": 5.0, "cache_write": 1.25}},
        # an id two vendors list keeps the first vendor's price (the owner comes first in VENDORS)
        "moonshotai": {"kimi-k3": {"input": 1.0, "output": 5.0, "cache_read": 0.1}},
    }


def test_render_and_replace_rewrite_only_the_marked_block(refresh_tables_module: ModuleType) -> None:
    mod = refresh_tables_module
    block = mod.render_block(mod.select_prices(_api(), SINCE), TODAY, SINCE)
    text = f"head\n{mod.BEGIN}\n_STATIC_PRICES = {{}}\n{mod.END}\ntail\n"
    new = mod.replace_block(text, block)
    assert new.startswith("head\n")
    assert new.endswith("\ntail\n")
    assert '    "gpt-new": {"input": 1.0, "output": 5.0, "cache_write": 1.25},' in new
    assert "2 models released on or after 2026-07-06" in new
    assert mod.replace_block(new, block) == new  # a rerun on the same data changes nothing
    namespace: dict[str, Any] = {}
    exec(block, namespace)  # noqa: S102 - the generated block must be valid Python on its own
    assert namespace["_STATIC_PRICES"]["kimi-k3"]["output"] == pytest.approx(5.0)


@pytest.mark.parametrize("text", ["no markers", "# END generated static prices\n# BEGIN generated static prices"])
def test_replace_block_refuses_a_file_without_the_markers(refresh_tables_module: ModuleType, text: str) -> None:
    with pytest.raises(ValueError, match="expected one"):
        refresh_tables_module.replace_block(text, "x")


def test_context_report_lists_wrong_and_missing_sizes(refresh_tables_module: ModuleType) -> None:
    mod = refresh_tables_module
    table = {"gpt": 1_000_000, "gpt-old": 128_000}
    report = mod.context_report(_api(), table)
    assert report == [
        "  differs  openai/gpt-old: models.dev 1000000, table 128000 via 'gpt-old'",  # older than the price window
        "  missing  openai/undated: models.dev 1000000, no key matches",
        "  missing  moonshotai/kimi-k3: models.dev 1000000, no key matches",  # listed once, though Alibaba has it
    ]


def test_static_context_sizes_reads_the_table_without_importing_the_plugin(refresh_tables_module: ModuleType) -> None:
    mod = refresh_tables_module
    assert mod.static_context_sizes('_STATIC_CONTEXT_SIZES: dict[str, int] = {"a": 1}\n') == {"a": 1}
    with pytest.raises(ValueError, match="no _STATIC_CONTEXT_SIZES"):
        mod.static_context_sizes("x = 1\n")


def test_main_writes_from_a_saved_api_file_and_refuses_an_empty_selection(
    refresh_tables_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = refresh_tables_module
    plugin = tmp_path / "usage_display.py"
    plugin.write_text(f'_STATIC_CONTEXT_SIZES: dict[str, int] = {{"gpt": 1000000}}\n{mod.BEGIN}\n{mod.END}\n')
    monkeypatch.setattr(mod, "PLUGIN", plugin)
    api = tmp_path / "api.json"
    api.write_text(json.dumps(_api()))

    assert mod.main(["--source", str(api), "--today", TODAY.isoformat(), "--dry-run"]) == 0
    assert '"gpt-new"' in capsys.readouterr().out
    assert '"gpt-new"' not in plugin.read_text()  # a dry run writes nothing

    assert mod.main(["--source", str(api), "--today", TODAY.isoformat()]) == 0
    assert '"gpt-new"' in plugin.read_text()
    assert "missing  moonshotai/kimi-k3" in capsys.readouterr().out

    before = plugin.read_text()
    assert mod.main(["--source", str(api), "--today", "2030-01-01"]) == 1  # nothing that recent
    assert plugin.read_text() == before
    api.write_text("[]")
    with pytest.raises(TypeError):
        mod.main(["--source", str(api)])


def test_the_shipped_price_table_has_what_the_plugin_reads(usage_display_module: ModuleType) -> None:
    """Whatever the generator wrote: lowercase whole ids with numeric input and output rates."""
    table = usage_display_module._STATIC_PRICES
    assert table
    for model_id, rates in table.items():
        assert model_id == model_id.lower()
        assert set(rates) <= set(usage_display_module._PRICE_FIELDS)
        assert isinstance(rates["input"], float)
        assert isinstance(rates["output"], float)
