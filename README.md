# Portfolio Rebalancer — Temporal.io + Alpaca

Two portfolio rebalancing workflows built on Temporal.io, both targeting a 60/40 allocation using the Alpaca brokerage API.

| Workflow | Assets | Execution model | Entry points |
|----------|--------|-----------------|--------------|
| SPY/TLT  | SPY + TLT | Single run, schedule-triggered | `portfolio_worker.py` / `portfolio_trigger.py` |
| Crypto   | BTC/USD + ETH/USD | Perpetual loop (`continue_as_new`) | `crypto_worker.py` / `crypto_trigger.py` |

---

## Workflow Diagrams

### SPY/TLT — `PortfolioRebalanceWorkflow`

```dot
digraph spytlt {
    rankdir=TB
    graph [fontname="Helvetica" fontsize=12 pad=0.5]
    node  [fontname="Helvetica" fontsize=11 shape=box style="rounded,filled" fillcolor=white]
    edge  [fontname="Helvetica" fontsize=10]

    scheduler    [label="Temporal Scheduler\n(Mon–Fri 7:05 AM PDT)" shape=cylinder fillcolor="#dbeafe"]
    alpaca_trade [label="Alpaca Trading API"                                           shape=cylinder fillcolor="#dbeafe"]
    alpaca_data  [label="Alpaca Data API (IEX feed)"                                   shape=cylinder fillcolor="#dbeafe"]

    subgraph cluster_signals {
        label="Signals" style=dashed color="#3b82f6"
        node [fillcolor="#eff6ff"]
        sig_force  [label="force_rebalance\n(bypass drift check)"]
        sig_config [label="update_config\n(new RebalanceConfig)"]
    }

    subgraph cluster_queries {
        label="Queries" style=dashed color="#16a34a"
        node [fillcolor="#f0fdf4"]
        q_status [label="get_status → { status,\nlast_result, force_pending }"]
    }

    subgraph cluster_wf {
        label="PortfolioRebalanceWorkflow  (task queue: portfolio-rebalancer)"
        style=rounded color="#6b7280"

        wf_start [label="run(RebalanceConfig)"                              shape=oval   fillcolor="#d1fae5"]
        act1     [label="get_portfolio_positions\n→ PortfolioSnapshot"                  fillcolor="#fef9c3"]
        act2     [label="get_market_prices\n→ { SPY: $x, TLT: $y }"                    fillcolor="#fef9c3"]
        act3     [label="calculate_rebalance_orders\n→ RebalanceResult"                 fillcolor="#fef9c3"]
        drift    [label="needs_rebalance\nOR force_rebalance?"             shape=diamond fillcolor="#fee2e2"]
        act4     [label="execute_orders\n(one activity call per order)"                 fillcolor="#fef9c3"]
        saga     [label="execute_orders\n(compensating — reverse side)"                 fillcolor="#fecaca" style="rounded,filled,dashed"]
        wf_sleep [label="workflow.sleep(5s)\n— durable timer"                          fillcolor="#ede9fe"]
        act4b    [label="get_portfolio_positions\n(post-trade snapshot)"                fillcolor="#fef9c3"]
        act5     [label="send_notification"                                             fillcolor="#fef9c3"]
        wf_skip  [label="Return: status=skipped"                           shape=oval   fillcolor="#d1fae5"]
        wf_done  [label="Return: RebalanceWorkflowResult"                  shape=oval   fillcolor="#d1fae5"]
    }

    scheduler -> wf_start
    wf_start  -> act1
    act1      -> act2
    act2      -> act3
    act3      -> drift
    drift     -> wf_skip  [label="no"]
    drift     -> act4     [label="yes"]
    act4      -> saga     [label="order N fails\nafter N-1 placed" style=dashed color="#dc2626"]
    act4      -> wf_sleep [label="all placed"]
    wf_sleep  -> act4b
    act4b     -> act5
    saga      -> act5
    act5      -> wf_done

    act1  -> alpaca_trade [style=dashed arrowhead=none color="#9ca3af"]
    act4  -> alpaca_trade [style=dashed arrowhead=none color="#9ca3af"]
    saga  -> alpaca_trade [style=dashed arrowhead=none color="#9ca3af"]
    act4b -> alpaca_trade [style=dashed arrowhead=none color="#9ca3af"]
    act2  -> alpaca_data  [style=dashed arrowhead=none color="#9ca3af"]

    sig_force  -> drift    [style=dashed color="#3b82f6"]
    sig_config -> wf_start [style=dashed color="#3b82f6"]
}
```

