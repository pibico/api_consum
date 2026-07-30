-- 014_advice_prefs.sql — explainable forecast + advice email (Phase 1, S7/S8).
--
-- Minimal consent + dedupe for the daily/weekly advice email
-- (workers/advice_scheduler.py): one row per household opt-in (recipient =
-- the api_auth user's email, captured at toggle time — no live org lookup
-- needed at send time), and one row per SENT email (cooldown + daily-cap).
--
-- Runs as superuser (TS_MIGRATE_USER); grants DML to role api_consum.

CREATE TABLE consum.advice_prefs (
  customer_id     UUID PRIMARY KEY REFERENCES public.customers(customer_id) ON DELETE CASCADE,
  opt_in          BOOLEAN NOT NULL DEFAULT false,
  recipient_email TEXT,
  lang            TEXT NOT NULL DEFAULT 'es',
  region          TEXT,           -- ISO 3166-2 subdivision (S4 behaviour holidays); NULL = national-only
  updated_by      TEXT,           -- ConsumContext user email (audit)
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.advice_prefs TO api_consum;

-- One row per SENT advice email — the cooldown/daily-cap dedupe ledger.
-- UNIQUE(customer_id, cadence, sent_date) IS the daily cap for ADVICE_DAILY_CAP=1;
-- a higher cap would need a counting query instead (not needed today).
CREATE TABLE consum.sent_advice (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id UUID NOT NULL REFERENCES public.customers(customer_id) ON DELETE CASCADE,
  cadence     TEXT NOT NULL CHECK (cadence IN ('daily', 'weekly')),
  sent_date   DATE NOT NULL,
  sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ok          BOOLEAN NOT NULL DEFAULT true,
  UNIQUE (customer_id, cadence, sent_date)
);
CREATE INDEX idx_sent_advice_customer ON consum.sent_advice (customer_id, sent_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.sent_advice TO api_consum;
GRANT USAGE, SELECT ON SEQUENCE consum.sent_advice_id_seq TO api_consum;
