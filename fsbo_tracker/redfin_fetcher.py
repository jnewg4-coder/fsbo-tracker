"""
FSBO Listing Tracker — Redfin fetcher
Two passes: GIS-CSV (all listings) + aboveTheFold detail (remarks, photos, assessed).
Uses curl_cffi with impersonation rotation + IPRoyal/OxyLabs proxy cascade.
"""

import csv
import io
import json
import os
import re
import time
import traceback
from datetime import datetime
from typing import Optional

from curl_cffi import requests as curl_requests

from .config import REDFIN_DELAY, DETAIL_FETCH_DELAY
from .proxy import get_proxy, record_success, record_failure


# ---------------------------------------------------------------------------
# Browser impersonation rotation (same as working RedfinAPI)
# ---------------------------------------------------------------------------
IMPERSONATE_ROTATION = ["chrome131", "safari17_0", "chrome136"]
BLOCK_CODES = (403, 405, 429, 503, 520, 521)

UA_MAP = {
    "chrome131": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "safari17_0": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "chrome136": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
}

_imp_index = 0

GIS_CSV_URL = "https://www.redfin.com/stingray/api/gis-csv"
DETAIL_URL = "https://www.redfin.com/stingray/api/home/details/aboveTheFold"
AUTOCOMPLETE_URL = "https://www.redfin.com/stingray/do/location-autocomplete"


def _get_headers(impersonate: str = "chrome131") -> tuple:
    """Get browser + API headers matching the impersonation fingerprint."""
    ua = UA_MAP.get(impersonate, UA_MAP["chrome131"])

    browser_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "User-Agent": ua,
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    api_headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "User-Agent": ua,
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Referer": "https://www.redfin.com/",
    }

    return browser_headers, api_headers


# ---------------------------------------------------------------------------
# Proxy helpers — shared sticky session module
# ---------------------------------------------------------------------------
# Using fsbo_tracker.proxy for sticky sessions + cascade


# ---------------------------------------------------------------------------
# Session management with rotation
# ---------------------------------------------------------------------------
def _rotate_impersonation() -> str:
    """Rotate to next browser impersonation."""
    global _imp_index
    old = IMPERSONATE_ROTATION[_imp_index]
    _imp_index = (_imp_index + 1) % len(IMPERSONATE_ROTATION)
    new = IMPERSONATE_ROTATION[_imp_index]
    print(f"[Redfin] Rotating impersonation: {old} → {new}")
    return new


def _make_session(impersonate: str = None) -> curl_requests.Session:
    """Create a curl_cffi session with proxy and current impersonation."""
    if impersonate is None:
        impersonate = IMPERSONATE_ROTATION[_imp_index]
    proxy = get_proxy()
    session = curl_requests.Session(impersonate=impersonate)
    if proxy:
        session.proxies = proxy
    return session


def _is_captcha(html: str) -> bool:
    """Detect captcha/challenge pages."""
    if len(html) < 50000:
        markers = ['cf-challenge', 'px-captcha', '/captcha/',
                    'g-recaptcha', 'hcaptcha', 'please verify']
        return any(m in html.lower() for m in markers)
    return False


def _request_with_retry(method: str, url: str, max_retries: int = 3,
                         use_api_headers: bool = True, **kwargs) -> Optional[curl_requests.Response]:
    """
    Make a request with retry, impersonation rotation, and proxy cascade on blocks.
    Creates and manages its own session lifecycle.
    """
    imp = IMPERSONATE_ROTATION[_imp_index]

    for attempt in range(max_retries):
        session = _make_session(imp)

        # Warm session on first attempt
        if attempt == 0:
            browser_hdrs, _ = _get_headers(imp)
            try:
                session.get("https://www.redfin.com", headers=browser_hdrs, timeout=15)
                time.sleep(0.5)
            except Exception:
                pass

        try:
            resp = session.request(method, url, **kwargs)

            is_blocked = resp.status_code in BLOCK_CODES or _is_captcha(resp.text)
            if is_blocked:
                record_failure()
                reason = "captcha" if _is_captcha(resp.text) else str(resp.status_code)
                print(f"[Redfin] Blocked ({reason}) on attempt {attempt + 1}/{max_retries}")
                session.close()

                if attempt < max_retries - 1:
                    imp = _rotate_impersonation()
                    time.sleep(2)
                    continue
                return None

            record_success()
            # Don't close session yet — caller may need response
            return resp

        except Exception as e:
            print(f"[Redfin] Request error on attempt {attempt + 1}: {e}")
            record_failure()
            session.close()
            if attempt < max_retries - 1:
                imp = _rotate_impersonation()
                time.sleep(2)
                continue
            return None

    return None


