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
from .proxy import get_proxy, record_success, record_failure, burn_session


# ---------------------------------------------------------------------------
# Browser impersonation rotation (same as working RedfinAPI)
# ---------------------------------------------------------------------------
IMPERSONATE_ROTATION = ["chrome131", "safari17_0", "chrome136"]

# P1 fix: Split block classification
# Hard blocks → burn session + rotate IP (definitive bot detection)
HARD_BLOCK_CODES = (403, 429)
# Transient errors → retry same IP, no burn (upstream issues)
TRANSIENT_CODES = (405, 503, 520, 521)

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

# ---------------------------------------------------------------------------
# Module-level session state — REUSE same session (sticky cookies + IP)
# ---------------------------------------------------------------------------
_session: Optional[curl_requests.Session] = None
_session_warmed: bool = False

# ---------------------------------------------------------------------------
# Circuit breaker — skip endpoints that are consistently blocked
# After CIRCUIT_THRESHOLD consecutive hard-blocks on an endpoint, skip it
# entirely for CIRCUIT_COOLDOWN seconds and go straight to fallback.
# ---------------------------------------------------------------------------
_circuit_state: dict = {}  # key -> {"fails": int, "skip_until": float, "skips": int}
CIRCUIT_THRESHOLD = 3
CIRCUIT_COOLDOWN = 300  # 5 minutes

# Endpoint metrics (reset per batch)
_metrics: dict = {
    "api_attempts": 0, "api_success": 0, "api_hard_blocked": 0,
    "api_transient": 0, "scrape_attempts": 0, "scrape_success": 0,
    "burns": 0, "circuit_skips": 0,
}


def _is_circuit_open(key: str) -> bool:
    """Check if circuit breaker is tripped (should skip this endpoint)."""
    state = _circuit_state.get(key)
    if not state or state["fails"] < CIRCUIT_THRESHOLD:
        return False
    now = time.time()
    if state["skip_until"] and now < state["skip_until"]:
        state["skips"] += 1
        _metrics["circuit_skips"] += 1
        return True
    # Cooldown expired — allow one probe attempt
    if state["skip_until"] and now >= state["skip_until"]:
        state["skip_until"] = 0  # Reset for probe
        print(f"[Circuit:{key}] Cooldown expired, probing endpoint")
    return False


def _circuit_record_failure(key: str):
    """Record a hard-block failure for circuit breaker."""
    state = _circuit_state.setdefault(key, {"fails": 0, "skip_until": 0, "skips": 0})
    state["fails"] += 1
    if state["fails"] >= CIRCUIT_THRESHOLD and not state["skip_until"]:
        state["skip_until"] = time.time() + CIRCUIT_COOLDOWN
        print(f"[Circuit:{key}] OPEN — skipping for {CIRCUIT_COOLDOWN}s "
              f"after {state['fails']} consecutive hard blocks")


def _circuit_record_success(key: str):
    """Record success — close circuit breaker."""
    state = _circuit_state.get(key)
    if state and state["fails"] > 0:
        print(f"[Circuit:{key}] Closed (success after {state['fails']} failures, "
              f"{state['skips']} skips)")
        state["fails"] = 0
        state["skip_until"] = 0
        state["skips"] = 0


def get_metrics() -> dict:
    """Get current endpoint metrics."""
    return dict(_metrics)


def reset_metrics():
    """Reset metrics (call at start of each batch)."""
    for k in _metrics:
        _metrics[k] = 0


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
# Session management — sticky session, reuse across requests
# ---------------------------------------------------------------------------
def _rotate_impersonation() -> str:
    """Rotate to next browser impersonation."""
    global _imp_index
    old = IMPERSONATE_ROTATION[_imp_index]
    _imp_index = (_imp_index + 1) % len(IMPERSONATE_ROTATION)
    new = IMPERSONATE_ROTATION[_imp_index]
    print(f"[Redfin] Rotating impersonation: {old} → {new}")
    return new


