-- 002_invoices.sql — closed billing periods (facturas) for CONSUM-IA.
--
-- Phase 2 of the business domain (contracts → invoices → PDF). An invoice is
-- a FROZEN settlement document: the full breakdown (including a snapshot of
-- the contract(s) that applied) lives in `breakdown` JSONB, so a closed
-- invoice survives later edits/deletes of the underlying contract. Totals are
-- promoted to columns for cheap listing/aggregation.
--
-- Settlement is HOURLY (settlement='hourly'): domestic retailers bill the
-- hourly meter curve at the hourly quarter-mean price. 'quarter' is reserved
-- for future >50 kW / tipo 1-3 supply points that settle 15-min for real.
--
-- Runs as superuser (TS_MIGRATE_USER); grants give role api_consum
-- INSERT/UPDATE (void) but NEVER DELETE — a closed period is auditable history.

CREATE TABLE consum.invoices (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id       UUID NOT NULL REFERENCES public.customers(customer_id),
  period_start      DATE NOT NULL,
  period_end        DATE NOT NULL,
  status            TEXT NOT NULL DEFAULT 'closed'
                      CHECK (status IN ('closed', 'void')),
  settlement        TEXT NOT NULL DEFAULT 'hourly'
                      CHECK (settlement IN ('hourly', 'quarter')),

  -- Promoted totals (the frozen source of truth is `breakdown`)
  energy_kwh        NUMERIC(12,3) NOT NULL DEFAULT 0,
  energy_eur        NUMERIC(12,2) NOT NULL DEFAULT 0,
  power_eur         NUMERIC(12,2) NOT NULL DEFAULT 0,
  fixed_eur         NUMERIC(12,2) NOT NULL DEFAULT 0,
  iee_eur           NUMERIC(12,2) NOT NULL DEFAULT 0,
  vat_eur           NUMERIC(12,2) NOT NULL DEFAULT 0,
  total_eur         NUMERIC(12,2) NOT NULL DEFAULT 0,
  uncosted_kwh      NUMERIC(12,3) NOT NULL DEFAULT 0,

  -- Frozen document: {segments:[{contract snapshot, bill_breakdown, ...}], ...}
  breakdown         JSONB NOT NULL,

  created_by        TEXT,                       -- ConsumContext user email
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  voided_at         TIMESTAMPTZ,
  voided_by         TEXT,

  CHECK (period_end >= period_start),

  -- No two CLOSED invoices may cover overlapping periods for one customer;
  -- a voided invoice frees its period so it can be re-closed.
  CONSTRAINT invoices_no_overlap EXCLUDE USING gist (
    customer_id WITH =,
    daterange(period_start, period_end, '[]') WITH &&
  ) WHERE (status = 'closed')
);

CREATE INDEX invoices_customer_idx ON consum.invoices (customer_id, period_start DESC);

GRANT SELECT, INSERT, UPDATE ON consum.invoices TO api_consum;
