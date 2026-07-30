"""Advice email scheduler (S7) — explainable forecast + advice, two cadences.

api_consum had NO scheduler before Phase 1 (unlike api_exo's APScheduler
jobs) — this is the first. Mirrors `notify_sub.start()`'s lifespan pattern:
started once from `main.py`'s lifespan, degrades to a no-op log line if
misconfigured, never blocks app startup.

  - Cadence A (daily, ~21:00 Europe/Madrid — AFTER api_exo's 20:20
    prices_tomorrow warehouse job): tomorrow's forecast, real D+1 price.
  - Cadence B (weekly, Monday 08:00): 7-day kWh forecast, € real for day 1
    only (S3 placeholder for days 2-7).

Per household: forecast_explained (S2) -> AdviceSkill.narrate (S6) ->
cooldown/daily-cap dedupe (advice_prefs.py, migration 014) -> mailer.send_email
(S1) -> mark_sent. Best-effort per household — one failing household must
never abort the batch.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services import advice_prefs, mailer, oe3
from app.services.ai.advice import AdviceSkill

logger = logging.getLogger("consum.advice_scheduler")

_advice_skill = AdviceSkill()
_scheduler = None  # apscheduler.schedulers.asyncio.AsyncIOScheduler, set in start()


def _aggregate_week(forecasts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Additive weekly rollup: total_week = base_week + sum(phi_week) holds
    EXACTLY because each day's total already equals base_day + sum(phi_day)
    (efficiency property, S2) and both sides just sum over the 7 days."""
    total_kwh = sum(f["kwh"] for f in forecasts)
    band_lo = sum(f["band_lo"] for f in forecasts)
    band_hi = sum(f["band_hi"] for f in forecasts)
    base_sum = 0.0
    feat_phi: Dict[str, float] = {}
    feat_label: Dict[str, str] = {}
    for f in forecasts:
        base_sum += f["attribution"]["base"]
        for c in f["attribution"]["contributions"]:
            feat_phi[c["feature"]] = feat_phi.get(c["feature"], 0.0) + c["phi"]
            feat_label.setdefault(c["feature"], c["label"])
    contributions = sorted(
        [{"feature": k, "label": feat_label.get(k, k), "phi": round(v, 2),
          "x": None, "x_bar": None, "unit": "kWh"} for k, v in feat_phi.items()],
        key=lambda c: -abs(c["phi"]))
    day1 = forecasts[0]
    # WS-EXO-PRICE (2026-07-29): day 1 is REAL D+1 PVPC; days 2-7 now carry an
    # ESTIMATED eur (method=official|proxy|climatology, see price_status) once
    # api_exo's forecast is available — sum whatever is populated so the
    # weekly total isn't silently capped at day 1's price alone. Falls back
    # to day-1-only (old behaviour) if every later day is still "pending"
    # (api_exo cold-start / both providers down).
    eur_days = [f.get("eur") for f in forecasts if f.get("eur") is not None]
    week_eur = round(sum(eur_days), 2) if eur_days else None
    # Absence prior (2026-07-30): only frame the WHOLE week as vacation-mode
    # when EVERY day is (fully or partially) inside a declared absence — a
    # 2-of-7-day trip must not make the entire weekly forecast read as
    # "away" (effect strictly bounded to the actual window). Per-day flags
    # still ride along in periods_by_day for the 7-day strip regardless.
    all_away = all(f.get("ausencia") for f in forecasts)
    week_ausencia = day1.get("ausencia") if all_away else None
    return {
        "kwh": round(total_kwh, 1), "band_lo": round(band_lo, 1), "band_hi": round(band_hi, 1),
        "eur": week_eur,
        "attribution": {"base": round(base_sum, 2), "contributions": contributions,
                        "total": round(base_sum + sum(feat_phi.values()), 2)},
        # Explicit from->to for the WHOLE week (owner requirement 2026-07-28) —
        # day1's period_from through the last day's period_to.
        "period_from": day1["period_from"], "period_to": forecasts[-1]["period_to"],
        "period_label": f"{day1['period_label'].split(' → ')[0]} → {forecasts[-1]['period_label'].split(' → ')[1]}",
        "cheap_window": day1.get("cheap_window"),
        "ausencia": week_ausencia,
        "periods_by_day": [{"date": f["date"], "periods": f["periods"],
                           "price_status": f["price_status"],
                           "period_from": f["period_from"], "period_to": f["period_to"],
                           "period_label": f["period_label"],
                           "ausencia": f.get("ausencia")} for f in forecasts],
    }


