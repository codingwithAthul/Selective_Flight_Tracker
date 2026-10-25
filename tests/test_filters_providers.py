from datetime import date, datetime, time

from flight_tracker.config import Filters
from flight_tracker.filters import apply_filters, dedupe, rejection_reason
from flight_tracker.models import FlightOffer, Leg, SearchQuery, Segment
from flight_tracker.providers.duffel import DuffelProvider, parse_iso_duration
from flight_tracker.providers.mock import MockProvider
from flight_tracker.providers.serpapi import SerpApiProvider, _stops_param

Q = SearchQuery("T", "YYZ", "LHR", date(2026, 12, 1), date(2026, 12, 10))


def seg(o, d, dep, arr, carrier="AC", name="Air Canada", fn="1"):
    return Segment(o, d, datetime.fromisoformat(dep), datetime.fromisoformat(arr), carrier, fn, name)


def offer(price=500.0, provider="mock", stops=0, dep="2026-12-01T09:00", carrier="AC", name="Air Canada", fn="1"):
    if stops == 0:
        out = Leg([seg("YYZ", "LHR", dep, "2026-12-01T17:00", carrier, name, fn)])
    else:
        out = Leg([seg("YYZ", "KEF", dep, "2026-12-01T13:00", carrier, name, fn),
                   seg("KEF", "LHR", "2026-12-01T20:00", "2026-12-01T23:00", carrier, name, fn + "1")])
    return FlightOffer(provider, Q, price, "USD", out)


def test_filters():
    assert rejection_reason(offer(), Filters()) is None
    assert rejection_reason(offer(price=900), Filters(max_price=800)) == "price"
    assert rejection_reason(offer(stops=1), Filters(max_stops=0)) == "stops"
    assert rejection_reason(offer(stops=1), Filters(max_layover_hours=6)) == "layover_long"  # 7h at KEF
    assert rejection_reason(offer(stops=1), Filters(min_layover_minutes=500)) == "layover_short"
    assert rejection_reason(offer(), Filters(max_duration_hours=7)) == "duration"
    assert rejection_reason(offer(), Filters(exclude_airlines=["ac"])) == "airline_excluded"
    assert rejection_reason(offer(), Filters(exclude_airlines=["Air Canada"])) == "airline_excluded"
    assert rejection_reason(offer(), Filters(include_airlines=["BA"])) == "airline_not_included"
    assert rejection_reason(offer(), Filters(include_airlines=["AC"])) is None
    assert rejection_reason(offer(dep="2026-12-01T05:00"), Filters(depart_after=time(6))) == "depart_time"
    kept, rejected = apply_filters([offer(), offer(price=2000)], Filters(max_price=1000))
    assert len(kept) == 1 and rejected == {"price": 1}


def test_dedupe_keeps_cheapest_and_records_providers():
    a, b, c = offer(500, "serpapi"), offer(480, "duffel"), offer(700, "mock", fn="9")
    out = dedupe([a, b, c])
    assert [o.price for o in out] == [480, 700]
    assert out[0].also_seen_on == ["serpapi"]


def test_mock_is_deterministic_and_respects_max_stops():
    p = MockProvider(today=date(2026, 10, 21))
    q = SearchQuery("T", "YYZ", "LHR", date(2026, 12, 1), None, max_stops=0)
    a, b = p.search(q), p.search(q)
    assert [o.price for o in a] == [o.price for o in b]
    assert all(o.max_stops == 0 and o.inbound is None for o in a)


