"""Core data models shared by every module.

Providers convert their API responses into these classes so that filtering,
storage and reporting never need to know which API a fare came from.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class SearchQuery:
    """One concrete search: a single airport pair on fixed dates."""

    trip_name: str
    origin: str
    destination: str
    depart_date: date
    return_date: Optional[date] = None  # None means one-way
    adults: int = 1
    cabin: str = "economy"
    currency: str = "USD"
    max_stops: Optional[int] = None  # passed to providers that can filter server-side

    @property
    def is_round_trip(self) -> bool:
        return self.return_date is not None

    @property
    def nights(self) -> Optional[int]:
        if self.return_date is None:
            return None
        return (self.return_date - self.depart_date).days

    def key(self) -> str:
        """Stable identifier used for de-duplicating searches within a run."""
        ret = self.return_date.isoformat() if self.return_date else "-"
        return f"{self.trip_name}|{self.origin}|{self.destination}|{self.depart_date.isoformat()}|{ret}|{self.adults}|{self.cabin}"


@dataclass
class Segment:
    """A single flown leg (one take-off, one landing)."""

    origin: str
    destination: str
    depart_at: datetime
    arrive_at: datetime
    carrier: str  # airline IATA code or name
    flight_number: str = ""
    carrier_name: str = ""

    @property
    def duration_minutes(self) -> int:
        return int((self.arrive_at - self.depart_at).total_seconds() // 60)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["depart_at"] = self.depart_at.isoformat()
        d["arrive_at"] = self.arrive_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(
            origin=d["origin"],
            destination=d["destination"],
            depart_at=datetime.fromisoformat(d["depart_at"]),
            arrive_at=datetime.fromisoformat(d["arrive_at"]),
            carrier=d.get("carrier", ""),
            flight_number=d.get("flight_number", ""),
            carrier_name=d.get("carrier_name", ""),
        )


@dataclass
class Leg:
    """One direction of travel (outbound or inbound), made of 1+ segments."""

    segments: list[Segment]
    duration_minutes: Optional[int] = None  # provider-reported, includes layovers

    def __post_init__(self) -> None:
        if self.duration_minutes is None and self.segments:
            # Naive clock difference; providers that know time zones should set it.
            delta = self.segments[-1].arrive_at - self.segments[0].depart_at
            self.duration_minutes = int(delta.total_seconds() // 60)

    @property
    def stops(self) -> int:
        return max(len(self.segments) - 1, 0)

    @property
    def depart_at(self) -> datetime:
        return self.segments[0].depart_at

    @property
    def arrive_at(self) -> datetime:
        return self.segments[-1].arrive_at

    @property
    def layovers(self) -> list[tuple[str, int]]:
        """(airport, minutes) for each connection."""
        out = []
        for a, b in zip(self.segments, self.segments[1:]):
            out.append((a.destination, int((b.depart_at - a.arrive_at).total_seconds() // 60)))
        return out

    @property
    def carriers(self) -> list[str]:
        seen: list[str] = []
        for s in self.segments:
            name = s.carrier_name or s.carrier
            if name not in seen:
                seen.append(name)
        return seen

    @property
    def carrier_codes(self) -> list[str]:
        return sorted({s.carrier for s in self.segments if s.carrier})

    def to_dict(self) -> dict:
        return {"duration_minutes": self.duration_minutes, "segments": [s.to_dict() for s in self.segments]}

    @classmethod
    def from_dict(cls, d: dict) -> "Leg":
        return cls(segments=[Segment.from_dict(s) for s in d["segments"]], duration_minutes=d.get("duration_minutes"))


@dataclass
class FlightOffer:
    """A priced itinerary returned by a provider, normalised."""

    provider: str
    query: SearchQuery
    price: float
    currency: str
    outbound: Leg
    inbound: Optional[Leg] = None
    booking_url: str = ""
    found_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    also_seen_on: list[str] = field(default_factory=list)  # other providers with the same itinerary
    extra: dict = field(default_factory=dict)  # provider-specific data, e.g. SerpApi's departure_token

    @property
    def total_duration_minutes(self) -> int:
        total = self.outbound.duration_minutes or 0
        if self.inbound:
            total += self.inbound.duration_minutes or 0
        return total

    @property
    def max_stops(self) -> int:
        stops = self.outbound.stops
        if self.inbound:
            stops = max(stops, self.inbound.stops)
        return stops

    @property
    def airlines(self) -> list[str]:
        names = list(self.outbound.carriers)
        if self.inbound:
            names += [c for c in self.inbound.carriers if c not in names]
        return names

    @property
    def airline_codes(self) -> list[str]:
        codes = set(self.outbound.carrier_codes)
        if self.inbound:
            codes |= set(self.inbound.carrier_codes)
        return sorted(codes)

    def fingerprint(self) -> str:
        """Identity of the itinerary regardless of provider (flight numbers and times)."""
        parts = []
        for leg in (self.outbound, self.inbound):
            if leg is None:
                continue
            for s in leg.segments:
                parts.append(f"{s.carrier}{s.flight_number}@{s.depart_at.isoformat()}")
        raw = "|".join(parts) or f"{self.provider}|{self.price}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def value_score(self, value_of_hour: float) -> float:
        """Price plus a cost for time in the air; lower is better."""
        return self.price + value_of_hour * (self.total_duration_minutes / 60.0)
