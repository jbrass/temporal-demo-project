# Portfolio Rebalancer — Temporal.io + Alpaca

Two portfolio rebalancing workflows built on Temporal.io, both targeting a 60/40 allocation using the Alpaca brokerage API.

| Workflow | Assets | Execution model | Entry points |
|----------|--------|-----------------|--------------|
| SPY/TLT  | SPY + TLT | Single run, schedule-triggered | `portfolio_worker.py` / `portfolio_trigger.py` |
| Crypto   | BTC/USD + ETH/USD | Perpetual loop (`continue_as_new`) | `crypto_worker.py` / `crypto_trigger.py` |

---

## Workflow Diagrams

### SPY/TLT — `PortfolioRebalanceWorkflow`

```mermaid
flowchart TD
    scheduler[("Temporal Scheduler<br/>Mon–Fri 7:05 AM PDT")]
    alpaca_trade[("Alpaca Trading API")]
    alpaca_data[("Alpaca Data API — IEX")]

    subgraph signals["Signals"]
        sig_force["force_rebalance<br/>(bypass drift check)"]
        sig_config["update_config<br/>(new RebalanceConfig)"]
    end

    subgraph queries["Queries"]
        q_status["get_status → {status, last_result, force_pending}"]
    end

    subgraph wf["PortfolioRebalanceWorkflow · task queue: portfolio-rebalancer"]
        wf_start(["run(RebalanceConfig)"])
        act1["get_portfolio_positions<br/>→ PortfolioSnapshot"]
        act2["get_market_prices<br/>→ {SPY: $x, TLT: $y}"]
        act3["calculate_rebalance_orders<br/>→ RebalanceResult"]
        drift{"needs_rebalance OR force_rebalance?"}
        act4["execute_orders<br/>(one activity per order)"]
        saga["execute_orders — compensation<br/>(reverse side)"]
        wf_sleep["workflow.sleep(5s)<br/>durable timer"]
        act4b["get_portfolio_positions<br/>(post-trade snapshot)"]
        act5[send_notification]
        wf_skip(["Return: status = skipped"])
        wf_done(["Return: RebalanceWorkflowResult"])
    end

    scheduler --> wf_start
    wf_start --> act1 --> act2 --> act3 --> drift
    drift -->|no| wf_skip
    drift -->|yes| act4
    act4 -.->|"order N fails after N-1 placed"| saga
    act4 -->|all placed| wf_sleep --> act4b --> act5 --> wf_done
    saga --> act5

    act1 & act4 & saga & act4b -.-> alpaca_trade
    act2 -.-> alpaca_data

    sig_force -.-> drift
    sig_config -.-> wf_start

    classDef act fill:#fef9c3,stroke:#ca8a04
    classDef dec fill:#fee2e2,stroke:#dc2626
    classDef term fill:#d1fae5,stroke:#16a34a
    classDef tmr fill:#ede9fe,stroke:#7c3aed
    classDef comp fill:#fecaca,stroke:#dc2626,stroke-dasharray:5 5
    classDef ext fill:#dbeafe,stroke:#3b82f6
    classDef sig fill:#eff6ff,stroke:#3b82f6
    classDef qry fill:#f0fdf4,stroke:#16a34a

    class act1,act2,act3,act4,act4b,act5 act
    class drift dec
    class wf_start,wf_skip,wf_done term
    class wf_sleep tmr
    class saga comp
    class scheduler,alpaca_trade,alpaca_data ext
    class sig_force,sig_config sig
    class q_status qry
```

### Crypto — `CryptoRebalanceWorkflow` (perpetual)

```mermaid
flowchart TD
    alpaca_trade[("Alpaca Trading API")]
    alpaca_data[("Alpaca Crypto Data")]

    subgraph signals["Signals"]
        sig_force["force_rebalance<br/>(interrupts sleep)"]
        sig_pause[pause / resume]
        sig_interval["set_interval(hours)"]
        sig_config["update_config<br/>(new CryptoRebalanceConfig)"]
    end

    subgraph queries["Queries"]
        q_status["get_status → {status, paused,<br/>cycle_count, total_rebalances, last_rebalance_time, ...}"]
    end

    subgraph wf["CryptoRebalanceWorkflow · task queue: crypto-rebalancer · perpetual loop"]
        wf_start(["run(CryptoRebalanceConfig, CryptoWorkflowState?)"])
        pause_chk{"paused?"}
        pause_wait["wait_condition(not paused)<br/>timeout = 24 h"]
        act1["get_crypto_positions<br/>(reads ALPACA_* from env)"]
        act2["get_crypto_prices<br/>→ {BTC/USD: $x, ETH/USD: $y}"]
        act3["calculate_crypto_rebalance<br/>→ CryptoRebalanceResult"]
        drift{"needs_rebalance OR force_rebalance?"}
        act4["execute_crypto_orders<br/>(one activity per order)"]
        saga["execute_crypto_orders<br/>(compensation)"]
        act5[send_crypto_notification]
        can_chk{"cycle_count >= 200?"}
        wf_can(["workflow.continue_as_new<br/>(config, CryptoWorkflowState)"])
        wf_sleep["wait_condition(force_rebalance)<br/>timeout = interval_hours<br/>durable timer — interruptible"]
        loop_back(["cycle_count += 1"])
    end

    wf_start --> pause_chk
    pause_chk -->|yes| pause_wait -->|resumed| act1
    pause_chk -->|no| act1
    act1 --> act2 --> act3 --> drift
    drift -->|no| can_chk
    drift -->|yes| act4
    act4 -.->|"order N fails after N-1 placed"| saga
    act4 -->|all placed| act5
    saga --> act5 --> can_chk
    can_chk -->|"yes — reset event history"| wf_can
    can_chk -->|no| wf_sleep
    wf_sleep -->|"elapsed or force_rebalance"| loop_back
    loop_back --> pause_chk

    act1 & act4 & saga -.-> alpaca_trade
    act2 -.-> alpaca_data

    sig_force -.-> wf_sleep
    sig_pause -.-> pause_chk
    sig_interval -.-> wf_sleep
    sig_config -.-> wf_start

    classDef act fill:#fef9c3,stroke:#ca8a04
    classDef dec fill:#fee2e2,stroke:#dc2626
    classDef term fill:#d1fae5,stroke:#16a34a
    classDef tmr fill:#ede9fe,stroke:#7c3aed
    classDef comp fill:#fecaca,stroke:#dc2626,stroke-dasharray:5 5
    classDef ext fill:#dbeafe,stroke:#3b82f6
    classDef sig fill:#eff6ff,stroke:#3b82f6
    classDef qry fill:#f0fdf4,stroke:#16a34a
    classDef can fill:#fde68a,stroke:#d97706

    class act1,act2,act3,act4,act5 act
    class pause_chk,drift,can_chk dec
    class wf_start,loop_back term
    class wf_sleep,pause_wait tmr
    class saga comp
    class alpaca_trade,alpaca_data ext
    class sig_force,sig_pause,sig_interval,sig_config sig
    class q_status qry
    class wf_can can
```

---

## Setup

**Prerequisites:** Python 3.12+, an [Alpaca paper trading account](https://alpaca.markets), and a running Temporal server (`brew install temporal && temporal server start-dev` — UI at http://localhost:8233).

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
