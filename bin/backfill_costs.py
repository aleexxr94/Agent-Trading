#!/usr/bin/env python3
"""Backfill modelled Alpaca costs onto legacy paper fills.

The cost model (slippage + SEC/TAF, see ``lib/alpaca_costs.py``) stamps every
NEW fill with ``fees_usd`` / ``slippage_usd`` / ``fee_source`` at sync time. But
fills written BEFORE that change predate those fields, so ``trades_pnl_view``,
``realized_balance_series`` and the SPY/Sharpe comparison stay GROSS for all
pre-upgrade history — which would inflate the promote-to-live Sharpe gate.

This one-time, idempotent tool nets that history: it finds rows MISSING
``fee_source`` (the legacy marker), groups them by order, and applies the SAME
modelled-cost logic the live sync uses (``alpaca_costs.model_order_fill_costs``),
so the two never diverge. Rows already tagged (``real`` / ``modelled`` / ``none``)
are left untouched, so re-running is safe.

Paper-only: refuses to run unless the cost model is active
(``alpaca_costs.cost_model_active()`` — i.e. ``PAPER_COST_MODEL`` enabled and not
live). Live history already carries real fees and real fill prices.

Run:
    python -m bin.backfill_costs --dry-run    # report, write nothing
    python -m bin.backfill_costs              # back up + rewrite in place
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import alpaca_costs, state  # noqa: E402


def _needs_backfill(row: dict) -> bool:
    """A legacy row is one written before the cost model — it has no
    ``fee_source`` marker. Already-tagged rows are skipped (idempotent)."""
    return "fee_source" not in row


def backfill_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """Return (new_rows, changed_count). Pure — does not touch disk.

    Legacy rows are grouped by ``alpaca_order_id`` and assigned modelled costs
    via the shared ``alpaca_costs.model_order_fill_costs`` helper. A pre-existing
    real ``fees_usd > 0`` is preserved (tagged ``real``, slippage still added);
    otherwise the modelled order-level fee is used (tagged ``modelled``).
    """
    legacy_idx_by_order: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if _needs_backfill(r):
            legacy_idx_by_order[r.get("alpaca_order_id") or ""].append(i)

    changed = 0
    for _oid, idxs in legacy_idx_by_order.items():
        order_fills = [
            {
                "side": rows[i].get("side"),
                "symbol": rows[i].get("symbol") or "",
                "shares": float(rows[i].get("qty") or 0.0),
                "price": float(rows[i].get("fill_price") or 0.0),
            }
            for i in idxs
        ]
        modelled = alpaca_costs.model_order_fill_costs(order_fills)
        for i, (m_fee, m_slip) in zip(idxs, modelled):
            row = rows[i]
            real_fee = float(row.get("fees_usd") or 0.0)
            row["slippage_usd"] = m_slip
            if real_fee > 0:
                row["fee_source"] = "real"          # keep the real fee as-is
            else:
                row["fees_usd"] = m_fee
                row["fee_source"] = "modelled"
            changed += 1
    return rows, changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill modelled costs onto legacy paper fills.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; write nothing.")
    args = ap.parse_args(argv)

    if not alpaca_costs.cost_model_active():
        print(
            "Cost model is not active (PAPER_COST_MODEL disabled or live "
            "trading engaged) — refusing to backfill. Legacy live history "
            "already carries real fees.",
            file=sys.stderr,
        )
        return 2

    path = state.TRADES_LOG
    if not path.exists():
        print(f"No trade log at {path} — nothing to backfill.")
        return 0

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    legacy = sum(1 for r in rows if _needs_backfill(r))
    if legacy == 0:
        print(f"{len(rows)} fills, 0 legacy (all already tagged) — nothing to do.")
        return 0

    new_rows, changed = backfill_rows(rows)
    print(f"{len(rows)} fills total; {changed} legacy rows would be backfilled with modelled costs.")

    if args.dry_run:
        print("--dry-run: no changes written.")
        return 0

    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    # Atomic write: write to a temp file, then replace.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "".join(json.dumps(r, sort_keys=False) + "\n" for r in new_rows),
        encoding="utf-8",
    )
    tmp.replace(path)
    print(f"Backed up to {backup.name}; rewrote {path.name} ({changed} rows updated).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
