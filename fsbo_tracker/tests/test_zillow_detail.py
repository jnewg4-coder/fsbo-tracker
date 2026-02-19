"""Test Zillow detail page scraping with Safari impersonation."""
import json
import os
import re
import sys
import time

import psycopg2
from psycopg2.extras import RealDictCursor
from curl_cffi import requests as curl_requests

DB_URL = os.environ.get("FSBO_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("Set FSBO_DATABASE_URL or DATABASE_URL env var")


def get_zillow_listings(limit=5):
    conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, address, zillow_url FROM fsbo_listings "
        "WHERE status = 'active' AND zillow_url IS NOT NULL "
        "AND LENGTH(zillow_url) > 5 ORDER BY score DESC LIMIT %s",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def try_zillow_detail(url):
    """Try to scrape Zillow detail page for description."""
    session = curl_requests.Session(impersonate="safari17_0")
    try:
        # Warm up
        session.get("https://www.zillow.com/", timeout=10)
        time.sleep(1)

        resp = session.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
        )

        print(f"  Status: {resp.status_code}, size: {len(resp.text)}")

        if resp.status_code != 200:
            return None

        html = resp.text

        # Method 1: __NEXT_DATA__
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                next_data = json.loads(m.group(1))
                props = next_data.get("props", {}).get("pageProps", {})

                # Try gdpClientCache
                cache = props.get("componentProps", {}).get("gdpClientCache", {})
                if isinstance(cache, dict):
                    for key, val in cache.items():
                        if isinstance(val, dict):
                            prop = val.get("property", val)
                            desc = prop.get("description", "")
                            if desc and len(desc) > 20:
                                return desc

                # Try initialReduxState
                initial = props.get("initialReduxState", {})
                if initial:
                    gdp = initial.get("gdp", {})
                    for sub_key in ("building", "property"):
                        obj = gdp.get(sub_key, {})
                        if isinstance(obj, dict):
                            desc = obj.get("description", "")
                            if desc and len(desc) > 20:
                                return desc
                    print(f"  Redux keys: {list(gdp.keys())[:8]}")
                else:
                    print(f"  PageProps keys: {list(props.keys())[:8]}")
            except json.JSONDecodeError:
                print("  Invalid __NEXT_DATA__ JSON")
        else:
            if "captcha" in html.lower()[:5000]:
                print("  CAPTCHA detected")
            elif "zillow" not in html.lower()[:2000]:
                print("  Unexpected page (not Zillow)")
            else:
                print("  No __NEXT_DATA__ block")

        # Method 2: Regex for description in page JSON
        desc_m = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){30,})"', html)
        if desc_m:
            return desc_m.group(1).replace("\\n", " ").replace("\\r", "")

        # Method 3: Meta description
        meta = re.search(r'<meta name="description" content="([^"]{50,})"', html)
        if meta:
            return meta.group(1)

        return None

    except Exception as e:
        print(f"  Error: {e}")
        return None
    finally:
        session.close()


if __name__ == "__main__":
    listings = get_zillow_listings(5)
    print(f"Testing {len(listings)} Zillow detail pages with Safari17_0 (no proxy)...")
    print()

    found = 0
    for listing in listings:
        url = listing["zillow_url"]
        print(f'{listing["address"]} -> {url}')

        desc = try_zillow_detail(url)
        if desc:
            found += 1
            print(f'  DESCRIPTION: "{desc[:150]}..."')
        else:
            print("  No description found")

        time.sleep(3)
        print()

    print(f"\nResult: {found}/{len(listings)} descriptions found")
