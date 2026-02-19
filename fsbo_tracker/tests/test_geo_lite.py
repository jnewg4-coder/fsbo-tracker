from fsbo_tracker import geo_lite


def test_extract_geometry_returns_lon_lat_pairs():
    path_geom = {"paths": [[[-86.81, 36.16], [-86.80, 36.17], [-86.79, 36.18]]]}
    ring_geom = {"rings": [[[-86.81, 36.16], [-86.80, 36.17], [-86.79, 36.18], [-86.81, 36.16]]]}

    path_result = geo_lite._extract_geometry(path_geom, distance_mi=0.1)
    assert path_result is not None
    assert path_result[0] == [-86.81, 36.16]

    ring_result = geo_lite._extract_geometry(ring_geom, distance_mi=0.1)
    assert ring_result is not None
    assert ring_result[-1] == [-86.81, 36.16]


def test_extract_geometry_none_when_too_far():
    geom = {"paths": [[[-86.81, 36.16], [-86.80, 36.17]]]}
    assert geo_lite._extract_geometry(geom, distance_mi=1.0) is None


def test_query_layer_includes_geometry(monkeypatch):
    def fake_query(*args, **kwargs):
        return [
            {
                "attributes": {"SIGN1": "I-40"},
                "geometry": {"paths": [[[-86.81, 36.16], [-86.80, 36.17], [-86.79, 36.18]]]},
            }
        ]

    monkeypatch.setattr(geo_lite, "_query_arcgis", fake_query)
    config = {
        "url": "unused",
        "radius_mi": 1.0,
        "out_fields": "SIGN1,LNAME",
        "detail_field": "SIGN1",
        "detail_fallback": "LNAME",
        "decay": {"max_pct": -12, "zero_mi": 10.0},
    }
    factors = geo_lite._query_layer("highway", config, 36.17, -86.80)
    assert len(factors) == 1
    assert factors[0]["geometry"][0] == [-86.81, 36.16]
    assert factors[0]["layer"] == "highway"


def test_lookup_flood_zone_high_risk(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "features": [
                    {"attributes": {"FLD_ZONE": "AE", "ZONE_SUBTY": "", "SFHA_TF": "T"}}
                ]
            }

    def fake_get(url, params=None, timeout=0):
        assert "msc.fema.gov" in url
        assert params["geometryType"] == "esriGeometryPoint"
        return FakeResp()

    monkeypatch.setattr(geo_lite.requests, "get", fake_get)
    result = geo_lite.lookup_flood_zone(36.17276, -86.807464)

    assert result["zone"] == "AE"
    assert result["risk_level"] == "high"
    assert result["adjustment_pct"] == -12.0


def test_check_flood_returns_none_for_zone_x(monkeypatch):
    monkeypatch.setattr(
        geo_lite,
        "lookup_flood_zone",
        lambda *_args, **_kwargs: {
            "zone": "X",
            "risk_level": "minimal",
            "adjustment_pct": 0.0,
            "details": "FEMA Zone X",
        },
    )
    assert geo_lite._check_flood(36.17276, -86.807464) is None


def test_lookup_flood_zone_unmapped(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"features": []}

    monkeypatch.setattr(geo_lite.requests, "get", lambda *args, **kwargs: FakeResp())
    result = geo_lite.lookup_flood_zone(36.17276, -86.807464)
    assert result["zone"] == "UNMAPPED"
    assert result["adjustment_pct"] == 0.0
