-- 005_contract_components.sql — indexed price components on the contract.
--
-- Brings api_exo's indexed-tariff component model (PMD/SA/CR/DSV/PP/CC/IM/ATR/BS
-- per band) server-side onto the household's contract, so the indexed price is
-- costed with the real formula instead of the old flat margin+passthru:
--   Final = [(PMD + SA + CR + DSV) × (1 + PP) + CC] × (1 + IM) + ATR + BS
-- (PMD is live from OMIE; the rest live in `components`.) The legacy
-- margin_eur_kwh / passthru_* columns stay for back-compat; when `components`
-- is present it takes precedence (services/pricing.py). See
-- services/tariff_components.py for the model + BOE defaults.

ALTER TABLE consum.contracts ADD COLUMN IF NOT EXISTS components JSONB;

-- Indexed contracts may now carry `components` INSTEAD of the legacy margin:
-- relax the type check accordingly (margin OR components required).
ALTER TABLE consum.contracts DROP CONSTRAINT IF EXISTS contracts_check2;
ALTER TABLE consum.contracts ADD CONSTRAINT contracts_indexed_price_check
  CHECK (contract_type <> 'indexed' OR margin_eur_kwh IS NOT NULL OR components IS NOT NULL);
