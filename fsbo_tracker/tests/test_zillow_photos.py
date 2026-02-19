"""
Test script: Dump photo-related fields from Zillow search API responses.
Shows what photo/media data is available per listing without a detail page fetch.
"""

import json
import os
import re
import sys
import time

# Load .env from avm_platform
from pathlib import Path

env_path = Path("/Users/jimnewgent/Projects/real-estate-core/avm_platform/.env")
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

# Need ADMIN_PASSWORD to avoid import issues
os.environ.setdefault("ADMIN_PASSWORD", "test")

from curl_cffi import requests as curl_requests

# ---------------------------------------------------------------------------
# Reuse the session/request pattern from zillow_fetcher
# ---------------------------------------------------------------------------
SEARCH_URL = "https://www.zillow.com/async-create-search-page-state"

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.zillow.com",
    "Referer": "https://www.zillow.com/",
}

# Charlotte NC MSA bbox from config
CHARLOTTE_SEARCH = {
    "id": "charlotte-nc",
    "name": "Charlotte NC MSA",
    "max_price": 500_000,
    "min_beds": 0,
    "min_dom": 0,
    "max_lat": 35.58, "min_lat": 34.88,
    "max_lng": -80.38, "min_lng": -81.25,
}

# Photo-related key patterns (case-insensitive)
PHOTO_PATTERNS = re.compile(
    r"photo|img|image|carousel|media|picture", re.IGNORECASE
)


def make_session():
    """Safari 17 impersonation — same as zillow_fetcher."""
    return curl_requests.Session(impersonate="safari17_0")


def build_payload(search: dict) -> dict:
    """Build the search PUT payload with FSBO filter."""
    return {
        "searchQueryState": {
            "pagination": {},
            "isMapVisible": True,
            "mapBounds": {
                "north": search["max_lat"],
                "south": search["min_lat"],
                "east": search["max_lng"],
                "west": search["min_lng"],
            },
            "filterState": {
                "isForSaleByOwner": {"value": True},
                "isForSaleByAgent": {"value": False},
                "isNewConstruction": {"value": False},
                "isComingSoon": {"value": False},
                "isAuction": {"value": False},
                "isForSaleForeclosure": {"value": False},
                "isPreMarketForeclosure": {"value": False},
                "isPreMarketPreForeclosure": {"value": False},
                "isRecentlySold": {"value": False},
                "price": {"max": search["max_price"]},
            },
            "isListVisible": True,
        },
        "wants": {"cat1": ["listResults"]},
        "requestId": 2,
        "isDebugRequest": False,
    }


def find_photo_keys(obj, prefix="", depth=0):
    """
    Recursively walk a dict/list and find all keys matching photo patterns.
    Returns list of (dotted_path, value) tuples.
    """
    results = []
    if depth > 10:  # safety limit
        return results

    if isinstance(obj, dict):
        for key, val in obj.items():
            dotted = f"{prefix}.{key}" if prefix else key
            if PHOTO_PATTERNS.search(key):
                results.append((dotted, val))
            # Recurse into nested structures (but don't double-report)
            if isinstance(val, (dict, list)):
                results.extend(find_photo_keys(val, dotted, depth + 1))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:5]):  # limit to first 5 items in arrays
            dotted = f"{prefix}[{i}]"
            results.extend(find_photo_keys(item, dotted, depth + 1))

    return results


def truncate(val, max_len=200):
    """Truncate a string representation for readability."""
    s = json.dumps(val, default=str) if not isinstance(val, str) else val
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def main():
    print("=" * 80)
    print("Zillow Search API — Photo Field Inspector")
    print("=" * 80)

    session = make_session()

    # Warm session
    print("\n[1] Warming session with zillow.com homepage...")
    try:
        session.get("https://www.zillow.com/", headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=15)
        time.sleep(1)
        print("    OK")
    except Exception as e:
        print(f"    Warning: {e}")

    # Search
    print("\n[2] Sending FSBO search for Charlotte NC...")
    payload = build_payload(CHARLOTTE_SEARCH)

    try:
        resp = session.put(SEARCH_URL, json=payload, headers=HEADERS, timeout=30)
        print(f"    Status: {resp.status_code}")

        if resp.status_code == 403:
            print("    Blocked (403) — retrying with fresh session...")
            session.close()
            time.sleep(3)
            session = make_session()
            # Re-warm
            try:
                session.get("https://www.zillow.com/", timeout=15)
                time.sleep(1)
            except:
                pass
            resp = session.put(SEARCH_URL, json=payload, headers=HEADERS, timeout=30)
            print(f"    Retry status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"    FAILED — cannot proceed (status {resp.status_code})")
            # Dump first 500 chars of response for debugging
            print(f"    Response preview: {resp.text[:500]}")
            return

        data = resp.json()
    except Exception as e:
        print(f"    Error: {e}")
        return
    finally:
        session.close()

    # Extract results
    results = (
        data.get("cat1", {})
        .get("searchResults", {})
        .get("listResults", [])
    )

    print(f"\n[3] Got {len(results)} total results")

    if not results:
        print("    No results — dumping top-level keys:")
        print(f"    {list(data.keys())}")
        if "cat1" in data:
            print(f"    cat1 keys: {list(data['cat1'].keys())}")
        return

    # Also dump ALL top-level keys of first result for reference
    print(f"\n[4] All top-level keys of first result:")
    first = results[0]
    for key in sorted(first.keys()):
        val_type = type(first[key]).__name__
        val_preview = truncate(first[key], 80)
        print(f"    {key} ({val_type}): {val_preview}")

    # Inspect first 3 results for photo-related fields
    print(f"\n{'=' * 80}")
    print("PHOTO FIELD INSPECTION — First 3 results")
    print("=" * 80)

    for i, item in enumerate(results[:3]):
        zpid = item.get("zpid", "?")
        address = item.get("address", "?")
        price = item.get("unformattedPrice", item.get("price", "?"))

        print(f"\n{'─' * 60}")
        print(f"RESULT #{i+1}  zpid={zpid}  {address}  ${price}")
        print(f"{'─' * 60}")

        # Find all photo-related keys recursively
        photo_keys = find_photo_keys(item)

        if photo_keys:
            print(f"\n  Photo-related keys found ({len(photo_keys)}):")
            for path, val in photo_keys:
                print(f"\n    KEY: {path}")
                print(f"    VAL: {truncate(val, 300)}")
        else:
            print("\n  NO photo-related keys found!")

        # Explicitly dump carouselPhotosComposable if it exists
        carousel = item.get("carouselPhotosComposable")
        if carousel is not None:
            print(f"\n  === carouselPhotosComposable (full dump) ===")
            print(f"  Type: {type(carousel).__name__}, Length: {len(carousel) if isinstance(carousel, list) else 'N/A'}")
            # Pretty-print, but cap at reasonable size
            carousel_json = json.dumps(carousel, indent=2, default=str)
            if len(carousel_json) > 2000:
                print(f"  {carousel_json[:2000]}")
                print(f"  ... (truncated, total {len(carousel_json)} chars)")
            else:
                print(f"  {carousel_json}")
        else:
            print(f"\n  carouselPhotosComposable: NOT PRESENT")

        # Also check imgSrc (thumbnail fallback)
        img_src = item.get("imgSrc")
        if img_src:
            print(f"\n  imgSrc (thumbnail): {img_src}")

    print(f"\n{'=' * 80}")
    print("Done.")


if __name__ == "__main__":
    main()