def _get_or_create_session() -> curl_requests.Session:
    """Get existing session or create one. Reuses same session for sticky IP + cookies."""
    global _session, _session_warmed
    if _session is None:
        imp = IMPERSONATE_ROTATION[_imp_index]
        _session = curl_requests.Session(impersonate=imp)
        proxy = get_proxy()
        if proxy:
            _session.proxies = proxy
        _session_warmed = False
        print(f"[Redfin] Created session (imp={imp})")
    return _session


def _warm_session():
    """Visit homepage to establish cookies. Once per session lifecycle."""
    global _session_warmed
    if _session_warmed:
        return
    session = _get_or_create_session()
    imp = IMPERSONATE_ROTATION[_imp_index]
    browser_hdrs, _ = _get_headers(imp)
    try:
        resp = session.get("https://www.redfin.com", headers=browser_hdrs, timeout=15)
        if resp.status_code == 200:
            time.sleep(0.5)
            _session_warmed = True
            print(f"[Redfin] Session warmed (cookies: {len(session.cookies)})")
        else:
            print(f"[Redfin] Warmup got {resp.status_code} — session may be degraded")
            _session_warmed = True  # Still mark warmed to avoid infinite loops
    except Exception as e:
        print(f"[Redfin] Warmup failed: {e}")
        _session_warmed = True  # Avoid retrying warmup in a loop


def _reset_session(rotate: bool = True):
    """Destroy and recreate session after burn. New IP + new cookies."""
    global _session, _session_warmed
    if rotate:
        _rotate_impersonation()
    if _session:
        try:
            _session.close()
        except Exception:
            pass
    _session = None
    _session_warmed = False
    # Next _get_or_create_session() will build a fresh session with new proxy IP


def _is_captcha(html: str) -> bool:
    """Detect captcha/challenge pages."""
    if len(html) < 50000:
        markers = ['cf-challenge', 'px-captcha', '/captcha/',
                    'g-recaptcha', 'hcaptcha', 'please verify']
        return any(m in html.lower() for m in markers)
    return False


