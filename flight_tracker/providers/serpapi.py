"""SerpApi Google Flights provider (https://serpapi.com/google-flights-api).

Credentials: set SERPAPI_API_KEY (or the env var named in `api_key_env`).

Each search() costs one SerpApi search credit. For round trips, Google
Flights returns the round-trip price but only outbound flight details.
Return-leg details need a second call per offer (`departure_token`), which
we skip to save budget. The report links to the Google Flights page for
the full itinerary.
"""

from __future__ import annotations

import os
from datetime import datetime

import requests

from ..models import FlightOffer, Leg, SearchQuery, Segment
from .base import Provider, ProviderError, TransientProviderError

ENDPOINT = "https://serpapi.com/search.json"
ACCOUNT_ENDPOINT = "https://serpapi.com/account.json"
TRAVEL_CLASS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
NO_RESULTS_HINTS = ("hasn't returned any results", "no results")


def _stops_param(max_stops):
    # SerpApi: 0 any, 1 nonstop only, 2 one stop or fewer, 3 two stops or fewer
    if max_stops is None or max_stops >= 3:
        return 0
    return max_stops + 1


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


class SerpApiProvider(Provider):
    name = "serpapi"
    max_concurrency = 4
    min_interval = 0.25

    def __init__(self, **options):
        super().__init__(**options)
        self.api_key = os.environ.get(options.get("api_key_env", "SERPAPI_API_KEY"), "")
        self.timeout = float(options.get("timeout", 60))
        self.gl = options.get("gl", "us")
        self.hl = options.get("hl", "en")
        self.session = requests.Session()

    def check_ready(self) -> None:
        if not self.api_key:
            raise ProviderError("serpapi: SERPAPI_API_KEY is not set")

    def account(self) -> dict:
        """Plan and remaining searches (SerpApi's account endpoint is free and doesn't use a search)."""
        try:
            resp = self.session.get(ACCOUNT_ENDPOINT, params={"api_key": self.api_key}, timeout=self.timeout)
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise TransientProviderError(f"serpapi: account check failed: {e}") from e
        if data.get("error"):
            raise ProviderError(f"serpapi: {data['error']}")
        return {k: data.get(k) for k in ("plan_name", "searches_per_month", "plan_searches_left", "this_month_usage")}

    def build_params(self, q: SearchQuery) -> dict:
        params = {
            "engine": "google_flights",
            "departure_id": q.origin,
            "arrival_id": q.destination,
            "outbound_date": q.depart_date.isoformat(),
            "type": 1 if q.is_round_trip else 2,
            "currency": q.currency,
            "adults": q.adults,
            "travel_class": TRAVEL_CLASS.get(q.cabin, 1),
            "stops": _stops_param(q.max_stops),
            "hl": self.hl,
            "gl": self.gl,
            "api_key": self.api_key,
        }
        if q.return_date:
            params["return_date"] = q.return_date.isoformat()
        return params

    def _get(self, params: dict) -> dict:
        try:
            resp = self.session.get(ENDPOINT, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise TransientProviderError(f"serpapi: network error: {e}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientProviderError(f"serpapi: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise TransientProviderError("serpapi: invalid JSON") from e
        if resp.status_code >= 400 or data.get("error"):
            msg = str(data.get("error") or f"HTTP {resp.status_code}")
            if any(h in msg.lower() for h in NO_RESULTS_HINTS):
                return {}
            raise ProviderError(f"serpapi: {msg}")
        return data

    def search(self, query: SearchQuery) -> list[FlightOffer]:
        return self.parse(query, self._get(self.build_params(query)))

    def fetch_return(self, query: SearchQuery, departure_token: str) -> list[tuple[Leg, float]]:
        """Return flights for a chosen outbound (one extra search credit).

        Prices here are the full round-trip price with that return flight, cheapest first.
        """
        params = self.build_params(query)
        params["departure_token"] = departure_token
        options = [(leg, float(item["price"])) for item, leg in self._items(self._get(params))
                   if item.get("price") is not None]
        return sorted(options, key=lambda o: o[1])

    @staticmethod
    def _items(data: dict):
        """(raw item, Leg) for every itinerary in a Google Flights response."""
        for group in ("best_flights", "other_flights"):
            for item in data.get(group) or []:
                flights = item.get("flights") or []
                if not flights:
                    continue
                segments = []
                for f in flights:
                    fn = str(f.get("flight_number", "")).strip()
                    code = fn.split(" ")[0] if " " in fn else ""
                    segments.append(Segment(
                        origin=f["departure_airport"]["id"],
                        destination=f["arrival_airport"]["id"],
                        depart_at=_parse_time(f["departure_airport"]["time"]),
                        arrive_at=_parse_time(f["arrival_airport"]["time"]),
                        carrier=code or f.get("airline", ""),
                        carrier_name=f.get("airline", ""),
                        flight_number=fn.split(" ")[-1] if fn else "",
                    ))
                yield item, Leg(segments=segments, duration_minutes=item.get("total_duration"))

    def parse(self, query: SearchQuery, data: dict) -> list[FlightOffer]:
        url = (data.get("search_metadata") or {}).get("google_flights_url", "")
        currency = (data.get("search_parameters") or {}).get("currency", query.currency)
        offers = []
        for item, leg in self._items(data):
            if item.get("price") is None:
                continue
            token = item.get("departure_token")
            offers.append(FlightOffer(
                provider=self.name,
                query=query,
                price=float(item["price"]),
                currency=currency,
                outbound=leg,
                inbound=None,
                booking_url=url,
                extra={"departure_token": token} if token and query.is_round_trip else {},
            ))
        return offers
