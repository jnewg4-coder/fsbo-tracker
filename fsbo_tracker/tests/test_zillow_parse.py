from fsbo_tracker import zillow_fetcher


def test_parse_one_maps_zestimate_fields():
    item = {
        "zpid": "123456",
        "address": "123 Main St, Nashville, TN 37208",
        "unformattedPrice": 350000,
        "beds": 3,
        "baths": 2,
        "area": 1600,
        "latLong": {"latitude": 36.17, "longitude": -86.80},
        "detailUrl": "/homedetails/123456_zpid/",
        "hdpData": {
            "homeInfo": {
                "yearBuilt": 2008,
                "homeType": "SINGLE_FAMILY",
                "daysOnZillow": 66,
                "taxAssessedValue": 210000,
                "zestimate": 382500,
                "rentZestimate": 2260,
                "lastSoldPrice": 275000,
                "lastSoldDate": "2023-08-15",
            }
        },
    }

    parsed = zillow_fetcher._parse_one(item, "nashville-tn")

    assert parsed is not None
    assert parsed["id"] == "zl-123456"
    assert parsed["redfin_estimate"] is None
    assert parsed["zestimate"] == 382500
    assert parsed["rent_zestimate"] == 2260
    assert parsed["last_sold_price"] == 275000
    assert parsed["last_sold_date"] == "2023-08-15"


def test_extract_photos_uses_carousel_without_duplicates():
    item = {
        "carouselPhotosComposable": {
            "baseUrl": "https://photos.zillowstatic.com/fp/{photoKey}-p_e.jpg",
            "photoData": [{"photoKey": "abc"}, {"photoKey": "def"}],
        }
    }

    urls = zillow_fetcher._extract_photos(item)
    assert urls == [
        "https://photos.zillowstatic.com/fp/abc-p_e.jpg",
        "https://photos.zillowstatic.com/fp/def-p_e.jpg",
    ]