def test_serpapi_params_and_parse():
    p = SerpApiProvider()
    p.api_key = "k"
    params = p.build_params(Q)
    assert params["type"] == 1 and params["return_date"] == "2026-12-10" and params["stops"] == 0
    assert _stops_param(0) == 1 and _stops_param(1) == 2 and _stops_param(5) == 0
    data = {
        "search_metadata": {"google_flights_url": "https://g/f"},
        "best_flights": [{
            "price": 612, "total_duration": 600,
            "flights": [
                {"departure_airport": {"id": "YYZ", "time": "2026-12-01 18:00"},
                 "arrival_airport": {"id": "KEF", "time": "2026-12-01 23:00"},
                 "airline": "Icelandair", "flight_number": "FI 602"},
                {"departure_airport": {"id": "KEF", "time": "2026-12-02 01:00"},
                 "arrival_airport": {"id": "LHR", "time": "2026-12-02 04:00"},
                 "airline": "Icelandair", "flight_number": "FI 450"},
            ],
        }],
        "other_flights": [{"price": None, "flights": []}],
    }
    offers = p.parse(Q, data)
    assert len(offers) == 1
    o = offers[0]
    assert o.price == 612 and o.max_stops == 1 and o.booking_url == "https://g/f"
    assert o.outbound.segments[0].carrier == "FI" and o.outbound.segments[1].flight_number == "450"
    assert o.outbound.layovers == [("KEF", 120)]


def test_duffel_body_and_parse():
    p = DuffelProvider()
    body = p.build_body(SearchQuery("T", "YYZ", "LHR", date(2026, 12, 1), date(2026, 12, 10), adults=2, max_stops=1))
    assert len(body["data"]["slices"]) == 2 and len(body["data"]["passengers"]) == 2
    assert body["data"]["max_connections"] == 1
    assert parse_iso_duration("PT7H5M") == 425 and parse_iso_duration("P1DT2H") == 1560
    payload = {"data": {"offers": [{
        "total_amount": "455.10", "total_currency": "USD",
        "slices": [
            {"duration": "PT7H", "segments": [{
                "origin": {"iata_code": "YYZ"}, "destination": {"iata_code": "LHR"},
                "departing_at": "2026-12-01T18:00:00", "arriving_at": "2026-12-02T06:00:00",
                "marketing_carrier": {"iata_code": "AC", "name": "Air Canada"},
                "marketing_carrier_flight_number": "856"}]},
            {"duration": "PT8H", "segments": [{
                "origin": {"iata_code": "LHR"}, "destination": {"iata_code": "YYZ"},
                "departing_at": "2026-12-10T10:00:00", "arriving_at": "2026-12-10T13:00:00",
                "marketing_carrier": {"iata_code": "AC", "name": "Air Canada"},
                "marketing_carrier_flight_number": "857"}]},
        ]}]}}
    o = p.parse(Q, payload)[0]
    assert o.price == 455.10 and o.inbound is not None and o.total_duration_minutes == 900


def test_duffel_test_token_is_tagged_as_sandbox(monkeypatch):
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_abc")
    p = DuffelProvider()
    assert p.sandbox and p.label == "duffel-test"
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_abc")
    assert not DuffelProvider().sandbox


def test_serpapi_keeps_departure_token_and_fetches_returns(monkeypatch):
    p = SerpApiProvider()
    p.api_key = "k"
    flight = lambda o, d, t1, t2: {"departure_airport": {"id": o, "time": t1},
                                   "arrival_airport": {"id": d, "time": t2},
                                   "airline": "Air India", "flight_number": "AI 186"}
    out = {"best_flights": [{"price": 3337, "total_duration": 1865, "departure_token": "TOK",
                             "flights": [flight("YVR", "DEL", "2026-11-25 19:45", "2026-11-27 01:00")]}]}
    o = p.parse(Q, out)[0]
    assert o.extra == {"departure_token": "TOK"}
    seen = {}

    def fake_get(params):
        seen.update(params)
        return {"other_flights": [
            {"price": 3400, "total_duration": 1500, "flights": [flight("COK", "YVR", "2026-12-10 03:00", "2026-12-10 20:00")]},
            {"price": 3337, "total_duration": 1700, "flights": [flight("COK", "YVR", "2026-12-10 05:00", "2026-12-10 23:00")]},
        ]}
    monkeypatch.setattr(p, "_get", fake_get)
    options = p.fetch_return(Q, "TOK")
    assert seen["departure_token"] == "TOK"
    assert [price for _, price in options] == [3337, 3400]
    assert options[0][0].segments[0].origin == "COK"