def _cheap_window_text(cw: Dict[str, Any], lang: str) -> str:
    if lang == "en":
        return (f"The cheapest window tomorrow is {cw['label']} "
                f"(~{cw['avg_price_eur_kwh']:.4f} €/kWh) — shift deferrable loads there.")
    return (f"La franja más barata de mañana es de {cw['label']} "
           f"(~{cw['avg_price_eur_kwh']:.4f} €/kWh) — adelanta ahí los consumos aplazables.")


def _email_context(entry: Dict[str, Any], narrative, alert: Optional[Dict[str, Any]],
                   cadence: str, lang: str) -> Dict[str, Any]:
    """Assemble the flat context contract S1/api_auth's `advice` template
    expects: {title, period_label, period_from, period_to, summary, forecast,
    drivers, advice, alert, cta_url}. `period_label`/`period_from`/`period_to`
    are the MANDATORY explicit date+hour range this forecast covers (owner
    requirement 2026-07-28) — never a vague "tomorrow"/"next week" label."""
    fc = entry["attribution"]
    title = narrative.summary.split(".")[0][:120] or \
        ("Tu previsión" if lang != "en" else "Your forecast")
    drivers = [{"label": c["label"],
               "effect": f"{c['phi']:+.2f} kWh",
               "detail": None}
              for c in fc["contributions"][:3]]
    alert_block = None
    if alert:
        body_key = "body_en" if lang == "en" else "body_es"
        adv_key = "energy_advisory_en" if lang == "en" else "energy_advisory_es"
        alert_block = {
            "severity": alert.get("severity"),
            "phenomenon": alert.get("phenomenon"),
            "body": (alert.get(body_key) or alert.get("body_es") or "") + " " +
                   (alert.get(adv_key) or alert.get("energy_advisory_es") or ""),
        }
    base = settings.PUBLIC_BASE_URL or ""
    advice_tips = list(narrative.advice)
    cw = entry.get("cheap_window")
    if cw:
        # Deterministic, GUARANTEED-present tip (the LLM's own tips are prose
        # and may or may not spell out the exact hour range) — always leads.
        advice_tips.insert(0, _cheap_window_text(cw, lang))
    return {
        "title": title,
        "period_label": entry["period_label"],
        "period_from": entry["period_from"],
        "period_to": entry["period_to"],
        "summary": narrative.summary,
        "forecast": {"kwh": entry["kwh"], "eur": entry.get("eur"),
                    "band_lo": entry["band_lo"], "band_hi": entry["band_hi"]},
        "drivers": drivers,
        "advice": [{"text": a} for a in advice_tips],
        "alert": alert_block,
        "anomaly": None,
        "cta_url": (base.rstrip("/") + "/app/consumption") if base else None,
        # Weekly cadence only: per-day explicit ranges (S3 "period structure
        # known even where the € isn't yet") — daily cadence omits this key.
        "days": entry.get("periods_by_day"),
        # Absence prior (2026-07-30): None unless the whole forecast window
        # is inside a declared absence — see oe3.forecast_explained /
        # _aggregate_week. Consumed by the UI (badge) and by AdviceSkill's
        # deterministic vacation-mode bypass (narrate()).
        "ausencia": entry.get("ausencia"),
    }


