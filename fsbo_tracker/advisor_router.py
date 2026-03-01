"""FSBO Tracker — AI Advisor router.

SSE streaming chat endpoint + add-on billing (activate/cancel/top-up).

Endpoints:
- POST /advisor/chat       — Send message, get SSE stream response
- GET  /advisor/quota       — Check remaining messages
- GET  /advisor/history     — Load conversation history
- POST /advisor/clear       — Clear conversation history
- POST /advisor/activate    — Add advisor add-on to existing subscription (no card modal)
- POST /advisor/cancel      — Remove advisor add-on (access until period end)
- POST /advisor/topup       — Top up messages ($5 for +25)
- GET  /advisor/status      — Advisor subscription status
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .auth_router import get_current_user
from .db import db_cursor
from . import auth_db
from .rate_limit import limiter

logger = logging.getLogger("fsbo.advisor_router")

router = APIRouter(prefix="/fsbo/advisor", tags=["advisor"])

HELCIM_API_TOKEN = os.getenv("HELCIM_API_TOKEN")
BILLING_DEV_MODE = os.getenv("BILLING_DEV_MODE", "").lower() in ("1", "true")
ADVISOR_ADDON_ID = int(os.getenv("ADVISOR_ADDON_ID") or 0)
ADVISOR_MONTHLY_MESSAGES = 50

TOPUP_PRICE_CENTS = 500
TOPUP_MESSAGES = 25


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    voice: str = "neutral"  # neutral | mentor | aggressive


class PaymentVerifyRequest(BaseModel):
    session_id: str
    transaction_response: dict


# ---------------------------------------------------------------------------
# Chat endpoint (SSE streaming)
# ---------------------------------------------------------------------------
@router.post("/chat")
@limiter.limit("20/minute")
async def advisor_chat(
    request: Request,
    body: ChatRequest,
    user: dict = Depends(get_current_user),
):
    """Send a message to the FSBO Advisor and receive a streaming response.

    Returns SSE (text/event-stream) with events:
    - data: {"type": "text", "content": "..."} — streamed text chunks
    - data: {"type": "tool_use", "tool": "...", "result": {...}} — tool call results
    - data: {"type": "quota", ...} — remaining messages
    - data: {"type": "done"} — stream complete
    - data: {"type": "error", "message": "..."} — error
    """
    user_id = user.get("sub") or user.get("id")
    message = body.message.strip()

    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="Message too long (max 2000 chars)")

    async def event_stream():
        try:
            from .advisor_service import chat as advisor_chat_fn

            # Run sync chat function in thread to avoid blocking event loop
            voice = body.voice if body.voice in ("neutral", "mentor", "aggressive") else "neutral"
            result = await asyncio.to_thread(advisor_chat_fn, user_id, message, voice)

            # Stream tool results first (if any)
            if result.get("tool_results"):
                for tr in result["tool_results"]:
                    yield f"data: {json.dumps({'type': 'tool_use', 'tool': tr['tool'], 'result': tr['result']})}\n\n"

            # Stream the text response
            if result.get("response"):
                yield f"data: {json.dumps({'type': 'text', 'content': result['response']})}\n\n"

            # Send quota info
            if result.get("quota"):
                yield f"data: {json.dumps({'type': 'quota', **result['quota']})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except ValueError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        except Exception as e:
            logger.error("[ADVISOR] Chat error for user %s: %s", user_id, e, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Something went wrong. Please try again.'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Quota check
# ---------------------------------------------------------------------------
@router.get("/quota")
@limiter.limit("30/minute")
async def get_advisor_quota(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Check advisor message quota."""
    user_id = user.get("sub") or user.get("id")

    from .advisor_service import check_advisor_quota
    quota = check_advisor_quota(user_id)

    return {"success": True, **quota}


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------
@router.get("/history")
@limiter.limit("30/minute")
async def get_advisor_history(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Load recent conversation messages."""
    user_id = user.get("sub") or user.get("id")

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT role, content, tool_calls, tool_results, created_at
            FROM fsbo_advisor_messages
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 20
        """, (user_id,))
        rows = cur.fetchall()

    messages = []
    for row in reversed(rows):
        msg = {
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        if row.get("tool_calls"):
            tc = row["tool_calls"]
            if isinstance(tc, str):
                tc = json.loads(tc)
            msg["tool_calls"] = tc
        if row.get("tool_results"):
            tr = row["tool_results"]
            if isinstance(tr, str):
                tr = json.loads(tr)
            msg["tool_results"] = tr
        # Skip internal tool_result messages in display
        if row["role"] != "tool_result":
            messages.append(msg)

    return {"success": True, "messages": messages}


# ---------------------------------------------------------------------------
# Clear conversation
# ---------------------------------------------------------------------------
@router.post("/clear")
@limiter.limit("5/minute")
async def clear_advisor_history(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Clear all advisor conversation history for the user."""
    user_id = user.get("sub") or user.get("id")

    with db_cursor() as (conn, cur):
        cur.execute(
            "DELETE FROM fsbo_advisor_messages WHERE user_id = %s",
            (user_id,),
        )

    return {"success": True, "message": "Conversation cleared"}


# ---------------------------------------------------------------------------
# Advisor subscription status
# ---------------------------------------------------------------------------
@router.get("/status")
@limiter.limit("30/minute")
async def get_advisor_status(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Get advisor add-on subscription status."""
    user_id = user.get("sub") or user.get("id")

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT advisor_enabled, advisor_messages_used, advisor_messages_limit,
                   advisor_reset_date, advisor_addon_status, tier
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "success": True,
        "advisor_enabled": row["advisor_enabled"] or False,
        "messages_used": row["advisor_messages_used"] or 0,
        "messages_limit": row["advisor_messages_limit"] or 0,
        "messages_remaining": max(0, (row["advisor_messages_limit"] or 0) - (row["advisor_messages_used"] or 0)),
        "reset_date": row["advisor_reset_date"].isoformat() if row.get("advisor_reset_date") else None,
        "has_subscription": row.get("advisor_addon_status") == "active",
        "tier": row["tier"],
        "price": "$10/mo",
        "topup_price": "$5 for 25 messages",
    }


# ---------------------------------------------------------------------------
# Advisor activate (add-on to existing subscription — no card modal)
# ---------------------------------------------------------------------------
@router.post("/activate")
@limiter.limit("3/minute")
async def activate_advisor(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Add advisor add-on to existing tier subscription.

    No card modal needed — card already on file from tier subscription.
    Links Helcim add-on to user's subscription; first charge on next billing cycle.
    """
    user_id = user.get("sub") or user.get("id")

    with db_cursor() as (conn, cur):
        # Row lock to prevent concurrent activate calls
        cur.execute("""
            SELECT id, tier, subscription_id, subscription_status,
                   advisor_enabled, advisor_addon_status
            FROM fsbo_users WHERE id = %s FOR UPDATE
        """, (user_id,))
        db_user = cur.fetchone()

        if not db_user:
            raise HTTPException(status_code=404, detail="User not found")

        # Must be on a paid tier with active subscription
        if db_user["tier"] in ("free", "guest") or db_user["subscription_status"] != "active":
            raise HTTPException(
                status_code=403,
                detail="Upgrade to a paid plan first, then add the AI Advisor.",
            )

        if not db_user.get("subscription_id"):
            raise HTTPException(
                status_code=400,
                detail="No active subscription found. Please subscribe to a plan first.",
            )

        # Idempotency: already active → return success
        if db_user.get("advisor_addon_status") == "active":
            return {"success": True, "message": "Advisor already active", "already_active": True}

        sub_id = db_user["subscription_id"]

    if not HELCIM_API_TOKEN:
        raise HTTPException(status_code=503, detail="Payment system not configured")
    if not ADVISOR_ADDON_ID:
        raise HTTPException(status_code=503, detail="ADVISOR_ADDON_ID not configured")

    # Link add-on to subscription via Helcim API
    try:
        async with httpx.AsyncClient() as client:
            addon_resp = await client.post(
                f"https://api.helcim.com/v2/subscriptions/{sub_id}/add-ons",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "api-token": HELCIM_API_TOKEN,
                },
                json={"addOns": [{"addOnId": ADVISOR_ADDON_ID}]},
                timeout=30.0,
            )

            if addon_resp.status_code not in [200, 201]:
                logger.error("[ADVISOR] Add-on link failed: %s %s",
                             addon_resp.status_code, addon_resp.text[:500])
                raise HTTPException(
                    status_code=502,
                    detail="Could not activate advisor. Please try again.",
                )
    except httpx.RequestError as e:
        logger.error("[ADVISOR] Add-on link network error: %s", e)
        raise HTTPException(status_code=502, detail="Could not connect to payment provider")

    # Helcim succeeded — update DB with re-lock to prevent race
    now = datetime.now(timezone.utc)
    reset_date = (now + timedelta(days=30)).date()

    with db_cursor() as (conn, cur):
        # Re-lock + re-check idempotency (closes TOCTOU window)
        cur.execute("""
            SELECT advisor_addon_status FROM fsbo_users
            WHERE id = %s FOR UPDATE
        """, (user_id,))
        recheck = cur.fetchone()
        if recheck and recheck.get("advisor_addon_status") == "active":
            return {"success": True, "message": "Advisor already active", "already_active": True}

        cur.execute("""
            UPDATE fsbo_users SET
                advisor_enabled = true,
                advisor_addon_id = %s,
                advisor_addon_status = 'active',
                advisor_messages_used = 0,
                advisor_messages_limit = %s,
                advisor_reset_date = %s,
                token_version = COALESCE(token_version, 0) + 1
            WHERE id = %s
        """, (ADVISOR_ADDON_ID, ADVISOR_MONTHLY_MESSAGES, reset_date, user_id))

    logger.info("[ADVISOR] User %s activated advisor add-on (sub: %s)", user_id, sub_id)

    try:
        from .slack_alerts import get_alerter
        get_alerter().notify_subscription(user.get("email", "unknown"), "advisor", 1000)
    except Exception:
        pass

    return {
        "success": True,
        "message": "FSBO Advisor activated! You have 50 messages this month.",
        "messages_limit": ADVISOR_MONTHLY_MESSAGES,
    }


# ---------------------------------------------------------------------------
# Advisor cancel (remove add-on)
# ---------------------------------------------------------------------------
@router.post("/cancel")
@limiter.limit("3/minute")
async def cancel_advisor(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Remove advisor add-on from subscription.

    Access continues until next billing cycle (period end).
    Next renewal will omit the $10 add-on.
    """
    user_id = user.get("sub") or user.get("id")

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT subscription_id, advisor_addon_status, subscription_period_end
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        db_user = cur.fetchone()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    if db_user.get("advisor_addon_status") != "active":
        raise HTTPException(status_code=400, detail="No active advisor add-on to cancel")

    sub_id = db_user.get("subscription_id")

    helcim_removed = False
    if sub_id and HELCIM_API_TOKEN:
        try:
            async with httpx.AsyncClient() as client:
                del_resp = await client.delete(
                    f"https://api.helcim.com/v2/subscriptions/{sub_id}/add-ons/{ADVISOR_ADDON_ID}",
                    headers={
                        "accept": "application/json",
                        "api-token": HELCIM_API_TOKEN,
                    },
                    timeout=30.0,
                )
                if del_resp.status_code in [200, 204]:
                    helcim_removed = True
                else:
                    logger.error("[ADVISOR] Add-on remove failed: %s %s",
                                 del_resp.status_code, del_resp.text[:500])
        except Exception as e:
            logger.error("[ADVISOR] Add-on remove error: %s", e)

        if not helcim_removed:
            raise HTTPException(
                status_code=502,
                detail="Could not remove add-on from billing. Please try again or contact support.",
            )

    # Mark cancelled — keep advisor_enabled=true until period end
    # (webhook won't reset quota on next renewal since add-on is removed)
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_users SET
                advisor_addon_status = 'cancelled'
            WHERE id = %s
        """, (user_id,))

    period_end = db_user.get("subscription_period_end")

    logger.info("[ADVISOR] User %s cancelled advisor add-on", user_id)

    return {
        "success": True,
        "message": "Advisor cancelled. You can continue using it until the end of your billing period.",
        "active_until": period_end.isoformat() if period_end else None,
    }


# ---------------------------------------------------------------------------
# Top-up ($5 for +25 messages)
# ---------------------------------------------------------------------------
@router.post("/topup/initialize")
@limiter.limit("3/minute")
async def advisor_topup_init(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Initialize Helcim payment for advisor message top-up."""
    user_id = user.get("sub") or user.get("id")

    # Must have active advisor add-on (not cancelled)
    db_user = auth_db.get_user_by_id(user_id)
    if not db_user or not db_user.get("advisor_enabled"):
        raise HTTPException(status_code=403, detail="Activate FSBO Advisor first")

    if db_user.get("advisor_addon_status") != "active":
        raise HTTPException(status_code=403, detail="Advisor add-on is not active. Reactivate to top up.")

    if not HELCIM_API_TOKEN:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    session_id = str(uuid.uuid4())

    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_billing_sessions
                (id, user_id, tier_id, amount_cents, status, created_at)
            VALUES (%s, %s, 'advisor_topup', %s, 'pending', NOW())
        """, (session_id, user_id, TOPUP_PRICE_CENTS))

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.helcim.com/v2/helcim-pay/initialize",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "api-token": HELCIM_API_TOKEN,
                },
                json={
                    "paymentType": "purchase",
                    "amount": TOPUP_PRICE_CENTS / 100,
                    "currency": "USD",
                },
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error("[ADVISOR] Topup init error: %s", response.text[:500])
                raise HTTPException(status_code=502, detail="Payment initialization failed")

            helcim_data = response.json()

        with db_cursor() as (conn, cur):
            cur.execute("""
                UPDATE fsbo_billing_sessions
                SET helcim_checkout_token = %s, updated_at = NOW()
                WHERE id = %s
            """, (helcim_data.get("checkoutToken"), session_id))

        return {
            "checkout_token": helcim_data["checkoutToken"],
            "secret_token": helcim_data["secretToken"],
            "session_id": session_id,
            "amount_cents": TOPUP_PRICE_CENTS,
        }

    except httpx.RequestError as e:
        logger.error("[ADVISOR] Topup network error: %s", e)
        raise HTTPException(status_code=502, detail="Could not connect to payment provider")


@router.post("/topup/verify")
@limiter.limit("3/minute")
async def advisor_topup_verify(
    request: Request,
    body: PaymentVerifyRequest,
    user: dict = Depends(get_current_user),
):
    """Verify top-up payment and add messages."""
    user_id = user.get("sub") or user.get("id")
    tx_response = body.transaction_response

    # Client-side sanity check
    event_status = tx_response.get("eventStatus")
    tx_status = tx_response.get("transactionStatus") or tx_response.get("status")
    is_success = event_status == "SUCCESS" or tx_status in ["APPROVED", "approved", "1"]

    if not is_success:
        return {"success": False, "message": f"Payment failed: {event_status or tx_status}"}

    transaction_id = tx_response.get("transactionId") or tx_response.get("id")
    if not transaction_id:
        return {"success": False, "message": "No transaction ID"}

    if not HELCIM_API_TOKEN:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    # Server-side verification
    try:
        async with httpx.AsyncClient() as client:
            verify_resp = await client.get(
                f"https://api.helcim.com/v2/card-transactions/{transaction_id}",
                headers={
                    "accept": "application/json",
                    "api-token": HELCIM_API_TOKEN,
                },
                timeout=30.0,
            )
            if verify_resp.status_code != 200:
                return {"success": False, "message": "Verification failed"}

            verified_tx = verify_resp.json()
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail="Could not verify payment")

    verified_status = verified_tx.get("status") or verified_tx.get("transactionStatus")
    if verified_status not in ["APPROVED", "approved", "1"]:
        return {"success": False, "message": f"Payment not approved: {verified_status}"}

    # Verify payment amount matches expected topup price
    verified_amount = verified_tx.get("amount")
    try:
        verified_cents = int(float(verified_amount or 0) * 100)
    except (ValueError, TypeError):
        return {"success": False, "message": "Invalid transaction amount"}

    if verified_cents < TOPUP_PRICE_CENTS:
        logger.warning("[ADVISOR] Topup amount mismatch: expected %d, got %d cents",
                       TOPUP_PRICE_CENTS, verified_cents)
        return {"success": False, "message": "Payment amount does not match"}

    # Atomic: add messages + update session
    with db_cursor() as (conn, cur):
        cur.execute("""
            SELECT status FROM fsbo_billing_sessions
            WHERE id = %s AND user_id = %s FOR UPDATE
        """, (body.session_id, user_id))
        session = cur.fetchone()

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if session["status"] != "pending":
            return {"success": True, "message": "Already processed"}

        cur.execute("""
            UPDATE fsbo_users
            SET advisor_messages_limit = COALESCE(advisor_messages_limit, 0) + %s
            WHERE id = %s
            RETURNING advisor_messages_limit, advisor_messages_used
        """, (TOPUP_MESSAGES, user_id))
        updated = cur.fetchone()

        cur.execute("""
            UPDATE fsbo_billing_sessions SET
                status = 'active',
                helcim_transaction_id = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (transaction_id, body.session_id))

    new_limit = updated["advisor_messages_limit"] or 0
    used = updated["advisor_messages_used"] or 0

    logger.info("[ADVISOR] User %s topped up +%d messages (now %d/%d)",
                user_id, TOPUP_MESSAGES, used, new_limit)

    return {
        "success": True,
        "message": f"+{TOPUP_MESSAGES} messages added!",
        "messages_limit": new_limit,
        "messages_used": used,
        "messages_remaining": new_limit - used,
    }
