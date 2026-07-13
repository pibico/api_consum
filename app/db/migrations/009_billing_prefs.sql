-- 009: per-household billing preferences — the USER-SET expected close of the
-- open billing period. The "factura en curso" KPI estimates the close from the
-- median bill length; households usually KNOW their meter-reading day, so an
-- explicit date beats the estimate. One row per customer; a stale next_close
-- (before the current open period) is ignored and the estimate returns.

CREATE TABLE IF NOT EXISTS consum.billing_prefs (
  customer_id UUID PRIMARY KEY REFERENCES public.customers(customer_id),
  next_close  DATE,
  updated_by  TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.billing_prefs TO api_consum;
