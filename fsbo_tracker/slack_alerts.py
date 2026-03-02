"""
FSBO Tracker — Slack alerting.

All messages prefixed with [FSBO] to distinguish from AVMLens alerts
in the same Slack workspace. Rate-limited per alert type to prevent spam.
"""

import os
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger("fsbo.slack_alerts")

PREFIX = "[FSBO]"


class SlackAlerter:
    """Send alerts to Slack with rate limiting."""

    def __init__(self):
        self.webhook_url = os.getenv("SLACK_WEBHOOK_URL")
        self.enabled = os.getenv("SLACK_ALERTS_ENABLED", "false").lower() == "true"
        cooldown_minutes = int(os.getenv("SLACK_ALERT_COOLDOWN_MINUTES", "5"))

        self._last_alerts: Dict[str, datetime] = {}
        self._alert_cooldown = timedelta(minutes=cooldown_minutes)
        # Stale pipeline alert is capped to 1 per day regardless of global cooldown
        self._stale_pipeline_cooldown = timedelta(hours=24)

    def _can_send(self, alert_key: str) -> bool:
        now = datetime.utcnow()
        last = self._last_alerts.get(alert_key)
        if last and (now - last) < self._alert_cooldown:
            return False
        self._last_alerts[alert_key] = now
        return True

    def _send(self, message: dict) -> bool:
        """Fire-and-forget Slack post on background thread (never blocks request path)."""
        if not self.enabled or not self.webhook_url:
            return False
        url = self.webhook_url
        def _post():
            try:
                requests.post(url, json=message, timeout=5)
            except Exception as e:
                logger.error("Slack send failed: %s", e)
        threading.Thread(target=_post, daemon=True).start()
        return True

    # --- URGENT ---

    def alert_error(self, error_type: str, message: str, request_id: Optional[str] = None):
        """500-level server error."""
        if not self._can_send(f"error:{error_type}"):
            return
        self._send({
            "text": f"{PREFIX} ERROR: {error_type}",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{PREFIX} Server Error"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*Type:*\n{error_type}"},
                    {"type": "mrkdwn", "text": f"*Request ID:*\n`{request_id or 'unknown'}`"},
                    {"type": "mrkdwn", "text": f"*Time:*\n{datetime.utcnow().strftime('%H:%M:%S UTC')}"},
                    {"type": "mrkdwn", "text": f"*Detail:*\n{message[:200]}"},
                ]},
            ],
        })

    def alert_billing_failure(self, user_email: str, tier: str, error: str):
        """Payment flow failure."""
        if not self._can_send(f"billing:{user_email}"):
            return
        self._send({
            "text": f"{PREFIX} BILLING FAILURE: {tier}",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{PREFIX} Billing Failure"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": f"*User:*\n{user_email}"},
                    {"type": "mrkdwn", "text": f"*Tier:*\n{tier}"},
                    {"type": "mrkdwn", "text": f"*Error:*\n{error[:200]}"},
                    {"type": "mrkdwn", "text": f"*Time:*\n{datetime.utcnow().strftime('%H:%M:%S UTC')}"},
                ]},
            ],
        })

    def alert_scraper_failure(self, market: str, source: str, error: str):
        """Pipeline scrape failure."""
        if not self._can_send(f"scraper:{market}:{source}"):
            return
        self._send({
            "text": f"{PREFIX} SCRAPER FAIL: {source} in {market}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"*{PREFIX} Scraper Failure*\n*Market:* {market}\n*Source:* {source}\n*Error:* {error[:200]}"}},
            ],
        })

    def alert_database_error(self, error: str):
        """Database connection/query failure."""
        if not self._can_send("database_error"):
            return
        self._send({
            "text": f"{PREFIX} DATABASE ERROR",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{PREFIX} Database Error"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{error[:500]}```"}},
            ],
        })

    # --- INFO ---

    def notify_signup(self, email: str):
        """New user registration."""
        self._send({
            "text": f"{PREFIX} New signup: {email}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"{PREFIX} *New Signup*\n{email} — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"}},
            ],
        })

    def notify_subscription(self, email: str, tier: str, amount_cents: int):
        """New paid subscription."""
        self._send({
            "text": f"{PREFIX} New subscription: {email} → {tier} (${amount_cents/100:.0f}/mo)",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"{PREFIX} *New Subscription*\n*User:* {email}\n*Tier:* {tier}\n*Amount:* ${amount_cents/100:.0f}/mo"}},
            ],
        })

    def alert_stale_pipeline(self, run_at: str, markets_checked: int):
        """Daily run completed with 0 new listings — pipeline may be broken. Max 1/day."""
        now = datetime.utcnow()
        last = self._last_alerts.get("stale_pipeline")
        if last and (now - last) < self._stale_pipeline_cooldown:
            return
        self._last_alerts["stale_pipeline"] = now
        self._send({
            "text": f"{PREFIX} PIPELINE: 0 new listings found across {markets_checked} markets",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{PREFIX} Zero New Listings Today"}},
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"*Daily run completed but found 0 new listings across all {markets_checked} markets.*\n"
                            f"Run time: {run_at}\n"
                            f"Check proxy health, source availability, and Railway cron."}},
            ],
        })

    def send_test_alert(self) -> Dict[str, Any]:
        """Verify Slack integration works."""
        success = self._send({
            "text": f"{PREFIX} Test alert",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": f"*{PREFIX} Test Alert*\nSlack integration working.\n{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"}},
            ],
        })
        return {"success": success, "enabled": self.enabled, "webhook_configured": bool(self.webhook_url)}


# Singleton
_alerter: Optional[SlackAlerter] = None
_lock = threading.Lock()


def get_alerter() -> SlackAlerter:
    global _alerter
    if _alerter is None:
        with _lock:
            if _alerter is None:
                _alerter = SlackAlerter()
    return _alerter
