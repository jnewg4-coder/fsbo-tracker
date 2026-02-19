"""
FSBO Listing Tracker — Zillow fetcher
Adapted from ZillowCompsAPI. Uses async-create-search-page-state PUT endpoint
with isForSaleByOwner filter. curl_cffi Chrome 131 impersonation.
"""

import math
import os
import time
import traceback
from datetime import datetime
from typing import Optional

from curl_cffi import requests as curl_requests

from .config import ZILLOW_DELAY


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SEARCH_URL = "https://www.zillow.com/async-create-search-page-state"

_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.zillow.com",
    "Referer": "https://www.zillow.com/",
}


# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------
def _get_proxy() -> Optional[dict]:
    """Get proxy dict from environment (IPRoyal)."""
    host = os.getenv("IPROYAL_HOST")
    port = os.getenv("IPROYAL_PORT")
    user = os.getenv("IPROYAL_USER")
    password = os.getenv("IPROYAL_PASS")

    if not all([host, port, user, password]):
        return None

    session_id = f"fsbo_z_{int(time.time()) % 100000}"
    password_with_session = f"{password}_session-{session_id}"
    proxy_url = f"http://{user}:{password_with_session}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def _make_session() -> curl_requests.Session:
    """Create a curl_cffi session with Safari impersonation (bypasses Zillow blocks)."""
    # Safari17_0 works where Chrome131 gets captcha-blocked on Zillow
    session = curl_requests.Session(impersonate="safari17_0")
    # NOTE: Zillow BLOCKS residential proxies. Direct connection works with Safari fingerprint.
    return session


# ---------------------------------------------------------------------------
# FSBO search
# ---------------------------------------------------------------------------
def fetch_listings(search: dict) -> list:
    """
    Fetch FSBO listings from Zillow for a market.

    Args:
        search: Search config dict with bbox (min_lat/max_lat/min_lng/max_lng),
                max_price, min_beds, min_dom.

    Returns:
        List of normalized listing dicts ready for upsert.
    """
    session = _make_session()

    # Warm session with Zillow homepage
    try:
        session.get("https://www.zillow.com/", headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }, timeout=15)
        time.sleep(1)
    except Exception:
        pass

    payload = _build_search_payload(search)
    print(f"[Zillow] Fetching FSBO for {search.get('name', search['id'])}...")

    try:
        resp = session.put(SEARCH_URL, json=payload, headers=_HEADERS, timeout=30)

        if resp.status_code == 403:
            print("[Zillow] Blocked (403) — retrying with fresh session...")
            session.close()
            time.sleep(3)
            session = _make_session()
            resp = session.put(SEARCH_URL, json=payload, headers=_HEADERS, timeout=30)

        if resp.status_code == 429:
            print("[Zillow] Rate limited (429)")
            return []

        if resp.status_code != 200:
            print(f"[Zillow] Error {resp.status_code}")
            return []

        data = resp.json()
        return _parse_results(data, search["id"])

    except Exception as e:
        print(f"[Zillow] Fetch error: {e}")
        traceback.print_exc()
        return []
    finally:
        session.close()


def _build_search_payload(search: dict) -> dict:
    """Build Zillow PUT search payload with FSBO filter."""
    filter_state = {
        "isForSaleByOwner": {"value": True},
        "isForSaleByAgent": {"value": False},
        "isNewConstruction": {"value": False},
        "isComingSoon": {"value": False},
        "isAuction": {"value": False},
        "isForSaleForeclosure": {"value": False},
        "isPreMarketForeclosure": {"value": False},
        "isPreMarketPreForeclosure": {"value": False},
        "isRecentlySold": {"value": False},
    }

    max_price = search.get("max_price")
    if max_price is not None:
        filter_state["price"] = {"max": max_price}

    min_beds = search.get("min_beds")
    if min_beds is not None:
        filter_state["beds"] = {"min": min_beds}

    min_dom = search.get("min_dom")
    if min_dom is not None and min_dom > 0:
        filter_state["doz"] = {"value": str(min_dom)}

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
            "filterState": filter_state,
            "isListVisible": True,
        },
        "wants": {"cat1": ["listResults"]},
        "requestId": 2,
        "isDebugRequest": False,
    }