### Crypto — `CryptoRebalanceWorkflow` (perpetual)

```dot
digraph crypto {
    rankdir=TB
    graph [fontname="Helvetica" fontsize=12 pad=0.5]
    node  [fontname="Helvetica" fontsize=11 shape=box style="rounded,filled" fillcolor=white]
    edge  [fontname="Helvetica" fontsize=10]

    alpaca_trade [label="Alpaca Trading API"  shape=cylinder fillcolor="#dbeafe"]
    alpaca_data  [label="Alpaca Crypto Data"  shape=cylinder fillcolor="#dbeafe"]

    subgraph cluster_signals {
        label="Signals" style=dashed color="#3b82f6"
        node [fillcolor="#eff6ff"]
        sig_force    [label="force_rebalance\n(interrupts sleep)"]
        sig_pause    [label="pause / resume"]
        sig_interval [label="set_interval(hours)"]
        sig_config   [label="update_config\n(new CryptoRebalanceConfig)"]
    }

    subgraph cluster_queries {
        label="Queries" style=dashed color="#16a34a"
        node [fillcolor="#f0fdf4"]
        q_status [label="get_status → { status, paused,\ncycle_count, total_rebalances,\nlast_rebalance_time, ... }"]
    }

    subgraph cluster_wf {
        label="CryptoRebalanceWorkflow  (task queue: crypto-rebalancer)  —  perpetual loop"
        style=rounded color="#6b7280"

        wf_start   [label="run(CryptoRebalanceConfig,\nCryptoWorkflowState?)"         shape=oval   fillcolor="#d1fae5"]
        pause_chk  [label="paused?"                                                   shape=diamond fillcolor="#fee2e2"]
        pause_wait [label="wait_condition(!paused,\ntimeout=24h)"                                  fillcolor="#ede9fe"]
        act1       [label="get_crypto_positions\n(reads ALPACA_* from os.environ)"                fillcolor="#fef9c3"]
        act2       [label="get_crypto_prices\n→ { BTC/USD: $x, ETH/USD: $y }"                    fillcolor="#fef9c3"]
        act3       [label="calculate_crypto_rebalance\n→ CryptoRebalanceResult"                   fillcolor="#fef9c3"]
        drift      [label="needs_rebalance\nOR force_rebalance?"                     shape=diamond fillcolor="#fee2e2"]
        act4       [label="execute_crypto_orders\n(one activity call per order)"                   fillcolor="#fef9c3"]
        saga       [label="execute_crypto_orders\n(compensation)"                                  fillcolor="#fecaca" style="rounded,filled,dashed"]
        act5       [label="send_crypto_notification"                                               fillcolor="#fef9c3"]
        can_chk    [label="cycle_count >= 200?"                                      shape=diamond fillcolor="#fee2e2"]
        wf_can     [label="workflow.continue_as_new\n(config, CryptoWorkflowState)"  shape=oval   fillcolor="#fde68a"]
        wf_sleep   [label="wait_condition(force_rebalance,\ntimeout=interval_hours)\n— durable timer, interruptible" fillcolor="#ede9fe"]
        loop_back  [label="cycle_count += 1"                                          shape=oval   fillcolor="#d1fae5"]
    }

    wf_start   -> pause_chk
    pause_chk  -> pause_wait [label="yes"]
    pause_chk  -> act1       [label="no"]
    pause_wait -> act1       [label="resumed"]
    act1       -> act2
    act2       -> act3
    act3       -> drift
    drift      -> can_chk    [label="no"]
    drift      -> act4       [label="yes"]
    act4       -> saga       [label="order N fails\nafter N-1 placed" style=dashed color="#dc2626"]
    act4       -> act5       [label="all placed"]
    saga       -> act5
    act5       -> can_chk
    can_chk    -> wf_can     [label="yes — reset\nevent history"]
    can_chk    -> wf_sleep   [label="no"]
    wf_sleep   -> loop_back  [label="elapsed or\nforce_rebalance signal"]
    loop_back  -> pause_chk  [constraint=false]

    act1  -> alpaca_trade [style=dashed arrowhead=none color="#9ca3af"]
    act4  -> alpaca_trade [style=dashed arrowhead=none color="#9ca3af"]
    saga  -> alpaca_trade [style=dashed arrowhead=none color="#9ca3af"]
    act2  -> alpaca_data  [style=dashed arrowhead=none color="#9ca3af"]

    sig_force    -> wf_sleep  [style=dashed color="#3b82f6"]
    sig_pause    -> pause_chk [style=dashed color="#3b82f6"]
    sig_interval -> wf_sleep  [style=dashed color="#3b82f6"]
    sig_config   -> wf_start  [style=dashed color="#3b82f6"]
}
```

