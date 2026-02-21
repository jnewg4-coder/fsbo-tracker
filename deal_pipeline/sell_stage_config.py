"""SELL pipeline stage graph — TC workflow (sell_v1).

Stages map to real transaction coordinator workflow:
Onboarding → Turn → Pricing → Go To Market → Active Listing →
Under Contract → Cleared DD → Clear to Close → Sold
"""

# ── Stage Definitions (sell_v1) ──────────────────────────────────────────────

SELL_STAGES = [
    {"id": "onboarding",      "label": "Onboarding",      "order": 1},
    {"id": "turn",            "label": "Turn",             "order": 2},
    {"id": "pricing",         "label": "Pricing",          "order": 3},
    {"id": "go_to_market",    "label": "Go To Market",     "order": 4},
    {"id": "active_listing",  "label": "Active",           "order": 5},
    {"id": "under_contract",  "label": "Under Contract",   "order": 6},
    {"id": "cleared_dd",      "label": "Cleared DD",       "order": 7},
    {"id": "clear_to_close",  "label": "Clear to Close",   "order": 8},
    {"id": "sold",            "label": "Sold",             "order": 9},
]

SELL_STAGE_IDS = [s["id"] for s in SELL_STAGES]

SELL_TRANSITIONS = {
    "onboarding":      ["turn", "terminated"],
    "turn":            ["pricing", "terminated"],
    "pricing":         ["go_to_market", "terminated"],
    "go_to_market":    ["active_listing", "terminated"],
    "active_listing":  ["under_contract", "terminated"],
    "under_contract":  ["cleared_dd", "terminated"],
    "cleared_dd":      ["clear_to_close", "terminated"],
    "clear_to_close":  ["sold", "terminated"],
    "sold":            [],
    "terminated":      [],
}

# Required fields to advance (hard gates)
SELL_REQUIREMENTS = {
    "turn":            [],                          # TC assigned (checked via workflow_state)
    "pricing":         [],                          # turn scope approved
    "go_to_market":    ["floor_price"],             # must have floor price set
    "active_listing":  ["mls_number"],              # must be on MLS
    "under_contract":  ["accepted_offer_price"],    # must have accepted offer
    "cleared_dd":      [],                          # contract proof via workflow_state
    "clear_to_close":  [],                          # DD cleared via workflow_state
    "sold":            ["closing_date"],             # must have closing date
}

# Soft warnings (non-blocking)
SELL_WARNINGS = {
    "pricing":         ["bpo_price"],
    "go_to_market":    ["list_price"],
    "under_contract":  [],
    "cleared_dd":      ["emd_amount"],
    "clear_to_close":  [],
    "sold":            [],
}

# Deadline alerts for SELL pipeline
SELL_DEADLINE_ALERTS = {
    "closing_date":            {"yellow": 5, "red": 2},
    "dd_end_date":             {"yellow": 3, "red": 1},
    "offer_expiration_date":   {"yellow": 2, "red": 1},
}
