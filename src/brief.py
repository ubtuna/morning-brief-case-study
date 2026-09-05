"""LLM layer: turn the anomaly JSON into an executive brief, and prove it is
grounded in that JSON.

Grounding is enforced in three layers:

1. Input control    The model sees a *compacted* payload (only the fields a
                    manager needs) and nothing else. No web search, no tools,
                    no prior conversation. Sampling parameters such as temperature were removed in anthropic SDK 1.x and are ignored by current models; repeatability comes from the audit below, not from sampling.
2. Prompt contract  prompts/morning_brief_system.md forbids outside knowledge,
                    invented numbers, renamed campaigns and causal claims.
3. Output audit     `validate_brief()` extracts every number and every campaign
                    reference from the generated text and checks each one
                    against the payload. Rounding and percent/multiple forms
                    are allowed; anything else fails. On failure the model is
                    re-prompted once with the list of violations; if it fails
                    again the pipeline falls back to a deterministic template
                    brief so the morning send never depends on the model.

The validator is deterministic and unit-tested (tests/test_validate.py), which
is the "how do you audit output quality" answer: quality is not judged by
reading the brief, it is measured.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import config

log = logging.getLogger(__name__)

BRIEF_LANGUAGE = os.getenv("BRIEF_LANGUAGE", "Turkish")


# ---------------------------------------------------------------------------
# 1. Payload compaction
# ---------------------------------------------------------------------------
_ANOMALY_FIELDS = ["platform", "campaign_name", "country", "metric", "last_value",
                   "baseline_7d", "baseline_28d", "pct_change", "z_score", "direction",
                   "impact", "severity", "days_persisting", "category", "confidence",
                   "last_day_spend_eur", "note"]


def build_payload(result: dict) -> dict:
    """Keep what the brief needs; drop the 40+ lag-downgraded info rows and
    replace them with a count so the model cannot cherry-pick them."""
    actionable = [{k: a.get(k) for k in _ANOMALY_FIELDS}
                  for a in result["anomalies"] if a["severity"] != "info"]
    for a in actionable:
        if a["baseline_7d"] and a["last_value"] is not None:
            a["multiple_vs_7d"] = round(a["last_value"] / a["baseline_7d"], 2)
        if a["baseline_28d"] and a["last_value"] is not None:
            a["multiple_vs_28d"] = round(a["last_value"] / a["baseline_28d"], 2)
    downgraded = sum(1 for a in result["anomalies"] if a["severity"] == "info")
    return {
        "report_date": result["report_date"],
        "reporting_currency": result["reporting_currency"],
        "totals": result["totals"],
        "anomalies": actionable,
        "anomalies_downgraded_to_reporting_lag": downgraded,
        "systemic_flags": result["systemic_flags"],
        "chronic_issues": result["chronic_issues"],
        "data_quality": result["data_quality"],
        "counts": result["counts"],
    }


# ---------------------------------------------------------------------------
# 2. Prompt loading
# ---------------------------------------------------------------------------
def load_prompt(name: str) -> str:
    path = config.PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"prompt file missing: {path}")
    return path.read_text(encoding="utf-8")


def render_messages(payload: dict) -> tuple[str, str]:
    system = load_prompt("morning_brief_system.md").replace("{language}", BRIEF_LANGUAGE) \
                                                     .replace("{currency}", payload["reporting_currency"])
    user = load_prompt("morning_brief_user.md").format(
        report_date=payload["report_date"],
        currency=payload["reporting_currency"],
        payload_json=json.dumps(payload, ensure_ascii=False, indent=1),
    )
    return system, user


# ---------------------------------------------------------------------------
# 3. Grounding validator
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"(?<![\w.])[-+]?\d[\d.,]*")
# "NK | Search | Generic DE" style names: 2-3 caps, then pipe-separated words
_CAMPAIGN_RE = re.compile(r"[A-Z]{2,3} \| [A-Za-z+&][A-Za-z0-9+& ]*(?: \| [A-Za-z0-9+& ]+?)*(?= \[| \(| ·| - | –|,|\.|:|\"|\n|$)")
_SMALL_INT_ALLOWANCE = 31        # day counts, bullet numbers, "2 days"


def _walk_numbers(obj, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_numbers(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_numbers(v, out)
    elif isinstance(obj, str):
        for tok in _NUM_RE.findall(obj):
            out.update(_parse_candidates(tok))


def _parse_candidates(tok: str) -> list[float]:
    """Return every plausible reading of a numeric token.

    Number formatting is ambiguous across locales ("1.234" is 1234 in Turkish
    and 1.234 in English), so instead of guessing we return all readings and
    accept the token if *any* reading matches the payload.
    """
    t = tok.strip().strip("+").rstrip(".,")
    neg = t.startswith("-")
    t = t.lstrip("-")
    if not t:
        return []
    readings: set[str] = set()
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            readings.add(t.replace(".", "").replace(",", "."))
        else:
            readings.add(t.replace(",", ""))
    elif "," in t:
        readings.add(t.replace(",", ""))
        readings.add(t.replace(",", "."))
    elif "." in t:
        readings.add(t)
        readings.add(t.replace(".", ""))
    else:
        readings.add(t)
    out = []
    for r in readings:
        try:
            v = float(r)
            out.append(-v if neg else v)
        except ValueError:
            pass
    return out


def _parse_number(tok: str) -> float | None:
    c = _parse_candidates(tok)
    return c[0] if c else None


def allowed_numbers(payload: dict) -> set[float]:
    base: set[float] = set()
    _walk_numbers(payload, base)
    allowed: set[float] = set()
    for v in base:
        allowed.add(v)
        for nd in (0, 1, 2):
            allowed.add(round(v, nd))
        allowed.add(round(v * 100, 0))       # 0.49 -> 49 (%)
        allowed.add(round(v * 100, 1))
        allowed.add(round(1 + v, 1))         # +1.39 -> 2.4x
        allowed.add(round(1 + v, 2))
        allowed.add(round(abs(v), 2)); allowed.add(round(abs(v) * 100, 0))
        allowed.add(round(abs(1 + v), 1))
    allowed.update(float(i) for i in range(0, _SMALL_INT_ALLOWANCE + 1))
    # date parts
    for part in re.findall(r"\d+", payload.get("report_date", "")):
        allowed.add(float(part))
    return allowed


def _close(v: float, allowed: set[float]) -> bool:
    for a in allowed:
        if abs(v - a) <= max(0.05, 0.01 * abs(a)):
            return True
    return False


@dataclass
class ValidationResult:
    ok: bool
    unknown_numbers: list[str] = field(default_factory=list)
    unknown_campaigns: list[str] = field(default_factory=list)
    missing_critical: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        out = []
        if self.unknown_numbers:
            out.append(f"numbers not in data: {self.unknown_numbers}")
        if self.unknown_campaigns:
            out.append(f"campaign-like names not in data: {self.unknown_campaigns}")
        if self.missing_critical:
            out.append(f"critical/chronic items not mentioned: {self.missing_critical}")
        return out


def validate_brief(text: str, payload: dict) -> ValidationResult:
    res = ValidationResult(ok=True)
    allowed = allowed_numbers(payload)

    # --- numbers --------------------------------------------------------
    stripped = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)   # ISO dates already vetted
    for tok in _NUM_RE.findall(stripped):
        cands = _parse_candidates(tok)
        if not cands:
            continue
        if not any(_close(v, allowed) for v in cands):
            res.unknown_numbers.append(tok)

    # --- campaign names -------------------------------------------------
    known = set(_CAMPAIGN_RE.findall(json.dumps(payload, ensure_ascii=False)))
    known = {k.strip() for k in known}
    # anything that looks like "XX | ... | ..." is a campaign reference
    for m in _CAMPAIGN_RE.findall(text):
        name = m.strip()
        if not any(name.startswith(k) or k.startswith(name) for k in known):
            res.unknown_campaigns.append(name)

    # --- completeness ---------------------------------------------------
    must = [(a["campaign_name"]) for a in payload["anomalies"] if a["severity"] == "critical"] \
         + [c["campaign_name"] for c in payload["chronic_issues"]]
    for name in set(must):
        if name not in text:
            res.missing_critical.append(name)

    res.ok = not (res.unknown_numbers or res.unknown_campaigns or res.missing_critical)
    return res


# ---------------------------------------------------------------------------
# 4. Deterministic fallback (no LLM)
# ---------------------------------------------------------------------------
def template_brief(payload: dict) -> str:
    t = payload["totals"]; ld, p7 = t["last_day"], t["prev_7d_daily_avg"]
    lines = [f"**Sabah Brifingi — {payload['report_date']}** (LLM devre dışı, şablon çıktı)", ""]
    crit = [a for a in payload["anomalies"] if a["severity"] == "critical"]
    warn = [a for a in payload["anomalies"] if a["severity"] == "warning"]
    lines.append("**Bugün ne oldu**")
    for a in crit + warn:
        lines.append(f"- [{a['severity'].upper()}] {a['platform']} · {a['campaign_name']} [{a['country']}] · "
                     f"{a['metric']} {a['last_value']} vs 7g ort. {a['baseline_7d']} "
                     f"({a['pct_change']:+.0%}), {a['days_persisting']} gündür sürüyor.")
    for c in payload["chronic_issues"]:
        lines.append(f"- [KRONİK] {c['platform']} · {c['campaign_name']} [{c['country']}] · {c['note']}")
    if not (crit or warn or payload["chronic_issues"]):
        lines.append("- Aksiyon gerektiren anomali yok.")
    lines += ["", "**Toplam**",
              f"Dün: harcama €{ld['spend_eur']}, dönüşüm {ld['conversions']}, ROAS {ld['roas']}, CPA €{ld['cpa_eur']} "
              f"(önceki 7 gün günlük ort.: €{p7['spend_eur']}, {p7['conversions']}, ROAS {p7['roas']}, CPA €{p7['cpa_eur']}).",
              "", "**Veri notları**"]
    for s in payload["systemic_flags"] + payload["data_quality"]:
        lines.append(f"- {s['note']}")
    if payload["anomalies_downgraded_to_reporting_lag"]:
        lines.append(f"- {payload['anomalies_downgraded_to_reporting_lag']} dönüşüm bazlı sinyal raporlama gecikmesi olarak sınıflandırıldı.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Generation
# ---------------------------------------------------------------------------
def _call_model(system: str, messages: list[dict]) -> str:
    import anthropic  # imported lazily so --no-llm works without the SDK
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        system=system,
        messages=messages,
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def generate_brief(result: dict, use_llm: bool = True) -> tuple[str, dict]:
    """Returns (brief_markdown, audit_dict)."""
    payload = build_payload(result)
    audit: dict = {"model": config.LLM_MODEL if use_llm else None, "attempts": [], "source": None}

    if not use_llm or not os.getenv("ANTHROPIC_API_KEY"):
        if use_llm:
            log.warning("ANTHROPIC_API_KEY not set; using template brief")
        audit["source"] = "template"
        text = template_brief(payload)
        audit["validation"] = validate_brief(text, payload).__dict__
        return text, audit

    system, user = render_messages(payload)
    messages = [{"role": "user", "content": user}]
    for attempt in range(1, 3):
        try:
            text = _call_model(system, messages)
        except Exception as exc:  # network / auth / rate limit
            log.error("LLM call failed (attempt %d): %s", attempt, exc)
            audit["attempts"].append({"attempt": attempt, "error": str(exc)})
            break
        v = validate_brief(text, payload)
        audit["attempts"].append({"attempt": attempt, "ok": v.ok, "problems": v.problems()})
        if v.ok:
            audit["source"] = "llm"
            audit["validation"] = v.__dict__
            return text, audit
        log.warning("brief failed validation (attempt %d): %s", attempt, v.problems())
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content":
                      "Your brief failed the grounding audit:\n- " + "\n- ".join(v.problems()) +
                      "\nRewrite it. Use only numbers and campaign names present in the JSON, "
                      "and mention every critical and chronic item."}]

    log.error("falling back to template brief")
    audit["source"] = "template_fallback"
    text = template_brief(payload)
    audit["validation"] = validate_brief(text, payload).__dict__
    return text, audit


def main(use_llm: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    anomalies_path = config.OUTPUT_DIR / "anomalies.json"
    if not anomalies_path.exists():
        raise SystemExit("run anomaly.py first (output/anomalies.json missing)")
    result = json.loads(anomalies_path.read_text())
    text, audit = generate_brief(result, use_llm=use_llm)
    (config.OUTPUT_DIR / "brief.md").write_text(text, encoding="utf-8")
    (config.OUTPUT_DIR / "brief_audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False))
    log.info("brief source=%s validation_ok=%s", audit["source"], audit["validation"]["ok"])


if __name__ == "__main__":
    import sys
    main(use_llm="--no-llm" not in sys.argv)
