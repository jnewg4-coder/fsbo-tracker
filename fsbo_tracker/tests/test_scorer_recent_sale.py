from datetime import datetime, timedelta, timezone

from fsbo_tracker import scorer


def test_recent_sale_penalty_for_recent_near_parity():
    sale_date = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    penalty = scorer.penalty_recent_sale(
        price=305000,
        last_sold_price=300000,
        last_sold_date=sale_date,
    )
    assert penalty == -8  # binary penalty for sale within 3yr AND within 20%


def test_recent_sale_penalty_zero_when_old_or_far_from_ask():
    old_date = (datetime.now(timezone.utc) - timedelta(days=1200)).date().isoformat()
    assert scorer.penalty_recent_sale(300000, 295000, old_date) == 0

    recent_date = (datetime.now(timezone.utc) - timedelta(days=60)).date().isoformat()
    assert scorer.penalty_recent_sale(300000, 220000, recent_date) == 0


def test_score_listing_subtracts_recent_sale_penalty():
    listing = {
        "remarks": "",
        "photo_damage_score": None,
        "price": 180000,
        "assessed_value": 0,
        "redfin_estimate": 250000,
        "dom": 80,
        "days_seen": 0,
        "price_cuts": 1,
        "last_sold_price": 178000,
        "last_sold_date": (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat(),
    }
    result = scorer.score_listing(listing)
    breakdown = result["breakdown"]

    assert breakdown["price_ratio"] == 20  # redfin_estimate used
    assert breakdown["recent_sale"] == -8
    # total = 20 (price_ratio) + 6 (dom) + 3 (cuts) - 8 (recent_sale) = 21
    assert result["total"] == 21


def test_score_listing_uses_zestimate_when_redfin_missing():
    listing = {
        "remarks": "",
        "photo_damage_score": None,
        "price": 180000,
        "assessed_value": 0,
        "redfin_estimate": 0,
        "zestimate": 250000,
        "dom": 0,
        "days_seen": 0,
        "price_cuts": 0,
        "last_sold_price": 0,
        "last_sold_date": "",
    }
    result = scorer.score_listing(listing)
    assert result["breakdown"]["price_ratio"] == 20
