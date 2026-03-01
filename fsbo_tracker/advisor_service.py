"""FSBO Tracker — AI Advisor service.

Conversational deal advisor powered by Claude Haiku with tool-calling.
Users can create/manage saved searches, query listings, and get market
insights through natural language.

Cost controls:
- Model: claude-haiku-4-5-20251001 (cheapest capable model)
- max_tokens: 1024 per response
- Context: last 10 messages + system prompt (~2K input tokens/turn)
- Message quota: 50/mo default, $5 top-up for +25

Message metering:
- Each user-visible assistant response = 1 message
- Tool-call rounds (internal tool use + result) are NOT counted
- Quota checked BEFORE API call, debited AFTER success (debit-on-success)
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone, date, timedelta
from typing import Optional

import httpx

from .db import db_cursor
from .config import SEARCHES

logger = logging.getLogger("fsbo.advisor")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024
MAX_TOOL_ROUNDS = 3  # prevent infinite tool-call loops

# Market ID → friendly name mapping for tool context
MARKET_MAP = {s["id"]: s["name"] for s in SEARCHES}
MARKET_IDS = list(MARKET_MAP.keys())

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = f"""You are a residential real estate investment advisor with deep expertise in \
FSBO (For Sale By Owner) deal sourcing, underwriting, and negotiation strategy. You work inside \
the FSBO Deal Tracker platform.

Your background:
- Expert in buy-and-hold, BRRRR, fix-and-flip, and wholesale strategies
- Deep knowledge of FSBO seller psychology — why they sell without agents, common motivations \
(divorce, estate, relocation, financial distress), and how to approach each
- Skilled at reading distress signals in listings: deferred maintenance keywords, price cuts, \
high days-on-market, photo red flags
- Familiar with deal math: ARV, rehab budgets, the 70% rule, cap rates, cash-on-cash returns, \
rent-to-price ratios
- Understands market cycles, seasonal patterns, and metro-level supply/demand dynamics

Your platform capabilities:
- Create and manage saved search alerts matching investor criteria
- Query current active FSBO listings with filters (market, score, price, DOM)
- Pull market-level statistics (listing counts, avg scores, price ranges)
- Explain deal scores: 0-100 composite (keywords 0-40, photos 0-25, price/value 0-20, DOM 0-10, cuts 0-5)

Available markets (use exact IDs when creating searches):
{json.dumps(MARKET_MAP, indent=2)}

Communication style:
- Direct, concise, investor-to-investor tone — no fluff, no disclaimers about "consult a professional"
- Lead with numbers and actionable next steps
- When a user describes a deal, run the math with them (ARV, rehab estimate, offer price)
- Proactively flag risks: foundation issues, flood zones, title concerns, overpriced comps
- If asked about a market you have data for, pull stats with the tool first — never guess

