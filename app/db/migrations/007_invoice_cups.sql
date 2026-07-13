-- 007: CUPS as a first-class invoices column.
-- An invoice is a frozen document: its CUPS must be the one that applied at
-- close/upload time, NOT resolved from the currently-active contract (wrong
-- after a supply change). The value already exists inside `breakdown`
-- (generated: segments[i].contract.cups snapshot; uploaded: extracted.cups) —
-- promote it so the light list query (_SELECT_LIGHT, no JSONB) can show it.
-- Also the landing point for supply_points v2 (one invoice per CUPS).

ALTER TABLE consum.invoices ADD COLUMN IF NOT EXISTS cups TEXT;

-- Backfill existing rows from the frozen breakdown:
--   uploaded rows: breakdown.extracted.cups
--   generated rows: cups of the LAST segment with a contract snapshot
--                   (the contract covering period_end)
UPDATE consum.invoices SET cups = COALESCE(
  breakdown->'extracted'->>'cups',
  (SELECT s->'contract'->>'cups'
     FROM jsonb_array_elements(breakdown->'segments') AS s
    WHERE s->'contract'->>'cups' IS NOT NULL
    ORDER BY s->>'end' DESC LIMIT 1)
) WHERE cups IS NULL;
