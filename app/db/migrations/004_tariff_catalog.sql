-- 004_tariff_catalog.sql — reusable market tariff catalog for CONSUM-IA.
--
-- A catalog of retailer × product tariffs (PVPC + the popular fixed/indexed
-- products). It lets a household pick "my company / my product" and get the
-- pricing components WITHOUT understanding electricity — and it is the right
-- home for the REGULATED components (2.0TD peajes/cargos from the BOE) that
-- must NOT be guessed from a signed contract PDF. Populated by admins, either
-- by hand or from the AI ContractSkill extraction (always human-reviewed).
--
-- Runs as superuser (TS_MIGRATE_USER); the grant gives role api_consum SELECT
-- (the selector, any member) + full DML (admin endpoints populate/edit).

CREATE TABLE consum.tariff_catalog (
  id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  retailer               TEXT NOT NULL,
  product_name           TEXT NOT NULL,
  contract_type          TEXT NOT NULL DEFAULT 'fixed'
                           CHECK (contract_type IN ('pvpc', 'fixed', 'indexed')),
  access_tariff          TEXT NOT NULL DEFAULT '2.0TD',

  -- Fixed energy prices (fixed products) — €/kWh
  energy_p1_eur_kwh      NUMERIC(10,6),
  energy_p2_eur_kwh      NUMERIC(10,6),
  energy_p3_eur_kwh      NUMERIC(10,6),
  -- Indexed: commercialization margin + per-period access tolls (peajes+cargos)
  margin_eur_kwh         NUMERIC(10,6),
  passthru_p1_eur_kwh    NUMERIC(10,6),
  passthru_p2_eur_kwh    NUMERIC(10,6),
  passthru_p3_eur_kwh    NUMERIC(10,6),
  -- Power term €/kW·day
  power_p1_eur_kw_day    NUMERIC(10,6),
  power_p2_eur_kw_day    NUMERIC(10,6),
  -- Indexed component matrix (api_exo parity): {SA:{P1,P2,P3}, CC:{…}, ATR:{…}…}
  -- Formula: Final = [(PMD+SA+CR+DSV)·(1+PP)+CC]·(1+IM)+ATR+BS (services/tariff_components.py)
  components             JSONB,
  -- Fixed charges + taxes (regulated defaults)
  meter_rental_eur_month NUMERIC(8,4)  DEFAULT 0.81,
  electricity_tax_pct    NUMERIC(7,5)  DEFAULT 5.11269,
  vat_pct                NUMERIC(5,2)  DEFAULT 21,

  source_url             TEXT,          -- where the values came from (tariff sheet)
  valid_from             DATE,          -- values effective from
  confidence             NUMERIC(3,2),  -- 0..1 if AI-extracted
  notes                  TEXT,
  active                 BOOLEAN NOT NULL DEFAULT true,
  updated_by             TEXT,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (retailer, product_name, access_tariff)
);

CREATE INDEX tariff_catalog_retailer_idx
  ON consum.tariff_catalog (retailer) WHERE active;

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.tariff_catalog TO api_consum;
