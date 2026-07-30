-- 016_anomaly_notify.sql — anomaly-advice email loop closure (Phase 2 E4 ->
-- S-anomaly, 2026-07-30).
--
-- Two dedupe layers, mirroring the S7 forecast-advice email pattern
-- (mig 014) but keyed differently because an anomaly EVENT is a one-time
-- occurrence, not a daily cadence:
--
--   1. consum.notified_anomaly_events — EVENT-LEVEL idempotency: exactly one
--      row per (customer_id, anomaly_event_id) ever attempted. api_edge's
--      anomaly_events ALREADY carries a `notified` flag (mig 024, api_edge)
--      documented as "set by the api_consum poller", but the write endpoint
--      to set it doesn't exist yet (only POST /resolve) — adding one to
--      api_edge was deliberately avoided (out of scope: api_consum-only
--      task). This local ledger is the durable dedupe of record instead.
--      The SAME event keeps reappearing in api_edge's /anomalies lookback
--      window (ANOMALY_LOOKBACK_DAYS, default 14) across many 15-min poll
--      cycles — without a table keyed by the event id (not by day), a
--      per-day cap alone would let the identical event re-fire on the next
--      calendar day.
--   2. consum.sent_advice gets 'anomaly' added to its cadence CHECK — reused
--      AS-IS for the household-level cooldown/daily-cap (ADVICE_COOLDOWN_H /
--      ADVICE_DAILY_CAP via already_sent_today/last_sent_at), so a household
--      with several distinct anomalies in one day still gets at most one
--      advice email — same discipline as the daily/weekly forecast cadences.
--
-- Runs as superuser (TS_MIGRATE_USER); grants DML to api_consum.

CREATE TABLE consum.notified_anomaly_events (
  customer_id      UUID NOT NULL REFERENCES public.customers(customer_id) ON DELETE CASCADE,
  anomaly_event_id BIGINT NOT NULL,
  notified_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  ok               BOOLEAN NOT NULL DEFAULT true,
  PRIMARY KEY (customer_id, anomaly_event_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.notified_anomaly_events TO api_consum;

ALTER TABLE consum.sent_advice DROP CONSTRAINT sent_advice_cadence_check;
ALTER TABLE consum.sent_advice ADD CONSTRAINT sent_advice_cadence_check
  CHECK (cadence IN ('daily', 'weekly', 'anomaly'));
