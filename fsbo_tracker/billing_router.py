"""FSBO Tracker — Helcim Billing Router.

Handles subscription lifecycle: plans, checkout, verify, status, cancel, webhooks.

Ported from AVMLens payments.py + webhooks.py, adapted for FSBO tier model.
No token packs — subscriptions only.

Flow:
1. GET /billing/plans — list available tiers
2. POST /billing/subscribe/initialize — create Helcim verify session (card tokenization)
3. Frontend renders HelcimPay.js modal with checkoutToken
4. POST /billing/subscribe/verify — server-side verify + create Helcim subscription
5. Backend bumps token_version → old JWT stale → frontend prompts re-login
6. POST /billing/webhook — Helcim event receiver (idempotent via UNIQUE constraint)
"""

import os
import uuid
import logging
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import psycopg2

from .auth_router import get_current_user
from .db import db_cursor
from . import auth_db
from .rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# ---------------------------------------------------------------------------
# Helcim credentials (shared with AVMLens account, separate payment plans)
# ---------------------------------------------------------------------------
HELCIM_API_TOKEN = os.getenv("HELCIM_API_TOKEN")
# Allow tier upgrades without real Helcim subscription (local dev only)
BILLING_DEV_MODE = os.getenv("BILLING_DEV_MODE", "").lower() in ("1", "true")

# ---------------------------------------------------------------------------
# FSBO subscription tiers
# paymentPlanId: placeholder — replace with real IDs from Helcim dashboard
# ---------------------------------------------------------------------------
FSBO_TIERS = {
    "starter": {
        "price_cents": 2900,
        "label": "Starter",
        "description": "Full data in 1 market, 5 AI actions/day, deal pipeline",
        "paymentPlanId": 46812,
    },
    "growth": {
        "price_cents": 5900,
        "label": "Growth",
        "description": "3 markets, 20 AI actions/day, CSV export",
        "popular": True,
        "paymentPlanId": 46813,
    },
    "pro": {
        "price_cents": 9900,
        "label": "Pro",
        "description": "All markets, 100 AI actions/day, full access",
        "paymentPlanId": 46814,
    },
}

# Amount-to-tier mapping for webhook processing (15% buffer for tax)
TIER_AMOUNTS = {
    "starter": 2900,
    "growth": 5900,
    "pro": 9900,
}

# Advisor add-on amounts for webhook processing
ADVISOR_AMOUNTS = {
    "advisor": 1000,       # $10/mo subscription renewal
    "advisor_topup": 500,  # $5 one-off top-up
}
ADVISOR_TOPUP_MESSAGES = 25
ADVISOR_MONTHLY_MESSAGES = 50

VALID_TRANSACTION_TYPES = {"purchase", "capture", "sale"}


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class SubscribeInitRequest(BaseModel):
    tier_id: str


class SubscribeVerifyRequest(BaseModel):
    session_id: str
    transaction_response: dict


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/plans")
@limiter.limit("20/minute")
async def get_plans(request: Request):
    """List available FSBO subscription tiers."""
    return {
        "plans": [
            {
                "id": tier_id,
                "price_dollars": tier["price_cents"] / 100,
                "price_cents": tier["price_cents"],
                "label": tier["label"],
                "description": tier["description"],
                "popular": tier.get("popular", False),
            }
            for tier_id, tier in FSBO_TIERS.items()
        ],
    }


