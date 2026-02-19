"""Check scores and remarks after pipeline update."""
import json
import os

import psycopg2
from psycopg2.extras import RealDictCursor

DB_URL = os.environ.get("FSBO_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("Set FSBO_DATABASE_URL or DATABASE_URL env var")

conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
cur = conn.cursor()

# Score distribution
cur.execute(
    "SELECT score, count(*) as cnt FROM fsbo_listings "
    "WHERE status = 'active' GROUP BY score ORDER BY score DESC"
)
print("Score distribution:")
for row in cur.fetchall():
    print(f"  Score {row['score']:>3}: {row['cnt']} listings")

print()

# Top 15 listings
cur.execute(
    "SELECT id, address, city, price, dom, score, score_breakdown, "
    "keywords_matched, remarks, assessed_value "
    "FROM fsbo_listings WHERE status = 'active' "
    "ORDER BY score DESC, dom DESC NULLS LAST LIMIT 15"
)
print("Top 15 by score:")
for row in cur.fetchall():
    breakdown = row.get("score_breakdown", "{}")
    kw = row.get("keywords_matched", "[]")
    remarks = row.get("remarks") or "none"
    rmk = (remarks[:70] + "...") if len(remarks) > 70 else remarks

    print(f"  {row['score']:>3} | ${row['price']:>7,} | DOM:{str(row['dom'] or '?'):>3} | {row['address']}")
    print(f"      breakdown: {breakdown}")
    if kw and kw != "[]":
        print(f"      keywords: {kw}")
    print(f"      remarks: {rmk}")
    print()

# Remarks counts
cur.execute(
    "SELECT count(*) as total, "
    "count(CASE WHEN remarks IS NOT NULL AND LENGTH(remarks) > 5 THEN 1 END) as has_remarks "
    "FROM fsbo_listings WHERE status = 'active'"
)
counts = cur.fetchone()
print(f"Remarks coverage: {counts['has_remarks']}/{counts['total']} listings have remarks")

conn.close()
