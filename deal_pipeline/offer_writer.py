"""AI Offer Letter Generator — placeholder.

When implemented, takes deal data (property, financials, inspection findings,
comps) as context and generates a purchase offer letter in markdown.

Requires approval (approved_by + approved_at) before export/send.
Audit trail: model, model_version, generated_by, approval chain.
"""


def generate_offer_draft(deal_id: str, draft_type: str = "purchase_offer") -> dict:
    """Generate an AI-drafted offer letter for a deal.

    Args:
        deal_id: UUID of the deal
        draft_type: purchase_offer | counter_offer | amendment

    Returns:
        dict with output_md, model, model_version

    Raises:
        NotImplementedError: This feature is not yet implemented.
    """
    raise NotImplementedError(
        "AI Offer Writer is not yet implemented. "
        "This will use Claude to generate offer letters from deal data."
    )