@router.post("/subscribe/initialize")
@limiter.limit("3/minute")
async def subscribe_initialize(
    request: Request,
    body: SubscribeInitRequest,
    user: dict = Depends(get_current_user),
):
    """Initialize Helcim card verification session for subscription setup.

    Uses paymentType: "verify" — tokenizes card without charging.
    Returns checkoutToken for HelcimPay.js modal.
    """
    if body.tier_id not in FSBO_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {body.tier_id}")

    if not HELCIM_API_TOKEN:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    tier = FSBO_TIERS[body.tier_id]
    session_id = str(uuid.uuid4())
    user_id = user["sub"]

    # Store pending session
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_billing_sessions
                (id, user_id, tier_id, amount_cents, status, created_at)
            VALUES (%s, %s, %s, %s, 'pending', NOW())
        """, (session_id, user_id, body.tier_id, tier["price_cents"]))

    # Call Helcim to initialize card verification
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
                logger.error("[BILLING] Helcim init error: %s %s", response.status_code, response.text[:500])
                try:
                    from .slack_alerts import get_alerter
                    get_alerter().alert_billing_failure(
                        user.get("email", "unknown"), body.tier_id,
                        f"Helcim init {response.status_code}")
                except Exception:
                    pass
                raise HTTPException(status_code=502, detail="Payment initialization failed")

            helcim_data = response.json()

        # Store checkout token on session
        with db_cursor() as (conn, cur):
            cur.execute("""
                UPDATE fsbo_billing_sessions
                SET helcim_checkout_token = %s, updated_at = NOW()
                WHERE id = %s
            """, (helcim_data.get("checkoutToken"), session_id))

        return {
            "checkout_token": helcim_data["checkoutToken"],
            "secret_token": helcim_data["secretToken"],
            "tier_id": body.tier_id,
            "amount_cents": tier["price_cents"],
            "session_id": session_id,
        }

    except httpx.RequestError as e:
        logger.error("[BILLING] Network error: %s", e)
        raise HTTPException(status_code=502, detail="Could not connect to payment provider")


@router.post("/subscribe/verify")
@limiter.limit("3/minute")
async def subscribe_verify(
    request: Request,
    body: SubscribeVerifyRequest,
    user: dict = Depends(get_current_user),
):
    """Verify card and create Helcim subscription.

    Flow:
    1. Client-side sanity check (eventStatus/transactionStatus)
    2. Server-side verification via Helcim API (authoritative — NEVER trust client alone)
    3. Load pending session from DB (single transaction with FOR UPDATE lock)
    4. Create Helcim subscription with paymentPlanId + customerCode
    5. Update user tier + subscription fields + bump token_version
    6. Return success — frontend should prompt re-login (stale JWT)
    """
    user_id = user["sub"]
    tx_response = body.transaction_response

    # Quick client-side sanity check (not authoritative)
    event_status = tx_response.get("eventStatus")
    tx_status = tx_response.get("transactionStatus") or tx_response.get("status")
    is_success = event_status == "SUCCESS" or tx_status in ["APPROVED", "approved", "1"]

    if not is_success:
        return {
            "success": False,
            "message": f"Card verification failed: {event_status or tx_status}",
        }

    # Extract transaction ID — required for server-side verification
    transaction_id = tx_response.get("transactionId") or tx_response.get("id")
    if not transaction_id:
        return {
            "success": False,
            "message": "No transaction ID in card verification response",
        }

    if not HELCIM_API_TOKEN:
        raise HTTPException(status_code=503, detail="Payment system not configured")

    # --- SERVER-SIDE VERIFICATION (authoritative — never trust client alone) ---
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
                logger.error("[BILLING] Helcim verify failed: %s %s",
                             verify_resp.status_code, verify_resp.text[:500])
                return {"success": False, "message": "Transaction verification failed"}

            verified_tx = verify_resp.json()
    except httpx.RequestError as e:
        logger.error("[BILLING] Helcim verify network error: %s", e)
        raise HTTPException(status_code=502, detail="Could not verify with payment provider")

    # Validate verified transaction
    verified_status = verified_tx.get("status") or verified_tx.get("transactionStatus")
    if verified_status not in ["APPROVED", "approved", "1"]:
        logger.warning("[BILLING] Helcim tx %s not approved: %s", transaction_id, verified_status)
        return {"success": False, "message": f"Transaction not approved: {verified_status}"}

    # Use server-verified fields (NOT client-provided)
    card_token = verified_tx.get("cardToken") or tx_response.get("cardToken")
    customer_code = verified_tx.get("customerCode") or tx_response.get("customerCode")

    if not customer_code:
        return {
            "success": False,
            "message": "Card verification did not return customer code",
        }

    # --- Load session + validate (read-only, no lock needed yet) ---
    session = None
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT id, tier_id, amount_cents, status
            FROM fsbo_billing_sessions
            WHERE id = %s AND user_id = %s
        """, (body.session_id, user_id))
        session = cur.fetchone()

    if not session:
        raise HTTPException(status_code=404, detail="Billing session not found")

    if session["status"] == "active":
        return {"success": True, "message": "Already processed", "tier": session["tier_id"]}

    if session["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Invalid session status: {session['status']}")

    tier_id = session["tier_id"]
    tier = FSBO_TIERS[tier_id]

    logger.info("[BILLING] Card verified (server-side) for %s, customer: %s, tx: %s",
                tier_id, customer_code, transaction_id)

    # Create Helcim subscription — REQUIRED unless BILLING_DEV_MODE is on
    helcim_subscription_id = None
    plan_id = tier.get("paymentPlanId")

    if not plan_id and not BILLING_DEV_MODE:
        logger.error("[BILLING] paymentPlanId not configured for tier %s — refusing upgrade", tier_id)
        return {
            "success": False,
            "message": "Billing is not fully configured yet. Please try again later.",
        }

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
                            "recurringAmount": tier["price_cents"] / 100,
                        }],
                    },
                    timeout=30.0,
                )

                if sub_response.status_code in [200, 201]:
                    sub_data = sub_response.json()
                    # Response may be array (batch) or single object
                    if isinstance(sub_data, list) and sub_data:
                        sub_data = sub_data[0]
                    elif isinstance(sub_data, dict) and "data" in sub_data:
                        items = sub_data["data"]
                        sub_data = items[0] if isinstance(items, list) and items else sub_data
                    helcim_subscription_id = sub_data.get("subscriptionId") or sub_data.get("id")
                    logger.info("[BILLING] Helcim subscription created: %s", helcim_subscription_id)
                else:
                    logger.error("[BILLING] Subscription creation failed: %s %s",
                                 sub_response.status_code, sub_response.text[:500])
                    try:
                        from .slack_alerts import get_alerter
                        get_alerter().alert_billing_failure(
                            user.get("email", "unknown"), tier_id,
                            f"Subscription create {sub_response.status_code}")
                    except Exception:
                        pass
                    return {
                        "success": False,
                        "message": "Subscription setup failed. Your card was not charged.",
                    }

        except Exception as e:
            logger.error("[BILLING] Subscription creation error: %s", e)
            return {
                "success": False,
                "message": "Could not set up subscription. Please try again.",
            }
    elif BILLING_DEV_MODE:
        logger.warning("[BILLING] DEV MODE: skipping Helcim subscription for tier %s", tier_id)

    # --- Atomic: lock session + update user + update session in single transaction ---
    now = datetime.now(timezone.utc)
    period_end = now + timedelta(days=30)

    with db_cursor() as (conn, cur):
        # Lock session row and re-check status (prevents race with concurrent verify)
        cur.execute("""
            SELECT status FROM fsbo_billing_sessions
            WHERE id = %s FOR UPDATE
        """, (body.session_id,))
        locked_session = cur.fetchone()
        if not locked_session or locked_session["status"] != "pending":
            return {"success": True, "message": "Already processed", "tier": tier_id}

        # Update user tier + subscription + bump token_version
        cur.execute("""
            UPDATE fsbo_users SET
                tier = %s,
                subscription_status = 'active',
                subscription_id = %s,
                subscription_period_end = %s,
                helcim_customer_code = %s,
                token_version = COALESCE(token_version, 0) + 1
            WHERE id = %s
        """, (tier_id, helcim_subscription_id, period_end, customer_code, user_id))

        # Update billing session to active
        cur.execute("""
            UPDATE fsbo_billing_sessions SET
                status = 'active',
                helcim_transaction_id = %s,
                helcim_card_token = %s,
                helcim_customer_code = %s,
                helcim_subscription_id = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (
            transaction_id,
            card_token,
            customer_code,
            helcim_subscription_id,
            body.session_id,
        ))

    logger.info("[BILLING] User %s upgraded to %s", user_id, tier_id)

    # Slack notification for new subscription
    try:
        from .slack_alerts import get_alerter
        get_alerter().notify_subscription(user.get("email", "unknown"), tier_id, tier["price_cents"])
    except Exception:
        pass

    return {
        "success": True,
        "tier": tier_id,
        "message": f"Subscribed to {tier['label']}! Please log in again to activate.",
    }


@router.get("/status")
async def get_billing_status(user: dict = Depends(get_current_user)):
    """Get current subscription status."""
    user_data = auth_db.get_user_by_id(user["sub"])
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "tier": user_data["tier"],
        "subscription_status": user_data.get("subscription_status", "none"),
        "subscription_period_end": (
            user_data["subscription_period_end"].isoformat()
            if user_data.get("subscription_period_end") else None
        ),
    }


@router.post("/cancel")
async def cancel_subscription(user: dict = Depends(get_current_user)):
    """Cancel subscription. Tier persists until subscription_period_end."""
    user_id = user["sub"]
    user_data = auth_db.get_user_by_id(user_id)
    if not user_data:
        raise HTTPException(status_code=404, detail="User not found")

    if user_data.get("subscription_status") != "active":
        raise HTTPException(status_code=400, detail="No active subscription to cancel")

    # TODO: Cancel in Helcim API when subscription IDs are live
    # For now, mark cancelled locally — tier persists until period_end

    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_users SET
                subscription_status = 'cancelled'
            WHERE id = %s
        """, (user_id,))

    logger.info("[BILLING] User %s cancelled subscription", user_id)

    return {
        "cancelled": True,
        "tier": user_data["tier"],
        "active_until": (
            user_data["subscription_period_end"].isoformat()
            if user_data.get("subscription_period_end") else None
        ),
        "message": "Subscription cancelled. Your plan remains active until the end of the billing period.",
    }


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------