async def _build_entry(slug: str, cadence: str, region: Optional[str] = None,
                       sp: Optional[Dict[str, Any]] = None,
                       equipment: Optional[Dict[str, Any]] = None,
                       usage_type: Optional[str] = None
                       ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]],
                                 str, List[Dict[str, Any]]]:
    """forecast_explained -> (entry, alert, status, raw_forecasts). entry/alert
    are None when status != 'ok'. `raw_forecasts` is the UN-aggregated
    per-day list from forecast_explained (always returned, even for
    cadence='weekly' where `entry` is the _aggregate_week rollup) — the
    member-facing read endpoint (UI surfacing, 2026-07-29) needs the
    per-day kwh/eur/band for its 7-day strip, not just the week total.

    `equipment` (hard requirement 2026-07-29): {"electric_heating": bool,
    "electric_cooling": bool} from consumption.comfort_flex_for()'s
    `equipment` sub-object — threaded straight into forecast_explained,
    which drops the HDD/CDD driver entirely when the corresponding
    equipment is absent.

    `usage_type` (2026-07-30, consumption.comfort_flex_for()'s top-level
    `usage_type`) — threaded straight into forecast_explained to reframe the
    'calendar' driver's label/detail (vivienda/oficina/mixto/None).

    Shared by the email path (run_for_household, S7) and the read path
    (forecast_for_member, below) — the ONLY compute difference between them
    is what happens after this point (send email vs. return JSON)."""
    days_out = 1 if cadence == "daily" else 7
    try:
        forecast = await oe3.forecast_explained([slug], sp=sp, days_out=days_out, region=region,
                                                equipment=equipment, usage_type=usage_type)
    except Exception as exc:
        logger.error("forecast[%s/%s]: forecast_explained failed: %s", slug, cadence, exc)
        return None, None, "error", []
    if forecast["status"] != "ok" or not forecast["forecasts"]:
        return None, None, forecast["status"], []
    entry = forecast["forecasts"][0] if cadence == "daily" else _aggregate_week(forecast["forecasts"])
    return entry, forecast.get("alert"), "ok", forecast["forecasts"]


async def _narrate_with_cap(slug: str, entry: Dict[str, Any],
                            alert: Optional[Dict[str, Any]], lang: str,
                            equipment: Optional[Dict[str, Any]] = None
                            ) -> tuple[Any, int, int]:
    """AdviceSkill.narrate, gated by the monthly AI token cap (S6 acceptance
    criteria) — mirrors endpoints/ai.py's /ask gate. Over cap, or on any
    narrate failure, degrades to the deterministic template (never blocks
    the caller just because AI credits ran out or the LLM is unreachable).
    `equipment` is forwarded to AdviceSkill.narrate, which enforces the
    heating/cooling guardrail on EVERY return path (hard requirement
    2026-07-29) — never rely on the LLM alone.

    Absence prior (2026-07-30): every fallback path below also respects
    `entry["ausencia"]` (deterministic vacation-mode narrative instead of the
    generic template) — the AI-cap/error fallbacks must never let a
    declared-absence forecast slip back into ordinary (non-vacation)
    phrasing just because the cap was hit or the LLM errored.
    Returns (narrative, prompt_tokens, completion_tokens) — tokens are 0
    whenever the fallback was used (not a billable call)."""
    def _fallback():
        if entry.get("ausencia"):
            return _advice_skill._vacation_template(entry, lang, equipment)
        return _advice_skill._template_fallback(entry, alert, lang, equipment)

    from app.services import ai_usage
    used = await ai_usage.month_tokens(slug)
    if used >= settings.AI_MONTHLY_TOKEN_CAP:
        logger.info("advice[%s]: AI monthly cap reached (%d/%d) — template fallback",
                   slug, used, settings.AI_MONTHLY_TOKEN_CAP)
        return _fallback(), 0, 0
    try:
        return await _advice_skill.narrate(entry, alert=alert, lang=lang, equipment=equipment)
    except Exception as exc:
        logger.error("advice[%s]: narrate failed: %s", slug, exc)
        return _fallback(), 0, 0


