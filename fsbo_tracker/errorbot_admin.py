"""
ErrorBot admin endpoints — action targets for automated playbook execution.

Auth is via a shared webhook secret (ERRORBOT_WEBHOOK_SECRET),
NOT admin password or JWT — ErrorBot is a service, not a human.

Endpoints:
    POST /api/v2/errorbot/restart-pipeline
"""

import asyncio
import logging
import os
import threading
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException

from .error_bot_client import ErrorBotClient

logger = logging.getLogger("fsbo_tracker.errorbot_admin")

_error_bot = ErrorBotClient()

router = APIRouter(prefix="/errorbot", tags=["ErrorBot Admin"])

ERRORBOT_WEBHOOK_SECRET = os.getenv("ERRORBOT_WEBHOOK_SECRET", "")


def _verify_errorbot(x_errorbot_webhook_secret: Optional[str] = Header(None)):
    """Verify the request came from ErrorBot via shared secret."""
    if not ERRORBOT_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="ErrorBot integration not configured")
    if not x_errorbot_webhook_secret or x_errorbot_webhook_secret != ERRORBOT_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    return True


# Track pipeline state at module level
_pipeline_running = False
_pipeline_lock = threading.Lock()


@router.post("/restart-pipeline")
async def restart_pipeline(
    x_errorbot_webhook_secret: Optional[str] = Header(None),
):
    """
    Trigger a full FSBO pipeline run (fetch + score + export).

    Medium risk — kicks off a full data refresh cycle.
    Prevents concurrent runs via lock.
    """
    global _pipeline_running
    _verify_errorbot(x_errorbot_webhook_secret)

    with _pipeline_lock:
        if _pipeline_running:
            return {
                "action": "restart_pipeline",
                "success": True,
                "already_running": True,
                "message": "Pipeline is already running",
            }
        _pipeline_running = True

    workflow_run_id = str(uuid4())

    def _run():
        global _pipeline_running
        flow_id = "fsbo_daily_pipeline"
        step_id = "run_daily"
        try:
            from fsbo_tracker.tracker import run_daily
            logger.info("[ErrorBot] Pipeline restart triggered")
            summary = run_daily()
            logger.info("[ErrorBot] Pipeline complete: %s listings",
                        summary.get("total_fetched", 0))
            # Report success with pipeline summary
            try:
                asyncio.run(_error_bot.report_step(
                    flow_id=flow_id,
                    workflow_run_id=workflow_run_id,
                    step_id=step_id,
                    outcome="success",
                    metadata={
                        "total_fetched": summary.get("total_fetched", 0),
                        "new": summary.get("new", 0),
                        "price_cuts": summary.get("price_cuts", 0),
                        "errors_count": len(summary.get("errors", [])),
                        "trigger": "errorbot_playbook",
                    },
                ))
            except Exception:
                logger.warning("[ErrorBot] report_step failed (non-fatal)")
        except Exception as e:
            logger.error("[ErrorBot] Pipeline error: %s", e, exc_info=True)
            try:
                asyncio.run(_error_bot.report_step(
                    flow_id=flow_id,
                    workflow_run_id=workflow_run_id,
                    step_id=step_id,
                    outcome="failure",
                    error_class=f"{type(e).__module__}.{type(e).__name__}",
                    error_message=str(e)[:500],
                    severity="high",
                    metadata={"trigger": "errorbot_playbook"},
                ))
            except Exception:
                logger.warning("[ErrorBot] report_step failed (non-fatal)")
        finally:
            _pipeline_running = False

    threading.Thread(target=_run, daemon=True).start()

    return {
        "action": "restart_pipeline",
        "success": True,
        "already_running": False,
        "message": "Pipeline started in background",
    }