# ---------------------------------------------------------------------------
# GIS-CSV fetch (Pass 1: all listings for a market)
# ---------------------------------------------------------------------------
def fetch_listings(search: dict) -> list:
    """
    Fetch FSBO + MLS-FSBO + foreclosure listings from Redfin GIS-CSV endpoint.

    Note: GIS-CSV is MLS-restricted — some listings excluded per MLS data rules.
    Foreclosures included per user's search criteria.

    Args:
        search: Search config dict with region_id, max_price, min_beds, bbox.

    Returns:
        List of normalized listing dicts ready for upsert.
    """
    _, api_headers = _get_headers(IMPERSONATE_ROTATION[_imp_index])

    params = {
        "al": 1,
        "market": "national",
        "num_homes": 350,
        "ord": "redfin-recommended-asc",
        "page_number": 1,
        "region_id": search["region_id"],
        "region_type": 6,
        "sf": "1,2,3,5,6,7",
        "status": 1,
        "uipt": "1,2,3,4,5,6,7,8",
        "v": 8,
        "fsbo": "true",
        "mlsfsbo": "true",
        "foreclosure": "true",
        "max_price": search.get("max_price", 500000),
        "mpt": 99,
    }

    min_beds = search.get("min_beds", 0)
    if min_beds and min_beds > 0:
        params["min_beds"] = min_beds

    # No DOM filter on fetch — DOM used only in scoring + frontend UI filter
    # This ensures we capture all listings regardless of days on market

    if all(k in search for k in ("min_lat", "max_lat", "min_lng", "max_lng")):
        params["mapi_shire"] = search["max_lat"]
        params["mapi_fife"] = search["min_lat"]
        params["mapi_frodo"] = search["max_lng"]
        params["mapi_sam"] = search["min_lng"]

    print(f"[Redfin] Fetching GIS-CSV for {search.get('name', search['id'])}...")

    resp = _request_with_retry(
        "GET", GIS_CSV_URL,
        params=params, headers=api_headers, timeout=30,
    )

    if not resp or resp.status_code != 200:
        code = resp.status_code if resp else "no response"
        print(f"[Redfin] GIS-CSV error: {code}")
        return []

    return _parse_gis_csv(resp.text, search["id"])


def _parse_gis_csv(text: str, search_id: str) -> list:
    """Parse Redfin GIS-CSV response into normalized listing dicts."""
    if not text or not text.strip():
        print("[Redfin] Empty CSV response")
        return []

    listings = []
    reader = csv.DictReader(io.StringIO(text))

    url_key = None
    for key in (reader.fieldnames or []):
        if key and "URL" in key:
            url_key = key
            break

    for row in reader:
        try:
            sale_type = row.get("SALE TYPE", "")
            if sale_type and "accordance" in sale_type.lower():
                continue

            address = row.get("ADDRESS", "")
            if not address or not address.strip():
                continue
            address = address.strip()

            redfin_url = row.get(url_key, "").strip() if url_key else ""
            prop_id = _extract_property_id(redfin_url)
            if not prop_id:
                prop_id = address.lower().replace(" ", "-").replace(",", "")[:60]

            price = _safe_int(row.get("PRICE"))
            listing_type = _detect_listing_type(row)

            listing = {
                "id": f"rf-{prop_id}",
                "search_id": search_id,
                "source": "redfin",
                "address": address,
                "city": row.get("CITY", "").strip(),
                "state": row.get("STATE OR PROVINCE", "").strip(),
                "zip_code": row.get("ZIP OR POSTAL CODE", "").strip(),
                "latitude": _safe_float(row.get("LATITUDE")),
                "longitude": _safe_float(row.get("LONGITUDE")),
                "listing_type": listing_type,
                "price": price,
                "beds": _safe_float(row.get("BEDS")),
                "baths": _safe_float(row.get("BATHS")),
                "sqft": _safe_int(row.get("SQUARE FEET")),
                "year_built": _safe_int(row.get("YEAR BUILT")),
                "property_type": row.get("PROPERTY TYPE", "").strip(),
                "dom": _safe_int(row.get("DAYS ON MARKET")),
                "redfin_url": redfin_url,
            }

            listings.append(listing)

        except Exception as e:
            print(f"[Redfin] Row parse error: {e}")
            continue

    print(f"[Redfin] Parsed {len(listings)} listings from CSV")
    return listings