Platform specifics:
- Score ≥60 = high priority, ≥35 = shortlist-worthy, <20 = likely retail-priced
- FSBO types: "fsbo" (pure FSBO, no agent) and "mlsfsbo" (flat-fee MLS listing, technically FSBO)
- Confirm search criteria before saving
- You can discuss general RE investing topics freely — you're not limited to platform features
"""

VOICE_OVERLAYS = {
    "neutral": "",  # default — no overlay needed
    "mentor": (
        "\n\nVoice adjustment: You are a patient mentor. Explain your reasoning "
        "step-by-step. When a user asks about deal math, walk them through each "
        "number. Use phrases like 'here's why that matters' and 'a common mistake "
        "here is...'. Still concise, but teach as you go."
    ),
    "aggressive": (
        "\n\nVoice adjustment: You are a hard-nosed deal hawk. Push for maximum "
        "discount on every deal. Challenge assumptions — if a seller is asking too "
        "much, say so directly. Use phrases like 'that's a pass unless...' and "
        "'here's your walk-away number'. Never sugarcoat bad numbers."
    ),
}


def _build_system_prompt(voice: str = "neutral") -> str:
    """Compose system prompt with optional voice overlay."""
    overlay = VOICE_OVERLAYS.get(voice, "")
    return SYSTEM_PROMPT + overlay


# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "create_saved_search",
        "description": "Create a new saved search alert that will notify the user when matching listings appear. Requires at least a name and one market.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Descriptive name for the search (e.g., 'Charlotte high-score deals')",
                },
                "markets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"Market IDs to search. Valid IDs: {', '.join(MARKET_IDS[:5])}... (use exact IDs)",
                },
                "min_score": {
                    "type": "integer",
                    "description": "Minimum distress score (0-100). Suggest 35+ for shortlist, 60+ for high priority.",
                },
                "max_price": {
                    "type": "integer",
                    "description": "Maximum listing price in dollars",
                },
                "min_price": {
                    "type": "integer",
                    "description": "Minimum listing price in dollars",
                },
                "listing_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["fsbo", "mlsfsbo"]},
                    "description": "Types to include. Default: both fsbo and mlsfsbo.",
                },
                "min_dom": {
                    "type": "integer",
                    "description": "Minimum days on market",
                },
                "custom_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords to match in listing remarks (e.g., 'motivated', 'estate', 'as-is')",
                },
            },
            "required": ["name", "markets"],
        },
    },
    {
        "name": "update_saved_search",
        "description": "Update an existing saved search by ID. Only provided fields are changed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search_id": {
                    "type": "string",
                    "description": "The ID of the saved search to update",
                },
                "name": {"type": "string"},
                "markets": {"type": "array", "items": {"type": "string"}},
                "min_score": {"type": "integer"},
                "max_price": {"type": "integer"},
                "min_price": {"type": "integer"},
                "listing_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["fsbo", "mlsfsbo"]},
                },
                "min_dom": {"type": "integer"},
                "is_active": {"type": "boolean"},
            },
            "required": ["search_id"],
        },
    },
    {
        "name": "list_saved_searches",
        "description": "List the user's current saved searches with their criteria and match counts.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "query_listings",
        "description": "Query current active listings matching filters. Returns top results sorted by score. Use this for market snapshots and deal discovery.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {
                    "type": "string",
                    "description": f"Market ID to search. Valid: {', '.join(MARKET_IDS[:5])}...",
                },
                "min_score": {
                    "type": "integer",
                    "description": "Minimum distress score (0-100)",
                },
                "max_price": {
                    "type": "integer",
                    "description": "Maximum price in dollars",
                },
                "min_price": {
                    "type": "integer",
                    "description": "Minimum price in dollars",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 5, max 10)",
                },
            },
        },
    },
    {
        "name": "market_stats",
        "description": "Get aggregate statistics for a market: total listings, average score, price range, listing type breakdown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "market": {
                    "type": "string",
                    "description": "Market ID to get stats for",
                },
            },
            "required": ["market"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------
def _execute_tool(tool_name: str, tool_input: dict, user_id: str) -> dict:
    """Execute a tool call and return the result dict."""
    try:
        if tool_name == "create_saved_search":
            return _tool_create_search(tool_input, user_id)
        elif tool_name == "update_saved_search":
            return _tool_update_search(tool_input, user_id)
        elif tool_name == "list_saved_searches":
            return _tool_list_searches(user_id)
        elif tool_name == "query_listings":
            return _tool_query_listings(tool_input, user_id)
        elif tool_name == "market_stats":
            return _tool_market_stats(tool_input)
        else:
            return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        logger.error("[ADVISOR] Tool %s failed: %s", tool_name, e, exc_info=True)
        return {"error": str(e)}


def _tool_create_search(params: dict, user_id: str) -> dict:
    """Create a saved search via the same logic as the CRUD endpoint."""
    from .access import get_entitlements
    from . import auth_db

    # Check tier limits
    db_user = auth_db.get_user_by_id(user_id)
    if not db_user:
        return {"error": "User not found"}

    ents = get_entitlements(db_user)
    max_allowed = ents.get("max_saved_searches", 0)
    if max_allowed == 0:
        return {"error": "Upgrade to Starter to create saved searches"}

    with db_cursor(commit=False) as (conn, cur):
        cur.execute(
            "SELECT COUNT(*) as cnt FROM fsbo_saved_searches WHERE user_id = %s",
            (user_id,),
        )
        existing = cur.fetchone()["cnt"]

    if existing >= max_allowed:
        return {"error": f"Limit reached ({existing}/{max_allowed}). Upgrade for more."}

    # Validate markets
    markets = params.get("markets", [])
    invalid = [m for m in markets if m not in MARKET_MAP]
    if invalid:
        return {"error": f"Invalid market IDs: {invalid}. Use exact IDs like 'charlotte-nc'."}

    search_id = str(uuid.uuid4())
    name = params["name"]

    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_saved_searches
                (id, user_id, name, markets, min_score, max_price, min_price,
                 listing_types, min_dom, custom_keywords, created_via)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'advisor')
            RETURNING id, name
        """, (
            search_id, user_id, name,
            json.dumps(markets),
            params.get("min_score", 0),
            params.get("max_price"),
            params.get("min_price"),
            json.dumps(params.get("listing_types", ["fsbo", "mlsfsbo"])),
            params.get("min_dom"),
            json.dumps(params.get("custom_keywords")) if params.get("custom_keywords") else None,
        ))
        cur.fetchone()

    return {
        "success": True,
        "search_id": search_id,
        "name": name,
        "markets": markets,
        "message": f"Saved search '{name}' created. You'll get email alerts when matching deals appear.",
    }


