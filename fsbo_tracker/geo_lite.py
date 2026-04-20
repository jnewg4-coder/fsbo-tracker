"""
FSBO Geo Lite — Lightweight proximity check using public ArcGIS REST APIs.
Standalone replacement for AVMLens geo_service. Hits HIFLD + EPA + FEMA directly.
No database caching — results cached client-side in localStorage.
"""

import math
import logging
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger("fsbo.geo_lite")

# ---------------------------------------------------------------------------
# ArcGIS REST query helper
# ---------------------------------------------------------------------------
def _query_arcgis(url: str, lat: float, lon: float, radius_mi: float = 0.5,
                  out_fields: str = "*", max_features: int = 10) -> list:
    """
    Query an ArcGIS MapServer/FeatureServer layer for features near a point.
    Returns list of feature dicts.
    """
    # Convert miles to meters for buffer
    radius_m = radius_mi * 1609.34

    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_m,
        "units": "esriSRUnit_Meter",
        "outFields": out_fields,
        "returnGeometry": "true",
        "f": "json",
        "resultRecordCount": max_features,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("features", [])
    except Exception as e:
        logger.warning(f"ArcGIS query failed for {url}: {e}")
        return []


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Layer configs — public ArcGIS endpoints
# ---------------------------------------------------------------------------
LAYERS = {
    # BTS/NTAD — replaced geo.dot.gov (requires auth tokens since Aug 2025)
    "highway": {
        "url": "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_National_Highway_System/FeatureServer/0/query",
        "radius_mi": 0.3,
        "out_fields": "SIGN1,LNAME",
        "detail_field": "SIGN1",
        "detail_fallback": "LNAME",
        "decay": {"max_pct": -12, "zero_mi": 0.5},
    },
    "railroad": {
        "url": "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_North_American_Rail_Network_Lines/FeatureServer/0/query",
        "radius_mi": 0.4,
        "out_fields": "RROWNER1,NET",
        "detail_field": "RROWNER1",
        "detail_fallback": "NET",
        "decay": {"max_pct": -8, "zero_mi": 0.5},
    },
    # EPA — layer 22 field is PRIMARY_NAME (not SITE_NAME)
    "superfund": {
        "url": "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/22/query",
        "radius_mi": 2.0,
        "out_fields": "PRIMARY_NAME,CITY_NAME,STATE_CODE",
        "detail_field": "PRIMARY_NAME",
        "decay": {"max_pct": -15, "zero_mi": 3.0},
    },
    "brownfield": {
        "url": "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/0/query",
        "radius_mi": 0.5,
        "out_fields": "PRIMARY_NAME,CITY_NAME",
        "detail_field": "PRIMARY_NAME",
        "decay": {"max_pct": -8, "zero_mi": 1.0},
    },
    "tri": {
        "url": "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/23/query",
        "radius_mi": 1.0,
        "out_fields": "PRIMARY_NAME,CITY_NAME",
        "detail_field": "PRIMARY_NAME",
        "decay": {"max_pct": -6, "zero_mi": 2.0},
    },
    # FAA AIS — replaced HIFLD (shutdown Aug 26, 2025)
    "airport": {
        "url": "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/US_Airport/FeatureServer/0/query",
        "radius_mi": 2.0,
        "out_fields": "NAME,IDENT,TYPE_CODE",
        "detail_field": "NAME",
        "decay": {"max_pct": -10, "zero_mi": 3.0},
    },
    # FCC structural registrations — replaced HIFLD Cellular_Towers
    "cell_tower": {
        "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/ArcGIS/rest/services/Cellular_Towers_in_the_United_States/FeatureServer/0/query",
        "radius_mi": 0.3,
        "out_fields": "Licensee,LocCity",
        "detail_field": "Licensee",
        "decay": {"max_pct": -4, "zero_mi": 0.5},
    },
    # HIFLD transmission lines — still live (survived HIFLD shutdown)
    "transmission": {
        "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0/query",
        "radius_mi": 0.3,
        "out_fields": "OWNER,VOLTAGE",
        "detail_field": "OWNER",
        "decay": {"max_pct": -6, "zero_mi": 0.5},
    },
}

FLOOD_URL = "https://msc.fema.gov/arcgis/rest/services/NFHL_Print/NFHLQuery/MapServer/28/query"


# ---------------------------------------------------------------------------
# Geometry simplification (for map polylines)
# ---------------------------------------------------------------------------
MAX_POINTS_PER_LINE = 60
MAX_DISTANCE_MI = 0.5  # Only send geometry for features within 0.5 mi