def _parse_results(data: dict, search_id: str) -> list:
    """Parse Zillow search response into normalized listing dicts."""
    results = (
        data.get("cat1", {})
        .get("searchResults", {})
        .get("listResults", [])
    )

    if not results:
        print("[Zillow] No results in response")
        return []

    listings = []
    for item in results:
        try:
            parsed = _parse_one(item, search_id)
            if parsed:
                listings.append(parsed)
        except Exception as e:
            print(f"[Zillow] Parse error for item: {e}")
            continue

    print(f"[Zillow] Parsed {len(listings)} listings")
    return listings


def _parse_one(item: dict, search_id: str) -> Optional[dict]:
    """Parse a single Zillow listing result."""
    zpid = str(item.get("zpid", "")).strip()
    if not zpid:
        return None

    # Root-level fields
    address_full = item.get("address", "").strip()
    if not address_full:
        return None

    # Cascade: root fields → hdpData.homeInfo
    hdp = item.get("hdpData", {})
    info = hdp.get("homeInfo", {})

    price = item.get("unformattedPrice") or info.get("price")
    beds = item.get("beds") or info.get("bedrooms")
    baths = item.get("baths") or info.get("bathrooms")
    sqft = item.get("area") or info.get("livingArea")
    year_built = info.get("yearBuilt")

    # Location
    lat_lng = item.get("latLong", {})
    lat = lat_lng.get("latitude") or info.get("latitude")
    lng = lat_lng.get("longitude") or info.get("longitude")

    # Extra data from homeInfo
    zestimate = info.get("zestimate")
    tax_assessed = info.get("taxAssessedValue")
    home_type = info.get("homeType", "")
    dom = info.get("daysOnZillow") or item.get("daysOnZillow")

    # Price change data (Zillow includes this for listings with cuts)
    price_change = info.get("priceChange")  # Negative = cut
    price_change_date = info.get("datePriceChanged")  # Epoch ms

    # Zillow search API snippet text (feature highlight or "X days on Zillow")
    flex_text = item.get("flexFieldText", "")
    price_reduction_text = info.get("priceReduction", "")  # e.g. "$5,000 (Feb 3)"

    # Non-owner-occupied = vacant property (strong investor signal)
    is_non_owner_occupied = info.get("isNonOwnerOccupied", False)

    # Rent estimate (for yield calculations)
    rent_zestimate = info.get("rentZestimate")

    # URL
    detail_url = item.get("detailUrl", "")
    if detail_url and not detail_url.startswith("http"):
        detail_url = f"https://www.zillow.com{detail_url}"

    # Photos — get carousel photos if available, else thumbnail
    photo_urls = _extract_photos(item)

    # Parse address components from full address string
    city, state, zip_code = _parse_address_parts(address_full)

    # Build remarks from available text snippets
    # Zillow search API doesn't return full descriptions — detail pages are blocked.
    # Combine flex text and price reduction as our best available description.
    remarks_parts = []
    if flex_text and "days on Zillow" not in flex_text:
        remarks_parts.append(flex_text)
    if price_reduction_text:
        remarks_parts.append(f"Price reduction: {price_reduction_text}")
    if is_non_owner_occupied:
        remarks_parts.append("Non-owner occupied / vacant")
    remarks = ". ".join(remarks_parts) if remarks_parts else info.get("description")

    listing = {
        "id": f"zl-{zpid}",
        "search_id": search_id,
        "source": "zillow",
        "address": address_full.split(",")[0].strip() if "," in address_full else address_full,
        "city": city,
        "state": state,
        "zip_code": zip_code,
        "latitude": _safe_float(lat),
        "longitude": _safe_float(lng),
        "listing_type": "fsbo",
        "price": _safe_int(price),
        "beds": _safe_float(beds),
        "baths": _safe_float(baths),
        "sqft": _safe_int(sqft),
        "year_built": _safe_int(year_built),
        "property_type": home_type,
        "dom": _safe_int(dom),
        "redfin_url": None,
        "zillow_url": detail_url,
        "assessed_value": _safe_int(tax_assessed),
        "redfin_estimate": _safe_int(zestimate),
        "photo_urls": photo_urls,
        "remarks": remarks,
        "_is_non_owner_occupied": is_non_owner_occupied,
        "_rent_zestimate": _safe_int(rent_zestimate),
    }

    # Attach price change data for the upsert to detect
    if price_change is not None:
        listing["_price_change"] = _safe_int(price_change)
        listing["_price_change_date"] = price_change_date

    return listing