def _tool_update_search(params: dict, user_id: str) -> dict:
    """Update an existing saved search."""
    search_id = params.get("search_id")

    updates = {}
    if "name" in params:
        updates["name"] = params["name"]
    if "markets" in params:
        updates["markets"] = json.dumps(params["markets"])
    if "min_score" in params:
        updates["min_score"] = params["min_score"]
    if "max_price" in params:
        updates["max_price"] = params["max_price"]
    if "min_price" in params:
        updates["min_price"] = params["min_price"]
    if "listing_types" in params:
        updates["listing_types"] = json.dumps(params["listing_types"])
    if "min_dom" in params:
        updates["min_dom"] = params["min_dom"]
    if "is_active" in params:
        updates["is_active"] = params["is_active"]

    if not updates:
        return {"error": "No fields to update"}

    updates["updated_at"] = datetime.now(timezone.utc)
    set_parts = [f"{k} = %s" for k in updates]
    values = list(updates.values())

    with db_cursor() as (conn, cur):
        cur.execute(f"""
            UPDATE fsbo_saved_searches
            SET {', '.join(set_parts)}
            WHERE id = %s AND user_id = %s
            RETURNING id, name, is_active
        """, values + [search_id, user_id])
        updated = cur.fetchone()

    if not updated:
        return {"error": "Saved search not found"}

    return {
        "success": True,
        "search_id": updated["id"],
        "name": updated["name"],
        "is_active": updated["is_active"],
    }


