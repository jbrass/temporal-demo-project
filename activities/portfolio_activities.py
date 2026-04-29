"""
Portfolio Activities — Alpaca API Integration

Activities are the "doing" layer in Temporal. They run in workers,
can interact with external systems, and are automatically retried
by Temporal if they fail. Each activity is independently retryable,
idempotent where possible, and observable via Temporal's UI.

Key design decisions:
- Activities are sync (run in ThreadPoolExecutor) — safe with blocking Alpaca SDK
- Credentials are resolved from env vars inside activities; never passed through
  workflow arguments or stored in Temporal's event history
- Expensive/slow IO is done here, NOT in workflow code
- ApplicationError for non-retryable failures (e.g., invalid symbol, auth errors)
"""

import os
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from temporalio import activity
from temporalio.exceptions import ApplicationError

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.common.exceptions import APIError


def _trading_client(base_url: str) -> TradingClient:
    paper = "paper" in base_url
    return TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=paper,
    )


def _data_client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    )


# ---------------------------------------------------------------------------
# Data classes used across activities and the workflow
# ---------------------------------------------------------------------------

@dataclass
class Position:
    symbol: str
    qty: float
    market_value: float
    current_price: float
    weight: float = 0.0


@dataclass
class PortfolioSnapshot:
    total_value: float
    cash: float
    positions: list = field(default_factory=list)  # list of Position dicts
    spy_weight: float = 0.0
    tlt_weight: float = 0.0
    timestamp: str = ""


@dataclass
class RebalanceOrder:
    symbol: str
    side: str        # "buy" or "sell"
    qty: float
    notional: float  # dollar amount
    reason: str


@dataclass
class RebalanceResult:
    needs_rebalance: bool
    spy_drift: float
    tlt_drift: float
    spy_current_weight: float
    tlt_current_weight: float
    orders: list = field(default_factory=list)  # list of order dicts


# ---------------------------------------------------------------------------
# Activity: Fetch current positions from Alpaca
# ---------------------------------------------------------------------------

@activity.defn
def get_portfolio_positions(base_url: str) -> PortfolioSnapshot:
    """
    Fetch current portfolio positions from Alpaca.
    Returns a PortfolioSnapshot with current weights for SPY and TLT.
    """
    activity.logger.info("Fetching portfolio positions from Alpaca")
    client = _trading_client(base_url)

    try:
        account = client.get_account()
        positions_raw = client.get_all_positions()
    except APIError as e:
        raise ApplicationError(
            f"Alpaca account fetch failed: {e}",
            non_retryable=e.status_code == 403,
        )

    # portfolio_value may be None briefly after market open; fall back to equity
    portfolio_value = float(account.portfolio_value or account.equity or 0)
    cash = float(account.cash or 0)

    positions = []
    spy_weight = 0.0
    tlt_weight = 0.0

    for pos in positions_raw:
        symbol = pos.symbol
        market_value = float(pos.market_value or 0)
        weight = market_value / portfolio_value if portfolio_value > 0 else 0.0

        position = Position(
            symbol=symbol,
            qty=float(pos.qty or 0),
            market_value=market_value,
            current_price=float(pos.current_price or 0),
            weight=weight,
        )
        positions.append(asdict(position))

        if symbol == "SPY":
            spy_weight = weight
        elif symbol == "TLT":
            tlt_weight = weight

    snapshot = PortfolioSnapshot(
        total_value=portfolio_value,
        cash=cash,
        positions=positions,
        spy_weight=spy_weight,
        tlt_weight=tlt_weight,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    activity.logger.info(
        f"Portfolio: ${portfolio_value:,.2f} | SPY={spy_weight:.2%} | TLT={tlt_weight:.2%}"
    )
    return snapshot


# ---------------------------------------------------------------------------
# Activity: Get live market prices
# ---------------------------------------------------------------------------

@activity.defn
def get_market_prices(symbols: list[str]) -> dict:
    """
    Fetch latest quotes for given symbols from Alpaca Data API.
    Returns {symbol: price} mapping.
    """
    activity.logger.info(f"Fetching prices for {symbols}")
    client = _data_client()

    try:
        quotes = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbols, feed="iex")
        )
    except APIError as e:
        raise ApplicationError(f"Price fetch failed: {e}")

    prices = {}
    for symbol in symbols:
        quote = quotes.get(symbol)
        if quote:
            ask = float(quote.ask_price or 0)
            bid = float(quote.bid_price or 0)
            price = (ask + bid) / 2 if ask and bid else ask
        else:
            price = 0.0
        prices[symbol] = price
        activity.logger.info(f"{symbol}: ${price:.2f}")

    return prices


# ---------------------------------------------------------------------------
# Activity: Calculate rebalance orders (pure business logic)
# ---------------------------------------------------------------------------