async def forecast_for_member(slug: str, cadence: str, lang: str = "es",
                              region: Optional[str] = None,
                              sp: Optional[Dict[str, Any]] = None,
                              user_email: Optional[str] = None,
                              equipment: Optional[Dict[str, Any]] = None,
                              usage_type: Optional[str] = None) -> Dict[str, Any]:
    """Member-facing READ path (UI surfacing, 2026-07-29) — the SAME
    forecast_explained + AdviceSkill.narrate pipeline the S7 advice email
    uses, WITHOUT sending anything. `EMAIL_ADVICE_ENABLED`/opt-in/cooldown
    are irrelevant here (those only gate the EMAIL; viewing the forecast on
    the dashboard is always allowed). Caller (endpoints/advice.py) is
    responsible for caching this — every call may narrate via the LLM.

    `equipment` ({"electric_heating": bool, "electric_cooling": bool} —
    pass consumption.comfort_flex_for()'s `equipment` sub-object; hard
    requirement 2026-07-29) gates BOTH the SHAP attribution (oe3.py) and
    the advice tips (AdviceSkill guardrail) — a household without a given
    piece of equipment never sees its driver or its recommendation.

    Returns the same context shape _email_context builds (period_from/to/
    label, forecast{kwh,eur,band_lo,band_hi}, drivers, advice, alert, days)
    PLUS `attribution` (the full SHAP waterfall — base+ALL contributions,
    not just the top-3 `drivers`, for the "¿por qué?" slide panel) and,
    for cadence='weekly', `per_day` (raw per-day kwh/eur/band/price_status/
    price_confidence — the 7-day strip needs more than the aggregated
    week total). `status` is 'ok' | 'insufficient_data' | 'singular' | 'error'.
    """
    entry, alert, status, raw_forecasts = await _build_entry(
        slug, cadence, region=region, sp=sp, equipment=equipment, usage_type=usage_type)
    if status != "ok":
        return {"status": status, "cadence": cadence}

    narrative, prompt_t, completion_t = await _narrate_with_cap(slug, entry, alert, lang, equipment)
    context = _email_context(entry, narrative, alert, cadence, lang)
    context["status"] = "ok"
    context["cadence"] = cadence
    context["attribution"] = entry["attribution"]
    context["cheap_window"] = entry.get("cheap_window")
    if cadence == "weekly":
        context["per_day"] = [
            {"date": f["date"], "period_from": f["period_from"], "period_to": f["period_to"],
             "period_label": f["period_label"], "kwh": f["kwh"], "band_lo": f["band_lo"],
             "band_hi": f["band_hi"], "eur": f.get("eur"), "eur_lo": f.get("eur_lo"),
             "eur_hi": f.get("eur_hi"), "price_status": f.get("price_status"),
             "price_confidence": f.get("price_confidence"), "ausencia": f.get("ausencia")}
            for f in raw_forecasts
        ]
    if prompt_t or completion_t:
        from app.services import ai_usage
        await ai_usage.record(slug, user_email, "advice_forecast_view", prompt_t, completion_t)
    return context