def _extract_property_id(url: str) -> str:
    """Extract property ID from Redfin URL like .../home/12345."""
    if not url:
        return ""
    parts = url.rstrip("/").split("/")
    for i, part in enumerate(parts):
        if part == "home" and i + 1 < len(parts):
            return parts[i + 1]
    for part in reversed(parts):
        if part.isdigit():
            return part
    return ""


def _detect_listing_type(row: dict) -> str:
    """Detect listing type from CSV SALE TYPE column."""
    sale_type = row.get("SALE TYPE", "").lower()
    if "fsbo" in sale_type:
        return "fsbo"
    if "foreclos" in sale_type or "reo" in sale_type or "bank" in sale_type:
        return "foreclosure"
    return "mlsfsbo"


# ---------------------------------------------------------------------------
# Detail fetch (Pass 2: remarks, photos, assessed value)
# ---------------------------------------------------------------------------
def fetch_detail(listing: dict) -> Optional[dict]:
    """
    Fetch property details (remarks, photos, assessed value, Redfin estimate).
    Uses impersonation rotation + proxy cascade on blocks.
    """
    prop_id = listing.get("id", "").replace("rf-", "")
    if not prop_id or not prop_id.isdigit():
        prop_id = _extract_property_id(listing.get("redfin_url", ""))
    if not prop_id or not prop_id.isdigit():
        return None

    _, api_headers = _get_headers(IMPERSONATE_ROTATION[_imp_index])

    params = {
        "propertyId": prop_id,
        "accessLevel": 3,
    }

    resp = _request_with_retry(
        "GET", DETAIL_URL,
        params=params, headers=api_headers, timeout=30,
    )

    if not resp or resp.status_code != 200:
        code = resp.status_code if resp else "no response"
        print(f"[Redfin] Detail API failed ({code}) for {prop_id}, trying page scrape...")
        # Fallback: try scraping the listing page
        redfin_url = listing.get("redfin_url")
        if redfin_url:
            return _scrape_listing_page(redfin_url)
        return None

    return _parse_detail(resp.text)


