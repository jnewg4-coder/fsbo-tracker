"""
FSBO Geo Lite — Lightweight proximity check using public ArcGIS REST APIs.
Standalone replacement for AVMLens geo_service. Hits HIFLD + EPA + FEMA directly.
No database caching — results cached client-side in localStorage.
"""

import math
import logging
from typing import Optional
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
    "highway": {
        "url": "https://geo.dot.gov/server/rest/services/Hosted/National_Highway_System_LRS/FeatureServer/0/query",
        "radius_mi": 0.3,
        "out_fields": "ROUTE_ID,ROUTE_NAME",
        "detail_field": "ROUTE_NAME",
        "decay": {"max_pct": -12, "zero_mi": 0.5},
    },
    "railroad": {
        "url": "https://geo.dot.gov/server/rest/services/Hosted/North_American_Rail_Network_Lines/FeatureServer/0/query",
        "radius_mi": 0.4,
        "out_fields": "RROWNER1,STFIPS",
        "detail_field": "RROWNER1",
        "decay": {"max_pct": -8, "zero_mi": 0.5},
    },
    "superfund": {
        "url": "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/22/query",
        "radius_mi": 2.0,
        "out_fields": "SITE_NAME,CITY_NAME,STATE_CODE",
        "detail_field": "SITE_NAME",
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
    "airport": {
        "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Airports_2/FeatureServer/0/query",
        "radius_mi": 2.0,
        "out_fields": "FULLNAME,FAC_TYPE",
        "detail_field": "FULLNAME",
        "decay": {"max_pct": -10, "zero_mi": 3.0},
    },
    "cell_tower": {
        "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Cellular_Towers/FeatureServer/0/query",
        "radius_mi": 0.3,
        "out_fields": "LICENSEE,STRUCHEIGH",
        "detail_field": "LICENSEE",
        "decay": {"max_pct": -4, "zero_mi": 0.5},
    },
    "transmission": {
        "url": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0/query",
        "radius_mi": 0.3,
        "out_fields": "OWNER,VOLTAGE",
        "detail_field": "OWNER",
        "decay": {"max_pct": -6, "zero_mi": 0.5},
    },
}

FLOOD_URL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"


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
    """Check if point is in a FEMA flood zone."""
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = requests.get(FLOOD_URL, params=params, timeout=10)
        resp.raise_for_status()
        features = resp.json().get("features", [])
        if not features:
            return None

        attrs = features[0].get("attributes", {})
        zone = attrs.get("FLD_ZONE", "")
        sfha = attrs.get("SFHA_TF", "")

        # High-risk zones: A, AE, AH, AO, V, VE
        is_high_risk = zone.startswith(("A", "V")) and "X" not in zone
        if not is_high_risk and sfha != "T":
            return None

        return {
            "layer": "flood",
            "distance_mi": 0,
            "adjustment_pct": -12.0 if is_high_risk else -4.0,
            "details": f"FEMA Zone {zone}" + (f" ({attrs.get('ZONE_SUBTY', '')})" if attrs.get('ZONE_SUBTY') else ""),
            "lat": lat,
            "lon": lon,
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

        detail_val = attrs.get(config.get("detail_field", ""), "Unknown")
        if not detail_val or detail_val == "Null":
            detail_val = layer_name.replace("_", " ").title()

        factors.append({
            "layer": layer_name,
            "distance_mi": round(distance, 2),
            "adjustment_pct": adj,
            "details": str(detail_val)[:100],
            "lat": round(feat_lat, 6),
            "lon": round(feat_lon, 6),
        })

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

    # Run all layer queries in parallel
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}
        for name, config in LAYERS.items():
            futures[executor.submit(_query_layer, name, config, lat, lon)] = name
        futures[executor.submit(_check_flood, lat, lon)] = "flood"

        for future in as_completed(futures):
            layer_name = futures[future]
            try:
                result = future.result()
                layers_succeeded += 1
                if result:
                    if isinstance(result, list):
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
    }
