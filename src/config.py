"""Central configuration for the Morning Brief pipeline.

Everything that is an *assumption* rather than a fact lives here, so it can be
challenged in one place. Values can be overridden with environment variables
where noted.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
PROMPTS_DIR = ROOT / "prompts"

GOOGLE_CSV = DATA_DIR / "google_ads_daily.csv"
META_CSV = DATA_DIR / "meta_ads_daily.csv"

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------
# All reporting is done in EUR. Google exports EUR, Meta exports USD.
# In production this comes from a daily FX source (e.g. ECB reference rate)
# keyed by date; for the case study a single configurable rate is used and
# the rate applied is written into every normalized row so it is auditable.
REPORTING_CURRENCY = "EUR"
FX_TO_EUR: dict[str, float] = {
    "EUR": 1.0,
    "USD": float(os.getenv("FX_USD_EUR", "0.92")),
}

# ---------------------------------------------------------------------------
# Meta attribution
# ---------------------------------------------------------------------------
# Meta rows arrive un-deduplicated: one row per action_source (website_pixel,
# conversions_api) with identical spend/impressions/clicks. We keep BOTH
# conversion counts as separate columns and report on the one below.
META_PRIMARY_ATTRIBUTION = "conversions_api"
META_SECONDARY_ATTRIBUTION = "website_pixel"

# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------
BASELINE_SHORT_DAYS = 7     # recent baseline (captures current run-rate)
BASELINE_LONG_DAYS = 28     # long baseline (captures weekly seasonality)
MIN_DAILY_SPEND_EUR = 20.0  # below this, ratios are too noisy to alert on
MIN_DAILY_CONVERSIONS = 3.0 # below this, CPA/ROAS swings are mostly noise

# Percentage-change thresholds per metric: (warning, critical).
# Rationale is documented in README (Anomaly layer section).
THRESHOLDS: dict[str, tuple[float, float]] = {
    "cost_eur":      (0.30, 0.60),
    "cpc_eur":       (0.30, 0.60),
    "ctr":           (0.25, 0.50),
    "conversions":   (0.30, 0.50),
    "cpa_eur":       (0.30, 0.60),
    "roas":          (0.30, 0.50),
}
# Z-score gate: a move must also be at least this many stdevs from the
# 28-day daily distribution to count. Filters "big % on tiny base" cases.
MIN_Z_SCORE = 2.0

# Conversion-lag guard: conversions reported for the last N days are known to
# be incomplete on both platforms (view-through / delayed attribution windows).
CONVERSION_LAG_DAYS = 2

# Chronic check: 28-day ROAS below this on meaningful spend is flagged even if
# nothing changed today. 1.5 is a conservative floor; at typical e-commerce
# gross margins (40-60%) break-even ROAS sits around 1.7-2.5.
CHRONIC_ROAS_FLOOR = 1.5
CHRONIC_MIN_SPEND_EUR = 500.0

# Systemic conversion-lag signature: conversions down at least this much while
# delivery (clicks, impressions) moved less than DELIVERY_FLAT_TOLERANCE.
SYSTEMIC_CONV_DROP = -0.25
DELIVERY_FLAT_TOLERANCE = 0.25
SYSTEMIC_MIN_SHARE_DOWN = 0.7

# Pixel vs CAPI: expected ratio band. Outside it, flag as a tracking issue.
CAPI_PIXEL_RATIO_BAND = (1.05, 1.40)

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
LLM_MAX_TOKENS = 1800
