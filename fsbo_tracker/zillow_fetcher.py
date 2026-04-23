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
from .proxy import get_iproyal_proxy, get_oxylabs_proxy


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
    """Get proxy dict — delegates to shared sticky session module."""
    return get_iproyal_proxy()


def _make_session() -> curl_requests.Session:
    """Create a curl_cffi session with Safari impersonation (bypasses Zillow blocks)."""
    # Safari17_0 works where Chrome131 gets captcha-blocked on Zillow
    session = curl_requests.Session(impersonate="safari17_0")
    # NOTE: Zillow BLOCKS residential proxies. Direct connection works with Safari fingerprint.
    return session


# ---------------------------------------------------------------------------
# FSBO search
# ---------------------------------------------------------------------------
MAX_PAGES_PER_BBOX = 5       # Zillow caps ~40/page; 5 pages ~= 200/bbox
TILE_IF_TOTAL_OVER = 120     # If page 1 totalResultCount > this, tile the bbox
MAX_TILE_DEPTH = 1           # 1 = quad-split once (4 tiles max per market)


def _do_query(session, payload: dict) -> tuple:
    """Execute one Zillow search query. Returns (results_list, total_result_count, status).

    Cascade on 403: direct session → fresh direct session → OxyLabs Web Unlocker.
    """
    try:
        # Attempt 1: direct
        resp = session.put(SEARCH_URL, json=payload, headers=_HEADERS, timeout=30)

        # Attempt 2: fresh direct session on 403 (LOCAL — don't close caller's session)
        if resp.status_code == 403:
            time.sleep(2)
            retry_session = _make_session()
            try:
                retry_session.get("https://www.zillow.com/", timeout=15)
                time.sleep(1)
            except Exception:
                pass
            resp = retry_session.put(SEARCH_URL, json=payload, headers=_HEADERS, timeout=30)
            retry_session.close()

        # Attempt 3: IPRoyal residential proxy. On 403, rotate to fresh IP once.
        if resp.status_code == 403:
            for ipro_try in range(2):
                ipro = get_iproyal_proxy(force_new_session=(ipro_try > 0))
                if not ipro:
                    break
                label = "IPRoyal" if ipro_try == 0 else "IPRoyal (fresh session)"
                print(f"[Zillow] Direct blocked, trying {label}")
                try:
                    ipro_session = curl_requests.Session(impersonate="safari17_0")
                    try:
                        ipro_session.get("https://www.zillow.com/", proxies=ipro, timeout=15)
                        time.sleep(1)
                    except Exception:
                        pass
                    r3 = ipro_session.put(
                        SEARCH_URL,
                        json=payload,
                        headers=_HEADERS,
                        proxies=ipro,
                        timeout=45,
                    )
                    ipro_session.close()
                    if r3.status_code == 200:
                        data = r3.json()
                        cat1 = data.get("cat1", {}) or {}
                        results = (cat1.get("searchResults", {}) or {}).get("listResults", []) or []
                        total = (cat1.get("searchList", {}) or {}).get("totalResultCount", len(results)) or 0
                        print(f"[Zillow] {label} success: {len(results)} results")
                        return (results, total, 200)
                    else:
                        print(f"[Zillow] {label} returned {r3.status_code}")
                        if r3.status_code != 403:
                            break  # Non-403 status, stop retrying IPRoyal
                except Exception as e:
                    print(f"[Zillow] {label} error: {e}")
                    break

        # Attempt 4: OxyLabs Web Unlocker (last resort — slow but best bypass)
        oxy = get_oxylabs_proxy()
        if oxy:
            print("[Zillow] Trying OxyLabs Web Unlocker (last resort)")
            try:
                import requests as _std_requests
                import urllib3
                urllib3.disable_warnings()
                r2 = _std_requests.post(
                    SEARCH_URL,
                    json=payload,
                    headers=_HEADERS,
                    proxies=oxy,
                    timeout=60,
                    verify=False,
                )
                if r2.status_code == 200:
                    data = r2.json()
                    cat1 = data.get("cat1", {}) or {}
                    results = (cat1.get("searchResults", {}) or {}).get("listResults", []) or []
                    total = (cat1.get("searchList", {}) or {}).get("totalResultCount", len(results)) or 0
                    print(f"[Zillow] OxyLabs success: {len(results)} results")
                    return (results, total, 200)
                else:
                    print(f"[Zillow] OxyLabs returned {r2.status_code}")
                    return ([], 0, r2.status_code)
            except Exception as e:
                print(f"[Zillow] OxyLabs error: {e}")
                return ([], 0, 0)

        if resp.status_code != 200:
            return ([], 0, resp.status_code)
        data = resp.json()
        cat1 = data.get("cat1", {}) or {}
        results = (cat1.get("searchResults", {}) or {}).get("listResults", []) or []
        total = (cat1.get("searchList", {}) or {}).get("totalResultCount", len(results)) or 0
        return (results, total, 200)
    except Exception as e:
        print(f"[Zillow] Query error: {e}")
        return ([], 0, 0)