def _tool_list_searches(user_id: str) -> dict:
    """List user's saved searches."""
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT ss.id, ss.name, ss.markets, ss.min_score, ss.max_price,
                   ss.is_active, ss.created_at,
                   (SELECT COUNT(*) FROM fsbo_notification_matches nm
                    WHERE nm.search_id = ss.id) as total_matches
            FROM fsbo_saved_searches ss
            WHERE ss.user_id = %s
            ORDER BY ss.created_at DESC
        """, (user_id,))
        searches = cur.fetchall()

    if not searches:
        return {"searches": [], "message": "No saved searches yet. I can create one for you!"}

    results = []
    for s in searches:
        markets = s["markets"]
        if isinstance(markets, str):
            markets = json.loads(markets)
        results.append({
            "id": s["id"],
            "name": s["name"],
            "markets": markets,
            "min_score": s["min_score"],
            "max_price": s["max_price"],
            "active": s["is_active"],
            "total_matches": s["total_matches"],
        })

    return {"searches": results, "count": len(results)}


def _tool_query_listings(params: dict, user_id: str) -> dict:
    """Query active listings with filters."""
    conditions = ["l.status = 'active'"]
    query_params = []

    market = params.get("market")
    if market:
        if market not in MARKET_MAP:
            return {"error": f"Invalid market ID: {market}"}
        conditions.append("l.search_id = %s")
        query_params.append(market)

    if "min_score" in params and params["min_score"] is not None:
        conditions.append("COALESCE(l.score, 0) >= %s")
        query_params.append(params["min_score"])

    if "max_price" in params and params["max_price"] is not None:
        conditions.append("l.price <= %s")
        query_params.append(params["max_price"])

    if "min_price" in params and params["min_price"] is not None:
        conditions.append("l.price >= %s")
        query_params.append(params["min_price"])

    limit = min(params.get("limit", 5), 10)

    where = " AND ".join(conditions)

    with db_cursor(commit=False) as (conn, cur):
        cur.execute(f"""
            SELECT l.address, l.city, l.state, l.price, l.score,
                   l.beds, l.baths, l.sqft, l.dom, l.listing_type,
                   l.search_id
            FROM fsbo_listings l
            WHERE {where}
            ORDER BY l.score DESC NULLS LAST
            LIMIT %s
        """, query_params + [limit])
        listings = cur.fetchall()

    if not listings:
        return {"listings": [], "message": "No listings match those criteria."}

    results = []
    for l in listings:
        results.append({
            "address": l["address"],
            "city": l["city"],
            "state": l["state"],
            "price": l["price"],
            "score": l["score"],
            "beds": l["beds"],
            "baths": l["baths"],
            "sqft": l["sqft"],
            "dom": l["dom"],
            "type": l["listing_type"],
            "market": l["search_id"],
        })

    return {"listings": results, "count": len(results)}


def _tool_market_stats(params: dict) -> dict:
    """Get aggregate stats for a market."""
    market = params["market"]
    if market not in MARKET_MAP:
        return {"error": f"Invalid market ID: {market}"}

    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT
                COUNT(*) as total,
                AVG(score) as avg_score,
                MIN(price) as min_price,
                MAX(price) as max_price,
                AVG(price) as avg_price,
                AVG(dom) as avg_dom,
                COUNT(*) FILTER (WHERE score >= 60) as high_priority,
                COUNT(*) FILTER (WHERE score >= 35) as shortlist,
                COUNT(*) FILTER (WHERE listing_type = 'fsbo') as fsbo_count,
                COUNT(*) FILTER (WHERE listing_type = 'mlsfsbo') as mlsfsbo_count
            FROM fsbo_listings
            WHERE search_id = %s AND status = 'active'
        """, (market,))
        stats = cur.fetchone()

    return {
        "market": market,
        "market_name": MARKET_MAP[market],
        "total_active": stats["total"],
        "avg_score": round(float(stats["avg_score"] or 0), 1),
        "price_range": f"${stats['min_price']:,} - ${stats['max_price']:,}" if stats["min_price"] else "N/A",
        "avg_price": f"${int(stats['avg_price'] or 0):,}",
        "avg_dom": round(float(stats["avg_dom"] or 0), 0),
        "high_priority_count": stats["high_priority"],
        "shortlist_count": stats["shortlist"],
        "fsbo": stats["fsbo_count"],
        "mlsfsbo": stats["mlsfsbo_count"],
    }


