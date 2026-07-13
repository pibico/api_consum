-- 011: per-turn inference metadata on the persisted chat (model, start time,
-- elapsed ms, tokens) — rendered under each assistant bubble in /ai and kept
-- across page reloads. NULL for user turns and legacy rows.

ALTER TABLE consum.ai_chat ADD COLUMN IF NOT EXISTS meta JSONB;