def _parse_detail(text: str) -> Optional[dict]:
    """Parse aboveTheFold JSON response."""
    cleaned = text
    if cleaned.startswith("{}&&"):
        cleaned = cleaned[4:]

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        print("[Redfin] Failed to parse detail JSON")
        return None

    payload = data.get("payload", {})
    result = {
        "remarks": None,
        "photo_urls": None,
        "assessed_value": None,
        "redfin_estimate": None,
        "last_sold_price": None,
        "last_sold_date": None,
    }

    listing_remarks = payload.get("listingRemarks")
    if listing_remarks:
        result["remarks"] = listing_remarks.get("remarksCommon", "")

    photos = payload.get("mediaBrowserInfo", {}).get("photos", [])
    if photos:
        photo_urls = []
        for p in photos:
            url = p.get("photoUrls", {}).get("fullScreenPhotoUrl")
            if not url and p.get("photoUrls"):
                url = p["photoUrls"].get("nonFullScreenPhotoUrl")
            if url:
                if url.startswith("//"):
                    url = "https:" + url
                photo_urls.append(url)
        if photo_urls:
            result["photo_urls"] = photo_urls

    pr_info = payload.get("publicRecordsInfo", {})
    tax_info = pr_info.get("taxInfo", {})
    if tax_info:
        result["assessed_value"] = _safe_int(tax_info.get("totalAssessedValue"))

    avm = payload.get("avm", {})
    if avm:
        result["redfin_estimate"] = _safe_int(avm.get("predictedValue"))

    # Last sold — from publicRecordsInfo or propertyHistory
    sale_info = pr_info.get("lastSaleData") or pr_info.get("saleInfo", {})
    if isinstance(sale_info, dict):
        sold_price = _safe_int(sale_info.get("lastSoldPrice") or sale_info.get("amount"))
        sold_date = sale_info.get("lastSoldDate") or sale_info.get("date", "")
        if sold_price and sold_price > 0:
            result["last_sold_price"] = sold_price
            result["last_sold_date"] = str(sold_date)

    # Fallback: check property history events
    if not result["last_sold_price"]:
        history = payload.get("propertyHistoryInfo", {}).get("events", [])
        for evt in history:
            if isinstance(evt, dict):
                evt_type = (evt.get("eventDescription", "") or "").lower()
                if "sold" in evt_type:
                    sold_price = _safe_int(evt.get("price"))
                    if sold_price and sold_price > 0:
                        result["last_sold_price"] = sold_price
                        result["last_sold_date"] = str(evt.get("eventDate", ""))
                    break

    return result


