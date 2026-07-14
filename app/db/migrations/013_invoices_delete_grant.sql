-- 013: DELETE grant on consum.invoices for the app role.
-- Wrongly UPLOADED retailer bills (status='uploaded') are hard-deletable
-- self-service (DELETE /invoices/{id}); closed statements keep the
-- void-never-delete rule. The grant was applied live on 2026-07-15; this
-- migration makes it reproducible on fresh deploys.
GRANT DELETE ON consum.invoices TO api_consum;
