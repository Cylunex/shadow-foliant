"""User-declared stock budget derived from classified holdings and current prices."""

from __future__ import annotations

from datetime import date
import math
from typing import Any, Callable


STOCK_BUDGET_CNY = 300_000.0
STOCK_BUDGET_BASIS = "user_declared_stock_budget"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _symbols(frame: Any, column: str) -> set[str]:
    if frame is None or getattr(frame, "empty", True) or column not in frame.columns:
        return set()
    return {
        str(value).strip().zfill(6)
        for value in frame[column].tolist()
        if str(value or "").strip()
    }


def load_security_metadata(store: Any, as_of: str | None = None) -> dict[str, Any]:
    """Load independent stock/fund memberships; never infer from name or prefix."""
    target = str(as_of or date.today().isoformat())
    stock_symbols: set[str] = set()
    fund_symbols: set[str] = set()
    sources: list[dict[str, Any]] = []
    errors: list[str] = []

    try:
        universe = store.load_universe(target)
        stock_symbols = _symbols(universe, "symbol")
        sources.append({
            "asset_type": "stock",
            "source": "research_security_master_rows",
            "snapshot_id": getattr(universe, "attrs", {}).get("snapshot_id"),
            "as_of": getattr(universe, "attrs", {}).get("snapshot_date"),
            "count": len(stock_symbols),
        })
    except Exception:
        errors.append("stock_security_master_unavailable")

    # Prefer the repository's fund metadata adapter. It has a process cache and
    # returns explicit fund codes/types; an empty result is not guessed around.
    try:
        from fund.fund_data import list_funds

        funds = list_funds()
        fund_symbols = _symbols(funds, "基金代码")
        sources.append({
            "asset_type": "fund_or_etf_or_lof",
            "source": "fund.fund_data.list_funds",
            "count": len(fund_symbols),
        })
    except Exception:
        errors.append("fund_security_master_unavailable")

    return {
        "stock_symbols": stock_symbols,
        "fund_symbols": fund_symbols,
        "sources": sources,
        "errors": errors,
    }


def derive_stock_budget(
    holdings: list[dict[str, Any]],
    quote_rows: list[dict[str, Any]],
    metadata: dict[str, Any] | None,
    *,
    total_budget: float = STOCK_BUDGET_CNY,
) -> dict[str, Any]:
    """Derive cash without treating the declared budget as a broker balance."""
    metadata = metadata or {}
    stocks = {str(value).zfill(6) for value in metadata.get("stock_symbols") or ()}
    funds = {str(value).zfill(6) for value in metadata.get("fund_symbols") or ()}
    quote_by_symbol = {
        str(row.get("symbol") or "").zfill(6): row
        for row in quote_rows if isinstance(row, dict)
    }
    classifications: dict[str, str] = {}
    unknown: list[str] = []
    ambiguous: list[str] = []
    stock_rows: list[dict[str, Any]] = []
    fund_rows: list[dict[str, Any]] = []

    for holding in holdings:
        symbol = str(holding.get("symbol") or holding.get("code") or "").zfill(6)
        in_stock, in_fund = symbol in stocks, symbol in funds
        # An exchange security-master row is authoritative in this securities
        # portfolio namespace. Open-ended fund codes can numerically collide
        # with A-share symbols, so fund-list membership must not make those
        # exchange-listed stocks ambiguous.
        if in_stock:
            classifications[symbol] = "stock"
            stock_rows.append(holding)
        elif in_fund:
            classifications[symbol] = "fund_or_etf_or_lof"
            fund_rows.append(holding)
        else:
            classifications[symbol] = "unknown"
            unknown.append(symbol)

    missing_prices: list[str] = []
    invalid_quantities: list[str] = []
    market_value = 0.0
    quote_times: list[str] = []
    for holding in stock_rows:
        symbol = str(holding.get("symbol") or holding.get("code") or "").zfill(6)
        quantity = _finite(holding.get("quantity"))
        quote = quote_by_symbol.get(symbol) or {}
        price = _finite(quote.get("price"))
        if quantity is None or quantity <= 0:
            invalid_quantities.append(symbol)
            continue
        if price is None or price <= 0 or quote.get("freshness") not in {
            "actionable", "closing_current",
        }:
            missing_prices.append(symbol)
            continue
        market_value += quantity * price
        if quote.get("as_of"):
            quote_times.append(str(quote["as_of"]))

    blockers: list[str] = []
    if unknown:
        blockers.append("holding_asset_type_unknown")
    if ambiguous:
        blockers.append("holding_asset_type_ambiguous")
    if missing_prices:
        blockers.append("stock_holding_quote_unavailable")
    if invalid_quantities:
        blockers.append("stock_holding_quantity_invalid")
    if not stocks:
        blockers.append("stock_security_master_empty")
    if fund_rows and not funds:
        blockers.append("fund_security_master_empty")

    budget = max(0.0, float(total_budget))
    stock_market_value = round(market_value, 2) if not blockers else None
    available_cash = (
        round(max(0.0, budget - market_value), 2) if not blockers else None
    )
    over_budget = (
        round(max(0.0, market_value - budget), 2) if not blockers else None
    )
    return {
        "status": "complete" if not blockers else "blocked",
        "basis": STOCK_BUDGET_BASIS,
        "total_budget_cny": round(budget, 2),
        "stock_holding_count": len(stock_rows),
        "stock_market_value_cny": stock_market_value,
        "excluded_fund_holding_count": len(fund_rows),
        "excluded_asset_types": ["fund", "etf", "lof"],
        "available_cash_cny": available_cash,
        "over_budget": bool(over_budget and over_budget > 0),
        "over_budget_amount_cny": over_budget,
        "as_of": min(quote_times) if quote_times and not blockers else None,
        "classifications": classifications,
        "classification_basis": "explicit_security_master_membership",
        "metadata_sources": metadata.get("sources") or [],
        "metadata_errors": metadata.get("errors") or [],
        "unknown_symbols": sorted(set(unknown)),
        "ambiguous_symbols": sorted(set(ambiguous)),
        "missing_stock_quote_symbols": sorted(set(missing_prices)),
        "invalid_quantity_symbols": sorted(set(invalid_quantities)),
        "buy_side_blockers": blockers,
        "broker_cash_balance": False,
        "writeback": False,
        "auto_execution": False,
    }


def default_metadata_reader(store: Any) -> Callable[[str], dict[str, Any]]:
    return lambda as_of: load_security_metadata(store, as_of)
