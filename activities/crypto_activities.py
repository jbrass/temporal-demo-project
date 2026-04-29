"""
Crypto Activities — Alpaca Crypto API

- Activities are sync (run in ThreadPoolExecutor) — safe with blocking Alpaca SDK
- Credentials resolved from env vars; never passed through workflow args
- Crypto symbols use the "BTC/USD" format (not "BTCUSD")
- Orders use notional (dollar) amounts since crypto is fractional
- GTC (good-till-cancelled) instead of day orders — markets never close
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
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestQuoteRequest
from alpaca.common.exceptions import APIError


def _trading_client(base_url: str) -> TradingClient:
    paper = "paper" in base_url
    return TradingClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
        paper=paper,
    )


def _data_client() -> CryptoHistoricalDataClient:
    return CryptoHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CryptoPosition:
    symbol: str
    qty: float
    market_value: float
    current_price: float
    weight: float = 0.0


@dataclass
class CryptoSnapshot:
    total_value: float
    cash: float
    btc_weight: float = 0.0
    eth_weight: float = 0.0
    positions: list = field(default_factory=list)
    timestamp: str = ""


@dataclass
class CryptoRebalanceResult:
    needs_rebalance: bool
    btc_drift: float
    eth_drift: float
    btc_current_weight: float
    eth_current_weight: float
    orders: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Activity: Fetch crypto positions
# ---------------------------------------------------------------------------

@activity.defn
def get_crypto_positions(base_url: str) -> CryptoSnapshot:
    """Fetch current crypto positions from Alpaca."""
    activity.logger.info("Fetching crypto positions")

    client = _trading_client(base_url)
    account = client.get_account()
    positions_raw = client.get_all_positions()

    portfolio_value = float(account.portfolio_value or 0)
    cash = float(account.cash or 0)

    positions = []
    btc_weight = 0.0
    eth_weight = 0.0

    for pos in positions_raw:
        symbol = pos.symbol
        # Alpaca returns crypto as "BTCUSD" — normalize to "BTC/USD"
        normalized = symbol[:3] + "/" + symbol[3:] if "/" not in symbol and len(symbol) == 6 else symbol
        market_value = float(pos.market_value or 0)
        weight = market_value / portfolio_value if portfolio_value > 0 else 0.0

        positions.append(asdict(CryptoPosition(
            symbol=normalized,
            qty=float(pos.qty or 0),
            market_value=market_value,
            current_price=float(pos.current_price or 0),
            weight=weight,
        )))

        if "BTC" in normalized:
            btc_weight = weight
        elif "ETH" in normalized:
            eth_weight = weight

    snapshot = CryptoSnapshot(
        total_value=portfolio_value,
        cash=cash,
        btc_weight=btc_weight,
        eth_weight=eth_weight,
        positions=positions,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    activity.logger.info(
        f"Portfolio: ${portfolio_value:,.2f} | BTC={btc_weight:.2%} | ETH={eth_weight:.2%}"
    )
    return snapshot


# ---------------------------------------------------------------------------
# Activity: Get crypto prices
# ---------------------------------------------------------------------------

@activity.defn
def get_crypto_prices(symbols: list[str]) -> dict:
    """Fetch latest crypto quotes from Alpaca."""
    activity.logger.info(f"Fetching crypto prices for {symbols}")

    client = _data_client()
    request = CryptoLatestQuoteRequest(symbol_or_symbols=symbols)
    quotes = client.get_crypto_latest_quote(request)

    prices = {}
    for symbol in symbols:
        quote = quotes.get(symbol)
        if quote:
            ask = float(quote.ask_price or 0)
            bid = float(quote.bid_price or 0)
            price = (ask + bid) / 2 if ask and bid else ask or bid
        else:
            price = 0.0
        prices[symbol] = price
        activity.logger.info(f"{symbol}: ${price:,.2f}")

    return prices


# ---------------------------------------------------------------------------
# Activity: Calculate rebalance orders
# ---------------------------------------------------------------------------

@activity.defn
def calculate_crypto_rebalance(
    snapshot: CryptoSnapshot,
    prices: dict,
    target_btc_pct: float,
    target_eth_pct: float,
    drift_threshold: float,
) -> CryptoRebalanceResult:
    """
    Pure calculation — no I/O, no side effects.
    Computes drift and generates notional orders for BTC/ETH rebalancing.
    Uses notional (dollar) amounts since crypto is fully fractional.
    """
    btc_current = snapshot.btc_weight
    eth_current = snapshot.eth_weight
    total_value = snapshot.total_value

    btc_drift = abs(btc_current - target_btc_pct)
    eth_drift = abs(eth_current - target_eth_pct)
    needs_rebalance = btc_drift > drift_threshold or eth_drift > drift_threshold

    activity.logger.info(
        f"BTC: {btc_current:.2%} → {target_btc_pct:.2%} (drift {btc_drift:.2%}) | "
        f"ETH: {eth_current:.2%} → {target_eth_pct:.2%} (drift {eth_drift:.2%}) | "
        f"rebalance={needs_rebalance}"
    )

    orders = []

    if needs_rebalance:
        btc_price = prices.get("BTC/USD", 0)
        eth_price = prices.get("ETH/USD", 0)

        if not btc_price or not eth_price:
            raise ApplicationError("Missing BTC or ETH price", non_retryable=True)

        for symbol, current_weight, target_weight, price in [
            ("BTC/USD", btc_current, target_btc_pct, btc_price),
            ("ETH/USD", eth_current, target_eth_pct, eth_price),
        ]:
            delta_notional = (target_weight - current_weight) * total_value
            if abs(delta_notional) < 1.0:
                continue

            orders.append({
                "symbol": symbol.replace("/", ""),  # Alpaca trading uses "BTCUSD"
                "side": "buy" if delta_notional > 0 else "sell",
                "notional": round(abs(delta_notional), 2),
                "reason": f"{symbol} drift {abs(current_weight - target_weight):.2%} exceeds {drift_threshold:.2%}",
            })

    return CryptoRebalanceResult(
        needs_rebalance=needs_rebalance,
        btc_drift=btc_drift,
        eth_drift=eth_drift,
        btc_current_weight=btc_current,
        eth_current_weight=eth_current,
        orders=orders,
    )


# ---------------------------------------------------------------------------
# Activity: Execute crypto orders
# ---------------------------------------------------------------------------

@activity.defn
def execute_crypto_orders(orders: list, base_url: str) -> list:
    """
    Place notional crypto orders via Alpaca.
    Crypto orders use 'notional' instead of 'qty' and GTC time_in_force
    since crypto markets never close.

    Heartbeats on each order so Temporal knows the activity is alive
    during execution — important for long-running order fills.
    """
    client = _trading_client(base_url)
    placed = []

    for order in orders:
        activity.heartbeat(f"placing {order['side']} {order['symbol']}")

        client_order_id = "temporal-crypto-{}-{}-{}".format(
            order["symbol"], order["side"],
            hashlib.sha256(str(order["notional"]).encode()).hexdigest()[:16]
        )

        request = MarketOrderRequest(
            symbol=order["symbol"],
            notional=order["notional"],
            side=OrderSide.BUY if order["side"] == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            client_order_id=client_order_id,
        )

        activity.logger.info(
            f"Placing {order['side']} ${order['notional']:,.2f} of {order['symbol']}"
        )

        try:
            result = client.submit_order(request)
            placed.append({
                **order,
                "status": "accepted",
                "alpaca_order_id": str(result.id),
                "alpaca_status": result.status.value if result.status else None,
            })
            activity.logger.info(f"Order accepted: {result.id}")
        except APIError as e:
            if e.status_code == 422:
                activity.logger.warning(f"Possible duplicate order (422): {e}")
                placed.append({**order, "status": "duplicate_or_invalid"})
            else:
                raise ApplicationError(
                    f"Order failed {order['symbol']}: {e}",
                    non_retryable=e.status_code == 403,
                )

    return placed


# ---------------------------------------------------------------------------
# Activity: Notification / audit
# ---------------------------------------------------------------------------

@activity.defn
def send_crypto_notification(result: dict) -> None:
    activity.logger.info(
        f"CRYPTO REBALANCE | cycle={result.get('cycle')} | "
        f"status={result.get('status')} | "
        f"orders={len(result.get('orders', []))} | "
        f"BTC drift={result.get('btc_drift', 0):.2%} | "
        f"ETH drift={result.get('eth_drift', 0):.2%} | "
        f"total rebalances={result.get('total_rebalances')}"
    )
    # Extend here: Slack webhook, database audit table, PagerDuty, etc.