def _fetch_bbox(session, search: dict, bbox: dict, label: str) -> list:
    """Fetch all pages for a single bbox. Returns raw list items (not yet parsed)."""
    collected = []
    for page in range(1, MAX_PAGES_PER_BBOX + 1):
        payload = _build_search_payload(search, bbox=bbox, page=page)
        results, total, status = _do_query(session, payload)
        if status != 200:
            print(f"[Zillow] {label} p{page}: HTTP {status}, stopping")
            break
        print(f"[Zillow] {label} p{page}: {len(results)} results (total reported: {total})")
        if not results:
            break
        collected.extend(results)
        # Stop if we have everything or page returned less than a full page
        if len(collected) >= total or len(results) < 40:
            break
        time.sleep(1)
    return collected


def _quad_split(bbox: dict) -> list:
    """Split bbox into 4 quadrants (SW, SE, NW, NE)."""
    mid_lat = (bbox["north"] + bbox["south"]) / 2
    mid_lng = (bbox["east"] + bbox["west"]) / 2
    return [
        {"south": bbox["south"], "north": mid_lat, "west": bbox["west"], "east": mid_lng},  # SW
        {"south": bbox["south"], "north": mid_lat, "west": mid_lng, "east": bbox["east"]},  # SE
        {"south": mid_lat, "north": bbox["north"], "west": bbox["west"], "east": mid_lng},  # NW
        {"south": mid_lat, "north": bbox["north"], "west": mid_lng, "east": bbox["east"]},  # NE
    ]


