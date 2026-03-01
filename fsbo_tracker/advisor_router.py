"""FSBO Tracker — AI Advisor router.

SSE streaming chat endpoint + add-on billing (subscribe/top-up).

Endpoints:
- POST /advisor/chat       — Send message, get SSE stream response
- GET  /advisor/quota       — Check remaining messages
- GET  /advisor/history     — Load conversation history
- POST /advisor/clear       — Clear conversation history
- POST /advisor/subscribe   — Subscribe to advisor add-on ($10/mo)
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

# Advisor add-on Helcim plan
ADVISOR_PLAN = {
    "price_cents": 1000,
    "label": "FSBO Advisor",
    "messages_included": 50,
    "paymentPlanId": os.getenv("ADVISOR_PAYMENT_PLAN_ID"),  # set in Railway
}

TOPUP_PRICE_CENTS = 500
TOPUP_MESSAGES = 25


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str


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
            result = await asyncio.to_thread(advisor_chat_fn, user_id, message)

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
                   advisor_reset_date, advisor_subscription_id, tier
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
        "has_subscription": bool(row.get("advisor_subscription_id")),
        "tier": row["tier"],
        "price": "$10/mo",
        "topup_price": "$5 for 25 messages",
    }


# ---------------------------------------------------------------------------
# Advisor subscribe (initialize + verify)
# ---------------------------------------------------------------------------
@router.post("/subscribe/initialize")
@limiter.limit("3/minute")
async def advisor_subscribe_init(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Initialize Helcim card verification for advisor add-on subscription."""
    user_id = user.get("sub") or user.get("id")

    # Must be on a paid tier to add advisor
    db_user = auth_db.get_user_by_id(user_id)
    if not db_user or db_user.get("tier") in ("free", "guest"):
        raise HTTPException(
            status_code=403,
            detail="Upgrade to a paid plan first, then add the AI Advisor.",
        )

    # Already subscribed?
    if db_user.get("advisor_enabled"):
        return {"success": True, "message": "Advisor already active", "already_active": True}

    if not HELCIM_API_TOKEN:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    session_id = str(uuid.uuid4())

    # Store pending session
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_billing_sessions
                (id, user_id, tier_id, amount_cents, status, created_at)
            VALUES (%s, %s, 'advisor', %s, 'pending', NOW())
        """, (session_id, user_id, ADVISOR_PLAN["price_cents"]))

    # Initialize Helcim verify session
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
                    "paymentType": "verify",
                    "amount": 0,
                    "currency": "USD",
                },
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.error("[ADVISOR] Helcim init error: %s %s",
                             response.status_code, response.text[:500])
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
            "amount_cents": ADVISOR_PLAN["price_cents"],
        }

    except httpx.RequestError as e:
        logger.error("[ADVISOR] Network error: %s", e)
        raise HTTPException(status_code=502, detail="Could not connect to payment provider")


@router.post("/subscribe/verify")
@limiter.limit("3/minute")
async def advisor_subscribe_verify(
    request: Request,
    body: PaymentVerifyRequest,
    user: dict = Depends(get_current_user),
):
    """Verify card and create Helcim subscription for advisor add-on."""
    user_id = user.get("sub") or user.get("id")
    tx_response = body.transaction_response

    # Client-side sanity check
    event_status = tx_response.get("eventStatus")
    tx_status = tx_response.get("transactionStatus") or tx_response.get("status")
    is_success = event_status == "SUCCESS" or tx_status in ["APPROVED", "approved", "1"]

    if not is_success:
        return {"success": False, "message": f"Card verification failed: {event_status or tx_status}"}

    transaction_id = tx_response.get("transactionId") or tx_response.get("id")
    if not transaction_id:
        return {"success": False, "message": "No transaction ID in response"}

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
                return {"success": False, "message": "Transaction verification failed"}

            verified_tx = verify_resp.json()
    except httpx.RequestError as e:
        logger.error("[ADVISOR] Verify network error: %s", e)
        raise HTTPException(status_code=502, detail="Could not verify with payment provider")

    verified_status = verified_tx.get("status") or verified_tx.get("transactionStatus")
    if verified_status not in ["APPROVED", "approved", "1"]:
        return {"success": False, "message": f"Transaction not approved: {verified_status}"}

    customer_code = verified_tx.get("customerCode") or tx_response.get("customerCode")
    if not customer_code:
        return {"success": False, "message": "Card verification did not return customer code"}

    # Load session
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT id, status FROM fsbo_billing_sessions
            WHERE id = %s AND user_id = %s
        """, (body.session_id, user_id))
        session = cur.fetchone()

    if not session:
        raise HTTPException(status_code=404, detail="Billing session not found")

    if session["status"] == "active":
        return {"success": True, "message": "Already processed"}

    if session["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Invalid session status: {session['status']}")

    # Create Helcim subscription
    helcim_subscription_id = None
    plan_id = ADVISOR_PLAN.get("paymentPlanId")

    if plan_id and HELCIM_API_TOKEN:
        try:
            async with httpx.AsyncClient() as client:
                sub_response = await client.post(
                    "https://api.helcim.com/v2/subscriptions",
                    headers={
                        "accept": "application/json",
                        "content-type": "application/json",
                        "api-token": HELCIM_API_TOKEN,
                    },
                    json={
                        "subscriptions": [{
                            "paymentPlanId": int(plan_id),
                            "customerCode": customer_code,
                            "recurringAmount": ADVISOR_PLAN["price_cents"] / 100,
                        }],
                    },
                    timeout=30.0,
                )

                if sub_response.status_code in [200, 201]:
                    sub_data = sub_response.json()
                    if isinstance(sub_data, list) and sub_data:
                        sub_data = sub_data[0]
                    elif isinstance(sub_data, dict) and "data" in sub_data:
                        items = sub_data["data"]
                        sub_data = items[0] if isinstance(items, list) and items else sub_data
                    helcim_subscription_id = sub_data.get("subscriptionId") or sub_data.get("id")
                else:
                    logger.error("[ADVISOR] Subscription create failed: %s %s",
                                 sub_response.status_code, sub_response.text[:500])
                    return {"success": False, "message": "Subscription setup failed. Card was not charged."}
        except Exception as e:
            logger.error("[ADVISOR] Subscription create error: %s", e)
            return {"success": False, "message": "Could not set up subscription. Please try again."}
    elif BILLING_DEV_MODE:
        logger.warning("[ADVISOR] DEV MODE: skipping Helcim subscription")
    elif not plan_id:
        logger.error("[ADVISOR] ADVISOR_PAYMENT_PLAN_ID not configured")
        return {"success": False, "message": "Billing not fully configured yet."}

    # Atomic update: activate advisor + update session + bump token_version
    now = datetime.now(timezone.utc)
    reset_date = (now + timedelta(days=30)).date()

    with db_cursor() as (conn, cur):
        # Lock + verify session
        cur.execute("""
            SELECT status FROM fsbo_billing_sessions
            WHERE id = %s FOR UPDATE
        """, (body.session_id,))
        locked = cur.fetchone()
        if not locked or locked["status"] != "pending":
            return {"success": True, "message": "Already processed"}

        # Activate advisor on user + bump token_version
        cur.execute("""
            UPDATE fsbo_users SET
                advisor_enabled = true,
                advisor_messages_used = 0,
                advisor_messages_limit = %s,
                advisor_reset_date = %s,
                advisor_subscription_id = %s,
                token_version = COALESCE(token_version, 0) + 1
            WHERE id = %s
        """, (ADVISOR_PLAN["messages_included"], reset_date,
              helcim_subscription_id, user_id))

        # Update session
        cur.execute("""
            UPDATE fsbo_billing_sessions SET
                status = 'active',
                helcim_transaction_id = %s,
                helcim_customer_code = %s,
                helcim_subscription_id = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (transaction_id, customer_code, helcim_subscription_id,
              body.session_id))

    logger.info("[ADVISOR] User %s activated advisor add-on", user_id)

    try:
        from .slack_alerts import get_alerter
        get_alerter().notify_subscription(
            user.get("email", "unknown"), "advisor",
            ADVISOR_PLAN["price_cents"],
        )
    except Exception:
        pass

    return {
        "success": True,
        "message": "FSBO Advisor activated! You have 50 messages this month.",
        "messages_limit": ADVISOR_PLAN["messages_included"],
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

    # Must have advisor enabled
    db_user = auth_db.get_user_by_id(user_id)
    if not db_user or not db_user.get("advisor_enabled"):
        raise HTTPException(status_code=403, detail="Subscribe to FSBO Advisor first")

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

    # Client-side sanity check (same pattern as subscribe/verify)
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
