CREATE TYPE martingale_subaccount_status AS ENUM ('clean', 'warned', 'eliminated');
CREATE TYPE martingale_elimination_action AS ENUM ('none', 'pending_review', 'auto_fired', 'manually_confirmed');
CREATE TYPE martingale_trigger_outcome AS ENUM ('grace_period', 'warning', 'eliminate_eligible');

-- Current lifecycle state, one row per subaccount.
CREATE TABLE martingale_subaccount_state (
    synthetic_hotkey       VARCHAR(96) PRIMARY KEY,          -- "{entity_hotkey}_{subaccount_id}"
    status                 martingale_subaccount_status NOT NULL DEFAULT 'clean',
    monitoring_started_ms  BIGINT NOT NULL,                  -- first time this subaccount was ever evaluated. Orders
                                                             -- before this are never evaluated and never get a row in
                                                             -- martingale_order_evaluations -- they're only read
                                                             -- (fresh, from Vanta) as background context for computing
                                                             -- the detection window of orders that ARE evaluated.
    elimination_action     martingale_elimination_action NOT NULL DEFAULT 'none',
    updated_at             TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX martingale_subaccount_state_status_idx ON martingale_subaccount_state (status);


-- Every order the service has evaluated (only orders at/after monitoring_started_ms; see above).
-- One row per order_uuid, written once.
CREATE TABLE martingale_order_evaluations (
    order_uuid             TEXT PRIMARY KEY,                -- Vanta order id; usually a UUID, but bracket
                                                            -- and flat-all orders append a suffix (e.g.
                                                            -- "<uuid>-bracket-0", "<uuid>_flat_all"), so TEXT.
    synthetic_hotkey       VARCHAR(96) NOT NULL REFERENCES martingale_subaccount_state(synthetic_hotkey) ON DELETE RESTRICT,
    pair_id                TEXT NOT NULL,                  -- trade_pair_id, e.g. "EURUSD"
    order_processed_ms     BIGINT NOT NULL,                 -- this order's own timestamp
    triggering             BOOLEAN NOT NULL DEFAULT false,  -- is this a triggering order (completed a >=chain_length escalating chain)?
    outcome                martingale_trigger_outcome,      -- set only when triggering; decided ONCE, at evaluation
                                                             -- time, from subaccount_state.status as it stood at that
                                                             -- moment -- never recomputed later from timestamp order.
    chain                  JSONB,                           -- [{order_uuid, exposure, price, pnl}, ...] only when triggering.
                                                             -- exposure = net position size AFTER the order; pnl =
                                                             -- window-cumulative pnl right BEFORE the order executes.
    detector_params        JSONB,                           -- {escalation_factor, floor_fraction, chain_length,
                                                             --  floor_mode, window_days, grace_period_days}; set
                                                             --  only when triggering
    created_at             TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX martingale_order_evaluations_hotkey_idx     ON martingale_order_evaluations (synthetic_hotkey, order_processed_ms);
CREATE INDEX martingale_order_evaluations_triggering_idx ON martingale_order_evaluations (synthetic_hotkey) WHERE triggering;