> Render with `dot -Tsvg <file.dot> -o diagram.svg` (Graphviz), or paste into
> [Graphviz Online](https://dreampuf.github.io/GraphvizOnline/).

---

## Setup

**Prerequisites:** Python 3.12+, an [Alpaca paper trading account](https://alpaca.markets), and a running Temporal server (`brew install temporal && temporal server start-dev` — UI at http://localhost:8080).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Required environment variables:**
```bash
export ALPACA_API_KEY=<your key>
export ALPACA_SECRET_KEY=<your secret>
export ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

---

## Running

### Workers

```bash
# SPY/TLT worker (local Temporal only)
python portfolio_worker.py

# Crypto worker (local or Temporal Cloud via mTLS)
python crypto_worker.py
```

`TEMPORAL_HOST` defaults to `localhost:7233`; `TEMPORAL_NAMESPACE` defaults to `default`.

For Temporal Cloud, set `TEMPORAL_TLS_CERT` and `TEMPORAL_TLS_KEY` before starting `crypto_worker.py`.

### Docker

```bash
docker-compose up                   # both workers (local Temporal)
docker-compose up crypto-worker     # crypto worker only
docker-compose --profile cloud up   # both workers against Temporal Cloud
```

Cloud profile additionally requires `TEMPORAL_CLOUD_HOST`, `TEMPORAL_CLOUD_NAMESPACE`, and a cert directory at `TEMPORAL_CERT_DIR` (containing `client.pem` + `client.key`).

---

## Client Commands

### SPY/TLT (`portfolio_trigger.py`)

```bash
python portfolio_trigger.py dry-run        # calculate orders but do not place them
python portfolio_trigger.py start          # single live run
python portfolio_trigger.py schedule         # create Temporal Schedule (Mon–Fri 7:05 AM PDT)
python portfolio_trigger.py delete-schedule  # delete the existing schedule
python portfolio_trigger.py signal-force     # send force_rebalance signal to running workflow
python portfolio_trigger.py query-status     # query get_status
```

### Crypto (`crypto_trigger.py`)

```bash
python crypto_trigger.py start              # start perpetual workflow
python crypto_trigger.py dry-run            # start in dry-run mode
python crypto_trigger.py force              # signal: rebalance now (skip sleep)
python crypto_trigger.py pause              # signal: pause cycling
python crypto_trigger.py resume             # signal: resume cycling
python crypto_trigger.py set-interval 1.5   # signal: change interval to 1.5 h
python crypto_trigger.py status             # query get_status
python crypto_trigger.py stop               # terminate workflow
```

---

## Rebalancing Logic

Both workflows share the same threshold-based logic, applied to their respective asset pairs:

```
target_a = target_a_pct × portfolio_value
target_b = target_b_pct × portfolio_value

drift_a = |current_a_weight - target_a_pct|
drift_b = |current_b_weight - target_b_pct|

if drift_a > drift_threshold OR drift_b > drift_threshold:
    order_a_notional = target_a - current_a_value   # positive = buy, negative = sell
    order_b_notional = target_b - current_b_value
    → place notional market orders for both symbols
```

Default config: 60% equity/BTC, 40% bond/ETH, 5% drift threshold.

Orders smaller than $1 notional are skipped (sub-dollar rounding noise).

### Saga / Compensation

Orders are placed sequentially (one activity per order). If order N fails after orders 0…N-1 have already been accepted, the workflow immediately places compensating orders (same symbol, reversed side) for all previously placed orders. If compensation also fails, the workflow logs `MANUAL INTERVENTION REQUIRED` and continues.

### Idempotency

`execute_orders` derives a deterministic `client_order_id` from a hash of the order parameters. Alpaca returns HTTP 422 for duplicate IDs; the activity treats this as "already placed" so Temporal retries are safe.

---

## Credentials and Temporal History

All activities read `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` directly from `os.environ` — credentials are never passed through workflow arguments or stored in Temporal's event history.

For local development, set them as plain environment variables. On Temporal Cloud, inject them as environment variables via Cloud's secret management so workers pick them up at runtime without the values ever touching workflow state.

---

## Task Queues and Workflow IDs

| | SPY/TLT | Crypto |
|---|---|---|
| Task queue | `portfolio-rebalancer` | `crypto-rebalancer` |
| Workflow ID (single run) | `portfolio-rebalancer-main-live` | `crypto-rebalancer-perpetual` |
| Schedule ID | `portfolio-rebalancer-daily` | — |
