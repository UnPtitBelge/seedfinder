import pytest

from trainscanner import transitous


def _enc(v: int) -> str:
    v = ~(v << 1) if v < 0 else v << 1
    out = ""
    while v >= 0x20:
        out += chr((0x20 | (v & 0x1F)) + 63)
        v >>= 5
    return out + chr(v + 63)


def _polyline(pts: list[tuple[float, float]]) -> str:
    """Encode [(lat, lon)] with precision 7."""
    out, plat, plon = "", 0, 0
    for lat, lon in pts:
        la, lo = round(lat * 1e7), round(lon * 1e7)
        out += _enc(la - plat) + _enc(lo - plon)
        plat, plon = la, lo
    return out


def _itinerary(mode: str, hour: int, trip: str) -> dict:
    a = {"name": "Brussels-Midi", "lat": 50.8357, "lon": 4.3366, "tz": "Europe/Brussels"}
    b = {"name": "Paris Nord", "lat": 48.8809, "lon": 2.3553, "tz": "Europe/Paris"}
    return {
        "legs": [
            {
                "mode": mode,
                "agencyName": "SNCF",
                "displayName": "TGV 1",
                "from": {**a, "departure": f"2026-12-01T{hour:02d}:00:00Z"},
                "to": {**b, "arrival": f"2026-12-01T{hour + 2:02d}:00:00Z"},
                "startTime": f"2026-12-01T{hour:02d}:00:00Z",
                "duration": 7200,
                "tripId": trip,
                "legGeometry": {
                    "points": _polyline([(a["lat"], a["lon"]), (b["lat"], b["lon"])]),
                    "precision": 7,
                },
            }
        ]
    }


@pytest.fixture
def fake_plan(monkeypatch):
    its = [_itinerary("HIGHSPEED_RAIL", 8, "t1"), _itinerary("NIGHT_RAIL", 20, "t2")]
    monkeypatch.setattr(transitous, "plan", lambda *a, **k: its)


def _search(client):
    r = client.get("/api/journeys", params={"from": "A", "to": "B", "date": "2026-12-01"})
    assert r.status_code == 200
    return r.json()["journeys"]


def test_polyline_helper_roundtrips():
    assert transitous.decode_polyline(_polyline([(50.8357, 4.3366)]), 7) == [[4.3366, 50.8357]]


def test_journeys_priced_and_tagged(client, fake_plan):
    js = _search(client)
    assert len(js) == 2
    assert all(j["price_cents"] > 0 for j in js)
    assert any(j["tags"] for j in js)


def test_night_rail_mode_kept(client, fake_plan):
    assert any(lg["mode"] == "NIGHT_RAIL" for j in _search(client) for lg in j["legs"])


def test_basket_total_and_unknown(client, fake_plan):
    js = _search(client)
    ids = [j["id"] for j in js]
    r = client.post("/api/basket", json={"journey_ids": ids})
    assert r.status_code == 200
    assert r.json()["total_cents"] == sum(j["price_cents"] for j in js)
    assert client.post("/api/basket", json={"journey_ids": ["nope"]}).status_code == 404


def test_checkout_demo_lists_ids(client, fake_plan):
    ids = [j["id"] for j in _search(client)]
    r = client.post("/api/checkout", json={"journey_ids": ids})
    assert r.status_code == 200
    body = r.json()
    assert body["demo"] is True
    assert f"paid={ids[0]},{ids[1]}&order=" in body["url"]


def test_fare_scales_with_party_and_class(client, fake_plan):
    solo = _search(client)[0]
    r = client.get(
        "/api/journeys", params={"from": "A", "to": "B", "date": "2026-12-01", "adults": 2, "travel_class": 1}
    )
    duo_first = r.json()["journeys"][0]
    assert duo_first["id"] != solo["id"]  # another party is another priced offer
    assert duo_first["travellers"] == 2 and duo_first["travel_class"] == 1
    assert duo_first["price_cents"] > 2 * solo["price_cents"]


def test_near_and_transfers_reach_planner(client, monkeypatch):
    seen = {}

    def plan(from_place, to_place, when, max_transfers):
        seen.update(from_place=from_place, to_place=to_place, max_transfers=max_transfers)
        return [_itinerary("HIGHSPEED_RAIL", 8, "c1")]

    monkeypatch.setattr(transitous, "plan", plan)
    params = {"from": "A", "to": "B", "max_transfers": 0, "from_near": "48.8,2.3"}
    r = client.get("/api/journeys", params=params)
    assert r.status_code == 200
    assert r.json()["origin"]["id"] == "A"
    assert seen["from_place"] == "48.8,2.3" and seen["to_place"] == "B"
    assert seen["max_transfers"] == 0


def test_bad_options_rejected(client, fake_plan):
    base = {"from": "A", "to": "B"}
    assert client.get("/api/journeys", params={**base, "adults": 0}).status_code == 422
    assert client.get("/api/journeys", params={**base, "from_near": "nope"}).status_code == 422


def test_stays_in_basket_and_checkout(client, fake_plan):
    jid = _search(client)[0]["id"]
    r = client.get(
        "/api/stays",
        params={"city": "Paris", "lat": 48.8, "lon": 2.3, "checkin": "2026-12-01", "nights": 2, "guests": 3},
    )
    assert r.status_code == 200
    stays = r.json()
    assert [s["kind"] for s in stays] == ["hostel", "hotel", "boutique"]
    assert all(s["price_cents"] == 2 * s["per_night_cents"] for s in stays)
    basket = client.post("/api/basket", json={"journey_ids": [jid], "stay_ids": [stays[1]["id"]]}).json()
    assert basket["total_cents"] == basket["journeys"][0]["price_cents"] + stays[1]["price_cents"]
    assert basket["quote"]["subtotal_cents"] == basket["total_cents"]  # stays are charged with the tickets
    assert client.post("/api/basket", json={"journey_ids": [jid], "stay_ids": ["nope"]}).status_code == 404
    r = client.post("/api/checkout", json={"journey_ids": [jid], "stay_ids": [stays[1]["id"]]})
    assert r.status_code == 200 and r.json()["demo"] is True


def test_checkout_sends_tickets_and_stays_to_stripe(client, fake_plan, monkeypatch):
    import stripe

    from trainscanner.config import settings

    jid = _search(client)[0]["id"]
    params = {"city": "Paris", "lat": 48.8, "lon": 2.3, "checkin": "2026-12-01", "nights": 1, "guests": 1}
    stay = client.get("/api/stays", params=params).json()[0]
    sent = {}

    class Session:
        id = "cs_test_x"
        url = "https://checkout.stripe.test/s"
        client_secret = "cs_test_x_secret"

    def create(**kw):
        sent.update(kw)
        return Session()

    monkeypatch.setattr(settings, "stripe_secret_key", "server-only-value")
    monkeypatch.setattr(stripe.checkout.Session, "create", create)
    r = client.post("/api/checkout", json={"journey_ids": [jid], "stay_ids": [stay["id"]]})
    assert r.status_code == 200
    body = r.json()
    assert body["demo"] is False and body["client_secret"] == Session.client_secret
    amounts = [it["price_data"]["unit_amount"] for it in sent["line_items"]]
    assert amounts[-1] == stay["price_cents"]
    assert (
        sum(amounts)
        == client.post("/api/basket", json={"journey_ids": [jid], "stay_ids": [stay["id"]]}).json()[
            "total_cents"
        ]
    )
    assert sent["metadata"]["stay_ids"] == stay["id"]