# ---------------------------------------------------------------------------
# Message persistence
# ---------------------------------------------------------------------------
def _save_message(user_id: str, role: str, content: str,
                  tool_calls: Optional[list] = None,
                  tool_results: Optional[list] = None):
    """Persist a message to fsbo_advisor_messages."""
    msg_id = str(uuid.uuid4())
    with db_cursor() as (conn, cur):
        cur.execute("""
            INSERT INTO fsbo_advisor_messages
                (id, user_id, role, content, tool_calls, tool_results)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            msg_id, user_id, role, content,
            json.dumps(tool_calls) if tool_calls else None,
            json.dumps(tool_results) if tool_results else None,
        ))


def _load_history(user_id: str, limit: int = 10) -> list:
    """Load recent conversation messages for context.

    Merges consecutive same-role messages to satisfy Claude API's
    strict user/assistant alternation requirement.
    """
    with db_cursor(commit=False) as (conn, cur):
        cur.execute("""
            SELECT role, content, tool_calls, tool_results
            FROM fsbo_advisor_messages
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (user_id, limit))
        rows = cur.fetchall()

    # Build raw messages in chronological order
    raw_messages = []
    for row in reversed(rows):
        role = row["role"]
        content = row["content"]

        if role == "user":
            raw_messages.append({"role": "user", "content": content})
        elif role == "assistant":
            tc = row.get("tool_calls")
            if tc:
                if isinstance(tc, str):
                    tc = json.loads(tc)
                blocks = [{"type": "text", "text": content}] if content else []
                for call in tc:
                    blocks.append({
                        "type": "tool_use",
                        "id": call.get("id", str(uuid.uuid4())),
                        "name": call["name"],
                        "input": call["input"],
                    })
                raw_messages.append({"role": "assistant", "content": blocks})
            else:
                raw_messages.append({"role": "assistant", "content": content})
        elif role == "tool_result":
            tr = row.get("tool_results")
            if tr:
                if isinstance(tr, str):
                    tr = json.loads(tr)
                blocks = []
                for result in tr:
                    blocks.append({
                        "type": "tool_result",
                        "tool_use_id": result.get("tool_use_id", "unknown"),
                        "content": result.get("content", ""),
                    })
                raw_messages.append({"role": "user", "content": blocks})

    # Merge consecutive same-role messages (Claude API requires strict alternation)
    messages = []
    for msg in raw_messages:
        if messages and messages[-1]["role"] == msg["role"]:
            # Merge into previous message
            prev = messages[-1]
            prev_content = prev["content"]
            new_content = msg["content"]

            # Normalize both to list-of-blocks format for merging
            if isinstance(prev_content, str):
                prev_content = [{"type": "text", "text": prev_content}]
            if isinstance(new_content, str):
                new_content = [{"type": "text", "text": new_content}]

            prev["content"] = prev_content + new_content
        else:
            messages.append(msg)

    return messages


# ---------------------------------------------------------------------------
# Quota management
# ---------------------------------------------------------------------------
def check_advisor_quota(user_id: str) -> dict:
    """Check if user has remaining advisor messages (read-only).

    Returns {"allowed": bool, "used": int, "limit": int, "remaining": int}.
    Resets counter if 30 days have passed since last reset.
    """
    today = date.today()

    with db_cursor() as (conn, cur):
        cur.execute("""
            SELECT advisor_enabled, advisor_messages_used, advisor_messages_limit,
                   advisor_reset_date, advisor_addon_status
            FROM fsbo_users WHERE id = %s
        """, (user_id,))
        row = cur.fetchone()

    if not row:
        return {"allowed": False, "used": 0, "limit": 0, "remaining": 0,
                "reason": "User not found"}

    if not row["advisor_enabled"]:
        return {"allowed": False, "used": 0, "limit": 0, "remaining": 0,
                "reason": "Advisor add-on not active. Activate for $10/mo."}

    addon_status = row.get("advisor_addon_status") or "none"
    reset_date = row.get("advisor_reset_date")

    # If cancelled and billing period expired, disable access
    if addon_status == "cancelled" and reset_date and reset_date < today:
        with db_cursor() as (conn, cur):
            cur.execute("""
                UPDATE fsbo_users SET advisor_enabled = false
                WHERE id = %s
            """, (user_id,))
        return {"allowed": False, "used": 0, "limit": 0, "remaining": 0,
                "reason": "Advisor cancelled. Reactivate for $10/mo."}

    # Only reset monthly counter for active add-on subscribers
    if addon_status == "active" and (reset_date is None or reset_date < today - timedelta(days=30)):
        with db_cursor() as (conn, cur):
            cur.execute("""
                UPDATE fsbo_users
                SET advisor_messages_used = 0, advisor_reset_date = %s
                WHERE id = %s
            """, (today, user_id))

    used = row["advisor_messages_used"] or 0
    limit = row["advisor_messages_limit"] or 0

    if used >= limit:
        return {"allowed": False, "used": used, "limit": limit, "remaining": 0,
                "reason": "Message quota reached. Top up for $5 to get 25 more messages."}

    return {"allowed": True, "used": used, "limit": limit,
            "remaining": limit - used}


def _consume_message(user_id: str):
    """Increment advisor message counter (called AFTER successful API response).

    Unconditional increment — quota was already checked before the API call.
    Small race window where two concurrent requests both pass check is acceptable;
    worst case user gets 1 extra message (vs old bug of losing messages on failure).
    """
    with db_cursor() as (conn, cur):
        cur.execute("""
            UPDATE fsbo_users
            SET advisor_messages_used = COALESCE(advisor_messages_used, 0) + 1
            WHERE id = %s
        """, (user_id,))


