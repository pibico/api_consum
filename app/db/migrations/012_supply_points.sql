-- 012_supply_points.sql — multi-CUPS Fase 3: puntos de suministro.
--
-- Un customer (slug) puede tener VARIAS instalaciones reales — cada una con su
-- PLC, su medidor de cabecera (sensors.role='main') y su CUPS/contrato. Esta
-- entidad las nombra y ancla: los lectores de consumo filtran por el sub-árbol
-- (main + descendientes por sensors.parent), los contratos se auto-vinculan
-- por match de CUPS, y solar_config pasa a admitir una config por punto.
-- Derivación: app/services/supply_points.py hace upsert perezoso de un punto
-- por cada role='main' del registro `sensors` (nube) al pedir /context.
-- Decisiones del dueño 2026-07-13; primer caso real: pibico casa + oficina.
--
-- Runs as superuser (TS_MIGRATE_USER); grants DML al rol api_consum.

CREATE TABLE consum.supply_points (
  id               SERIAL PRIMARY KEY,
  customer_id      UUID NOT NULL REFERENCES public.customers(customer_id) ON DELETE CASCADE,
  name             TEXT NOT NULL,
  cups             TEXT,
  main_sensor_key  TEXT NOT NULL,
  main_channel     TEXT NOT NULL DEFAULT '',
  gateway_hostname TEXT,
  location         TEXT,
  latitude         DOUBLE PRECISION,
  longitude        DOUBLE PRECISION,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (customer_id, main_sensor_key, main_channel)
);
CREATE INDEX idx_supply_points_customer ON consum.supply_points (customer_id);
-- Un CUPS pertenece a UN punto (cuando se conoce).
CREATE UNIQUE INDEX supply_points_cups_uniq
  ON consum.supply_points (cups) WHERE cups IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.supply_points TO api_consum;
GRANT USAGE, SELECT ON SEQUENCE consum.supply_points_id_seq TO api_consum;

-- contracts ↔ supply point + exclusión por punto (antes: 1 contrato activo por
-- customer; ahora: 1 por punto — NULL = punto "legado" único, clave 0).
ALTER TABLE consum.contracts
  ADD COLUMN supply_point_id INT REFERENCES consum.supply_points(id) ON DELETE SET NULL;
ALTER TABLE consum.contracts DROP CONSTRAINT contracts_no_overlap;
ALTER TABLE consum.contracts ADD CONSTRAINT contracts_no_overlap
  EXCLUDE USING gist (
    customer_id WITH =,
    COALESCE(supply_point_id, 0) WITH =,
    daterange(start_date, COALESCE(end_date, DATE '9999-12-31'), '[]') WITH &&
  );

-- solar_config por punto: la PK era customer_id (1 fila/hogar). Pasa a
-- surrogate + unicidad por (customer, punto); NULL = config del hogar entero
-- (compat con las filas existentes).
ALTER TABLE consum.solar_config DROP CONSTRAINT solar_config_pkey;
ALTER TABLE consum.solar_config ADD COLUMN id SERIAL PRIMARY KEY;
ALTER TABLE consum.solar_config
  ADD COLUMN supply_point_id INT REFERENCES consum.supply_points(id) ON DELETE CASCADE;
CREATE UNIQUE INDEX solar_config_customer_sp
  ON consum.solar_config (customer_id, COALESCE(supply_point_id, 0));
GRANT USAGE, SELECT ON SEQUENCE consum.solar_config_id_seq TO api_consum;