def _extract_photos(item: dict) -> Optional[list]:
    """Extract photo URLs from Zillow result, preferring carousel over thumbnail."""
    urls = []

    # carouselPhotosComposable is a dict with baseUrl template + photoData array
    # Example: {"baseUrl": "https://photos.zillowstatic.com/fp/{photoKey}-p_e.jpg",
    #           "photoData": [{"photoKey": "abc123"}, ...]}
    carousel = item.get("carouselPhotosComposable")
    if carousel and isinstance(carousel, dict):
        base_url = carousel.get("baseUrl", "")
        photo_data = carousel.get("photoData", [])
        if base_url and isinstance(photo_data, list):
            for photo in photo_data:
                if isinstance(photo, dict):
                    key = photo.get("photoKey", "")
                    if key:
                        urls.append(base_url.replace("{photoKey}", key))

    # Fall back to single thumbnail
    if not urls:
        img_src = item.get("imgSrc", "")
        # Skip Google Maps satellite images (no real photos)
        if img_src and "googleapis.com" not in img_src:
            urls = [img_src]

    return urls if urls else None


def _parse_address_parts(full_address: str) -> tuple:
    """Parse 'Street, City, ST 12345' into (city, state, zip)."""
    parts = [p.strip() for p in full_address.split(",")]
    city = ""
    state = ""
    zip_code = ""

    if len(parts) >= 3:
        city = parts[1]
        state_zip = parts[2].strip().split()
        if state_zip:
            state = state_zip[0]
        if len(state_zip) >= 2:
            zip_code = state_zip[1]
    elif len(parts) == 2:
        city = parts[1].strip()

    return city, state, zip_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Detail fetch (scrape Zillow detail page for description + photos)
# ---------------------------------------------------------------------------
def _get_oxylabs_proxy() -> Optional[dict]:
    """Get OxyLabs Web Unblocker proxy dict."""
    user = os.getenv("OXYLABS_USERNAME")
    password = os.getenv("OXYLABS_PASSWORD")
    host = os.getenv("OXYLABS_HOST", "unblock.oxylabs.io")
    port = os.getenv("OXYLABS_PORT", "60000")
    if not user or not password:
        return None
    proxy_url = f"http://{user}:{password}@{host}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def fetch_detail(listing: dict) -> Optional[dict]:
    """
    Fetch listing details (description, photos) from Zillow detail page.
    Uses OxyLabs Web Unblocker (primary) or Safari impersonation (fallback).

    Returns:
        Dict with remarks, photo_urls, or None on failure.
    """
    url = listing.get("zillow_url")
    if not url:
        return None

    # Try OxyLabs Web Unblocker first (bypasses PerimeterX)
    oxy_proxy = _get_oxylabs_proxy()
    if oxy_proxy:
        session = curl_requests.Session(impersonate="chrome131")
        try:
            resp = session.get(url, proxies=oxy_proxy, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }, timeout=45, verify=False)

            if resp.status_code == 200 and len(resp.text) > 10000:
                result = _parse_detail_page(resp.text)
                if result and result.get("remarks"):
                    return result
        except Exception as e:
            print(f"[Zillow] OxyLabs detail error: {e}")
        finally:
            session.close()

    # Fallback: direct Safari (works sometimes, but usually 403 on detail pages)
    session = _make_session()
    try:
        resp = session.get(url, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=20)

        if resp.status_code != 200:
            return None

        return _parse_detail_page(resp.text)

    except Exception as e:
        print(f"[Zillow] Detail fetch error: {e}")
        return None
    finally:
        session.close()


