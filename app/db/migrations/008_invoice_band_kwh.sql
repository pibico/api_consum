-- 008: per-band energy (P1/P2/P3 kWh) as first-class invoices columns.
-- The list view is deliberately light (_SELECT_LIGHT, no breakdown JSONB) —
-- promoting the band split lets the table show "72 · 131 · 164" and kWh/day
-- without dragging the blob. Generated rows carry the split inside
-- breakdown.segments[].energy; uploaded rows get it from the AI amounts
-- extraction (backfilled in Python — the value lives in the bill text, not
-- in this JSON).

ALTER TABLE consum.invoices
  ADD COLUMN IF NOT EXISTS energy_p1_kwh NUMERIC(12,3),
  ADD COLUMN IF NOT EXISTS energy_p2_kwh NUMERIC(12,3),
  ADD COLUMN IF NOT EXISTS energy_p3_kwh NUMERIC(12,3);

-- Backfill GENERATED rows: sum each band over the frozen segments.
UPDATE consum.invoices SET
  energy_p1_kwh = sub.p1, energy_p2_kwh = sub.p2, energy_p3_kwh = sub.p3
FROM (
  SELECT id,
         SUM(COALESCE((s->'energy'->'P1'->>'kwh')::numeric, 0)) AS p1,
         SUM(COALESCE((s->'energy'->'P2'->>'kwh')::numeric, 0)) AS p2,
         SUM(COALESCE((s->'energy'->'P3'->>'kwh')::numeric, 0)) AS p3
  FROM consum.invoices, jsonb_array_elements(breakdown->'segments') AS s
  WHERE origin IS DISTINCT FROM 'uploaded'
  GROUP BY id
) sub
WHERE consum.invoices.id = sub.id AND consum.invoices.energy_p1_kwh IS NULL;
