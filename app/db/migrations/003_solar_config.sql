-- 003_solar_config.sql — per-household rooftop PV configuration for CONSUM-IA.
--
-- The Panel "Sol en tu tejado" card estimates production from api_exo's GHI
-- forecast scaled by the installation's peak power (kWp). Until now that was
-- pinned to a meaningless 3 kWp default, so the estimate said nothing about a
-- real home. One row per household; an absent row keeps api_exo's 3 kWp default.
--
-- tilt/azimuth/loss are kept for a future orientation editor but stay NULL for
-- now (→ api_exo defaults: 30° tilt, 180° south, 14% loss). Runs as superuser
-- (TS_MIGRATE_USER); the grant hands role api_consum full DML — a household can
-- set, change or clear its own PV config.

CREATE TABLE consum.solar_config (
  customer_id   UUID PRIMARY KEY REFERENCES public.customers(customer_id),
  peak_kwp      NUMERIC(6,2) NOT NULL DEFAULT 3.0
                  CHECK (peak_kwp >= 0 AND peak_kwp <= 100),
  tilt          NUMERIC(5,2) CHECK (tilt >= 0 AND tilt <= 90),
  azimuth       NUMERIC(6,2) CHECK (azimuth >= 0 AND azimuth <= 360),
  loss          NUMERIC(5,2) CHECK (loss >= 0 AND loss <= 50),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by    TEXT
);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.solar_config TO api_consum;