def _request_with_retry(method: str, url: str, max_retries: int = 2,
                         **kwargs) -> Optional[curl_requests.Response]:
    """
    Make a request with retry. Reuses sticky session.
    Max 2 retries by default (per user instruction: "stop after most 2 tries").

    On block (403/captcha): burn session → new IP + new cookies + rotate impersonation.
    On exception (timeout): record failure but keep same IP (transient error).
    """
    _warm_session()
    session = _get_or_create_session()

    for attempt in range(max_retries):
        try:
            resp = session.request(method, url, **kwargs)

            is_captcha_page = _is_captcha(resp.text)
            is_hard_block = resp.status_code in HARD_BLOCK_CODES or is_captcha_page
            is_transient = resp.status_code in TRANSIENT_CODES

            if is_hard_block:
                reason = "captcha" if is_captcha_page else str(resp.status_code)
                print(f"[Redfin] Hard block ({reason}) attempt {attempt + 1}/{max_retries}")
                burn_session(reason)
                _reset_session(rotate=True)
                _metrics["burns"] += 1

                if attempt < max_retries - 1:
                    _warm_session()
                    session = _get_or_create_session()
                    time.sleep(2)
                    continue
                return None

            if is_transient:
                # P1: transient upstream error — retry same IP, no burn
                print(f"[Redfin] Transient {resp.status_code} attempt "
                      f"{attempt + 1}/{max_retries} (keeping same IP)")
                _metrics["api_transient"] += 1
                record_failure()
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None

            record_success()
            return resp

        except Exception as e:
            print(f"[Redfin] Request error attempt {attempt + 1}/{max_retries}: {e}")
            record_failure()
            if attempt < max_retries - 1:
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

    Uses circuit breaker: if the aboveTheFold API is consistently blocked,
    skips it entirely and goes straight to page scrape (zero IP burns).
    """
    prop_id = listing.get("id", "").replace("rf-", "")
    if not prop_id or not prop_id.isdigit():
        prop_id = _extract_property_id(listing.get("redfin_url", ""))
    if not prop_id or not prop_id.isdigit():
        return None

    redfin_url = listing.get("redfin_url")

    # P0 fix: Circuit breaker — skip API if consistently blocked
    if not _is_circuit_open("detail_api"):
        _, api_headers = _get_headers(IMPERSONATE_ROTATION[_imp_index])
        params = {"propertyId": prop_id, "accessLevel": 3}

        _metrics["api_attempts"] += 1
        resp = _request_with_retry(
            "GET", DETAIL_URL,
            max_retries=2, params=params, headers=api_headers, timeout=30,
        )

        if resp and resp.status_code == 200:
            _circuit_record_success("detail_api")
            _metrics["api_success"] += 1
            return _parse_detail(resp.text)

        # API failed — record for circuit breaker
        _circuit_record_failure("detail_api")
        _metrics["api_hard_blocked"] += 1
        code = resp.status_code if resp else "no response"
        print(f"[Redfin] Detail API failed ({code}) for {prop_id}, falling back to scrape")

    # Fallback: page scrape (single attempt — no extra burns)
    if redfin_url:
        _metrics["scrape_attempts"] += 1
        result = _scrape_listing_page(redfin_url, single_attempt=True)
        if result:
            _metrics["scrape_success"] += 1
        return result
    return None


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

    # Attribution / listing agent info (FSBO: "agent" is often the seller)
    attr = payload.get("attributionInfo") or {}
    agent_name = attr.get("listingAgentName") or attr.get("agentName") or ""
    agent_phone = attr.get("listingAgentPhoneNumber") or attr.get("agentPhoneNumber") or ""
    broker = attr.get("listingBrokerName") or attr.get("brokerName") or ""
    if agent_name or agent_phone:
        result["seller_name"] = agent_name.strip() if agent_name else None
        result["seller_phone"] = agent_phone.strip() if agent_phone else None
        result["seller_broker"] = broker.strip() if broker else None

    # Extract phone/email from remarks via regex
    contact = _extract_contact_from_text(result.get("remarks") or "")
    if contact.get("phone") and not result.get("seller_phone"):
        result["seller_phone"] = contact["phone"]
    if contact.get("email"):
        result["seller_email"] = contact["email"]
    if contact.get("name") and not result.get("seller_name"):
        result["seller_name"] = contact["name"]

    return result


# ---------------------------------------------------------------------------
# Page scraping fallback — extract description from Redfin listing page HTML
# ---------------------------------------------------------------------------
def _scrape_listing_page(url: str, single_attempt: bool = False) -> Optional[dict]:
    """
    Scrape a Redfin listing page HTML for description, photos, and estimates.
    Fallback when aboveTheFold API is blocked.

    Args:
        single_attempt: If True, only one request attempt (no retry loop).
                       Used when called as fallback from fetch_detail to avoid
                       doubling the total retry count.
    """
    browser_headers, _ = _get_headers(IMPERSONATE_ROTATION[_imp_index])

    resp = _request_with_retry(
        "GET", url,
        max_retries=1 if single_attempt else 2,
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

    # Try extracting attribution from embedded JSON
    agent_match = re.search(r'"listingAgentName"\s*:\s*"([^"]+)"', html)
    phone_match = re.search(r'"listingAgentPhoneNumber"\s*:\s*"([^"]+)"', html)
    broker_match = re.search(r'"listingBrokerName"\s*:\s*"([^"]+)"', html)
    if agent_match:
        result["seller_name"] = agent_match.group(1).strip()
    if phone_match:
        result["seller_phone"] = phone_match.group(1).strip()
    if broker_match:
        result["seller_broker"] = broker_match.group(1).strip()

    # Extract contact from remarks text
    if result.get("remarks"):
        contact = _extract_contact_from_text(result["remarks"])
        if contact.get("phone") and not result.get("seller_phone"):
            result["seller_phone"] = contact["phone"]
        if contact.get("email"):
            result["seller_email"] = contact["email"]
        if contact.get("name") and not result.get("seller_name"):
            result["seller_name"] = contact["name"]

    if result["remarks"] or result["redfin_estimate"] or result["assessed_value"] or result.get("seller_phone"):
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

    # Step 2: Try aboveTheFold API (only if circuit breaker allows)
    if prop_id.isdigit() and not _is_circuit_open("detail_api"):
        _, api_headers = _get_headers(IMPERSONATE_ROTATION[_imp_index])
        _metrics["api_attempts"] += 1
        resp = _request_with_retry(
            "GET", DETAIL_URL,
            params={"propertyId": prop_id, "accessLevel": 3},
            headers=api_headers, timeout=30,
        )

        if resp and resp.status_code == 200:
            _circuit_record_success("detail_api")
            _metrics["api_success"] += 1
            detail = _parse_detail(resp.text)
            if detail and detail.get("remarks"):
                detail["redfin_url"] = redfin_url
                return detail
        else:
            _circuit_record_failure("detail_api")
            _metrics["api_hard_blocked"] += 1

    # Step 3: Fallback to page scrape (single attempt)
    if redfin_url:
        _metrics["scrape_attempts"] += 1
        detail = _scrape_listing_page(redfin_url, single_attempt=True)
        if detail:
            _metrics["scrape_success"] += 1
            detail["redfin_url"] = redfin_url
            return detail

    return None


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------
def fetch_details_batch(listings: list, delay: float = None,
                         max_consecutive_fails: int = 10) -> dict:
    """
    Fetch details for a batch of Redfin listings with rate limiting.
    Stops early if too many consecutive failures (avoids burning through
    the entire list when every request is blocked).

    Returns:
        Dict mapping listing_id → detail dict (or None for failures).
    """
    if delay is None:
        delay = DETAIL_FETCH_DELAY

    reset_metrics()
    results = {}
    total = len(listings)
    consecutive_fails = 0

    for i, listing in enumerate(listings):
        lid = listing.get("id", "unknown")
        print(f"[Redfin] Detail {i + 1}/{total}: {lid}")

        detail = fetch_detail(listing)
        results[lid] = detail

        if detail is not None:
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            if consecutive_fails >= max_consecutive_fails:
                print(f"[Redfin] Stopping batch — {consecutive_fails} consecutive failures")
                break

        if i < total - 1:
            time.sleep(delay)

    fetched = sum(1 for v in results.values() if v is not None)
    m = get_metrics()
    print(f"[Redfin] Details fetched: {fetched}/{total}")
    print(f"[Redfin] Metrics: API {m['api_success']}/{m['api_attempts']} ok, "
          f"scrape {m['scrape_success']}/{m['scrape_attempts']} ok, "
          f"burns={m['burns']}, circuit_skips={m['circuit_skips']}")
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
# Contact extraction from listing text
# ---------------------------------------------------------------------------
_PHONE_RE = re.compile(r'(?<!\d)(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})(?!\d)')
_EMAIL_RE = re.compile(r'[\w.\-]+@[\w.\-]+\.\w{2,}')
# Patterns like "Contact John Smith at 555-1234", "Call Jane Doe", "Ask for Bob"
# Case-sensitive on name parts so "at", "or", "for" don't get captured as names
_NAME_RE = re.compile(
    r'(?:[Cc]ontact|[Cc]all|[Aa]sk for|[Rr]each|[Tt]ext)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})',
)


def _extract_contact_from_text(text: str) -> dict:
    """Extract phone, email, and seller name from listing text."""
    result = {}
    if not text:
        return result

    phones = _PHONE_RE.findall(text)
    if phones:
        result["phone"] = phones[0].strip()

    emails = _EMAIL_RE.findall(text)
    if emails:
        result["email"] = emails[0].strip()

    names = _NAME_RE.findall(text)
    if names:
        result["name"] = names[0].strip()

    return result


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
