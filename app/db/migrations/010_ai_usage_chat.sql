-- 010: AI usage accounting + persisted day chat.
-- ai_usage: one row per billable LLM interaction (org+user scoped) — the
-- ledger for pay-per-use and for the monthly credit cutoff.
-- ai_chat: the day's conversation per household+user, so the /ai page
-- restores it after navigation (client-side state was lost on page change).

CREATE TABLE IF NOT EXISTS consum.ai_usage (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
  customer_slug     TEXT NOT NULL,
  user_email        TEXT,
  kind              TEXT NOT NULL,            -- ask | narrative | ...
  model             TEXT,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  tool_hops         SMALLINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ai_usage_scope_ts ON consum.ai_usage (customer_slug, ts);

CREATE TABLE IF NOT EXISTS consum.ai_chat (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
  day           DATE NOT NULL DEFAULT CURRENT_DATE,
  customer_slug TEXT NOT NULL,
  user_email    TEXT,
  role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_chat_day ON consum.ai_chat (customer_slug, user_email, day, id);

GRANT SELECT, INSERT, UPDATE, DELETE ON consum.ai_usage, consum.ai_chat TO api_consum;