@activity.defn
def calculate_rebalance_orders(
    snapshot: PortfolioSnapshot,
    prices: dict,
    target_equity_pct: float,
    target_bond_pct: float,
    drift_threshold: float,
) -> RebalanceResult:
    """
    Pure calculation activity — no side effects.

    Determines if drift exceeds threshold and, if so, computes
    the exact buy/sell orders needed to restore target allocation.

    60/40 logic:
    - SPY target: 60% of portfolio value
    - TLT target: 40% of portfolio value
    - If current weight deviates by more than drift_threshold, rebalance
    """
    activity.logger.info("Calculating rebalance orders")

    total_value = snapshot.total_value
    spy_current = snapshot.spy_weight
    tlt_current = snapshot.tlt_weight

    spy_drift = abs(spy_current - target_equity_pct)
    tlt_drift = abs(tlt_current - target_bond_pct)
    needs_rebalance = spy_drift > drift_threshold or tlt_drift > drift_threshold

    activity.logger.info(
        f"SPY: current={spy_current:.2%} target={target_equity_pct:.2%} drift={spy_drift:.2%} | "
        f"TLT: current={tlt_current:.2%} target={target_bond_pct:.2%} drift={tlt_drift:.2%} | "
        f"needs_rebalance={needs_rebalance}"
    )

    orders = []

    if needs_rebalance:
        spy_price = prices.get("SPY", 0)
        tlt_price = prices.get("TLT", 0)

        if not spy_price or not tlt_price:
            raise ApplicationError("Missing price data for SPY or TLT", non_retryable=True)

        spy_target_value = total_value * target_equity_pct
        tlt_target_value = total_value * target_bond_pct

        spy_current_value = total_value * spy_current
        tlt_current_value = total_value * tlt_current

        spy_delta = spy_target_value - spy_current_value
        tlt_delta = tlt_target_value - tlt_current_value

        def make_order(symbol, delta, price, reason):
            side = "buy" if delta > 0 else "sell"
            notional = abs(delta)
            qty = notional / price
            return {
                "symbol": symbol,
                "side": side,
                "qty": round(qty, 4),
                "notional": round(notional, 2),
                "reason": reason,
            }

        if abs(spy_delta) > 1.0:  # ignore sub-dollar rounding noise
            orders.append(make_order(
                "SPY", spy_delta, spy_price,
                f"SPY drift {spy_drift:.2%} exceeds threshold {drift_threshold:.2%}"
            ))

        if abs(tlt_delta) > 1.0:
            orders.append(make_order(
                "TLT", tlt_delta, tlt_price,
                f"TLT drift {tlt_drift:.2%} exceeds threshold {drift_threshold:.2%}"
            ))

        activity.logger.info(f"Generated {len(orders)} orders: {orders}")

    return RebalanceResult(
        needs_rebalance=needs_rebalance,
        spy_drift=spy_drift,
        tlt_drift=tlt_drift,
        spy_current_weight=spy_current,
        tlt_current_weight=tlt_current,
        orders=orders,
    )


# ---------------------------------------------------------------------------
# Activity: Execute orders via Alpaca
# ---------------------------------------------------------------------------

@activity.defn
def execute_orders(orders: list, base_url: str) -> list:
    """
    Execute a list of orders against Alpaca's trading API.

    Uses client_order_id for idempotency so that Temporal retries
    don't double-place orders. Alpaca returns 422 for duplicates,
    which is treated as "already placed."
    """
    trading_client = _trading_client(base_url)
    placed = []

    for order in orders:
        client_order_id = "temporal-rebalance-{}-{}-{}".format(
            order["symbol"], order["side"],
            hashlib.sha256(str(order).encode()).hexdigest()[:16]
        )

        request = MarketOrderRequest(
            symbol=order["symbol"],
            notional=order["notional"],
            side=OrderSide.BUY if order["side"] == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )

        activity.logger.info(
            f"Placing order: {order['side']} {order['qty']} {order['symbol']} (~${order['notional']:,.2f})"
        )

        try:
            order_result = trading_client.submit_order(request)
            placed.append({
                **order,
                "status": "accepted",
                "alpaca_order_id": str(order_result.id),
                "alpaca_status": order_result.status.value if order_result.status else None,
            })
            activity.logger.info(f"Order accepted: {order_result.id}")
        except APIError as e:
            if e.status_code == 422:
                # Likely a duplicate — treat as already placed
                activity.logger.warning(f"Order may be duplicate (422): {e}")
                placed.append({**order, "status": "duplicate_or_invalid", "alpaca_response": str(e)})
            else:
                raise ApplicationError(
                    f"Order failed for {order['symbol']}: {e}",
                    non_retryable=e.status_code == 403,
                )

    return placed


# ---------------------------------------------------------------------------
# Activity: Send notification / audit log
# ---------------------------------------------------------------------------

@activity.defn
def send_notification(result: dict) -> None:
    """
    Notification activity — in production this would post to Slack,
    send an email, write to a data warehouse, etc.
    """
    activity.logger.info(
        f"REBALANCE COMPLETE | "
        f"status={result.get('status')} | "
        f"orders={len(result.get('orders_placed', []))} | "
        f"message={result.get('message')}"
    )
    # In production: httpx.post("https://hooks.slack.com/...", json={...})
    # Or: write to a database audit table