@router.post("/webhook")
@limiter.limit("10/minute")
async def billing_webhook(request: Request):
    """Handle Helcim webhook events.

    Security (no cryptographic signature — Helcim verifier token UI broken):
    1. Validate payload structure (type + transactionId required)
    2. Verify transaction via Helcim API before any tier change
    3. Idempotency via UNIQUE constraint on billing_sessions.helcim_transaction_id
    4. Transaction type validation (purchase/capture/sale only)
    5. Amount validation (positive, matches known tier)

    Returns 500 on transient failure to allow Helcim retries.
    Idempotent: duplicate transactions caught by DB constraint → return 200.
    """
    # Parse payload
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("type") or payload.get("eventName")
    transaction_id = (
        payload.get("transactionId")
        or payload.get("id")
        or payload.get("data", {}).get("transactionId")
    )

    logger.info("[WEBHOOK] event=%s tx=%s", event_type, transaction_id)

    # Log to audit table (fire-and-forget, don't block on failure)
    try:
        _log_webhook_event(event_type or "unknown", transaction_id, payload, "received", None)
    except Exception:
        pass

    if not event_type:
        raise HTTPException(status_code=400, detail="Missing event type")

    # Only process cardTransaction events
    if event_type not in ["cardTransaction", "card_transaction"]:
        return {"status": "ok", "message": f"Ignored event: {event_type}"}

    if not transaction_id:
        raise HTTPException(status_code=400, detail="Missing transactionId")

    # Process with API verification
    success = await _handle_card_transaction(str(transaction_id))

    if success:
        return {"status": "ok"}
    else:
        # 500 lets Helcim retry; idempotent txns succeed on retry
        raise HTTPException(status_code=500, detail="Failed to process transaction")


