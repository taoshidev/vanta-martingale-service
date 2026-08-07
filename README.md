# vanta-martingale-service

This service detects martingale behavior, broadly defined as repeatedly and substantially increasing size while losing, on Vanta subaccounts and records warnings and elimination candidates with relevant reporting data for human review. The service only observes and
records; it never eliminates on its own.

## How detection works

For each trade pair, the service analyzes the subaccount's orders in chronological order and detects
repeated and relatively large increases in position size while the pair continues to lose.

1. **Losing stretch.** We track the realized and unrealized profit and loss for all positions active
   for any duration during the detection window. A
   *losing stretch* is a run of consecutive orders placed while that P&L is below zero; it ends
   the moment P&L returns to break-even or better.
2. **Escalating ladder.** Inside one losing stretch, look for orders whose net position size
   keeps growing, each at least `escalation_factor`× the size of an earlier order in the
   ladder. The orders need not be consecutive. Size is measured in quantity (lots), not
   leverage, so the escalation stays visible even as the price falls. Orders smaller than
   `floor_fraction` of the pair's typical maximum size are ignored.
3. **Trigger.** When the ladder reaches `chain_length` orders, the order that completes it is a
   **trigger**.
4. **Window.** Only orders within the last `lookback_days` are included in detection.

### What does not trigger

By design, ordinary trading does not look like the behavior described above. Below are examples that would not be flagged:

- A losing trade that is simply held or closed since no escalation position size is occurring.
- Re-entering at a normal or steady size wouldn't increase the ladder orders to `chain_length`.
- Positions below the floor are ignored.
- Growing a position while *winning* since only losing stretches count.

### Warning, then elimination

1. The **first** trigger after monitoring begins marks the subaccount **warned**. A warning is
   permanent.
2. A **later** trigger, on an order placed at least `grace_period_days` after the warning, marks
   it **eliminate-eligible** (`elimination_action = 'pending_review'`). Triggers inside the
   grace window are recorded but do not escalate.
3. Elimination is always a human decision since subaccounts are never eliminated automatically.

### Parameters

`martingale_service/config.py` is the source of truth; the current values are:

| parameter | value | meaning                                                       |
|---|---|---------------------------------------------------------------|
| `escalation_factor` | 1.5 | each ladder order must be ≥ this times an earlier order       |
| `chain_length` | 4 | ladder orders needed to trigger a warning or elimination      |
| `floor_fraction` | 0.03 | orders below this fraction of the pair's max size are ignored |
| `lookback_days` | 10 | length of the trailing detection window                       |
| `grace_period_days` | 3 | minimum gap from the warning to an eliminate-eligible trigger |

## Guarantees

- **Not Retroactive.** Historical orders will not be used for detection. Detection begins once the service goes live.
- **Evidence is kept.** A trigger stores the flagged orders along with relevant metadata. The record will
  not be retroactively changed by any order corrections.

## Architecture

Two inputs feed one worker, which writes to Postgres:

    /miner-positions   (full snapshot, every SWEEP_INTERVAL_SECONDS) ─┐
                                                                      ├─► evaluator ─► Postgres
    websocket          (one message per subaccount trade, optional) ──┘

- **Reconcile sweep — the source of truth.** Each cycle downloads the full position snapshot
  and evaluates every order not yet in the database (compared by `order_uuid`). This path alone
  is complete and correct: if anything else fails, the next sweep catches up. It also refreshes
  the per-pair catalog (max leverage, lot size) from `/trade-pairs`.
- **Websocket doorbell — an optional speed-up.** The validator broadcasts a message whenever a
  subaccount trades; the service then fetches just that account's orders and evaluates
  immediately, cutting latency from up to one sweep interval down to seconds. If the websocket
  drops or is disabled (`VANTA_WS_URL` unset), nothing is missed — only reaction time grows back
  to the sweep interval.
- **One evaluator.** Both inputs hand work to a single worker, so a subaccount's orders are
  evaluated one at a time, in order, with no races. Every write is keyed on `order_uuid` and
  applied once, so any retry after a failure is safe.

Only subaccounts are monitored (synthetic hotkeys, `{entity_hotkey}_{subaccount_id}`); entity
miners and regular miners are out of scope.

## Setup

Requires Python 3.11+ and a Postgres database.

    python3 -m venv venv
    venv/bin/pip install -r requirements.txt

Configuration comes from environment variables, with a git-ignored `secrets.json` next to the
code as a fallback for local runs (copy `secrets-example.json`; the environment wins):

| variable | required | meaning |
|---|---|---|
| `VANTA_REST_BASE_URL` | yes | validator REST endpoint |
| `VANTA_API_KEY` | yes | API key with subaccount read access |
| `DATABASE_URL` | yes | Postgres DSN |
| `VANTA_WS_URL` | no | validator websocket endpoint; unset = doorbell off |
| `SWEEP_INTERVAL_SECONDS` | no | reconcile cadence (default 60) |

This is a public repository: real endpoints, keys, and DSNs must only ever live in the
environment or in the git-ignored `secrets.json`, never in code or committed files.

### Database

In production the two tables are created through the Vanta UI project's migration flow from
`schema.sql` — this service never runs DDL.

## Tests

    venv/bin/pytest

Pure-logic tests always run. The state-machine tests need a throwaway local database and skip
without it:

    createdb martingale_test
    TEST_DATABASE_URL=postgresql://localhost/martingale_test venv/bin/pytest

The DB-backed tests drop and recreate this service's tables on every test, so point
`TEST_DATABASE_URL` at a dedicated scratch database.

## Local test run

Create the tables, point `secrets.json` at a local database, and run the service:

    createdb martingale_dev
    psql -d martingale_dev -f schema.sql          # create the two tables
    cp secrets-example.json secrets.json          # then fill in the real values
    venv/bin/python -m martingale_service

The service only records: it writes state and evaluation rows and logs triggers, and nothing
external happens on its own (warnings become visible only when a UI reads these tables, and
eliminations require a human to act on `pending_review` rows). On a fresh database every
subaccount is registered `clean` and monitored from now, so the first sweep logs no triggers —
expected, because the service never judges orders placed before monitoring started; triggers
appear only for orders placed while it runs. Inspect the tables:

    psql -d martingale_dev -c "SELECT status, count(*) FROM martingale_subaccount_state GROUP BY status;"

To start over from scratch, empty both tables:

    TRUNCATE martingale_order_evaluations, martingale_subaccount_state;

## When the UI goes live: wipe and restart

The warnings this service records are not shown to anyone until the UI displays them. On the
day the UI goes live, empty both tables so monitoring restarts from that moment:

    TRUNCATE martingale_order_evaluations, martingale_subaccount_state;

Otherwise an account could be marked eliminate-eligible off a warning the user never saw — its
`warned` state and grace-period clock would carry over from before anyone was watching. Wiping
resets everyone to `clean`, so the first warning users can actually see is the one that starts
the clock.

## License

MIT © 2026 Taoshi Inc
