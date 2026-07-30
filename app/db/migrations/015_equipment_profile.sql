-- 015_equipment_profile.sql — per-household/per-supply-point "comfort_flex"
-- profile: electric heating/cooling equipment + comfort bounds + flexible
-- loads. Stored as JSONB so it can hold the EXACT cross-team contract
-- unchanged (no further migration needed as that contract grows):
--
--   {"version": 1, "source": "plc"|"consum_fallback", "updated_at": "<ISO>",
--    "equipment": {"electric_heating": bool, "electric_cooling": bool,
--                  "electric_dhw": bool, "ev_charger": bool},
--    "comfort": {"temp_min_c": number|null, "temp_max_c": number|null},
--    "flexible_loads": [{"key","label","shiftable","allowed_windows","priority"}]}
--
-- Gates the OE3 forecast's SHAP attribution (app/services/oe3.py
-- forecast_explained) and the AdviceSkill's narration/tips on
-- equipment.electric_heating/electric_cooling: a household without electric
-- heating must NEVER have cold weather "explain" its consumption or be told
-- to "precalentar"/"usar la bomba de calor" (it may heat with gas); same
-- for electric cooling/HVAC (CDD, "aire acondicionado").
--
-- SOURCE OF TRUTH: the CM4 PLC's local-webui "Confort y flexibilidad"
-- capture + api_edge sync (separate workstream) is AUTHORITATIVE when a
-- household has a PLC — `source='plc'`, written via
-- PUT /consumption/comfort-flex/sync (service-key). The api_consum UI
-- toggle (`source='consum_fallback'`) is a FALLBACK/OVERRIDE only, for
-- invoice-only households with no PLC. Default (no row at all) → both
-- equipment flags false — conservative: never surface equipment we can't
-- confirm exists.
--
-- Same per-household / per-supply-point-override pattern as
-- consum.solar_config (mig 003/012) from day one (surrogate PK + unique on
-- (customer_id, COALESCE(supply_point_id,0))) — no two-step migration
-- needed here since supply_points already exists (mig 012).
--
-- Runs as superuser (TS_MIGRATE_USER); grants DML to api_consum.

CREATE TABLE consum.comfort_flex (
  id               SERIAL PRIMARY KEY,
  customer_id      UUID NOT NULL REFERENCES public.customers(customer_id) ON DELETE CASCADE,
  supply_point_id  INT REFERENCES consum.supply_points(id) ON DELETE CASCADE,
  source           TEXT NOT NULL DEFAULT 'consum_fallback' CHECK (source IN ('plc', 'consum_fallback')),
  payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by       TEXT
);

CREATE UNIQUE INDEX comfort_flex_customer_sp
  ON consum.comfort_flex (customer_id, COALESCE(supply_point_id, 0));

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.comfort_flex TO api_consum;
GRANT USAGE, SELECT ON SEQUENCE consum.comfort_flex_id_seq TO api_consum;
