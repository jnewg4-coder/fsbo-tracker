"""
FSBO Listing Tracker — NDVI Vegetation Enrichment (lightweight)

Queries USDA NAIP via USGS ArcGIS REST for NDVI stats.
No heavy dependencies — uses curl_cffi (already in requirements).
Ported from AVMLens naip_api.py (bands 0=Red, 3=NIR).
"""

import re
from datetime import datetime
from typing import Optional, Dict, Any

from curl_cffi import requests

NAIP_URL = "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def _build_bbox(lat: float, lng: float, buffer_deg: float = 0.0005) -> str:
    """Build bounding box string for NAIP query (~55m at mid-latitudes)."""
    return f"{lng - buffer_deg},{lat - buffer_deg},{lng + buffer_deg},{lat + buffer_deg}"


def _query_naip_stats(lat: float, lng: float, timeout: int = 20) -> Optional[Dict[str, Any]]:
    """Query NAIP computeStatisticsHistograms for raw band means."""
    bbox = _build_bbox(lat, lng)
    parts = bbox.split(",")

    params = {
        "geometry": f'{{"xmin":{parts[0]},"ymin":{parts[1]},"xmax":{parts[2]},"ymax":{parts[3]},"spatialReference":{{"wkid":4326}}}}',
        "geometryType": "esriGeometryEnvelope",
        "f": "json",
    }

    try:
        resp = requests.get(
            f"{NAIP_URL}/computeStatisticsHistograms",
            params=params,
            headers=_HEADERS,
            impersonate="chrome",
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if "statistics" in data and len(data["statistics"]) >= 4:
            red_mean = data["statistics"][0].get("mean", 0)
            nir_mean = data["statistics"][3].get("mean", 0)

            if (nir_mean + red_mean) > 0:
                ndvi = (nir_mean - red_mean) / (nir_mean + red_mean)
            else:
                ndvi = 0

            return {"ndvi_mean": round(ndvi, 3)}

        return None
    except Exception as e:
        print(f"[NDVI] Stats query failed: {e}")
        return None


def _get_capture_year(lat: float, lng: float, timeout: int = 15) -> Optional[int]:
    """Query NAIP identify endpoint for image capture year."""
    params = {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "sr": "4326",
        "returnGeometry": "false",
        "returnCatalogItems": "true",
        "f": "json",
    }

    try:
        resp = requests.get(
            f"{NAIP_URL}/identify",
            params=params,
            headers=_HEADERS,
            impersonate="chrome",
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        if "catalogItems" in data and data["catalogItems"].get("features"):
            for feature in data["catalogItems"]["features"]:
                attrs = feature.get("attributes", {})
                for field in ("SrcImgDate", "AcquisitionDate", "SRCIMAGEDA", "Year"):
                    if field in attrs and attrs[field]:
                        match = re.search(r"(20\d{2})", str(attrs[field]))
                        if match:
                            return int(match.group(1))

        if "properties" in data:
            for key, val in data["properties"].items():
                if "date" in key.lower() or "year" in key.lower():
                    match = re.search(r"(20\d{2})", str(val))
                    if match:
                        return int(match.group(1))

        return None
    except Exception as e:
        print(f"[NDVI] Capture year lookup failed: {e}")
        return None


def _estimate_overgrowth(ndvi_mean: float) -> tuple:
    """Convert NDVI to (overgrowth_pct, overgrowth_level). Ported from naip_api.py:242."""
    if ndvi_mean >= 0.7:
        pct = min(100, 50 + (ndvi_mean - 0.7) * 200)
        level = "HIGH"
    elif ndvi_mean >= 0.55:
        pct = 25 + (ndvi_mean - 0.55) * 166
        level = "MODERATE"
    elif ndvi_mean >= 0.35:
        pct = 10 + (ndvi_mean - 0.35) * 75
        level = "LOW"
    else:
        pct = max(0, ndvi_mean * 28)
        level = "MINIMAL"
    return round(pct, 1), level


def _assess_confidence(capture_year: Optional[int], current_year: int = 2026) -> str:
    """Rate data freshness: high (≤2yr), moderate (≤3yr), low (>3yr), none (missing)."""
    if capture_year is None:
        return "none"
    age = current_year - capture_year
    if age <= 2:
        return "high"
    if age <= 3:
        return "moderate"
    return "low"


def get_naip_ndvi(lat: float, lng: float) -> Optional[Dict[str, Any]]:
    """
    Full NDVI enrichment for a single coordinate.

    Returns dict with ndvi_mean, overgrowth_level, overgrowth_pct,
    capture_year, confidence — or None on failure.
    """
    stats = _query_naip_stats(lat, lng)
    if not stats:
        return None

    ndvi_mean = stats["ndvi_mean"]
    overgrowth_pct, overgrowth_level = _estimate_overgrowth(ndvi_mean)
    capture_year = _get_capture_year(lat, lng)
    confidence = _assess_confidence(capture_year)

    return {
        "ndvi_mean": ndvi_mean,
        "overgrowth_level": overgrowth_level,
        "overgrowth_pct": overgrowth_pct,
        "capture_year": capture_year,
        "confidence": confidence,
    }
