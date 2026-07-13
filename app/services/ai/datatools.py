"""Data tools for AIDA's agentic Q&A (/ai/ask) — the LLM asks, we fetch.

Each tool is HOUSEHOLD-SCOPED (slugs come from the caller's ConsumContext) and
returns a compact JSON-able dict. The 2026-07-13 mandate supersedes the old
aggregates-only ADR: AIDA may now see per-sensor series (with their PLC names)
and the household's OWN invoice contents — never another tenant's anything.
Emails are redacted from invoice text before it leaves (see _redact).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from app.core import db
from app.services import consumption, contracts, invoices

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _redact(text: str) -> str:
    return _EMAIL.sub("[email]", text or "")


def _iso(v: Any) -> Optional[str]:
    s = str(v or "")[:10]
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        return None


# Native (ollama/openai-style) tool schemas — the gateway forwards `tools`
# verbatim and gpt-oss answers cleanly once results arrive as role='tool'.
def _fn(name: str, desc: str, props: Dict[str, Any] | None = None,
        required: List[str] | None = None) -> Dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {},
                       "required": required or []}}}


NATIVE_TOOLS: List[Dict[str, Any]] = [
    _fn("list_sensors", "Aparatos/sensores del hogar (id y nombre). Úsala para resolver 'el router', 'la nevera'… a su id."),
    _fn("energy", "kWh consumidos por día u hora; sin device = toda la casa. Máx 92 días (day) / 3 días (hour).",
        {"device": {"type": "string", "description": "id del sensor (opcional)"},
         "start": {"type": "string", "description": "YYYY-MM-DD"},
         "end": {"type": "string", "description": "YYYY-MM-DD"},
         "bucket": {"type": "string", "enum": ["day", "hour"]}},
        ["start", "end"]),
    _fn("power_now", "Potencia AHORA de cada aparato (W) y total de la casa."),
    _fn("peak", "Pico de potencia por hora de UN día concreto.",
        {"date": {"type": "string", "description": "YYYY-MM-DD"},
         "device": {"type": "string"}}, ["date"]),
    _fn("peaks", "Picos de potencia por DÍA en un rango (máx 31 días): máximo, hora, y minutos por encima de un umbral (p.ej. la potencia contratada). Úsala para preguntas de potencia contratada/ICP.",
        {"start": {"type": "string", "description": "YYYY-MM-DD"},
         "end": {"type": "string", "description": "YYYY-MM-DD"},
         "threshold_w": {"type": "number", "description": "umbral en W, p.ej. 4400"},
         "device": {"type": "string"}}, ["start", "end"]),
    _fn("invoices_list", "Todas las facturas del hogar: id, periodo, CUPS, kWh por franjas, total €."),
    _fn("invoice", "Detalle de UNA factura: totales y desglose por conceptos.",
        {"id": {"type": "integer"}}, ["id"]),
    _fn("invoice_search", "Busca texto dentro de los documentos de las facturas y devuelve fragmentos.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("billing_period", "El periodo de facturación ABIERTO: acumulado, franjas, estimado a cierre."),
    _fn("tariff", "La tarifa/contrato vigente del hogar."),
]


TOOL_SPECS = """
HERRAMIENTAS DISPONIBLES (responde SOLO el JSON {"tool": ..., "args": {...}} para usarlas):
- list_sensors {} → los aparatos/sensores del hogar (id y nombre). Úsala para resolver "el router", "la nevera"… a su id.
- energy {"device": opcional id, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "bucket": "day"|"hour"} → kWh por día u hora; sin device = toda la casa. Máx 92 días (day) / 3 días (hour).
- power_now {} → potencia actual de cada aparato (W) y total.
- peak {"date": "YYYY-MM-DD", "device": opcional} → pico de potencia por hora de ese día.
- invoices_list {} → todas las facturas del hogar (id, periodo, días, CUPS, kWh por franjas, total €).
- invoice {"id": N} → detalle de una factura: totales y desglose por conceptos (energía, potencia, peajes, impuestos…).
- invoice_search {"query": "texto"} → busca ese texto dentro de los documentos de las facturas y devuelve fragmentos.
- billing_period {} → el periodo de facturación ABIERTO: acumulado, franjas, estimado a cierre.
- tariff {} → la tarifa/contrato vigente del hogar.
""".strip()


async def run_tool(slugs: Sequence[str], name: str,
                   args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one tool, household-scoped. Returns {"error": ...} on misuse —
    the model reads it and corrects itself."""
    args = args or {}
    try:
        if name == "list_sensors":
            rows = await consumption.sensor_devices(slugs)
            return {"sensors": [{"id": r["id"], "name": r["name"]} for r in rows]}

        if name == "energy":
            start, end = _iso(args.get("start")), _iso(args.get("end"))
            if not start or not end or end < start:
                return {"error": "start/end deben ser YYYY-MM-DD y start <= end"}
            bucket = "hour" if args.get("bucket") == "hour" else "day"
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days
            if bucket == "day" and span > 92:
                return {"error": "máximo 92 días con bucket=day"}
            if bucket == "hour" and span > 3:
                return {"error": "máximo 3 días con bucket=hour"}
            rows = await consumption.energy_series(
                slugs, start, end, bucket=bucket,
                device=args.get("device") or None)
            total = round(sum(r["kwh"] for r in rows), 2)
            out = {"bucket": bucket, "total_kwh": total,
                   "series": [{"ts": r["ts"][:13 if bucket == "hour" else 10],
                               "kwh": r["kwh"]} for r in rows]}
            if not rows:
                out["nota"] = ("sin mediciones en ese rango (el aparato no "
                               "reportó o aún no estaba dado de alta) — dilo "
                               "claramente al usuario")
            return out

        if name == "power_now":
            cur = await consumption.current_power(slugs)
            names = {r["id"]: r["name"] for r in await consumption.sensor_devices(slugs)}
            return {"total_w": cur.get("total_w"),
                    "devices": [{"name": names.get(d["device"], d["device"]),
                                 "power_w": d["power_w"]}
                                for d in (cur.get("devices") or [])]}

        if name == "peak":
            d = _iso(args.get("date"))
            if not d:
                return {"error": "date debe ser YYYY-MM-DD"}
            rows = await consumption.power_peak_hourly(
                slugs, d, device=args.get("device") or None)
            return {"date": d, "peaks": rows}

        if name == "peaks":
            start, end = _iso(args.get("start")), _iso(args.get("end"))
            if not start or not end or end < start:
                return {"error": "start/end deben ser YYYY-MM-DD y start <= end"}
            if (date.fromisoformat(end) - date.fromisoformat(start)).days > 31:
                return {"error": "máximo 31 días"}
            thr = args.get("threshold_w")
            thr = float(thr) if thr else None
            ids = await consumption._slugs_to_ids(slugs)
            dev_cond = "AND device_id = %s" if args.get("device") else \
                "AND channel ~ '^[0-9]+$'"
            params: List[Any] = [list(ids.values()), start, end]
            if args.get("device"):
                params.append(args["device"])
            async with db.raw_connection() as con:
                async with con.cursor() as cur:
                    # Daily peak + when it happened (whole-house = mains EM).
                    await cur.execute(f"""
                        WITH t AS (
                          SELECT time_bucket('1 day', ts) AS d, ts, value_num
                          FROM sensor_data
                          WHERE variable='apower' AND customer_id::text = ANY(%s)
                            AND ts >= %s::timestamptz
                            AND ts < (%s::timestamptz + interval '1 day')
                            {dev_cond}
                        )
                        SELECT DISTINCT ON (d) d::date, value_num,
                               to_char(ts, 'HH24:MI') AS at
                        FROM t ORDER BY d, value_num DESC
                    """, params)
                    days = [{"date": str(r[0]), "max_w": round(float(r[1] or 0), 1),
                             "at": r[2]} for r in await cur.fetchall()]
                    if thr:
                        # Approx minutes above the threshold per day: samples
                        # over × spacing (samples arrive ~every 30-60 s).
                        await cur.execute(f"""
                            SELECT time_bucket('1 day', ts)::date AS d,
                                   count(*) FILTER (WHERE value_num > %s) AS over,
                                   count(*) AS total,
                                   86400.0 / GREATEST(count(*), 1) AS spacing_s
                            FROM sensor_data
                            WHERE variable='apower' AND customer_id::text = ANY(%s)
                              AND ts >= %s::timestamptz
                              AND ts < (%s::timestamptz + interval '1 day')
                              {dev_cond}
                            GROUP BY 1
                        """, [thr] + params)
                        mins = {str(r[0]): round(float(r[1]) * float(r[3]) / 60.0, 1)
                                for r in await cur.fetchall()}
                        for d in days:
                            d["min_sobre_umbral"] = mins.get(d["date"], 0.0)
            out: Dict[str, Any] = {"days": days}
            if thr:
                out["threshold_w"] = thr
                out["nota"] = ("minutos aproximados (muestreo ~30-60 s). El pico "
                               "es instantáneo: en doméstico el ICP corta solo si "
                               "el exceso se SOSTIENE; picos breves por encima de "
                               "la potencia contratada pueden pasar sin corte, "
                               "pero indican que el margen es justo")
            if not days:
                out["nota"] = "sin mediciones de potencia en ese rango"
            return out

        if name == "invoices_list":
            rows = await invoices.list_for(slugs)
            return {"invoices": [{
                "id": r["id"], "periodo": f"{r['period_start']} a {r['period_end']}",
                "cups": r.get("cups"), "origen": r.get("origin") or "generada",
                "estado": r.get("status"),
                "kwh_franjas": {"caras": r.get("energy_p1_kwh"),
                                "normales": r.get("energy_p2_kwh"),
                                "baratas": r.get("energy_p3_kwh")},
                "total_eur": r.get("total_eur")} for r in rows]}

        if name == "invoice":
            inv = await invoices.get(int(args.get("id") or 0))
            if not inv:
                return {"error": "factura no encontrada"}
            ids = await consumption._slugs_to_ids(slugs)
            if inv["customer_id"] not in {str(v) for v in ids.values()}:
                return {"error": "esa factura no es de este hogar"}
            bd = inv.get("breakdown") or {}
            return {"id": inv["id"],
                    "periodo": f"{inv['period_start']} a {inv['period_end']}",
                    "cups": inv.get("cups"), "total_eur": inv.get("total_eur"),
                    "energia_kwh": inv.get("energy_kwh"),
                    "conceptos": bd.get("ai_amounts"),
                    "kwh_franjas": {"caras": inv.get("energy_p1_kwh"),
                                    "normales": inv.get("energy_p2_kwh"),
                                    "baratas": inv.get("energy_p3_kwh")}}

        if name == "invoice_search":
            query = str(args.get("query") or "").strip()
            if len(query) < 3:
                return {"error": "query demasiado corta"}
            ids = await consumption._slugs_to_ids(slugs)
            hits = []
            async with db.raw_connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(
                        "SELECT id, period_start, period_end, "
                        "breakdown->>'markdown' FROM consum.invoices "
                        "WHERE customer_id::text = ANY(%s) AND status != 'void' "
                        "AND breakdown->>'markdown' ILIKE %s "
                        "ORDER BY period_start DESC LIMIT 5",
                        (list(ids.values()), f"%{query}%"))
                    for iid, ps, pe, md in await cur.fetchall():
                        md = md or ""
                        i = md.lower().find(query.lower())
                        frag = md[max(i - 120, 0):i + 180]
                        hits.append({"invoice_id": iid,
                                     "periodo": f"{ps} a {pe}",
                                     "fragmento": _redact(frag)})
            return {"hits": hits}

        if name == "billing_period":
            return await invoices.billing_period_live(sorted(slugs)[0])

        if name == "tariff":
            ids = await consumption._slugs_to_ids(slugs)
            if not ids:
                return {"error": "hogar sin registrar"}
            row = await contracts.get_active(str(sorted(ids.values())[0]),
                                             date.today().isoformat())
            if not row:
                return {"error": "sin contrato vigente"}
            keep = ("retailer", "label", "contract_type", "access_tariff",
                    "cups", "energy_p1_eur_kwh", "energy_p2_eur_kwh",
                    "energy_p3_eur_kwh", "margin_eur_kwh", "power_p1_kw",
                    "power_p2_kw", "power_p1_eur_kw_day", "power_p2_eur_kw_day",
                    "vat_pct", "electricity_tax_pct", "start_date")
            out = {k: row.get(k) for k in keep}
            # Retailer CO2 factor — the bill's IMPACTO MEDIOAMBIENTAL section
            # states it ("Dióxido de Carbono g/kWh Media nacional 102
            # <Retailer> 0"). A 0 g/kWh retailer (renewable with GdO) means
            # THIS home's consumption emits 0; the ENTSO-E grid intensity in
            # the aggregate context then only describes the network.
            async with db.raw_connection() as con:
                async with con.cursor() as cur:
                    await cur.execute(
                        "SELECT substring(breakdown->>'markdown' FROM "
                        "'Di[oó]xido de Carbono[^\\n]*') FROM consum.invoices "
                        "WHERE customer_id::text = ANY(%s) AND status != 'void' "
                        "AND breakdown->>'markdown' ~* 'Di[oó]xido de Carbono' "
                        "ORDER BY period_end DESC LIMIT 1",
                        ([str(v) for v in ids.values()],))
                    got = await cur.fetchone()
            line = (got[0] if got else None) or ""
            nums = re.findall(r"\d+(?:[.,]\d+)?", line)
            if len(nums) >= 2:      # first = national average, last = retailer
                retail = float(nums[-1].replace(",", "."))
                out["co2_comercializadora_gkwh"] = retail
                out["co2_red_media_gkwh"] = float(nums[0].replace(",", "."))
                out["origen_renovable"] = retail == 0.0
                if retail == 0.0:
                    out["co2_consumo"] = (
                        "0 gCO2/kWh — la comercializadora vende energía "
                        "certificada renovable según sus propias facturas")
            else:
                out["origen_renovable"] = None      # bills don't state it
            return out

        return {"error": f"herramienta desconocida: {name}"}
    except Exception as exc:                       # noqa: BLE001
        return {"error": f"fallo ejecutando {name}: {exc}"}