async def _site_block(slug: str, customer_id: str,
                      sp: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """{name, cups, slug} for the email header (owner requirement
    2026-07-30 — members can have multiple CUPS/homes; an unlabeled advice/
    anomaly email is ambiguous about which one it covers). Shared by BOTH
    dispatch paths below.

    `sp` (a resolved consum.supply_points row — name/cups/main_sensor_key/
    main_channel) wins when the caller already knows exactly which
    installation the report is about (the anomaly path always does — see
    `_sp_for_device`). Without `sp` (the household-wide forecast cadences,
    which aggregate ALL of a customer's registered supply points), falls
    back to the customer's `display_name`/slug and only shows a CUPS when
    the household has EXACTLY one supply point — showing one CUPS for a
    total that spans several would mislabel it."""
    from app.core import db
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("SELECT display_name FROM customers WHERE customer_id = %s", (customer_id,))
            r = await cur.fetchone()
    name = (r[0] if r and r[0] else None) or slug

    if sp:
        return {"name": sp.get("name") or name, "cups": sp.get("cups"), "slug": slug}

    from app.services import supply_points as supply_points_svc
    points = await supply_points_svc.ensure_for_slugs([customer_id])
    cups = points[0].get("cups") if len(points) == 1 else None
    return {"name": name, "cups": cups, "slug": slug}


async def _sp_for_device(customer_id: str, device_id: Optional[str],
                         channel: Optional[str]) -> Optional[Dict[str, Any]]:
    """Which supply point (if any) a sensor (device_id, channel) belongs to
    — anomaly_events carries the sensor that fired, not a supply_point id
    (predates F3's supply_points model — same note as endpoints/anomalies.py).
    Walks each of the customer's points' subtree(), exactly like the
    ?supply= filter in endpoints/anomalies.py's list_anomalies."""
    if not device_id:
        return None
    from app.services import supply_points as supply_points_svc
    points = await supply_points_svc.ensure_for_slugs([customer_id])
    if len(points) == 1:
        return points[0]
    for sp in points:
        if (device_id, channel) in await supply_points_svc.subtree(sp):
            return sp
    return None


async def run_for_household(household: Dict[str, Any], cadence: str,
                            dry_run: bool = False) -> Dict[str, Any]:
    """One household, one cadence. Returns a result dict for logging/manual
    trigger responses — never raises (best-effort, caller loops many)."""
    slug = household["slug"]
    customer_id = household["customer_id"]
    lang = household.get("lang") or "es"
    region = household.get("region")
    recipient = household.get("recipient_email")
    result: Dict[str, Any] = {"slug": slug, "cadence": cadence, "sent": False, "reason": None}

    if not recipient:
        result["reason"] = "no_recipient"
        return result

    if not dry_run:
        if await advice_prefs.already_sent_today(customer_id, cadence):
            result["reason"] = "daily_cap_reached"
            return result
        last_sent = await advice_prefs.last_sent_at(customer_id)
        if not advice_prefs.cooldown_ok(last_sent, settings.ADVICE_COOLDOWN_H):
            result["reason"] = "cooldown"
            return result

    from app.services import consumption
    comfort = await consumption.comfort_flex_for([slug])
    equipment = comfort.get("equipment")
    usage_type = comfort.get("usage_type")

    entry, alert, status, _raw = await _build_entry(
        slug, cadence, region=region, equipment=equipment, usage_type=usage_type)
    if status != "ok":
        result["reason"] = f"forecast_{status}"
        return result

    narrative, prompt_t, completion_t = await _narrate_with_cap(slug, entry, alert, lang, equipment)

    context = _email_context(entry, narrative, alert, cadence, lang)
    context["site"] = await _site_block(slug, customer_id)
    result["context"] = context

    if dry_run:
        result["sent"] = False
        result["reason"] = "dry_run"
        return result

    ok = await mailer.send_email("advice", recipient, context, lang=lang)
    await advice_prefs.mark_sent(customer_id, cadence, ok=ok)
    if prompt_t or completion_t:
        from app.services import ai_usage
        await ai_usage.record(slug, recipient, "advice_email", prompt_t, completion_t)
    result["sent"] = ok
    result["reason"] = "ok" if ok else "send_failed_or_dark_launch"
    return result


# ── Anomaly -> advice email (Phase 2 E4 loop closure, 2026-07-30) ──────────
# Distinct pipeline from run_for_household above: the source is ONE api_edge
# anomaly_events row (already fetched by anomaly_poller), not a freshly
# computed forecast, and the household-selection/dedupe/opt-in checks are
# the CALLER's job (anomaly_poller._dispatch_new_anomalies) — this module
# only does entry-mapping -> narrate -> send -> mark-both-ledgers.

def _anomaly_entry(anomaly: Dict[str, Any]) -> Dict[str, Any]:
    """Map one api_edge `anomaly_events` row (mig 024 shape — ts, score,
    value_observed, value_expected, severity, meta{drivers[]}) into the
    shape `_facts_for_anomaly`/`_anomaly_email_context` expect. `ts` is the
    START of the scored hour bucket (api_edge evaluates hourly averages of
    `apower`, WATTS — not a kWh forecast); the email states the exact
    Europe/Madrid hour window (platform rule: every forecast/anomaly must
    give an explicit from->to date+hour, never a vague label)."""
    ts_raw = anomaly.get("ts")
    dt = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    madrid = dt.astimezone(ZoneInfo("Europe/Madrid"))
    end = madrid + timedelta(hours=1)
    meta = anomaly.get("meta") or {}
    return {
        "period_from": madrid.isoformat(),
        "period_to": end.isoformat(),
        "period_label": f"{madrid.strftime('%d/%m %H:%M')} → {end.strftime('%H:%M')}",
        "observed_w": anomaly.get("value_observed"),
        "expected_w": anomaly.get("value_expected"),
        "score": anomaly.get("score"),
        "severity": anomaly.get("severity"),
        "attribution": {"contributions": meta.get("drivers") or []},
    }


def _anomaly_email_context(entry: Dict[str, Any], narrative, lang: str) -> Dict[str, Any]:
    """Flat context for the SAME `advice` api_auth template the forecast
    path uses (S1) — `forecast` is left all-None (explicit dict, not `{}`,
    so the template's `is not none` guards skip that block cleanly) since an
    anomaly has no kWh/€ forecast; observed/expected/deviation are surfaced
    as `drivers` rows instead (same table, different content). `alert` is
    explicitly None here — that block is reserved for real AEMET weather
    alerts on the forecast path (_email_context); a consumption anomaly gets
    its OWN `anomaly` block (2026-07-30 fix — was wrongly rendered under the
    weather "Aviso meteorologico" header with a raw severity enum)."""
    observed, expected = entry.get("observed_w"), entry.get("expected_w")
    deviation = observed - expected if observed is not None and expected is not None else None
    es = lang != "en"
    drivers: List[Dict[str, Any]] = []
    if expected is not None:
        drivers.append({"label": "Potencia esperada (normal)" if es else "Expected power (normal)",
                        "effect": f"{expected:.0f} W", "detail": None})
    if observed is not None:
        drivers.append({"label": "Potencia observada" if es else "Observed power",
                        "effect": f"{observed:.0f} W", "detail": None})
    if deviation is not None:
        score = entry.get("score")
        effect = f"{deviation:+.0f} W" + (f" (z={score:+.1f})" if score is not None else "")
        drivers.append({"label": "Desviación" if es else "Deviation", "effect": effect, "detail": None})

    base = settings.PUBLIC_BASE_URL or ""
    title = narrative.summary.split(".")[0][:120] or \
        ("Consumo inusual detectado" if es else "Unusual consumption detected")
    # Localized severity WORD for the reader — never the raw 'critical'/
    # 'warning' enum from anomaly_service.py (edge mig 024).
    sev = entry.get("severity")
    sev_word = ("Crítico" if sev == "critical" else "Alto") if es else \
        ("Critical" if sev == "critical" else "High")
    anomaly_block = {
        "level": sev_word,
        "body": (f"Detectamos un consumo fuera de lo habitual en tu vivienda entre {entry['period_label']}."
                if es else
                f"We detected unusual consumption in your home between {entry['period_label']}."),
    }
    return {
        "title": title,
        "period_label": entry["period_label"],
        "period_from": entry["period_from"],
        "period_to": entry["period_to"],
        "summary": narrative.summary,
        "forecast": {"kwh": None, "eur": None, "band_lo": None, "band_hi": None},
        "drivers": drivers,
        "advice": [{"text": a} for a in narrative.advice],
        "alert": None,
        "anomaly": anomaly_block,
        "cta_url": (base.rstrip("/") + "/app/consumption") if base else None,
        "days": None,
    }


async def _narrate_anomaly_with_cap(slug: str, entry: Dict[str, Any], lang: str,
                                    equipment: Optional[Dict[str, Any]] = None
                                    ) -> tuple[Any, int, int]:
    """Anomaly counterpart to `_narrate_with_cap` — same monthly AI token
    cap gate (S6 acceptance criteria, shared budget across ALL AI kinds for
    the household), same degrade-to-template discipline on any failure."""
    from app.services import ai_usage
    used = await ai_usage.month_tokens(slug)
    if used >= settings.AI_MONTHLY_TOKEN_CAP:
        logger.info("advice(anomaly)[%s]: AI monthly cap reached (%d/%d) — template fallback",
                   slug, used, settings.AI_MONTHLY_TOKEN_CAP)
        return _advice_skill._anomaly_template_fallback(entry, lang, equipment), 0, 0
    try:
        return await _advice_skill.narrate_anomaly(entry, lang=lang, equipment=equipment)
    except Exception as exc:
        logger.error("advice(anomaly)[%s]: narrate failed: %s", slug, exc)
        return _advice_skill._anomaly_template_fallback(entry, lang, equipment), 0, 0


async def run_for_anomaly(anomaly: Dict[str, Any], prefs: Dict[str, Any],
                          customer_id: str, slug: str, dry_run: bool = False) -> Dict[str, Any]:
    """One anomaly event, one (already opt-in-checked) household. Caller
    (anomaly_poller._dispatch_new_anomalies) is responsible for the
    event-level dedupe pre-check and the household cooldown/daily-cap
    pre-check — this function always attempts narrate+send and is the ONLY
    place that WRITES the event-level ledger (mark_anomaly_notified), so
    every attempt (success or failure) is recorded exactly once, mirroring
    run_for_household's mark_sent discipline."""
    event_id = anomaly.get("id")
    lang = prefs.get("lang") or "es"
    recipient = prefs.get("recipient_email")
    result: Dict[str, Any] = {"slug": slug, "event_id": event_id, "sent": False, "reason": None}
    if event_id is None:
        result["reason"] = "no_event_id"
        return result
    if not recipient:
        result["reason"] = "no_recipient"
        return result

    from app.services import consumption
    equipment = (await consumption.comfort_flex_for([slug])).get("equipment")

    entry = _anomaly_entry(anomaly)
    narrative, prompt_t, completion_t = await _narrate_anomaly_with_cap(slug, entry, lang, equipment)
    context = _anomaly_email_context(entry, narrative, lang)
    sp = await _sp_for_device(customer_id, anomaly.get("device_id"), anomaly.get("channel"))
    context["site"] = await _site_block(slug, customer_id, sp)
    result["context"] = context

    if dry_run:
        result["reason"] = "dry_run"
        return result

    ok = await mailer.send_email("advice", recipient, context, lang=lang)
    await advice_prefs.mark_anomaly_notified(customer_id, event_id, ok=ok)
    await advice_prefs.mark_sent(customer_id, "anomaly", ok=ok)
    if prompt_t or completion_t:
        from app.services import ai_usage
        await ai_usage.record(slug, recipient, "advice_anomaly_email", prompt_t, completion_t)
    result["sent"] = ok
    result["reason"] = "ok" if ok else "send_failed_or_dark_launch"
    return result


async def _run_cadence(cadence: str) -> None:
    households = await advice_prefs.opted_in_households()
    logger.info("advice_scheduler: cadence=%s households=%d", cadence, len(households))
    for h in households:
        try:
            r = await run_for_household(h, cadence)
            logger.info("advice_scheduler: %s", r)
        except Exception as exc:  # noqa: BLE001 — one household must never abort the batch
            logger.error("advice_scheduler: household %s failed: %s", h.get("slug"), exc)


async def run_daily() -> None:
    await _run_cadence("daily")


async def run_weekly() -> None:
    await _run_cadence("weekly")


def start() -> None:
    """Wire the two cron jobs into an AsyncIOScheduler (Europe/Madrid) —
    called once from main.py's lifespan, mirrors notify_sub.start()."""
    global _scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed — advice_scheduler disabled")
        return

    _scheduler = AsyncIOScheduler(timezone="Europe/Madrid")
    _scheduler.add_job(run_daily, CronTrigger(hour=settings.ADVICE_DAILY_HOUR, minute=0),
                       id="advice_daily", replace_existing=True, misfire_grace_time=900)
    _scheduler.add_job(run_weekly, CronTrigger(day_of_week=settings.ADVICE_WEEKLY_DOW,
                                               hour=settings.ADVICE_WEEKLY_HOUR, minute=0),
                       id="advice_weekly", replace_existing=True, misfire_grace_time=900)
    _scheduler.start()
    logger.info("advice_scheduler started: daily=%02d:00, weekly=dow%d %02d:00 (Europe/Madrid)",
               settings.ADVICE_DAILY_HOUR, settings.ADVICE_WEEKLY_DOW, settings.ADVICE_WEEKLY_HOUR)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