def fetch_listings(search: dict) -> list:
    """
    Fetch FSBO listings from Zillow for a market.

    Strategy: paginate the bbox up to MAX_PAGES_PER_BBOX. If the initial total count
    suggests there are more listings than pagination can reach (>120), quad-split
    the bbox and fetch each tile separately.
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

    market_name = search.get("name", search["id"])
    print(f"[Zillow] Fetching FSBO for {market_name}...")

    bbox_full = {
        "north": search["max_lat"],
        "south": search["min_lat"],
        "east": search["max_lng"],
        "west": search["min_lng"],
    }

    try:
        # First: probe page 1 of full bbox to see total count
        probe_payload = _build_search_payload(search, bbox=bbox_full, page=1)
        probe_results, probe_total, probe_status = _do_query(session, probe_payload)
        if probe_status != 200:
            print(f"[Zillow] {market_name}: probe failed ({probe_status})")
            return []

        raw_items_by_zpid = {}
        for item in probe_results:
            zpid = str(item.get("zpid", "")).strip()
            if zpid:
                raw_items_by_zpid[zpid] = item

        # If there are likely more than MAX_PAGES_PER_BBOX * 40 results, tile the bbox.
        # Otherwise just paginate the existing bbox.
        if probe_total > TILE_IF_TOTAL_OVER:
            print(f"[Zillow] {market_name}: total {probe_total} > {TILE_IF_TOTAL_OVER}, tiling into 4 quadrants")
            tiles = _quad_split(bbox_full)
            for i, tile in enumerate(tiles):
                tile_label = f"{market_name} T{i+1}/4"
                tile_items = _fetch_bbox(session, search, tile, tile_label)
                for item in tile_items:
                    zpid = str(item.get("zpid", "")).strip()
                    if zpid:
                        raw_items_by_zpid[zpid] = item
                time.sleep(1.5)
        else:
            # Just paginate the full bbox (pages 2-5; page 1 already in raw_items_by_zpid)
            for page in range(2, MAX_PAGES_PER_BBOX + 1):
                payload = _build_search_payload(search, bbox=bbox_full, page=page)
                results, _total, status = _do_query(session, payload)
                if status != 200 or not results:
                    break
                print(f"[Zillow] {market_name} p{page}: {len(results)} results")
                for item in results:
                    zpid = str(item.get("zpid", "")).strip()
                    if zpid:
                        raw_items_by_zpid[zpid] = item
                if len(results) < 40:
                    break
                time.sleep(1)

        # Parse all collected items
        listings = []
        for item in raw_items_by_zpid.values():
            try:
                parsed = _parse_one(item, search["id"])
                if parsed:
                    listings.append(parsed)
            except Exception as e:
                print(f"[Zillow] Parse error: {e}")
                continue

        print(f"[Zillow] {market_name}: {len(listings)} listings (deduped by zpid)")
        return listings

    except Exception as e:
        print(f"[Zillow] Fetch error: {e}")
        traceback.print_exc()
        return []
    finally:
        session.close()


def _build_search_payload(search: dict, bbox: Optional[dict] = None, page: int = 1) -> dict:
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

    map_bounds = bbox if bbox is not None else {
        "north": search["max_lat"],
        "south": search["min_lat"],
        "east": search["max_lng"],
        "west": search["min_lng"],
    }

    return {
        "searchQueryState": {
            "pagination": {"currentPage": page} if page > 1 else {},
            "isMapVisible": True,
            "mapBounds": map_bounds,
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
    last_sold_price = info.get("lastSoldPrice") or info.get("lastSalePrice")
    last_sold_date = info.get("lastSoldDate") or info.get("lastSaleDate")

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
        "redfin_estimate": None,
        "zestimate": _safe_int(zestimate),
        "photo_urls": photo_urls,
        "remarks": remarks,
        "_is_non_owner_occupied": is_non_owner_occupied,
        "rent_zestimate": _safe_int(rent_zestimate),
        "last_sold_price": _safe_int(last_sold_price),
        "last_sold_date": str(last_sold_date) if last_sold_date else None,
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
    """Get OxyLabs Web Unblocker proxy — delegates to shared module."""
    from .proxy import get_oxylabs_proxy
    return get_oxylabs_proxy()


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

    result = {"remarks": None, "photo_urls": None, "last_sold_price": None, "last_sold_date": None}

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

                        # Price history — find most recent "Sold" event
                        if not result["last_sold_price"]:
                            price_history = prop.get("priceHistory", [])
                            for evt in price_history:
                                if isinstance(evt, dict) and evt.get("event") == "Sold":
                                    sold_price = evt.get("price")
                                    sold_date = evt.get("date", "")
                                    if sold_price and sold_price > 0:
                                        result["last_sold_price"] = int(sold_price)
                                        result["last_sold_date"] = sold_date
                                    break  # Most recent sold event first

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

            if result["remarks"] or result["photo_urls"] or result["last_sold_price"]:
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

    # Extract contact info from remarks and page
    if result.get("remarks"):
        from .redfin_fetcher import _extract_contact_from_text
        contact = _extract_contact_from_text(result["remarks"])
        if contact.get("phone"):
            result["seller_phone"] = contact["phone"]
        if contact.get("email"):
            result["seller_email"] = contact["email"]
        if contact.get("name"):
            result["seller_name"] = contact["name"]

    # Try listing agent from Zillow page JSON
    agent_match = _re.search(r'"listingAgent"[^}]*"name"\s*:\s*"([^"]+)"', html)
    phone_match = _re.search(r'"listingAgent"[^}]*"phone"[^}]*"areacode"\s*:\s*"(\d{3})"[^}]*"number"\s*:\s*"(\d{7})"', html)
    if agent_match and not result.get("seller_name"):
        result["seller_name"] = agent_match.group(1).strip()
    if phone_match and not result.get("seller_phone"):
        result["seller_phone"] = f"({phone_match.group(1)}) {phone_match.group(2)[:3]}-{phone_match.group(2)[3:]}"

    return result if (result.get("remarks") or result.get("seller_phone")) else None


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
