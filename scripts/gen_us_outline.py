#!/usr/bin/env python3
"""
Generate a simplified continental US outline SVG path using Albers projection.
Uses ~90 waypoints tracing the US border (approximate, for decorative use).
"""

import math


def albers_project(lat: float, lng: float) -> tuple[float, float]:
    to_rad = math.pi / 180
    lng0 = -96.0 * to_rad
    lat0 = 38.0 * to_rad
    phi1 = 29.5 * to_rad
    phi2 = 45.5 * to_rad

    n = 0.5 * (math.sin(phi1) + math.sin(phi2))
    C = math.cos(phi1) ** 2 + 2 * n * math.sin(phi1)
    rho0 = math.sqrt(C - 2 * n * math.sin(lat0)) / n

    phi = lat * to_rad
    lam = lng * to_rad
    theta = n * (lam - lng0)
    rho = math.sqrt(C - 2 * n * math.sin(phi)) / n

    x = rho * math.sin(theta)
    y = rho0 - rho * math.cos(theta)

    return round(480 + x * 1070, 1), round(260 - y * 1070, 1)


# Simplified continental US border waypoints (clockwise from Maine)
# These trace the approximate outline — not cartographically precise
US_BORDER = [
    # Northeast coast
    (47.3, -68.3),   # Northern Maine
    (45.0, -67.0),   # Eastport ME
    (43.7, -69.8),   # Portland ME
    (42.3, -71.0),   # Boston MA
    (41.3, -72.0),   # CT coast
    (40.7, -74.0),   # NYC
    (39.5, -74.2),   # NJ shore
    (38.9, -75.0),   # Delaware
    (37.8, -75.5),   # Chesapeake
    (37.0, -76.0),   # Norfolk VA
    (35.2, -75.5),   # Outer Banks NC
    (34.2, -77.8),   # Wilmington NC
    (33.0, -79.2),   # Charleston SC
    (32.0, -80.8),   # Savannah GA
    (31.0, -81.3),   # Brunswick GA
    (30.3, -81.4),   # Jacksonville FL
    (29.0, -80.9),   # Daytona FL
    (27.8, -80.3),   # Vero Beach FL
    (26.1, -80.1),   # Miami FL
    (25.1, -80.4),   # Florida Keys tip
    (25.8, -81.8),   # SW Florida
    (26.6, -82.2),   # Ft Myers
    (28.0, -82.7),   # Tampa FL
    (29.1, -83.0),   # Big Bend FL
    (29.7, -84.9),   # Apalachicola FL
    (30.3, -86.5),   # Pensacola FL
    (30.2, -88.0),   # Mobile AL
    (30.0, -89.5),   # Gulfport MS
    (29.3, -89.5),   # Mississippi Delta
    (29.0, -90.0),   # Louisiana coast
    (29.2, -91.0),   # Vermilion Bay
    (29.5, -93.5),   # TX border
    (29.0, -94.7),   # Galveston TX
    (28.5, -96.0),   # Corpus Christi area
    (27.5, -97.3),   # South TX
    (26.0, -97.2),   # Brownsville TX
    # Mexican border (west)
    (29.5, -100.5),  # Del Rio TX
    (31.0, -103.5),  # West TX
    (31.8, -106.5),  # El Paso TX
    (32.0, -108.0),  # NM border
    (31.3, -109.0),  # SE Arizona
    (31.5, -111.0),  # SW Arizona
    (32.5, -114.7),  # Yuma AZ
    # Pacific coast (north)
    (32.7, -117.2),  # San Diego CA
    (33.7, -118.3),  # LA CA
    (34.5, -120.5),  # Santa Barbara
    (36.0, -121.8),  # Monterey
    (37.8, -122.5),  # San Francisco
    (40.0, -124.1),  # Eureka CA
    (42.0, -124.3),  # OR border
    (43.5, -124.3),  # Coos Bay OR
    (46.2, -124.0),  # Columbia River
    (47.5, -124.6),  # Olympic Peninsula
    (48.4, -124.7),  # Cape Flattery WA
    # Canadian border (east)
    (48.8, -123.0),  # Strait of Juan de Fuca
    (49.0, -122.5),  # BC border
    (49.0, -117.0),  # Idaho
    (49.0, -112.0),  # Montana
    (49.0, -107.0),  # Montana/ND
    (49.0, -102.0),  # North Dakota
    (49.0, -97.0),   # Minnesota
    (48.0, -95.0),   # NW Minnesota
    (48.5, -93.0),   # Boundary Waters
    (48.0, -90.0),   # Lake Superior N
    (47.0, -88.5),   # Upper Peninsula MI
    (46.5, -87.5),   # Upper MI
    (45.5, -84.5),   # Mackinac MI
    (46.0, -83.5),   # Sault Ste Marie
    (44.0, -82.5),   # Lake Huron
    (43.0, -82.5),   # SE Michigan
    (42.0, -83.0),   # Detroit MI
    (41.7, -83.0),   # Toledo OH
    (42.5, -79.5),   # Lake Erie
    (42.9, -78.8),   # Buffalo NY
    (43.5, -76.0),   # Lake Ontario
    (44.5, -75.5),   # St Lawrence
    (45.0, -74.5),   # NY/Canada
    (45.0, -73.5),   # Vermont
    (45.0, -71.5),   # NH/Maine border
    (47.3, -68.3),   # Close at northern Maine
]


def main():
    points = [albers_project(lat, lng) for lat, lng in US_BORDER]

    # Build SVG path
    parts = [f"M{points[0][0]},{points[0][1]}"]
    for x, y in points[1:]:
        parts.append(f"L{x},{y}")
    parts.append("Z")

    path_d = "".join(parts)
    print(f'<path class="us-outline" d="{path_d}"/>')
    print(f"\n<!-- Path length: {len(path_d)} chars, {len(points)} points -->", file=__import__("sys").stderr)


if __name__ == "__main__":
    main()
