"""Delivery layer: push the brief to Slack (incoming webhook) and/or email (SMTP).

Both channels are optional and configured purely through environment variables
so the same code runs locally, in GitHub Actions, or in any scheduler.

    SLACK_WEBHOOK_URL   https://hooks.slack.com/services/...
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD
    EMAIL_FROM, EMAIL_TO (comma separated)

Failures on one channel never block the other; every attempt is reported back
so the run can exit non-zero if nothing was delivered.
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

log = logging.getLogger(__name__)


def _md_to_slack(text: str) -> str:
    """Slack mrkdwn uses *bold* not **bold**."""
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.M)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.M)
    return text.replace("**", "*")


def send_slack(text: str, webhook: str | None = None, timeout: int = 15) -> bool:
    webhook = webhook or os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        log.info("SLACK_WEBHOOK_URL not set; skipping Slack")
        return False
    body = json.dumps({"text": _md_to_slack(text)}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
            # Slack answers a literal "ok". Anything else (e.g. a redirect to
            # slack.com that still returns 200) is a failure, not a success.
            ok = resp.status == 200 and body == "ok"
            log.info("slack status=%s body=%s", resp.status, body[:40])
            return ok
    except Exception as exc:
        log.error("slack delivery failed: %s", exc)
        return False


def send_email(subject: str, text: str) -> bool:
    host, user, pwd = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    sender, to = os.getenv("EMAIL_FROM"), os.getenv("EMAIL_TO")
    if not all([host, user, pwd, sender, to]):
        log.info("SMTP settings incomplete; skipping email")
        return False
    port = int(os.getenv("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(text)
    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(user, pwd)
            smtp.send_message(msg, to_addrs=[a.strip() for a in to.split(",")])
        log.info("email sent to %s", to)
        return True
    except Exception as exc:
        log.error("email delivery failed: %s", exc)
        return False


def deliver(text: str, report_date: str) -> dict[str, bool]:
    subject = f"Sabah Brifingi — {report_date}"
    return {"slack": send_slack(text), "email": send_email(subject, text)}
