"""Deal Pipeline — transaction coordination module.

Tracks properties from Offer through Closing with stage-based workflows,
document management, AI inspection analysis, and tiered billing hooks.
Dual-sided: BUY and SELL share one deals table, differentiated by stage_profile.
"""