def _simplify_path(coords: list, max_pts: int = MAX_POINTS_PER_LINE) -> List[list]:
    """Nth-point sampling — keep first + last, evenly sample middle."""
    if len(coords) <= max_pts:
        return coords
    step = max(1, len(coords) // max_pts)
    sampled = coords[::step]
    if sampled[-1] != coords[-1]:
        sampled.append(coords[-1])
    return sampled


def _extract_geometry(geom: dict, distance_mi: float) -> Optional[list]:
    """
    Extract polyline paths from ArcGIS geometry.
    Returns list of [lon, lat] pairs (frontend converts to Leaflet order).
    Returns None if no line geometry / too far.
    """
    if distance_mi > MAX_DISTANCE_MI:
        return None

    paths = []
    if "paths" in geom:
        # Line geometry (highways, railroads, transmission)
        for path in geom["paths"]:
            simplified = _simplify_path(path)
            # ArcGIS already returns [lon, lat]
            paths.extend([[pt[0], pt[1]] for pt in simplified])
    elif "rings" in geom:
        # Polygon geometry (brownfield, superfund boundaries)
        for ring in geom["rings"]:
            simplified = _simplify_path(ring)
            paths.extend([[pt[0], pt[1]] for pt in simplified])

    return paths if paths else None


# ---------------------------------------------------------------------------
# Decay calculation
# ---------------------------------------------------------------------------
def _calc_adjustment(distance_mi: float, decay: dict) -> float:
    """Exponential decay: max_pct at distance=0, approaches 0 at zero_mi."""
    max_pct = decay["max_pct"]
    zero_mi = decay["zero_mi"]
    if distance_mi >= zero_mi:
        return 0.0
    ratio = 1.0 - (distance_mi / zero_mi)
    return round(max_pct * (ratio ** 2), 2)


# ---------------------------------------------------------------------------
# Flood zone check (point-in-polygon)
# ---------------------------------------------------------------------------
def _check_flood(lat: float, lon: float) -> Optional[dict]:
    """Return flood risk factor only when zone is moderate/high risk."""
    summary = lookup_flood_zone(lat, lon)
    if not summary:
        return None
    if (summary.get("adjustment_pct") or 0) == 0:
        return None
    return {
        "layer": "flood",
        "distance_mi": 0,
        "adjustment_pct": summary["adjustment_pct"],
        "details": summary["details"],
        "lat": lat,
        "lon": lon,
        "zone": summary.get("zone"),
        "risk_level": summary.get("risk_level"),
    }


def _flood_enrich(lat: float, lon: float) -> dict:
    """Return FEMA flood summary plus optional risk factor for map overlays."""
    summary = lookup_flood_zone(lat, lon)
    if not summary:
        return {"summary": None, "factor": None}
    factor = None
    if (summary.get("adjustment_pct") or 0) != 0:
        factor = {
            "layer": "flood",
            "distance_mi": 0,
            "adjustment_pct": summary["adjustment_pct"],
            "details": summary["details"],
            "lat": lat,
            "lon": lon,
            "zone": summary.get("zone"),
            "risk_level": summary.get("risk_level"),
        }
    return {"summary": summary, "factor": factor}


def lookup_flood_zone(lat: float, lon: float) -> Optional[dict]:
    """Look up FEMA flood zone summary for any property (including Zone X)."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = None
        for attempt in range(2):
            try:
                resp = requests.get(FLOOD_URL, params=params, timeout=25)
                resp.raise_for_status()
                break
            except Exception:
                if attempt == 0:
                    import time as _t; _t.sleep(2)
                    continue
                raise
        features = resp.json().get("features", [])
        if not features:
            return {
                "zone": "UNMAPPED",
                "risk_level": "undetermined",
                "adjustment_pct": 0.0,
                "details": "FEMA Zone UNMAPPED",
            }

        attrs = features[0].get("attributes", {})
        zone = str(attrs.get("FLD_ZONE", "") or "").upper().strip()
        subtype = str(attrs.get("ZONE_SUBTY", "") or "").upper()
        sfha = attrs.get("SFHA_TF", "")

        is_high = zone.startswith(("A", "V")) or sfha == "T"
        is_moderate = zone == "X" and "0.2 PCT" in subtype

        if is_high:
            risk_level = "high"
            adjustment = -12.0
        elif is_moderate:
            risk_level = "moderate"
            adjustment = -4.0
        elif zone in ("X", "C"):
            risk_level = "minimal"
            adjustment = 0.0
        elif zone in ("B",):
            risk_level = "moderate"
            adjustment = -2.0
        elif zone in ("D",):
            risk_level = "undetermined"
            adjustment = 0.0
        else:
            risk_level = "undetermined"
            adjustment = 0.0

        return {
            "zone": zone or "UNMAPPED",
            "risk_level": risk_level,
            "adjustment_pct": adjustment,
            "details": f"FEMA Zone {zone}" + (f" ({attrs.get('ZONE_SUBTY', '')})" if attrs.get('ZONE_SUBTY') else ""),
        }
    except Exception as e:
        logger.warning(f"Flood check failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Query a single layer
# ---------------------------------------------------------------------------
def _query_layer(layer_name: str, config: dict, lat: float, lon: float) -> list:
    """Query one layer, return list of factor dicts."""
    features = _query_arcgis(
        config["url"], lat, lon,
        radius_mi=config["radius_mi"],
        out_fields=config.get("out_fields", "*"),
        max_features=5,
    )

    factors = []
    for feat in features:
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry", {})

        # Get feature location
        if "x" in geom and "y" in geom:
            feat_lat, feat_lon = geom["y"], geom["x"]
        elif "paths" in geom and geom["paths"]:
            # Line geometry — use midpoint of first path
            path = geom["paths"][0]
            mid = path[len(path)//2] if path else [lon, lat]
            feat_lon, feat_lat = mid[0], mid[1]
        elif "rings" in geom and geom["rings"]:
            ring = geom["rings"][0]
            mid = ring[len(ring)//2] if ring else [lon, lat]
            feat_lon, feat_lat = mid[0], mid[1]
        else:
            feat_lat, feat_lon = lat, lon

        distance = _haversine(lat, lon, feat_lat, feat_lon)
        adj = _calc_adjustment(distance, config["decay"])

        if adj == 0:
            continue

        detail_val = (attrs.get(config.get("detail_field", ""), "") or "").strip()
        if not detail_val or detail_val in ("Null", "XXXX", "X"):
            fallback = config.get("detail_fallback", "")
            detail_val = (attrs.get(fallback, "") or "").strip() if fallback else ""
        if not detail_val or detail_val in ("Null", "XXXX", "X"):
            detail_val = layer_name.replace("_", " ").title()

        factor = {
            "layer": layer_name,
            "distance_mi": round(distance, 2),
            "adjustment_pct": adj,
            "details": str(detail_val)[:100],
            "lat": round(feat_lat, 6),
            "lon": round(feat_lon, 6),
        }

        # Attach simplified polyline geometry for map rendering
        line_geom = _extract_geometry(geom, distance)
        if line_geom:
            factor["geometry"] = line_geom

        factors.append(factor)

    return factors


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def enrich(lat: float, lon: float) -> dict:
    """
    Run all geo layers in parallel. Returns dict with factors + totals.
    Reports success only if at least one layer returned data or completed without error.
    """
    all_factors = []
    layers_succeeded = 0
    total_layers = len(LAYERS) + 1  # +1 for flood
    flood_summary = None

    # Run all layer queries in parallel
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for name, config in LAYERS.items():
            futures[executor.submit(_query_layer, name, config, lat, lon)] = name
        futures[executor.submit(_flood_enrich, lat, lon)] = "flood"

        for future in as_completed(futures):
            layer_name = futures[future]
            try:
                result = future.result()
                layers_succeeded += 1
                if not result:
                    continue
                if layer_name == "flood":
                    flood_summary = result.get("summary")
                    flood_factor = result.get("factor")
                    if flood_factor:
                        all_factors.append(flood_factor)
                elif isinstance(result, list):
                    all_factors.extend(result)
                elif isinstance(result, dict):
                    all_factors.append(result)
            except Exception as e:
                logger.warning(f"Layer {layer_name} error: {e}")

    # Sort by adjustment magnitude (worst first)
    all_factors.sort(key=lambda f: f["adjustment_pct"])

    total_adj = round(sum(f["adjustment_pct"] for f in all_factors), 2)

    # Risk level
    if total_adj <= -20:
        risk_level = "HIGH"
    elif total_adj <= -10:
        risk_level = "MODERATE"
    elif total_adj < 0:
        risk_level = "LOW"
    else:
        risk_level = "MINIMAL"

    risk_flags = []
    for f in all_factors:
        if f["adjustment_pct"] <= -8:
            risk_flags.append(f"{f['layer']}: {f['details']} ({f['adjustment_pct']}%)")

    return {
        "success": layers_succeeded > 0,
        "layers_queried": total_layers,
        "layers_succeeded": layers_succeeded,
        "factors": all_factors,
        "total_adjustment_pct": total_adj,
        "risk_level": risk_level,
        "risk_flags": risk_flags,
        "flood_summary": flood_summary,
    }
