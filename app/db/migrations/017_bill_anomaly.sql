-- 017_bill_anomaly.sql — bill-level anomaly channel loop closure (Phase 2.5,
-- 2026-07-30, spec 2026-07-28_phase2_5_spec_bill_anomaly_channel.md, B4).
--
-- Dedupe mirrors migration 016's anomaly_events pattern EXACTLY, but keyed
-- by INVOICE id instead of an api_edge anomaly_events id — a bill anomaly's
-- "event" is the invoice import itself (one-shot: fired at import time, not
-- rediscovered on a polling loop), so unlike 016's anomaly_events (which
-- keeps reappearing in api_edge's lookback window across many poll cycles)
-- a bill only needs ONE durable per-invoice check:
--
--   1. consum.notified_bill_invoices — INVOICE-LEVEL idempotency: exactly
--      one row per (customer_id, invoice_id) ever attempted. Chosen over
--      "just don't re-insert the same invoice" because store_uploaded()'s
--      own (file-hash / CUPS+period) duplicate guard already prevents a
--      second ROW for the same bill — this ledger instead guards against
--      re-running the CHECK on the same invoice_id from a retried
--      background task, and is the literal, auditable "have we ever
--      notified about invoice N" record the spec asks for.
--   2. consum.sent_advice gets 'bill' added to its cadence CHECK — reused
--      AS-IS for the household-level cooldown/daily-cap (ADVICE_COOLDOWN_H
--      / ADVICE_DAILY_CAP via already_sent_today/last_sent_at/cooldown_ok),
--      exactly like the 'anomaly' cadence added in 016 — a household with
--      several distinct bill/power anomalies in one day still gets at most
--      one advice email overall (shared budget across ALL advice kinds).
--
-- Runs as superuser (TS_MIGRATE_USER); grants DML to api_consum.

CREATE TABLE consum.notified_bill_invoices (
  customer_id UUID NOT NULL REFERENCES public.customers(customer_id) ON DELETE CASCADE,
  invoice_id  BIGINT NOT NULL REFERENCES consum.invoices(id) ON DELETE CASCADE,
  notified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ok          BOOLEAN NOT NULL DEFAULT true,
  PRIMARY KEY (customer_id, invoice_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.notified_bill_invoices TO api_consum;

ALTER TABLE consum.sent_advice DROP CONSTRAINT sent_advice_cadence_check;
ALTER TABLE consum.sent_advice ADD CONSTRAINT sent_advice_cadence_check
  CHECK (cadence IN ('daily', 'weekly', 'anomaly', 'bill'));