def _parse_detail_page(html: str) -> Optional[dict]:
    """Extract description and photos from Zillow detail page HTML."""
    import re as _re
    import json as _json

    result = {"remarks": None, "photo_urls": None}

    # Try __NEXT_DATA__ JSON blob (Next.js SSR data)
    m = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, _re.DOTALL)
    if m:
        try:
            next_data = _json.loads(m.group(1))
            props = next_data.get("props", {}).get("pageProps", {})

            # gdpClientCache: values may be stringified JSON or dicts
            cache = props.get("componentProps", {}).get("gdpClientCache", {})
            if isinstance(cache, str):
                try:
                    cache = _json.loads(cache)
                except (_json.JSONDecodeError, TypeError):
                    cache = {}

            if isinstance(cache, dict):
                for key, val in cache.items():
                    # Values can be stringified JSON
                    if isinstance(val, str):
                        try:
                            val = _json.loads(val)
                        except (_json.JSONDecodeError, TypeError):
                            continue

                    if isinstance(val, dict):
                        prop = val.get("property", val)

                        # Description
                        desc = prop.get("description", "")
                        if desc and len(desc) > 20 and not result["remarks"]:
                            result["remarks"] = desc

                        # Photos (detail page has higher quality)
                        if not result["photo_urls"]:
                            photos = prop.get("responsivePhotos") or prop.get("photos") or []
                            if photos:
                                urls = _extract_detail_photos(photos)
                                if urls:
                                    result["photo_urls"] = urls

            # Fallback: initialReduxState path
            if not result["remarks"]:
                redux = props.get("initialReduxState", {}).get("gdp", {})
                for sub in ("building", "property"):
                    obj = redux.get(sub, {})
                    if isinstance(obj, dict):
                        desc = obj.get("description", "")
                        if desc and len(desc) > 20:
                            result["remarks"] = desc
                            break

            if result["remarks"] or result["photo_urls"]:
                return result
        except (_json.JSONDecodeError, TypeError):
            pass

    # Fallback: regex for longest description in page JSON
    all_descs = _re.findall(r'"description"\s*:\s*"((?:[^"\\]|\\.){50,})"', html)
    if all_descs:
        best = max(all_descs, key=len)
        result["remarks"] = best.replace("\\n", " ").replace("\\r", "").replace('\\"', '"')
        return result

    # Fallback: meta description tag
    m = _re.search(r'<meta name="description" content="([^"]{50,})"', html)
    if m:
        result["remarks"] = m.group(1)
        return result

    return result if result["remarks"] else None


def _extract_detail_photos(photos: list) -> Optional[list]:
    """Extract photo URLs from detail page responsive photo data."""
    urls = []
    for p in photos[:30]:
        if isinstance(p, dict):
            for size_key in ("mixedSources", "sources"):
                sources = p.get(size_key, {})
                if isinstance(sources, dict):
                    for src_list in sources.values():
                        if isinstance(src_list, list) and src_list:
                            url = src_list[0].get("url", "")
                            if url:
                                urls.append(url)
                                break
                    if len(urls) > len(photos) - 1:
                        break
        elif isinstance(p, str):
            urls.append(p)
    return urls[:30] if urls else None


def fetch_details_batch(listings: list, delay: float = 3.0) -> dict:
    """
    Fetch details for a batch of Zillow listings.

    Returns:
        Dict mapping listing_id → detail dict (or None for failures).
    """
    results = {}
    total = len(listings)

    for i, listing in enumerate(listings):
        lid = listing.get("id", "unknown")
        print(f"[Zillow] Detail {i + 1}/{total}: {lid}")

        detail = fetch_detail(listing)
        results[lid] = detail

        if i < total - 1:
            time.sleep(delay)

    fetched = sum(1 for v in results.values() if v is not None)
    print(f"[Zillow] Details fetched: {fetched}/{total}")
    return results
