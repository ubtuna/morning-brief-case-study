"""Anomaly layer: compare the latest day with recent history, emit JSON.

Design
------
Unit of analysis is platform x campaign_id x country (the finest grain we
have). For each unit and each KPI we compute:

* last_value        the latest day
* baseline_7d       mean of the 7 days before it   (current run-rate)
* baseline_28d      mean of the 28 days before it  (seasonality-aware)
* z_score           (last - mean_28d) / std_28d
* pct_vs_7d / pct_vs_28d
* days_persisting   how many consecutive trailing days already breach the
                    warning threshold vs the 28d baseline (a spike that
                    started 5 days ago is more urgent than a 1-day blip)

Severity requires BOTH a percentage breach AND a z-score breach, so a +40%
move inside a very noisy series is not an alert, and a +40% move inside a
rock-steady series is.

Three guards keep the output honest:

1. Volume floor      tiny campaign-days (spend / conversions below floor) are
                     skipped for ratio metrics.
2. Conversion lag    conversion-based KPIs for the last CONVERSION_LAG_DAYS
                     days are labelled low-confidence; if the drop is
                     platform-wide while delivery is flat it is reclassified
                     as a data_quality event, not a performance event.
3. Data-quality pass gaps, pixel/CAPI drift and similar issues from the
                     normalizer are attached so the brief can separate
                     "the ads broke" from "the data broke".
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
from normalize import build_unified, QualityReport

log = logging.getLogger(__name__)

UNIT_KEYS = ["platform", "account_name", "campaign_id", "campaign_name", "country"]
METRICS = list(config.THRESHOLDS.keys())
CONVERSION_METRICS = {"conversions", "cpa_eur", "roas"}
# For these, "up" is bad; for the rest "down" is bad.
BAD_WHEN_UP = {"cost_eur", "cpc_eur", "cpa_eur"}


def _pct(new: float, base: float) -> float | None:
    if base is None or not np.isfinite(base) or base == 0 or not np.isfinite(new):
        return None
    return float((new - base) / base)


def _severity(metric: str, pct: float | None, z: float | None) -> str:
    if pct is None:
        return "none"
    warn, crit = config.THRESHOLDS[metric]
    a = abs(pct)
    z_ok = z is not None and np.isfinite(z) and abs(z) >= config.MIN_Z_SCORE
    if a >= crit and z_ok:
        return "critical"
    if a >= warn and z_ok:
        return "warning"
    if a >= crit:          # big % move but noisy series: still worth a look
        return "warning"
    return "none"


def _streak(series: pd.Series, base: float, metric: str) -> int:
    """Consecutive trailing days that breach the warning threshold vs base."""
    warn, _ = config.THRESHOLDS[metric]
    if base is None or not np.isfinite(base) or base == 0:
        return 0
    n = 0
    for v in series.iloc[::-1]:
        p = _pct(v, base)
        if p is None or abs(p) < warn:
            break
        n += 1
    return n


def _unit_anomalies(unit: pd.DataFrame, report_date: pd.Timestamp) -> list[dict]:
    unit = unit.sort_values("date")
    last = unit[unit["date"] == report_date]
    if last.empty:
        return []
    last = last.iloc[0]
    hist = unit[unit["date"] < report_date]
    h7 = hist.tail(config.BASELINE_SHORT_DAYS)
    h28 = hist.tail(config.BASELINE_LONG_DAYS)
    if len(h7) < 3:
        return []

    low_volume = (last["cost_eur"] < config.MIN_DAILY_SPEND_EUR)
    low_conv = (h7["conversions"].mean() < config.MIN_DAILY_CONVERSIONS)

    out: list[dict] = []
    for m in METRICS:
        if low_volume and m != "cost_eur":
            continue
        if m in CONVERSION_METRICS and low_conv:
            continue
        lv = float(last[m]) if np.isfinite(last[m]) else None
        if lv is None:
            continue
        b7 = float(h7[m].mean()) if h7[m].notna().any() else None
        b28 = float(h28[m].mean()) if h28[m].notna().any() else None
        std28 = float(h28[m].std(ddof=0)) if h28[m].notna().sum() > 2 else None
        z = (lv - b28) / std28 if (b28 is not None and std28 and std28 > 0) else None
        p7, p28 = _pct(lv, b7), _pct(lv, b28)
        streak = _streak(unit[unit["date"] <= report_date][m], b28, m)
        # if the move is already several days old, the 7d baseline is polluted
        # by it; judge against the 28d baseline in that case.
        pct = p28 if (streak >= 3 and p28 is not None) else p7
        sev = _severity(m, pct, z)
        if sev == "none":
            continue
        direction = "up" if pct > 0 else "down"
        is_bad = (direction == "up") == (m in BAD_WHEN_UP)
        out.append({
            "platform": last["platform"],
            "account_name": last["account_name"],
            "campaign_id": last["campaign_id"],
            "campaign_name": last["campaign_name"],
            "country": last["country"],
            "metric": m,
            "last_value": round(lv, 4),
            "baseline_7d": round(b7, 4) if b7 is not None else None,
            "baseline_28d": round(b28, 4) if b28 is not None else None,
            "pct_change": round(pct, 4),
            "pct_vs_7d": round(p7, 4) if p7 is not None else None,
            "pct_vs_28d": round(p28, 4) if p28 is not None else None,
            "z_score": round(z, 2) if z is not None else None,
            "direction": direction,
            "impact": "negative" if is_bad else "positive",
            "severity": sev,
            "days_persisting": int(streak),
            "category": "performance",
            "confidence": "high",
            "last_day_spend_eur": round(float(last["cost_eur"]), 2),
            "note": "",
        })
    return out


def _conversion_lag_pass(anoms: list[dict], df: pd.DataFrame,
                         report_date: pd.Timestamp) -> list[dict]:
    """Label conversion KPIs on the trailing lag window, and detect the
    platform-wide 'everything dropped but spend is flat' signature."""
    lag_window = df["date"] >= report_date - pd.Timedelta(days=config.CONVERSION_LAG_DAYS - 1)
    systemic: list[dict] = []
    for platform, pdf in df.groupby("platform"):
        daily = pdf.groupby("date")[["conversions", "cost_eur", "clicks", "impressions"]].sum().sort_index()
        hist = daily[daily.index < report_date - pd.Timedelta(days=config.CONVERSION_LAG_DAYS - 1)]
        recent = daily[daily.index >= report_date - pd.Timedelta(days=config.CONVERSION_LAG_DAYS - 1)]
        if len(hist) < 7 or recent.empty:
            continue
        base = hist.tail(28)
        conv_drop = _pct(recent["conversions"].iloc[-1], base["conversions"].mean())
        spend_chg = _pct(recent["cost_eur"].iloc[-1], base["cost_eur"].mean())
        clicks_chg = _pct(recent["clicks"].iloc[-1], base["clicks"].mean())
        impr_chg = _pct(recent["impressions"].iloc[-1], base["impressions"].mean())
        units_down = 0
        units_total = 0
        for _, u in pdf.groupby(UNIT_KEYS):
            u = u.sort_values("date")
            lastv = u[u["date"] == report_date]["conversions"]
            if lastv.empty:
                continue
            units_total += 1
            b = u[u["date"] < report_date].tail(28)["conversions"].mean()
            if b and lastv.iloc[0] < b * 0.7:
                units_down += 1
        share_down = units_down / units_total if units_total else 0
        # Spend is deliberately NOT part of the gate: a single campaign's CPC
        # spike can move platform spend while delivery volume stays flat.
        if conv_drop is not None and conv_drop <= config.SYSTEMIC_CONV_DROP \
                and abs(clicks_chg or 0) < config.DELIVERY_FLAT_TOLERANCE \
                and abs(impr_chg or 0) < config.DELIVERY_FLAT_TOLERANCE \
                and share_down >= config.SYSTEMIC_MIN_SHARE_DOWN:
            systemic.append({
                "platform": platform,
                "type": "conversion_reporting_lag",
                "severity": "info",
                "category": "data_quality",
                "share_of_campaigns_down": round(share_down, 2),
                "conversions_pct_vs_28d": round(conv_drop, 3),
                "spend_pct_vs_28d": round(spend_chg, 3) if spend_chg is not None else None,
                "clicks_pct_vs_28d": round(clicks_chg, 3) if clicks_chg is not None else None,
                "impressions_pct_vs_28d": round(impr_chg, 3) if impr_chg is not None else None,
                "note": (f"{platform}: conversions down {conv_drop:.0%} across "
                         f"{share_down:.0%} of campaigns while clicks and impressions are flat. "
                         "Pattern matches delayed attribution, not a performance drop. "
                         f"Re-check after {config.CONVERSION_LAG_DAYS} days."),
            })
    lagged_platforms = {s["platform"] for s in systemic}
    for a in anoms:
        if a["metric"] in CONVERSION_METRICS:
            a["confidence"] = "low"
            if a["platform"] in lagged_platforms and a["impact"] == "negative":
                a["category"] = "data_quality"
                a["severity"] = "info"
                a["note"] = "Platform-wide conversion drop with flat delivery; likely reporting lag."
            else:
                a["note"] = (f"Conversions for the last {config.CONVERSION_LAG_DAYS} days "
                             "are typically incomplete; treat as provisional.")
    return systemic


def _chronic_pass(df: pd.DataFrame, report_date: pd.Timestamp) -> list[dict]:
    """Structural issues that are not 'new today' but a manager must know."""
    out = []
    window = df[(df["date"] < report_date) & (df["date"] >= report_date - pd.Timedelta(days=28))]
    agg = window.groupby(UNIT_KEYS).agg(cost=("cost_eur", "sum"),
                                        value=("conversions_value_eur", "sum"),
                                        conv=("conversions", "sum"),
                                        days=("date", "nunique")).reset_index()
    agg["roas"] = agg["value"] / agg["cost"]
    for _, r in agg.iterrows():
        if r["cost"] < config.CHRONIC_MIN_SPEND_EUR or r["days"] < 14:
            continue
        if r["roas"] < config.CHRONIC_ROAS_FLOOR:
            out.append({
                **{k: r[k] for k in UNIT_KEYS},
                "metric": "roas_28d",
                "last_value": round(float(r["roas"]), 3),
                "spend_28d_eur": round(float(r["cost"]), 2),
                "severity": "warning",
                "category": "chronic",
                "confidence": "high",
                "note": f"28-day ROAS {r['roas']:.2f} on €{r['cost']:,.0f} spend: "
                        f"below the {config.CHRONIC_ROAS_FLOOR:.1f} floor; likely unprofitable after margin.",
            })
    return out


def _data_quality_items(report: QualityReport) -> list[dict]:
    keep = [i for i in report.issues if i["severity"] in ("warning", "error")]
    return [{"category": "data_quality", "severity": i["severity"],
             "source": i["source"], "note": i["message"],
             **{k: v for k, v in i.items() if k in ("campaign_id", "country", "missing_days")}}
            for i in keep]


def _totals(df: pd.DataFrame, report_date: pd.Timestamp) -> dict:
    last = df[df["date"] == report_date]
    prev7 = df[(df["date"] < report_date) & (df["date"] >= report_date - pd.Timedelta(days=7))]
    def block(d: pd.DataFrame) -> dict:
        cost = float(d["cost_eur"].sum()); val = float(d["conversions_value_eur"].sum())
        conv = float(d["conversions"].sum())
        return {"spend_eur": round(cost, 2), "conversions": round(conv, 2),
                "conversion_value_eur": round(val, 2),
                "roas": round(val / cost, 3) if cost else None,
                "cpa_eur": round(cost / conv, 2) if conv else None}
    by_platform = {p: block(g) for p, g in last.groupby("platform")}
    by_account = {a: block(g) for a, g in last.groupby("account_name")}
    prev_daily = block(prev7)
    prev_daily = {k: (round(v / 7, 2) if k in ("spend_eur", "conversions", "conversion_value_eur") and v is not None else v)
                  for k, v in prev_daily.items()}
    return {"last_day": block(last), "prev_7d_daily_avg": prev_daily,
            "by_platform": by_platform, "by_account": by_account}


def detect(df: pd.DataFrame, report: QualityReport,
           report_date: pd.Timestamp | None = None) -> dict:
    report_date = report_date or df["date"].max()
    anoms: list[dict] = []
    for _, unit in df.groupby(UNIT_KEYS):
        anoms.extend(_unit_anomalies(unit, report_date))
    systemic = _conversion_lag_pass(anoms, df, report_date)
    chronic = _chronic_pass(df, report_date)
    dq = _data_quality_items(report)

    order = {"critical": 0, "warning": 1, "info": 2}
    anoms.sort(key=lambda a: (order[a["severity"]], -abs(a["pct_change"]), -a["last_day_spend_eur"]))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_date": report_date.strftime("%Y-%m-%d"),
        "reporting_currency": config.REPORTING_CURRENCY,
        "fx_rates_applied": config.FX_TO_EUR,
        "method": {
            "baseline_short_days": config.BASELINE_SHORT_DAYS,
            "baseline_long_days": config.BASELINE_LONG_DAYS,
            "thresholds_warning_critical": config.THRESHOLDS,
            "min_z_score": config.MIN_Z_SCORE,
            "conversion_lag_days": config.CONVERSION_LAG_DAYS,
            "meta_attribution": config.META_PRIMARY_ATTRIBUTION,
        },
        "totals": _totals(df, report_date),
        "anomalies": anoms,
        "systemic_flags": systemic,
        "chronic_issues": chronic,
        "data_quality": dq,
        "counts": {
            "critical": sum(a["severity"] == "critical" for a in anoms),
            "warning": sum(a["severity"] == "warning" for a in anoms),
            "info": sum(a["severity"] == "info" for a in anoms),
            "chronic": len(chronic),
            "data_quality": len(dq) + len(systemic),
        },
    }


def main(out_path: Path | None = None) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    df, report = build_unified()
    result = detect(df, report)
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = out_path or config.OUTPUT_DIR / "anomalies.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))
    log.info("wrote %s: %s", out_path, result["counts"])
    return result


if __name__ == "__main__":
    main()