# ---------------------------------------------------------------------------
# Page scraping fallback — extract description from Redfin listing page HTML
# ---------------------------------------------------------------------------
def _scrape_listing_page(url: str) -> Optional[dict]:
    """
    Scrape a Redfin listing page HTML for description, photos, and estimates.
    Fallback when aboveTheFold API is blocked.
    """
    browser_headers, _ = _get_headers(IMPERSONATE_ROTATION[_imp_index])

    resp = _request_with_retry(
        "GET", url,
        headers=browser_headers, timeout=30,
    )

    if not resp or resp.status_code != 200:
        return None

    html = resp.text
    result = {"remarks": None, "photo_urls": None, "assessed_value": None, "redfin_estimate": None}

    # Method 1: Extract remarksCommon from embedded JSON
    remarks_match = re.search(r'"remarksCommon"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if remarks_match:
        try:
            result["remarks"] = json.loads(f'"{remarks_match.group(1)}"')
        except (json.JSONDecodeError, ValueError):
            result["remarks"] = remarks_match.group(1)

    # Method 2: Try description meta tag
    if not result["remarks"]:
        meta_match = re.search(r'<meta\s+name="description"\s+content="([^"]{30,})"', html)
        if meta_match:
            result["remarks"] = meta_match.group(1)

    # Extract Redfin estimate
    est_match = re.search(r'"predictedValue"\s*:\s*([0-9]+(?:\.[0-9]+)?)', html)
    if est_match:
        result["redfin_estimate"] = int(float(est_match.group(1)))

    # Extract assessed value
    assessed_match = re.search(r'"totalAssessedValue"\s*:\s*([0-9]+)', html)
    if assessed_match:
        result["assessed_value"] = int(assessed_match.group(1))

    if result["remarks"] or result["redfin_estimate"] or result["assessed_value"]:
        return result
    return None


# ---------------------------------------------------------------------------
# Address autocomplete — find Redfin property ID + URL from any address
# ---------------------------------------------------------------------------
def search_address(address: str, city: str = "", state: str = "", zip_code: str = "") -> Optional[dict]:
    """
    Find a Redfin property by address using autocomplete.

    Returns:
        Dict with 'property_id', 'url', 'name' or None.
    """
    query = address
    if city:
        query += f", {city}"
    if state:
        query += f", {state}"
    if zip_code:
        query += f" {zip_code}"

    _, api_headers = _get_headers(IMPERSONATE_ROTATION[_imp_index])

    params = {
        "location": query,
        "start": 0,
        "count": 10,
        "v": 2,
        "market": "national",
        "al": 1,
        "iss": "false",
        "ooa": "true",
    }

    resp = _request_with_retry(
        "GET", AUTOCOMPLETE_URL,
        params=params, headers=api_headers, timeout=20,
    )

    if not resp or resp.status_code != 200:
        return None

    try:
        text = resp.text
        if text.startswith("{}&&"):
            text = text[4:]
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None

    payload = data.get("payload", {})

    # Try exact match first
    exact = payload.get("exactMatch")
    if exact:
        prop_id = exact.get("id")
        url = exact.get("url", "")
        if prop_id:
            if url and not url.startswith("http"):
                url = f"https://www.redfin.com{url}"
            return {"property_id": str(prop_id), "url": url, "name": exact.get("name", "")}

    # Try first section result
    sections = payload.get("sections", [])
    for section in sections:
        rows = section.get("rows", [])
        if rows:
            row = rows[0]
            prop_id = row.get("id")
            url = row.get("url", "")
            if prop_id:
                if url and not url.startswith("http"):
                    url = f"https://www.redfin.com{url}"
                return {"property_id": str(prop_id), "url": url, "name": row.get("name", "")}

    return None


def find_description_by_address(address: str, city: str = "", state: str = "",
                                 zip_code: str = "") -> Optional[dict]:
    """
    Find and fetch a listing description by address through Redfin.
    Combines autocomplete + detail fetch/page scrape.

    Returns:
        Dict with 'remarks', 'redfin_url', 'redfin_estimate', 'assessed_value' or None.
    """
    # Step 1: Find the property on Redfin
    found = search_address(address, city, state, zip_code)
    if not found:
        return None

    prop_id = found["property_id"]
    redfin_url = found.get("url", "")

    # Step 2: Try aboveTheFold API first (structured, fast)
    _, api_headers = _get_headers(IMPERSONATE_ROTATION[_imp_index])

    if prop_id.isdigit():
        resp = _request_with_retry(
            "GET", DETAIL_URL,
            params={"propertyId": prop_id, "accessLevel": 3},
            headers=api_headers, timeout=30,
        )

        if resp and resp.status_code == 200:
            detail = _parse_detail(resp.text)
            if detail and detail.get("remarks"):
                detail["redfin_url"] = redfin_url
                return detail

    # Step 3: Fallback to page scrape
    if redfin_url:
        detail = _scrape_listing_page(redfin_url)
        if detail:
            detail["redfin_url"] = redfin_url
            return detail

    return None


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------
def fetch_details_batch(listings: list, delay: float = None) -> dict:
    """
    Fetch details for a batch of Redfin listings with rate limiting.

    Returns:
        Dict mapping listing_id → detail dict (or None for failures).
    """
    if delay is None:
        delay = DETAIL_FETCH_DELAY

    results = {}
    total = len(listings)

    for i, listing in enumerate(listings):
        lid = listing.get("id", "unknown")
        print(f"[Redfin] Detail {i + 1}/{total}: {lid}")

        detail = fetch_detail(listing)
        results[lid] = detail

        if i < total - 1:
            time.sleep(delay)

    fetched = sum(1 for v in results.values() if v is not None)
    print(f"[Redfin] Details fetched: {fetched}/{total}")
    return results


def fetch_descriptions_batch(listings: list, delay: float = 3.0) -> dict:
    """
    Cross-reference listings through Redfin to get descriptions.
    For Zillow-sourced listings without remarks — find on Redfin by address.

    Returns:
        Dict mapping listing_id → detail dict (or None for failures).
    """
    results = {}
    total = len(listings)

    for i, listing in enumerate(listings):
        lid = listing.get("id", "unknown")
        address = listing.get("address", "")
        city = listing.get("city", "")
        state = listing.get("state", "")
        zip_code = listing.get("zip_code", "")

        print(f"[Redfin] Cross-ref {i + 1}/{total}: {address}")

        detail = find_description_by_address(address, city, state, zip_code)
        results[lid] = detail

        if detail and detail.get("remarks"):
            desc_preview = detail["remarks"][:80] + "..." if len(detail.get("remarks", "")) > 80 else detail.get("remarks", "")
            print(f"  Found: {desc_preview}")

        if i < total - 1:
            time.sleep(delay)

    found = sum(1 for v in results.values() if v and v.get("remarks"))
    print(f"[Redfin] Descriptions found: {found}/{total}")
    return results


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