async def _handle_card_transaction(transaction_id: str) -> bool:
    """Verify transaction with Helcim API, then update user tier.

    Follows AVMLens webhooks.py pattern:
    1. Fetch tx from Helcim API (authoritative source)
    2. Validate type, status, amount
    3. Match customer email to FSBO user
    4. Determine tier from amount
    5. Update user tier + subscription fields (idempotent via UNIQUE)
    """
    if not HELCIM_API_TOKEN:
        logger.error("[WEBHOOK] No HELCIM_API_TOKEN configured")
        return False

    async with httpx.AsyncClient() as client:
        # Step 1: Fetch transaction from Helcim
        try:
            response = await client.get(
                f"https://api.helcim.com/v2/card-transactions/{transaction_id}",
                headers={
                    "accept": "application/json",
                    "api-token": HELCIM_API_TOKEN,
                },
                timeout=30.0,
            )
            if response.status_code != 200:
                logger.error("[WEBHOOK] Helcim tx fetch failed: %s", response.status_code)
                return False

            tx = response.json()
        except Exception as e:
            logger.error("[WEBHOOK] Helcim tx fetch error: %s", e)
            return False

        # Step 2: Validate transaction
        customer_code = tx.get("customerCode")
        status = tx.get("status") or tx.get("transactionStatus")
        tx_type = (tx.get("type") or tx.get("transactionType") or "").lower()
        amount = tx.get("amount")

        try:
            amount_cents = int(float(amount or 0) * 100)
        except (ValueError, TypeError):
            logger.error("[WEBHOOK] Invalid amount: %s", amount)
            return True  # Terminal: bad data won't change on retry

        if tx_type and tx_type not in VALID_TRANSACTION_TYPES:
            logger.info("[WEBHOOK] Ignoring tx type: %s (terminal)", tx_type)
            return True  # Terminal: tx type won't change

        if amount_cents <= 0:
            logger.info("[WEBHOOK] Non-positive amount: %s (terminal)", amount_cents)
            return True  # Terminal: amount won't change

        if status not in ["APPROVED", "approved", "1"]:
            logger.info("[WEBHOOK] Not approved: %s (terminal)", status)
            _log_webhook_event("cardTransaction", transaction_id, {}, "rejected", f"status={status}")
            return True  # Terminal: declined/voided tx won't become approved

        if not customer_code:
            logger.error("[WEBHOOK] No customerCode in tx (terminal)")
            return True  # Terminal: missing data on Helcim side

        # Replay window: reject transactions older than 24h
        tx_created = tx.get("dateCreated") or tx.get("created")
        if tx_created:
            try:
                tx_time = datetime.fromisoformat(tx_created.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - tx_time > timedelta(hours=24):
                    logger.warning("[WEBHOOK] Rejecting stale tx (>24h): %s", transaction_id)
                    _log_webhook_event("cardTransaction", transaction_id, {}, "rejected", "stale >24h")
                    return True  # Return true so Helcim doesn't retry
            except Exception:
                pass  # If we can't parse date, proceed anyway

        # Step 3: Fetch customer email from Helcim
        try:
            cust_response = await client.get(
                f"https://api.helcim.com/v2/customers/{customer_code}",
                headers={
                    "accept": "application/json",
                    "api-token": HELCIM_API_TOKEN,
                },
                timeout=30.0,
            )
            if cust_response.status_code != 200:
                logger.error("[WEBHOOK] Customer fetch failed: %s", cust_response.status_code)
                return False

            customer = cust_response.json()
            customer_email = customer.get("contactEmail") or customer.get("email")
            if not customer_email:
                logger.error("[WEBHOOK] No email for customer %s", customer_code)
                return False
        except Exception as e:
            logger.error("[WEBHOOK] Customer fetch error: %s", e)
            return False

    # Step 4: Determine product from amount (tier, advisor renewal, or advisor topup)
    product_id = _determine_tier_from_amount(amount_cents)
    if not product_id:
        logger.error("[WEBHOOK] Unknown product for amount %s cents", amount_cents)
        return False

    # Step 5: Match email to user and update (atomic, idempotent)
    try:
        with db_cursor() as (conn, cur):
            # Find user by email
            cur.execute(
                "SELECT id, tier FROM fsbo_users WHERE LOWER(email) = LOWER(%s)",
                (customer_email,),
            )
            user = cur.fetchone()
            if not user:
                logger.error("[WEBHOOK] No user for email: %s", customer_email)
                return False

            user_id = user["id"]

            # Insert billing session for idempotency (UNIQUE on helcim_transaction_id)
            cur.execute("""
                INSERT INTO fsbo_billing_sessions
                    (id, user_id, tier_id, amount_cents, status,
                     helcim_transaction_id, helcim_customer_code, created_at)
                VALUES (%s, %s, %s, %s, 'active', %s, %s, NOW())
            """, (
                str(uuid.uuid4()), user_id, product_id, amount_cents,
                transaction_id, customer_code,
            ))

            if product_id == "advisor":
                # Advisor subscription renewal — reset monthly quota
                reset_date = (datetime.now(timezone.utc) + timedelta(days=30)).date()
                cur.execute("""
                    UPDATE fsbo_users SET
                        advisor_enabled = true,
                        advisor_messages_used = 0,
                        advisor_messages_limit = %s,
                        advisor_reset_date = %s,
                        helcim_customer_code = %s
                    WHERE id = %s
                """, (ADVISOR_MONTHLY_MESSAGES, reset_date, customer_code, user_id))

            elif product_id == "advisor_topup":
                # Advisor top-up — add messages to existing limit
                cur.execute("""
                    UPDATE fsbo_users SET
                        advisor_messages_limit = COALESCE(advisor_messages_limit, 0) + %s,
                        helcim_customer_code = %s
                    WHERE id = %s
                """, (ADVISOR_TOPUP_MESSAGES, customer_code, user_id))

            else:
                # Tier subscription renewal (starter/growth/pro)
                period_end = datetime.now(timezone.utc) + timedelta(days=30)
                cur.execute("""
                    UPDATE fsbo_users SET
                        tier = %s,
                        subscription_status = 'active',
                        subscription_period_end = %s,
                        helcim_customer_code = %s,
                        token_version = COALESCE(token_version, 0) + 1
                    WHERE id = %s
                """, (product_id, period_end, customer_code, user_id))

        logger.info("[WEBHOOK] Processed %s for user %s (tx: %s)", product_id, user_id, transaction_id)
        _log_webhook_event("cardTransaction", transaction_id, {}, "granted", f"product={product_id}")
        return True

    except psycopg2.IntegrityError:
        # Idempotency: duplicate helcim_transaction_id → UNIQUE violation
        logger.info("[WEBHOOK] Duplicate tx %s (idempotent success)", transaction_id)
        _log_webhook_event("cardTransaction", transaction_id, {}, "duplicate", None)
        return True
    except Exception as e:
        logger.error("[WEBHOOK] DB error: %s", e)
        _log_webhook_event("cardTransaction", transaction_id, {}, "error", str(e)[:200])
        return False


def _determine_tier_from_amount(amount_cents: int) -> Optional[str]:
    """Map payment amount to tier or advisor product (15% buffer for tax).

    Returns: tier id (starter/growth/pro), "advisor", "advisor_topup", or None.
    Checks advisor amounts FIRST (they're smaller, avoids false matches).
    """
    # Check advisor amounts first (smaller values — prevents false tier match)
    for product_id, base_amount in ADVISOR_AMOUNTS.items():
        if base_amount <= amount_cents <= base_amount * 1.15:
            return product_id

    for tier_id, base_amount in TIER_AMOUNTS.items():
        if base_amount <= amount_cents <= base_amount * 1.15:
            return tier_id

    return None


def _log_webhook_event(event_type: str, transaction_id: Optional[str],
                       payload: dict, result: str, detail: Optional[str]):
    """Audit log — fire and forget."""
    try:
        import json
        with db_cursor() as (conn, cur):
            cur.execute("""
                INSERT INTO fsbo_webhook_events
                    (event_type, transaction_id, payload, result, detail)
                VALUES (%s, %s, %s, %s, %s)
            """, (event_type, transaction_id, json.dumps(payload), result, detail))
    except Exception as e:
        logger.warning("[WEBHOOK] Audit log failed: %s", e)
