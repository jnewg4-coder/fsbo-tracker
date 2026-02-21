"""BUY pipeline stage graph, transition rules, tier limits, deadline thresholds."""

# ── Stage Definitions (buy_v1) ──────────────────────────────────────────────

BUY_STAGES = [
    {"id": "offer",           "label": "Offer",           "order": 1},
    {"id": "contract",        "label": "Contract",        "order": 2},
    {"id": "title",           "label": "Order Title",     "order": 3},
    {"id": "due_diligence",   "label": "Due Diligence",   "order": 4},
    {"id": "retrade",         "label": "Retrade",         "order": 5},
    {"id": "clear_to_close",  "label": "Clear to Close",  "order": 6},
    {"id": "closed",          "label": "Closed",          "order": 7},
]

BUY_STAGE_IDS = [s["id"] for s in BUY_STAGES]

VALID_TRANSITIONS = {
    "offer":           ["contract", "terminated"],
    "contract":        ["title", "terminated"],
    "title":           ["due_diligence", "terminated"],
    "due_diligence":   ["retrade", "clear_to_close", "terminated"],
    "retrade":         ["clear_to_close", "terminated"],
    "clear_to_close":  ["closed", "terminated"],
    "closed":          [],
    "terminated":      [],
}

# Required fields to advance (hard gate — blocks transition if missing)
ADVANCE_REQUIREMENTS = {
    "contract":        ["acceptance_date"],
    "title":           ["contract_signed_date", "binding_date"],
    "due_diligence":   ["title_ordered_date"],
    "retrade":         [],   # dd_status checked in logic
    "clear_to_close":  [],   # dd_status or retrade_status checked in logic
    "closed":          ["closing_date", "final_purchase_price"],
}

# Soft warnings (non-blocking) when advancing
ADVANCE_WARNINGS = {
    "title":           ["disclosures_received"],
    "due_diligence":   ["title_received_date"],
    "clear_to_close":  ["final_walkthrough_date", "wire_instructions_received"],
    "closed":          ["deed_recorded", "final_hud_received"],
}

# ── Deadline Alerts ─────────────────────────────────────────────────────────

DEADLINE_ALERTS = {
    "offer_expiration_date":  {"yellow": 2, "red": 1},
    "emd_due_date":           {"yellow": 2, "red": 1},
    "dd_end_date":            {"yellow": 3, "red": 1},
    "closing_date":           {"yellow": 5, "red": 2},
    "final_walkthrough_date": {"yellow": 2, "red": 1},
}

# ── File Constraints ────────────────────────────────────────────────────────

ALLOWED_MIME_TYPES = [
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
]

MAX_FILE_SIZE = 10 * 1024 * 1024        # 10MB per file
MAX_DEAL_STORAGE = 50 * 1024 * 1024     # 50MB total per deal

# ── Tier Limits ─────────────────────────────────────────────────────────────

TIER_LIMITS = {
    "free": {
        "max_active_deals": 2,
        "ai_inspection_analysis_enabled": False,
        "ai_offer_writer_enabled": False,
        "auto_fill_from_listing": False,
        "sell_pipeline_enabled": False,
        "max_docs_per_deal": 3,
    },
    "paid": {
        "max_active_deals": None,  # unlimited
        "ai_inspection_analysis_enabled": True,
        "ai_offer_writer_enabled": True,
        "auto_fill_from_listing": True,
        "sell_pipeline_enabled": True,
        "max_docs_per_deal": None,  # unlimited (within MAX_DEAL_STORAGE)
    },
}


def get_tier(admin_mode: bool = True) -> str:
    """Return current tier. Admin auth always gets 'paid'."""
    return "paid" if admin_mode else "free"


def check_tier_limit(feature: str, tier: str = "paid") -> tuple[bool, str | None]:
    """Check if tier allows a feature. Returns (allowed, message)."""
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    val = limits.get(feature)
    if val is False:
        return False, f"Upgrade required: {feature} is not available on the free tier."
    return True, None


def get_stage_config(stage_profile: str):
    """Load stage graph by profile. Returns (stages, transitions, requirements)."""
    if stage_profile == "buy_v1":
        return BUY_STAGES, VALID_TRANSITIONS, ADVANCE_REQUIREMENTS
    elif stage_profile == "sell_v1":
        from deal_pipeline.sell_stage_config import SELL_STAGES, SELL_TRANSITIONS, SELL_REQUIREMENTS
        return SELL_STAGES, SELL_TRANSITIONS, SELL_REQUIREMENTS
    elif stage_profile == "sell_v2":
        # Alias — sell_v1 was updated in-place to the TC workflow
        from deal_pipeline.sell_stage_config import SELL_STAGES, SELL_TRANSITIONS, SELL_REQUIREMENTS
        return SELL_STAGES, SELL_TRANSITIONS, SELL_REQUIREMENTS
    raise ValueError(f"Unknown stage_profile: {stage_profile}")
