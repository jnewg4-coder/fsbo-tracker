"""
FSBO Listing Tracker — Photo condition analyzer (Claude Haiku vision)
CONDITIONAL: only triggered when other signals justify it (keyword score, price ratio, DOM+cuts).
Results stored in DB — never re-run automatically; on-demand or first-trigger only.
"""

import base64
import json
import os
import time
import traceback
from io import BytesIO
from typing import Optional

import httpx


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
MAX_PHOTOS = 8  # Limit photos per analysis to control cost


SYSTEM_PROMPT = """You are a property condition inspector reviewing listing photos for a real estate investor.
Focus on visible damage, deferred maintenance, and renovation needs.
Be concise and factual — this feeds a scoring system."""

USER_PROMPT = """Review these property listing photos. Assess visible condition and damage.

Respond in JSON only:
{
  "damage_score": <0-10 integer, 0=pristine, 10=severe damage/gut job>,
  "damage_notes": "<1-2 sentence summary of visible condition>",
  "major_work_items": ["<list of big-ticket items visible: roof, HVAC, kitchen, bath, etc.>"],
  "estimated_work_level": "<minor|moderate|heavy|gut>",
  "red_flags": ["<structural cracks, water damage, mold, foundation issues, etc.>"],
  "opportunity_notes": "<brief note on investment opportunity if any>"
}"""


def analyze_photos(photo_urls: list, max_photos: int = MAX_PHOTOS) -> Optional[dict]:
    """
    Analyze listing photos using Claude Haiku vision.

    Args:
        photo_urls: List of photo URLs to analyze.
        max_photos: Max number of photos to include (cost control).

    Returns:
        Analysis dict with damage_score, damage_notes, etc.
        None on failure.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("[PhotoAI] ANTHROPIC_API_KEY not set — skipping analysis")
        return None

    if not photo_urls:
        print("[PhotoAI] No photo URLs provided")
        return None

    # Limit photos
    urls = photo_urls[:max_photos]

    # Download and encode photos
    images = _download_photos(urls)
    if not images:
        print("[PhotoAI] No photos downloaded successfully")
        return None

    print(f"[PhotoAI] Analyzing {len(images)} photos...")

    # Build message content with images
    content = []
    for img_data, media_type in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": img_data,
            },
        })
    content.append({"type": "text", "text": USER_PROMPT})

    payload = {
        "model": MODEL,
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        resp = httpx.post(
            ANTHROPIC_API_URL,
            json=payload,
            headers=headers,
            timeout=60,
        )

        if resp.status_code != 200:
            print(f"[PhotoAI] API error {resp.status_code}: {resp.text[:200]}")
            return None

        result = resp.json()
        text_block = result.get("content", [{}])[0].get("text", "")
        return _parse_response(text_block)

    except Exception as e:
        print(f"[PhotoAI] Analysis error: {e}")
        traceback.print_exc()
        return None


def _download_photos(urls: list) -> list:
    """
    Download photos and return as base64-encoded tuples.

    Returns:
        List of (base64_data, media_type) tuples.
    """
    images = []

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = client.get(url)
                if resp.status_code != 200:
                    continue

                content_type = resp.headers.get("content-type", "image/jpeg")
                if "jpeg" in content_type or "jpg" in content_type:
                    media_type = "image/jpeg"
                elif "png" in content_type:
                    media_type = "image/png"
                elif "webp" in content_type:
                    media_type = "image/webp"
                elif "gif" in content_type:
                    media_type = "image/gif"
                else:
                    media_type = "image/jpeg"  # default

                encoded = base64.b64encode(resp.content).decode("utf-8")

                # Skip if too large (>5MB encoded)
                if len(encoded) > 5 * 1024 * 1024:
                    print(f"[PhotoAI] Skipping oversized photo: {url[:80]}")
                    continue

                images.append((encoded, media_type))

            except Exception as e:
                print(f"[PhotoAI] Download failed: {url[:80]} — {e}")
                continue

            time.sleep(0.2)  # Polite delay between downloads

    return images


def _parse_response(text: str) -> Optional[dict]:
    """Parse Haiku's JSON response, handling markdown code fences."""
    if not text:
        return None

    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        print(f"[PhotoAI] Failed to parse JSON response: {text[:200]}")
        return {
            "damage_score": 5,
            "damage_notes": f"AI response parse error. Raw: {text[:300]}",
            "major_work_items": [],
            "estimated_work_level": "unknown",
            "red_flags": [],
            "opportunity_notes": "",
        }

    # Validate and clamp damage_score
    score = result.get("damage_score", 0)
    if not isinstance(score, (int, float)):
        score = 5
    result["damage_score"] = max(0, min(10, int(score)))

    return result


def estimate_cost(photo_count: int) -> float:
    """Estimate API cost for analyzing N photos (Haiku vision pricing)."""
    # ~$0.0003 per photo (input) + ~$0.001 for output ≈ $0.002 per listing
    return photo_count * 0.0003 + 0.001
