"""Data layer: normalize Google Ads and Meta Ads daily exports into one schema.

Unified schema (one row = platform x campaign x country x day):

    date                 datetime64  reporting day
    platform             str         "google" | "meta"
    account_name         str
    campaign_id          str         platform-native id, kept as string
    campaign_name        str
    country              str         ISO-ish 2-letter code as delivered
    impressions          int
    clicks               int
    cost_original        float       spend in the platform's currency
    currency_original    str
    fx_rate_to_eur       float       rate actually applied (auditable)
    cost_eur             float
    conversions          float       primary conversion count (see config)
    conversions_value_eur float
    conversions_pixel    float       Meta only, NaN for Google
    conversions_capi     float       Meta only, NaN for Google
    attribution_source   str         which count feeds `conversions`

Also produces a data-quality report (dict) listing what was fixed or flagged.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)

UNIFIED_COLUMNS = [
    "date", "platform", "account_name", "campaign_id", "campaign_name",
    "country", "impressions", "clicks", "cost_original", "currency_original",
    "fx_rate_to_eur", "cost_eur", "conversions", "conversions_value_eur",
    "conversions_pixel", "conversions_capi", "attribution_source",
]

GOOGLE_REQUIRED = {
    "date", "account_name", "campaign_id", "campaign_name", "country_code",
    "impressions", "clicks", "cost_micros", "conversions", "conversions_value",
    "currency_code",
}
META_REQUIRED = {
    "date_start", "account_name", "campaign_id", "campaign_name", "country",
    "impressions", "inline_link_clicks", "spend", "action_source",
    "actions_purchase", "action_values_purchase", "currency",
}


class SchemaError(ValueError):
    """Raised when an input file does not match the expected layout."""


@dataclass
class QualityReport:
    """Everything the normalizer changed or noticed, for the README/brief."""
    issues: list[dict] = field(default_factory=list)

    def add(self, severity: str, source: str, message: str, **detail) -> None:
        self.issues.append({"severity": severity, "source": source,
                            "message": message, **detail})
        getattr(log, "warning" if severity != "info" else "info")(
            "[%s] %s", source, message)

    def to_dict(self) -> dict:
        return {"issue_count": len(self.issues), "issues": self.issues}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _require_columns(df: pd.DataFrame, required: set[str], name: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise SchemaError(f"{name}: missing columns {sorted(missing)}")


def _fx_rate(currency: str) -> float:
    try:
        return config.FX_TO_EUR[currency]
    except KeyError as exc:
        raise SchemaError(f"No FX rate configured for currency {currency!r}") from exc


def _coerce_numeric(df: pd.DataFrame, cols: list[str], report: QualityReport,
                    source: str) -> pd.DataFrame:
    for c in cols:
        before = df[c].isna().sum()
        df[c] = pd.to_numeric(df[c], errors="coerce")
        bad = df[c].isna().sum() - before
        if bad:
            report.add("warning", source, f"{bad} non-numeric values in {c} set to NaN")
        neg = (df[c] < 0).sum()
        if neg:
            report.add("warning", source, f"{neg} negative values in {c} clipped to 0")
            df[c] = df[c].clip(lower=0)
    return df


def _detect_gaps(df: pd.DataFrame, report: QualityReport, source: str) -> None:
    """Flag campaign x country series that are missing days inside the window."""
    all_days = pd.date_range(df["date"].min(), df["date"].max(), freq="D")
    for (cid, cname, country), grp in df.groupby(["campaign_id", "campaign_name", "country"]):
        present = set(grp["date"])
        missing = [d for d in all_days if d not in present]
        if missing:
            report.add(
                "warning", source,
                f"{cname} [{country}] missing {len(missing)} day(s): "
                f"{missing[0].date()} .. {missing[-1].date()}",
                campaign_id=str(cid), country=country,
                missing_days=[d.strftime("%Y-%m-%d") for d in missing],
            )


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------
def normalize_google(path: Path, report: QualityReport) -> pd.DataFrame:
    src = "google"
    raw = pd.read_csv(path)
    _require_columns(raw, GOOGLE_REQUIRED, src)
    df = raw.copy()

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    bad_dates = df["date"].isna().sum()
    if bad_dates:
        report.add("warning", src, f"{bad_dates} rows with unparseable date dropped")
        df = df.dropna(subset=["date"])

    df = _coerce_numeric(df, ["impressions", "clicks", "cost_micros",
                              "conversions", "conversions_value"], report, src)

    dupes = df.duplicated(["date", "campaign_id", "country_code"]).sum()
    if dupes:
        report.add("warning", src, f"{dupes} exact duplicate key rows aggregated")
        df = (df.groupby(["date", "account_name", "campaign_id", "campaign_name",
                          "country_code", "currency_code"], as_index=False)
                .sum(numeric_only=True))

    # Same campaign_name under multiple ids (e.g. Brand DE vs Brand NL): keep
    # ids as the key but tell the reader.
    name_ids = df.groupby("campaign_name")["campaign_id"].nunique()
    for name, n in name_ids[name_ids > 1].items():
        report.add("info", src, f"campaign_name {name!r} maps to {n} campaign_ids; "
                                "keyed by campaign_id + country")

    currencies = df["currency_code"].unique().tolist()
    if len(currencies) > 1:
        report.add("info", src, f"multiple currencies present: {currencies}")
    df["fx_rate_to_eur"] = df["currency_code"].map(_fx_rate)

    out = pd.DataFrame({
        "date": df["date"],
        "platform": src,
        "account_name": df["account_name"].astype(str).str.strip(),
        "campaign_id": df["campaign_id"].astype(str),
        "campaign_name": df["campaign_name"].astype(str).str.strip(),
        "country": df["country_code"].astype(str).str.strip().str.upper(),
        "impressions": df["impressions"].fillna(0).astype(int),
        "clicks": df["clicks"].fillna(0).astype(int),
        "cost_original": df["cost_micros"] / 1_000_000,
        "currency_original": df["currency_code"],
        "fx_rate_to_eur": df["fx_rate_to_eur"],
        "conversions": df["conversions"].fillna(0.0),
        "conversions_pixel": np.nan,
        "conversions_capi": np.nan,
        "attribution_source": "google_conversions",
    })
    out["cost_eur"] = out["cost_original"] * out["fx_rate_to_eur"]
    out["conversions_value_eur"] = df["conversions_value"].fillna(0.0) * out["fx_rate_to_eur"]
    out = out.reset_index(drop=True)
    _detect_gaps(out, report, src)
    return out[UNIFIED_COLUMNS]


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------
def normalize_meta(path: Path, report: QualityReport) -> pd.DataFrame:
    src = "meta"
    raw = pd.read_csv(path)
    _require_columns(raw, META_REQUIRED, src)
    df = raw.copy()

    df["date"] = pd.to_datetime(df["date_start"], errors="coerce")
    bad_dates = df["date"].isna().sum()
    if bad_dates:
        report.add("warning", src, f"{bad_dates} rows with unparseable date dropped")
        df = df.dropna(subset=["date"])

    df = _coerce_numeric(df, ["impressions", "inline_link_clicks", "spend",
                              "actions_purchase", "action_values_purchase"], report, src)

    key = ["date", "account_name", "campaign_id", "campaign_name", "country", "currency"]

    # --- Pivot the two attribution rows into columns ----------------------
    sources = set(df["action_source"].unique())
    unknown = sources - {config.META_PRIMARY_ATTRIBUTION, config.META_SECONDARY_ATTRIBUTION}
    if unknown:
        report.add("warning", src, f"unexpected action_source values ignored: {sorted(unknown)}")
        df = df[~df["action_source"].isin(unknown)]

    # Delivery metrics must be identical across the pixel/CAPI rows. Verify
    # instead of assuming; if they differ, that is itself a data issue.
    deliv = df.groupby(key)[["impressions", "inline_link_clicks", "spend"]].nunique()
    inconsistent = (deliv > 1).any(axis=1).sum()
    if inconsistent:
        report.add("warning", src,
                   f"{inconsistent} keys have differing spend/impressions across "
                   "action_source rows; taking max")

    delivery = df.groupby(key, as_index=False)[["impressions", "inline_link_clicks", "spend"]].max()
    conv = df.pivot_table(index=key, columns="action_source",
                          values=["actions_purchase", "action_values_purchase"],
                          aggfunc="sum")
    conv.columns = [f"{m}__{s}" for m, s in conv.columns]
    conv = conv.reset_index()
    merged = delivery.merge(conv, on=key, how="left")

    n_raw, n_dedup = len(df), len(merged)
    report.add("info", src,
               f"collapsed {n_raw} attribution rows into {n_dedup} campaign-day rows "
               f"(spend would have been double-counted otherwise)")

    prim, sec = config.META_PRIMARY_ATTRIBUTION, config.META_SECONDARY_ATTRIBUTION
    p_col, s_col = f"actions_purchase__{prim}", f"actions_purchase__{sec}"
    pv_col = f"action_values_purchase__{prim}"
    for c in (p_col, s_col, pv_col):
        if c not in merged.columns:
            merged[c] = np.nan
    missing_prim = merged[p_col].isna().sum()
    if missing_prim:
        report.add("warning", src,
                   f"{missing_prim} campaign-days lack a {prim} row; falling back to {sec}")
        merged[p_col] = merged[p_col].fillna(merged[s_col])
        merged[pv_col] = merged[pv_col].fillna(merged.get(f"action_values_purchase__{sec}"))

    # CAPI vs pixel sanity band
    ratio = merged[f"actions_purchase__conversions_api"] / merged["actions_purchase__website_pixel"]
    lo, hi = config.CAPI_PIXEL_RATIO_BAND
    off = merged[(ratio < lo) | (ratio > hi)]
    if len(off):
        report.add("warning", src,
                   f"{len(off)} campaign-days with CAPI/pixel purchase ratio outside "
                   f"[{lo}, {hi}] — possible tracking issue",
                   examples=off[["date", "campaign_name", "country"]].head(5)
                            .assign(date=lambda d: d["date"].dt.strftime("%Y-%m-%d"))
                            .to_dict("records"))
    report.add("info", src, f"median CAPI/pixel purchase ratio = {ratio.median():.3f}")

    merged["fx_rate_to_eur"] = merged["currency"].map(_fx_rate)

    out = pd.DataFrame({
        "date": merged["date"],
        "platform": src,
        "account_name": merged["account_name"].astype(str).str.strip(),
        "campaign_id": merged["campaign_id"].astype(str),
        "campaign_name": merged["campaign_name"].astype(str).str.strip(),
        "country": merged["country"].astype(str).str.strip().str.upper(),
        "impressions": merged["impressions"].fillna(0).astype(int),
        "clicks": merged["inline_link_clicks"].fillna(0).astype(int),
        "cost_original": merged["spend"],
        "currency_original": merged["currency"],
        "fx_rate_to_eur": merged["fx_rate_to_eur"],
        "conversions": merged[p_col].fillna(0.0),
        "conversions_pixel": merged["actions_purchase__website_pixel"],
        "conversions_capi": merged["actions_purchase__conversions_api"],
        "attribution_source": prim,
    })
    out["cost_eur"] = out["cost_original"] * out["fx_rate_to_eur"]
    out["conversions_value_eur"] = merged[pv_col].fillna(0.0) * out["fx_rate_to_eur"]
    out = out.reset_index(drop=True)
    _detect_gaps(out, report, src)
    return out[UNIFIED_COLUMNS]


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def build_unified(google_path: Path = config.GOOGLE_CSV,
                  meta_path: Path = config.META_CSV) -> tuple[pd.DataFrame, QualityReport]:
    report = QualityReport()
    frames = []
    for name, fn, path in (("google", normalize_google, google_path),
                           ("meta", normalize_meta, meta_path)):
        try:
            frames.append(fn(path, report))
        except FileNotFoundError:
            report.add("error", name, f"file not found: {path}")
        except SchemaError as exc:
            report.add("error", name, f"schema error: {exc}")
    if not frames:
        raise RuntimeError("No input could be normalized; see quality report")

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["platform", "campaign_id", "country", "date"]).reset_index(drop=True)

    # Derived KPIs (safe division)
    df["ctr"] = np.where(df["impressions"] > 0, df["clicks"] / df["impressions"], np.nan)
    df["cpc_eur"] = np.where(df["clicks"] > 0, df["cost_eur"] / df["clicks"], np.nan)
    df["cpa_eur"] = np.where(df["conversions"] > 0, df["cost_eur"] / df["conversions"], np.nan)
    df["roas"] = np.where(df["cost_eur"] > 0, df["conversions_value_eur"] / df["cost_eur"], np.nan)

    if df["date"].max() != df.groupby("platform")["date"].max().min():
        report.add("warning", "unified", "platforms end on different dates; "
                   "last-day comparison uses each platform's own max date")
    return df, report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    df, report = build_unified()
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    out_csv = config.OUTPUT_DIR / "normalized.csv"
    out_json = config.OUTPUT_DIR / "data_quality.json"
    df.to_csv(out_csv, index=False)
    out_json.write_text(json.dumps(report.to_dict(), indent=2, default=str))
    log.info("wrote %s (%d rows) and %s", out_csv, len(df), out_json)


if __name__ == "__main__":
    main()
