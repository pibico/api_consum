-- 001_consum_schema.sql — consum schema + supply contracts (CONSUM-IA).
--
-- api_consum's business-domain schema inside pibiconnect_ts (5433), per
-- docs/consumia/02_data_architecture.md §1: same-DB schemas keep phase-2
-- readings→invoices a single SQL JOIN (sensor_hourly/daily + exogenous.series
-- + contracts). Runs as superuser (TS_MIGRATE_USER); the grants below hand
-- role api_consum DML on consum.* while it stays SELECT-only on public.*.
--
-- Tenancy is app-level (slugs authorized by ConsumContext → customer_id),
-- same discipline as every other query in the service — no RLS. The
-- one-active-contract-per-date rule IS enforced in the DB (exclusion
-- constraint survives concurrent writers and future invoicing jobs).

CREATE EXTENSION IF NOT EXISTS btree_gist;   -- uuid equality in gist exclusion

CREATE SCHEMA IF NOT EXISTS consum;
GRANT USAGE ON SCHEMA consum TO api_consum;

CREATE TABLE consum.contracts (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id         UUID NOT NULL REFERENCES public.customers(customer_id),
  contract_type       TEXT NOT NULL CHECK (contract_type IN ('pvpc','fixed','indexed')),
  label               TEXT,
  retailer            TEXT,                    -- comercializadora
  cups                TEXT,
  access_tariff       TEXT NOT NULL DEFAULT '2.0TD',
  start_date          DATE NOT NULL,
  end_date            DATE,                    -- NULL = vigente

  -- Energy term (fixed): P2/P3 NULL → fall back to P1 (single-price contracts)
  energy_p1_eur_kwh   NUMERIC(10,6),
  energy_p2_eur_kwh   NUMERIC(10,6),
  energy_p3_eur_kwh   NUMERIC(10,6),

  -- Energy term (indexed): raw OMIE + retailer margin [+ per-period
  -- pass-through peajes+cargos — raw OMIE excludes access tolls; 0 = pure fee]
  margin_eur_kwh      NUMERIC(10,6),
  passthru_p1_eur_kwh NUMERIC(10,6) NOT NULL DEFAULT 0,
  passthru_p2_eur_kwh NUMERIC(10,6) NOT NULL DEFAULT 0,
  passthru_p3_eur_kwh NUMERIC(10,6) NOT NULL DEFAULT 0,

  -- Power term (2.0TD: two contracted powers, billed €/kW/day)
  power_p1_kw         NUMERIC(6,3),
  power_p2_kw         NUMERIC(6,3),
  power_p1_eur_kw_day NUMERIC(10,6),
  power_p2_eur_kw_day NUMERIC(10,6),

  -- Fixed monthly charges
  meter_rental_eur_month NUMERIC(8,4) NOT NULL DEFAULT 0.81,
  other_fixed_eur_month  NUMERIC(8,4) NOT NULL DEFAULT 0,

  -- Taxes (per contract; defaults = current Spanish law)
  electricity_tax_pct NUMERIC(7,5) NOT NULL DEFAULT 5.11269,
  vat_pct             NUMERIC(5,2) NOT NULL DEFAULT 21.00,

  notes               TEXT,
  created_by          TEXT,                    -- ConsumContext user email (audit)
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  CHECK (end_date IS NULL OR end_date >= start_date),
  CHECK (contract_type <> 'fixed'   OR energy_p1_eur_kwh IS NOT NULL),
  CHECK (contract_type <> 'indexed' OR margin_eur_kwh IS NOT NULL),

  -- One active contract per customer per date; history preserved for invoicing
  CONSTRAINT contracts_no_overlap EXCLUDE USING gist (
    customer_id WITH =,
    daterange(start_date, COALESCE(end_date, DATE '9999-12-31'), '[]') WITH &&
  )
);

CREATE INDEX contracts_customer_idx ON consum.contracts (customer_id, start_date DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.contracts TO api_consum;
