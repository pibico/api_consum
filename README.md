# api_consum — CONSUM-IA product (BFF + frontend)

Member-facing, multi-tenant product of the **CONSUM-IA** R&D project: public landing,
login and household energy app served at **https://consum.pibico.es** (dedicated nginx
vhost → uvicorn `127.0.0.1:8183`, `ROOT_PATH` empty). Supervisor program: `api_consum`.
Venv: `/home/erpnext/api_consum_env`. The edge counterpart runs on the household PLC
(repo mirror: `../cm4-consumia/`).

## Pages

| Route | What |
|---|---|
| `/` | Public landing |
| `/login` | OTP login via api_auth (shared `.pibico.es` SSO cookie) |
| `/guia` | User guide |
| `/app` | Dashboard (consumption today, PVPC, solar, OE3 insights) |
| `/app/consumption` | Series, per-sensor detail, Sankey topology (PLC `appliances.yaml`) |
| `/app/savings` | Savings insights |
| `/app/contract` | Contract CRUD, tariff catalog, bill estimate, AI extraction from PDF/photo |
| `/app/invoices` | Billing periods, closed/uploaded invoices, PDF, explain, anomaly |
| `/app/ai` | AIDA — agentic AI advisor chat |
| `/app/plc` | SSO handoff to the household PLC's local web UI (via reverse tunnel) |

Frontend: Jinja templates (`templates/`) + vanilla JS (`static/`), pibico-guidelines.
Gotchas: use the local `cfetch` (plain `App.apiFetch` logs out on 403); never
`toISOString()` for "today" (UTC shift).

## Data

- **Shared DB**: PostgreSQL 16 + TimescaleDB `pibiconnect_ts` on `:5433`. api_consum
  owns the **`consum` schema**; reads `public` (api_edge: `sensor_data`,
  `sensor_hourly` cagg, `customers`, `sensors`) with app-level tenancy (no RLS).
- **Local SQLite** `consum.db` — family-notify subscriber state + OE3 history.
- **Migrations** (`app/db/migrations/`, run `python -m app.db.migrate`; ledger table
  `_migrations` is SHARED with api_edge/api_exo — never reuse their filenames):

| # | File | Adds |
|---|---|---|
| 001 | `001_consum_schema.sql` | `consum` schema, contracts |
| 002 | `002_invoices.sql` | `consum.invoices` (frozen `breakdown` JSONB, void never delete) |
| 003 | `003_solar_config.sql` | Per-household installed kWp |
| 004 | `004_tariff_catalog.sql` | Retailer/tariff catalog |
| 005 | `005_contract_components.sql` | Indexed-tariff `components` JSONB on contracts |
| 006 | `006_uploaded_invoices.sql` | Uploaded real invoices |
| 007 | `007_invoice_cups.sql` | CUPS on invoices |
| 008 | `008_invoice_band_kwh.sql` | Per-band kWh on invoices |
| 009 | `009_billing_prefs.sql` | Billing-period preferences |
| 010 | `010_ai_usage_chat.sql` | `consum.ai_usage` (billable-LLM ledger) + `consum.ai_chat` |

## Main API (`/api/v1`)

- `health`, `events` (edge-event feed)
- `auth/*` — `request-otp`, `verify-otp`, `logout`, `validate` (proxied to api_auth; org/tier RBAC)
- `consumption/*` — `context`, `current`, `topology`, `series`, `power-peak`, `summary`,
  `sensors`, `devices`, `environment`. Hybrid reads: recent = raw `sensor_data`,
  old = `sensor_hourly` cagg; whole-house totals use EM numeric channels only.
- `savings/insights`
- `contracts/*` — CRUD, `active`, `bill-estimate` (pricing.py dispatcher: pvpc/fixed/indexed),
  `extract` (AI, self-service), `components-base` (SSOT lives in api_exo), `catalog/*`
- `invoices/*` — list, `billing-period`, `upload`, `close`, `void`, `pdf` (WeasyPrint),
  `explain`, `explain/pdf`, `ask`, `anomaly`, `amounts`. Internal pricing is 15-min but
  **settlement is hourly** (`hourly_rollup`).
- `ai/*` — `status`, `narrative` (aggregates-only, cached per scope·day), **`ask`
  (AGENTIC)**: LLM with native tools (`app/services/ai/datatools.py`, ≤5 tool hops)
  over the household's OWN sensors/invoices, `chat` (server-side transcript).
  Gates: `require_ai` tier, `AI_ASK_DAILY_LIMIT` per org·day, **monthly token credits
  `AI_MONTHLY_TOKEN_CAP`** — enforced against the `consum.ai_usage` ledger, which is
  also the pay-per-use billing source. Skills layer in `app/services/ai/`
  (registry: contract, invoice).
- `plc/*` — `session`, `auth`, `ensure` (device-scoped session for the PLC web UI)

## Config (`.env` — names only, no values here)

`PORT`, `ROOT_PATH`, `MQTT_BROKER/PORT/USER/PASS/EVENT_TOPIC` (family-notify),
`AUTH_BASE_URL`, `API_KEY`, `ADMIN_API_KEY`, `CORS_ORIGINS`, `TS_DB_HOST/PORT/NAME/USER/PASSWORD`,
`EXO_BASE_URL/EXO_API_KEY`, `EDGE_BASE_URL/EDGE_API_KEY`,
`CHAT_BASE_URL/CHAT_API_KEY/CHAT_PROVIDER/CHAT_MODEL` (LLM gateway `api.pibico.es/chat`,
native tools forwarded to ollama), `CONVERT_BASE_URL/CONVERT_API_KEY` (docling).
`AI_MONTHLY_TOKEN_CAP` / `AI_ASK_DAILY_LIMIT` have code defaults in `app/core/config.py`.

## Ops

```bash
sudo supervisorctl restart api_consum
tail -f logs/api_consum.log logs/api_consum.err
source /home/erpnext/api_consum_env/bin/activate && python -m app.db.migrate
curl https://consum.pibico.es/api/v1/health
```

Plans in `../plans/api_consum_plans/`; architecture handoff:
`../plans/api_consum_plans/00_CONSUMIA_architecture_handoff.md`.
