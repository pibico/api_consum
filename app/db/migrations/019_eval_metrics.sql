-- 019_eval_metrics.sql — accuracy evaluation harness (2026-07-30, spec
-- plans/api_consum_plans/2026-07-30_eval_harness.md).
--
-- Pure OBSERVABILITY: a time series of model-accuracy metrics computed from
-- data ALREADY persisted elsewhere (virtual_readings/sensor_hourly in
-- public, consum.invoices, exogenous.series) — this migration only adds the
-- place to WRITE those computed numbers. No forecasts are re-persisted here,
-- no user-facing behaviour changes; it exists so a human can watch MAPE/WAPE
-- trend down over time and decide, with actual numbers, when to flip
-- NILM_ENABLED / keep EMAIL_ADVICE_ENABLED on.
--
-- One row per (metric_key, scope, slug, computed_at) snapshot — append-only,
-- never updated/deleted, so the full history survives (the trend IS the
-- product). `horizon` is a general-purpose secondary split dimension re-used
-- per metric: price_accuracy could use it for a forecast lead time (today it
-- is always 'last_forecast_before_realization' — see eval_harness.py's
-- price_accuracy docstring for why true D+1/D+2-7 isn't reconstructable from
-- exogenous.series' current upsert-only persistence); nilm_accuracy reuses
-- it to carry the archetype name (its natural "by-X" split, e.g.
-- 'frigorifico'); expected_accuracy/bill_accuracy leave it NULL.
--
-- Runs as superuser (TS_MIGRATE_USER); grants SELECT/INSERT to api_consum —
-- deliberately NO UPDATE/DELETE, this ledger is append-only audit history.

CREATE TABLE consum.eval_metrics (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  metric_key    TEXT NOT NULL,                 -- 'expected_accuracy'|'nilm_accuracy'|'bill_accuracy'|'price_accuracy'
  scope         TEXT NOT NULL CHECK (scope IN ('fleet', 'household')),
  slug          TEXT,                          -- NULL for scope='fleet'
  window_from   TIMESTAMPTZ NOT NULL,
  window_to     TIMESTAMPTZ NOT NULL,
  horizon       TEXT,                          -- metric-specific secondary split (see above); NULL when n/a
  value         DOUBLE PRECISION,               -- the metric's headline number (MAPE, %); NULL on cold-start (n=0)
  unit          TEXT NOT NULL,                  -- 'pct_mape' | 'pct_kwh' | 'pct_eur' ...
  n             INT NOT NULL DEFAULT 0,          -- sample size backing `value`; 0 = cold-start, never an error
  meta          JSONB NOT NULL DEFAULT '{}'::jsonb  -- {wape, bias, ...metric-specific notes}
);

CREATE INDEX idx_eval_metrics_key_time ON consum.eval_metrics (metric_key, computed_at DESC);
CREATE INDEX idx_eval_metrics_slug ON consum.eval_metrics (slug, metric_key, computed_at DESC)
  WHERE slug IS NOT NULL;

GRANT SELECT, INSERT ON consum.eval_metrics TO api_consum;
