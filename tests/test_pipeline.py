"""Unit tests: run with `pytest -q` from the repo root."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402
from normalize import build_unified, normalize_meta, QualityReport, SchemaError  # noqa: E402
from anomaly import detect  # noqa: E402
from brief import build_payload, validate_brief, template_brief  # noqa: E402


@pytest.fixture(scope="module")
def unified():
    return build_unified()


@pytest.fixture(scope="module")
def result(unified):
    df, rep = unified
    return detect(df, rep)


# ---------------- data layer ----------------
def test_meta_spend_not_double_counted(unified):
    df, _ = unified
    raw = pd.read_csv(config.META_CSV)
    raw_spend_dedup = raw.drop_duplicates(["date_start", "campaign_id", "country"])["spend"].sum()
    meta = df[df.platform == "meta"]
    assert abs(meta["cost_original"].sum() - raw_spend_dedup) < 0.01
    assert len(meta) == len(raw) / 2


def test_both_attribution_sources_kept(unified):
    df, _ = unified
    meta = df[df.platform == "meta"]
    assert meta["conversions_pixel"].notna().all()
    assert meta["conversions_capi"].notna().all()
    assert (meta["conversions"] == meta["conversions_capi"]).all()


def test_currency_normalized(unified):
    df, _ = unified
    assert set(df["currency_original"]) == {"EUR", "USD"}
    usd = df[df.currency_original == "USD"]
    assert (usd["cost_eur"] == usd["cost_original"] * config.FX_TO_EUR["USD"]).all()


def test_gap_detected(unified):
    _, rep = unified
    gaps = [i for i in rep.issues if "missing_days" in i]
    assert any(i["campaign_id"].endswith("831044") and len(i["missing_days"]) == 12 for i in gaps)


def test_schema_error_on_bad_file(tmp_path):
    bad = tmp_path / "meta.csv"
    bad.write_text("date_start,spend\n2026-01-01,1\n")
    with pytest.raises(SchemaError):
        normalize_meta(bad, QualityReport())


# ---------------- anomaly layer ----------------
def test_cpc_spike_is_critical(result):
    crit = [a for a in result["anomalies"] if a["severity"] == "critical"]
    assert any(a["campaign_name"] == "NK | Search | Generic DE" and a["metric"] == "cpc_eur"
               and a["days_persisting"] >= 5 for a in crit)


def test_platform_wide_conversion_drop_is_data_quality(result):
    platforms = {s["platform"] for s in result["systemic_flags"]}
    assert platforms == {"google", "meta"}
    conv_perf = [a for a in result["anomalies"]
                 if a["metric"] in ("conversions", "roas", "cpa_eur")
                 and a["category"] == "performance" and a["severity"] != "info"]
    assert conv_perf == []


def test_chronic_low_roas_flagged(result):
    assert any(c["campaign_name"] == "VC | Prospecting | US" for c in result["chronic_issues"])


def test_json_has_required_fields(result):
    for a in result["anomalies"]:
        for k in ("campaign_name", "metric", "pct_change", "severity"):
            assert k in a
    json.dumps(result, default=str)  # serializable


# ---------------- LLM audit ----------------
def test_template_brief_passes_audit(result):
    p = build_payload(result)
    assert validate_brief(template_brief(p), p).ok


def test_audit_catches_fabricated_number(result):
    p = build_payload(result)
    text = template_brief(p) + "\nNK | Search | Generic DE [DE] CPC now €4.87 (+310%)."
    v = validate_brief(text, p)
    assert not v.ok and any("4.87" in n for n in v.unknown_numbers)


def test_audit_catches_fabricated_campaign(result):
    p = build_payload(result)
    v = validate_brief(template_brief(p) + "\nAH | Display | Remarketing [FR] spend up 20%.", p)
    assert v.unknown_campaigns == ["AH | Display | Remarketing"]


def test_audit_catches_missing_critical(result):
    p = build_payload(result)
    text = template_brief(p).replace("NK | Search | Generic DE", "NK | Search | Brand")
    assert "NK | Search | Generic DE" in validate_brief(text, p).missing_critical


def test_audit_accepts_locale_formats(result):
    p = build_payload(result)
    text = template_brief(p) + "\nToplam harcama 2.949,91 € oldu; CPC 1,40 € (2,4x), +139 %."
    assert validate_brief(text, p).ok
