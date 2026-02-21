"""SELL pipeline stage graph — placeholder.

Stages and transitions defined; no business logic yet.
Will be implemented when sell_pipeline_enabled tier flag is activated.
"""

SELL_STAGES = [
    {"id": "prep",          "label": "Prep",          "order": 1},
    {"id": "list",          "label": "List",          "order": 2},
    {"id": "market",        "label": "Market",        "order": 3},
    {"id": "showings",      "label": "Showings",      "order": 4},
    {"id": "offer_review",  "label": "Offer Review",  "order": 5},
    {"id": "contract",      "label": "Contract",      "order": 6},
    {"id": "close",         "label": "Close",         "order": 7},
]

SELL_STAGE_IDS = [s["id"] for s in SELL_STAGES]

SELL_TRANSITIONS = {
    "prep":          ["list", "terminated"],
    "list":          ["market", "terminated"],
    "market":        ["showings", "terminated"],
    "showings":      ["offer_review", "terminated"],
    "offer_review":  ["contract", "terminated"],
    "contract":      ["close", "terminated"],
    "close":         [],
    "terminated":    [],
}

# Placeholder — no required fields enforced yet
SELL_REQUIREMENTS = {
    "list":          [],
    "market":        [],
    "showings":      [],
    "offer_review":  [],
    "contract":      [],
    "close":         [],
}
