"""Dump all Zillow search API fields to find hidden description/tag data."""
import json
import time

from curl_cffi import requests as curl_requests

session = curl_requests.Session(impersonate="safari17_0")
session.get("https://www.zillow.com/", timeout=15)
time.sleep(1)

payload = {
    "searchQueryState": {
        "pagination": {},
        "isMapVisible": True,
        "mapBounds": {"north": 35.58, "south": 34.88, "east": -80.38, "west": -81.25},
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
            "price": {"max": 500000},
        },
        "isListVisible": True,
    },
    "wants": {"cat1": ["listResults"]},
    "requestId": 2,
    "isDebugRequest": False,
}

resp = session.put(
    "https://www.zillow.com/async-create-search-page-state",
    json=payload,
    headers={
        "Content-Type": "application/json",
        "Origin": "https://www.zillow.com",
        "Referer": "https://www.zillow.com/",
    },
    timeout=30,
)
session.close()

data = resp.json()
results = data.get("cat1", {}).get("searchResults", {}).get("listResults", [])

# Dump first result in FULL to see everything
if results:
    print("=== FULL FIRST RESULT ===")
    print(json.dumps(results[0], indent=2, default=str)[:3000])
    print("...")
    print()

    # Summary of all listings with interesting fields
    print(f"\n=== {len(results)} LISTINGS SUMMARY ===\n")
    for item in results:
        addr = item.get("address", "?")
        info = item.get("hdpData", {}).get("homeInfo", {})
        price = item.get("unformattedPrice", 0)
        dom = info.get("daysOnZillow")
        zest = info.get("zestimate", 0)
        tax = info.get("taxAssessedValue", 0)
        pc = info.get("priceChange")
        flex = item.get("flexFieldText", "")

        ratio_str = ""
        if price and tax and tax > 0:
            ratio = price / tax
            ratio_str = f" ratio:{ratio:.2f}"

        cut_str = f" CUT:{pc}" if pc else ""
        flex_str = f" [{flex}]" if flex else ""

        print(f"${price:>7,} | DOM:{dom or '?':>3} | {addr}{ratio_str}{cut_str}{flex_str}")
