-- 006_uploaded_invoices.sql — archive the user's UPLOADED (retailer) invoices.
--
-- The Facturas page is the single user-facing entry point (2026-07-12 design):
-- uploading your real bill both configures the tariff AND must keep the bill.
-- Uploaded bills live in the same consum.invoices table so the list is ONE:
--   origin='uploaded', status='uploaded' — outside the closed-period EXCLUDE
--   constraint (WHERE status='closed'), so they never collide with generated
--   settlement invoices. The PDF itself is stored inline (bytea — pilot scale).

ALTER TABLE consum.invoices DROP CONSTRAINT invoices_status_check;
ALTER TABLE consum.invoices ADD CONSTRAINT invoices_status_check
  CHECK (status IN ('closed', 'void', 'uploaded'));
ALTER TABLE consum.invoices ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL
  DEFAULT 'generated' CHECK (origin IN ('generated', 'uploaded'));
ALTER TABLE consum.invoices ADD COLUMN IF NOT EXISTS pdf BYTEA;
ALTER TABLE consum.invoices ADD COLUMN IF NOT EXISTS pdf_filename TEXT;
