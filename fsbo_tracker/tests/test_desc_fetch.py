"""Test script for description fetching via DuckDuckGo + Redfin page scrape."""
import time
import re
from curl_cffi import requests as curl_requests


def search_ddg(query):
    """Search DuckDuckGo for a Redfin URL."""
    session = curl_requests.Session(impersonate="chrome131")
    try:
        url = f"https://html.duckduckgo.com/html/?q={query}"
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        urls = re.findall(r'https?://www\.redfin\.com/[^\s"<>]+/home/\d+', resp.text)
        return urls[0] if urls else None
    except Exception as e:
        print(f"  DDG error: {e}")
        return None
    finally:
        session.close()


def fetch_redfin_description(url):
    """Fetch description from a Redfin property page."""
    session = curl_requests.Session(impersonate="chrome131")
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        html = resp.text
        # Extract remarksCommon from JSON in page
        m = re.search(r'"remarksCommon"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        if m:
            desc = m.group(1).replace("\\n", " ").replace("\\r", "")
            return desc
        # Fallback: meta description
        meta = re.search(r'<meta name="description" content="([^"]+)"', html)
        if meta and len(meta.group(1)) > 50:
            return meta.group(1)
        return None
    except Exception as e:
        print(f"  Fetch error: {e}")
        return None
    finally:
        session.close()


# Test addresses from Charlotte FSBO listings
addresses = [
    "2321 Remount Rd, Charlotte, NC",
    "1037 Fern Forest Dr, Charlotte, NC",
    "1801 Wilmore Dr, Charlotte, NC",
    "5308 Windy Valley Dr, Charlotte, NC",
]

for addr in addresses:
    query = f"site:redfin.com {addr}"
    print(f"Searching: {addr}")
    url = search_ddg(query)
    if url:
        print(f"  Redfin URL: {url}")
        desc = fetch_redfin_description(url)
        if desc:
            print(f"  Description: {desc[:150]}...")
        else:
            print("  No description found in page")
    else:
        print("  Not found on Redfin via DDG")
    time.sleep(2)
    print()
