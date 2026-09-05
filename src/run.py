"""End-to-end runner: normalize -> detect -> brief -> deliver.

    python src/run.py                 # full run, LLM + delivery (if configured)
    python src/run.py --no-llm        # deterministic template brief
    python src/run.py --no-deliver    # write files only
    python src/run.py --date 2026-09-01   # re-run for a past report date

Exit codes: 0 ok, 1 pipeline error, 2 brief produced but nothing delivered
(only when delivery was requested and at least one channel was configured).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import pandas as pd

import config
from anomaly import detect
from brief import generate_brief
from deliver import deliver
from normalize import build_unified

log = logging.getLogger("run")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Morning brief pipeline")
    p.add_argument("--no-llm", action="store_true", help="skip the LLM, use template brief")
    p.add_argument("--no-deliver", action="store_true", help="do not send to Slack/email")
    p.add_argument("--date", help="report date YYYY-MM-DD (default: latest in data)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config.OUTPUT_DIR.mkdir(exist_ok=True)

    try:
        df, quality = build_unified()
        report_date = pd.Timestamp(args.date) if args.date else None
        result = detect(df, quality, report_date)
        (config.OUTPUT_DIR / "normalized.csv").write_text(df.to_csv(index=False))
        (config.OUTPUT_DIR / "data_quality.json").write_text(
            json.dumps(quality.to_dict(), indent=2, default=str))
        (config.OUTPUT_DIR / "anomalies.json").write_text(
            json.dumps(result, indent=2, default=str))
        log.info("anomalies: %s", result["counts"])

        text, audit = generate_brief(result, use_llm=not args.no_llm)
        (config.OUTPUT_DIR / "brief.md").write_text(text, encoding="utf-8")
        (config.OUTPUT_DIR / "brief_audit.json").write_text(
            json.dumps(audit, indent=2, ensure_ascii=False))
        log.info("brief source=%s validation_ok=%s", audit["source"], audit["validation"]["ok"])
    except Exception as exc:
        log.exception("pipeline failed: %s", exc)
        return 1

    if args.no_deliver:
        return 0
    status = deliver(text, result["report_date"])
    log.info("delivery: %s", status)
    configured = bool(os.getenv("SLACK_WEBHOOK_URL")) or bool(os.getenv("SMTP_HOST"))
    if configured and not any(status.values()):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