# ---------------------------------------------------------------------------
# Main chat function (non-streaming, for SSE wrapper)
# ---------------------------------------------------------------------------
def chat(user_id: str, user_message: str, voice: str = "neutral") -> dict:
    """Process a user message and return the assistant response.

    Returns:
        {
            "response": str,           # assistant text
            "tool_results": list|None,  # tool call results if any
            "quota": dict,              # remaining messages
        }

    Raises ValueError if quota exceeded or API unavailable.
    """
    if not ANTHROPIC_API_KEY:
        raise ValueError("AI advisor not configured (missing API key)")

    logger.info("[ADVISOR] chat user=%s msg_len=%d voice=%s", user_id, len(user_message), voice)

    # 1. Check quota (read-only) — raise if exceeded
    quota = check_advisor_quota(user_id)
    if not quota["allowed"]:
        raise ValueError(quota.get("reason", "Message quota exceeded"))

    # 2. Load history + append user message
    history = _load_history(user_id, limit=10)
    history.append({"role": "user", "content": user_message})

    # Save user message
    _save_message(user_id, "user", user_message)

    # 3. Call Claude API with tool loop
    system_prompt = _build_system_prompt(voice)
    response_text, tool_call_results = _call_claude_with_tools(
        user_id, history, system_prompt
    )

    # 4. Success — NOW consume the message
    _consume_message(user_id)

    # Save assistant response
    _save_message(user_id, "assistant", response_text)

    # Get updated quota for display
    updated_quota = check_advisor_quota(user_id)

    return {
        "response": response_text,
        "tool_results": tool_call_results,
        "quota": updated_quota,
    }


def _call_claude_with_tools(user_id: str, messages: list, system_prompt: str = "") -> tuple:
    """Call Claude API, handle tool calls in a loop.

    Returns (response_text, tool_results).
    """
    tool_results_all = []

    for round_num in range(MAX_TOOL_ROUNDS + 1):
        # Call Claude
        payload = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt or SYSTEM_PROMPT,
            "messages": messages,
            "tools": TOOLS,
        }

        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=30.0,
        )

        if resp.status_code != 200:
            logger.error("[ADVISOR] Claude API error: %s %s",
                         resp.status_code, resp.text[:500])
            raise ValueError("AI advisor temporarily unavailable. Please try again.")

        result = resp.json()
        stop_reason = result.get("stop_reason")
        content_blocks = result.get("content", [])

        # Extract text and tool_use blocks
        text_parts = []
        tool_uses = []
        for block in content_blocks:
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_uses.append(block)

        # If no tool calls, we're done
        if stop_reason != "tool_use" or not tool_uses:
            return "\n".join(text_parts), tool_results_all or None

        # Execute tools and continue the loop
        # Add assistant message with tool_use blocks to conversation
        messages.append({"role": "assistant", "content": content_blocks})

        # Save assistant tool-call message
        tc_data = [{"id": t["id"], "name": t["name"], "input": t["input"]}
                    for t in tool_uses]
        _save_message(user_id, "assistant", "\n".join(text_parts) or "",
                      tool_calls=tc_data)

        # Execute each tool and build tool_result blocks
        tool_result_blocks = []
        for tool_use in tool_uses:
            result_data = _execute_tool(
                tool_use["name"], tool_use["input"], user_id
            )
            tool_results_all.append({
                "tool": tool_use["name"],
                "input": tool_use["input"],
                "result": result_data,
            })
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tool_use["id"],
                "content": json.dumps(result_data),
            })

        # Save tool results
        tr_data = [{"tool_use_id": t["tool_use_id"], "content": t["content"]}
                    for t in tool_result_blocks]
        _save_message(user_id, "tool_result", "", tool_results=tr_data)

        # Add tool results to conversation
        messages.append({"role": "user", "content": tool_result_blocks})

    # If we exhausted tool rounds, return whatever text we have
    logger.warning("[ADVISOR] Exhausted %d tool rounds for user %s",
                   MAX_TOOL_ROUNDS, user_id)
    return "I ran into a complex situation. Could you rephrase your request?", tool_results_all or None
