-- 018_absence_periods.sql — declared absence/vacation windows per CUPS
-- (2026-07-30, spec 2026-07-30_absence_vacation_periods.md).
--
-- A member declares an away window (Europe/Madrid, explicit date+hour ->
-- date+hour) for ONE supply point (CUPS) so the forecast/advice/anomaly/bill
-- channels can treat the expected drop in consumption as EXPECTED rather than
-- a problem. Lives in api_consum only — this is the app-level declaration,
-- not a PLC/CM4 concept. Several absences per CUPS are allowed and MAY
-- overlap (unified at query time by app/services/absences.py, never in SQL —
-- keeps the "merge touching/overlapping intervals" rule in one testable
-- place instead of a window-function query).
--
-- Runs as superuser (TS_MIGRATE_USER); grants DML to api_consum.

CREATE TABLE consum.absence_periods (
  id              SERIAL PRIMARY KEY,
  supply_point_id INT NOT NULL REFERENCES consum.supply_points(id) ON DELETE CASCADE,
  starts_at       TIMESTAMPTZ NOT NULL,
  ends_at         TIMESTAMPTZ NOT NULL,
  label           TEXT,
  created_by      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT absence_periods_valid_range CHECK (ends_at > starts_at)
);

-- (supply_point_id, starts_at) — the index the spec asks for: every read
-- (absences_for/list_for) filters by supply_point_id and orders/bounds by
-- starts_at.
CREATE INDEX idx_absence_periods_sp_start
  ON consum.absence_periods (supply_point_id, starts_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.absence_periods TO api_consum;
GRANT USAGE, SELECT ON SEQUENCE consum.absence_periods_id_seq TO api_consum;
