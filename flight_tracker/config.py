"""Load and validate config.yaml.

The config is deliberately small and readable; see docs/CONFIGURATION.md for
every key. Validation errors raise ConfigError with a message that names the
offending key, so a typo fails the run loudly instead of silently searching
the wrong thing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

IATA_RE = re.compile(r"^[A-Z]{3}$")
CODE_RE = re.compile(r"^[A-Za-z0-9]+$")
CABINS = {"economy", "premium_economy", "business", "first"}
TRIP_TYPES = {"one_way", "round_trip"}


class ConfigError(ValueError):
    pass


@dataclass
class DateWindow:
    start: date
    end: date

    def days(self) -> list[date]:
        n = (self.end - self.start).days
        return [self.start + timedelta(days=i) for i in range(n + 1)]


@dataclass
class Filters:
    max_stops: Optional[int] = None
    max_price: Optional[float] = None
    max_duration_hours: Optional[float] = None  # per direction
    max_layover_hours: Optional[float] = None
    min_layover_minutes: Optional[int] = None
    include_airlines: list[str] = field(default_factory=list)  # IATA codes or names; offer must use only these
    exclude_airlines: list[str] = field(default_factory=list)
    depart_after: Optional[time] = None  # outbound local departure time window
    depart_before: Optional[time] = None
    return_depart_after: Optional[time] = None
    return_depart_before: Optional[time] = None
    allowed_weekdays: list[int] = field(default_factory=list)  # 0=Mon..6=Sun for outbound departure


@dataclass
class Strategy:
    mode: str = "smart"  # "smart" (sample then zoom) or "exhaustive"
    budget: int = 60  # max provider searches for this trip per run (per provider)
    budget_auto: bool = False  # budget: auto -> spread the provider's remaining monthly quota over the days left
    auto_reserve: int = 10  # searches kept back each month for manual runs and key checks
    sample_every_days: int = 3
    nights_samples: int = 3  # how many stay lengths to sample in phase 1
    refine_top_k: int = 5
    refine_radius_days: int = 2
    keep_offers_per_search: int = 5  # how many cheapest offers to store per search
    rotate_daily: bool = False  # shift the sample grid each day (good for small budgets)
    return_details: int = 0  # fetch return-flight details for this many of the day's cheapest (1 extra search each)


@dataclass
class Trip:
    name: str
    origins: list[str]
    destinations: list[str]
    trip_type: str
    depart_window: DateWindow
    return_window: Optional[DateWindow] = None  # round trips: come back within these dates
    nights_min: int = 0
    nights_max: int = 0
    adults: int = 1
    cabin: str = "economy"
    filters: Filters = field(default_factory=Filters)
    strategy: Strategy = field(default_factory=Strategy)
    target_price: Optional[float] = None  # alert when any offer is at or below this
    enabled: bool = True
    code: str = ""  # e.g. "T1"; names the trip's report folder
    from_name: str = ""  # display names, e.g. "Vancouver"; default to the airport codes
    to_name: str = ""

    @property
    def is_round_trip(self) -> bool:
        return self.trip_type == "round_trip"

    @property
    def report_folder(self) -> str:
        """e.g. 'T1 Vancouver - Kochi (Round)'. No ':' or '/' so it is a valid folder name on every OS."""
        kind = "Round" if self.is_round_trip else "One-Way"
        return f"{self.code} {self.from_name} - {self.to_name} ({kind})"


@dataclass
class ProviderConfig:
    name: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotifierConfig:
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReportConfig:
    top_n: int = 15
    output_dir: str = "Report"  # one sub-folder per trip, named by Trip.report_folder
    value_of_hour: float = 0.0  # 0 disables the "best value" pick
    drop_alert_percent: float = 5.0


@dataclass
class VerifyConfig:
    """Final check: re-price the cheapest date combos found by the search providers on a second source."""

    provider: str
    top_k: int = 5  # distinct airport/date combos re-checked per trip per run
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    trips: list[Trip]
    providers: list[ProviderConfig]
    notifiers: list[NotifierConfig]
    report: ReportConfig
    verify: Optional[VerifyConfig] = None
    currency: str = "USD"
    workers: int = 8
    database: str = "data/tracker.db"


# ---------------------------------------------------------------- parsing


def _parse_date(value: Any, key: str, today: date) -> date:
    """Accept YYYY-MM-DD, a date object, or a relative '+30d' offset from today."""
    if isinstance(value, date):
        return value
    if isinstance(value, int):
        return today + timedelta(days=value)
    if isinstance(value, str):
        v = value.strip()
        m = re.fullmatch(r"\+?(\d+)d", v)
        if m:
            return today + timedelta(days=int(m.group(1)))
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    raise ConfigError(f"{key}: expected YYYY-MM-DD or '+Nd', got {value!r}")


def _parse_time(value: Any, key: str) -> Optional[time]:
    if value is None:
        return None
    if isinstance(value, int):  # YAML may parse 06:00 as sexagesimal int (360)
        return time(value // 60, value % 60)
    try:
        return time.fromisoformat(str(value))
    except ValueError as e:
        raise ConfigError(f"{key}: expected HH:MM, got {value!r}") from e


def _airports(value: Any, key: str) -> list[str]:
    items = value if isinstance(value, list) else [value]
    out = []
    for item in items:
        code = str(item).strip().upper()
        if not IATA_RE.match(code):
            raise ConfigError(f"{key}: {item!r} is not a 3-letter IATA airport code")
        out.append(code)
    if not out:
        raise ConfigError(f"{key}: at least one airport is required")
    return out


def _known(d: dict, allowed: set[str], where: str) -> None:
    unknown = set(d) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")


def _parse_filters(raw: dict, where: str) -> Filters:
    raw = raw or {}
    _known(raw, set(Filters.__dataclass_fields__), where)
    f = Filters(**{k: v for k, v in raw.items() if k not in {
        "depart_after", "depart_before", "return_depart_after", "return_depart_before",
        "include_airlines", "exclude_airlines"}})
    for k in ("depart_after", "depart_before", "return_depart_after", "return_depart_before"):
        setattr(f, k, _parse_time(raw.get(k), f"{where}.{k}"))
    f.include_airlines = [str(a).strip() for a in raw.get("include_airlines") or []]
    f.exclude_airlines = [str(a).strip() for a in raw.get("exclude_airlines") or []]
    if f.max_stops is not None and f.max_stops < 0:
        raise ConfigError(f"{where}.max_stops must be >= 0")
    for wd in f.allowed_weekdays:
        if wd not in range(7):
            raise ConfigError(f"{where}.allowed_weekdays: {wd} not in 0..6 (0=Monday)")
    return f


def _parse_strategy(raw: dict, where: str) -> Strategy:
    raw = raw or {}
    _known(raw, set(Strategy.__dataclass_fields__) - {"budget_auto"}, where)
    raw = dict(raw)
    auto = raw.get("budget") == "auto"
    if auto:
        raw["budget"] = 7  # fallback when the quota can't be read
    s = Strategy(**raw, budget_auto=auto)
    if not isinstance(s.budget, int):
        raise ConfigError(f"{where}.budget must be a whole number or 'auto'")
    if s.mode not in {"smart", "exhaustive"}:
        raise ConfigError(f"{where}.mode must be 'smart' or 'exhaustive'")
    if s.budget < 1 or s.sample_every_days < 1 or s.nights_samples < 1:
        raise ConfigError(f"{where}: budget, sample_every_days and nights_samples must be >= 1")
    return s


def _parse_trip(raw: dict, idx: int, today: date) -> Trip:
    where = f"trips[{idx}]"
    allowed = {"name", "origin", "destination", "trip_type", "depart_window", "return_window", "nights", "adults",
               "cabin", "filters", "strategy", "target_price", "enabled", "code", "from_name", "to_name"}
    _known(raw, allowed, where)
    for req in ("origin", "destination", "depart_window"):
        if req not in raw:
            raise ConfigError(f"{where}: missing required key '{req}'")
    origins = _airports(raw["origin"], f"{where}.origin")
    destinations = _airports(raw["destination"], f"{where}.destination")
    if set(origins) & set(destinations):
        raise ConfigError(f"{where}: origin and destination share an airport")

    trip_type = raw.get("trip_type", "round_trip")
    if trip_type not in TRIP_TYPES:
        raise ConfigError(f"{where}.trip_type must be one of {sorted(TRIP_TYPES)}")

    dw = raw["depart_window"] or {}
    start = _parse_date(dw.get("start"), f"{where}.depart_window.start", today)
    end = _parse_date(dw.get("end"), f"{where}.depart_window.end", today)
    if end < start:
        raise ConfigError(f"{where}.depart_window: end is before start")
    # Never search the past; airlines rarely sell same-day either.
    start = max(start, today + timedelta(days=1))
    if end < start:
        raise ConfigError(f"{where}.depart_window is entirely in the past")

    nights_min = nights_max = 0
    return_window = None
    if raw.get("return_window") is not None:
        if trip_type != "round_trip":
            raise ConfigError(f"{where}.return_window only applies to round_trip")
        rw = raw["return_window"] or {}
        return_window = DateWindow(_parse_date(rw.get("start"), f"{where}.return_window.start", today),
                                   _parse_date(rw.get("end"), f"{where}.return_window.end", today))
        if return_window.end < return_window.start:
            raise ConfigError(f"{where}.return_window: end is before start")
        if return_window.end <= start:
            raise ConfigError(f"{where}.return_window ends before the earliest departure")
    if trip_type == "round_trip":
        nights = raw.get("nights")
        if nights is None and return_window is not None:
            # Any stay that fits both windows.
            nights = {"min": max(1, (return_window.start - end).days), "max": (return_window.end - start).days}
        if nights is None:
            raise ConfigError(f"{where}: round_trip needs 'nights: {{min: N, max: M}}' or a return_window")
        if isinstance(nights, int):
            nights_min = nights_max = nights
        else:
            nights_min, nights_max = int(nights.get("min", 1)), int(nights.get("max", nights.get("min", 1)))
        if nights_min < 0 or nights_max < nights_min:
            raise ConfigError(f"{where}.nights: need 0 <= min <= max")

    cabin = raw.get("cabin", "economy")
    if cabin not in CABINS:
        raise ConfigError(f"{where}.cabin must be one of {sorted(CABINS)}")
    code = str(raw.get("code") or f"T{idx + 1}").strip()
    if not CODE_RE.match(code):
        raise ConfigError(f"{where}.code: {code!r} must be letters/digits only, e.g. T1")
    names = {}
    for key, default in (("from_name", "+".join(origins)), ("to_name", "+".join(destinations))):
        value = str(raw.get(key) or default).strip()
        if any(c in value for c in '/\\:*?"<>|'):
            raise ConfigError(f"{where}.{key}: {value!r} can't contain / \\ : * ? \" < > |")
        names[key] = value
    adults = int(raw.get("adults", 1))
    if not 1 <= adults <= 9:
        raise ConfigError(f"{where}.adults must be 1..9")

    return Trip(
        name=str(raw.get("name") or f"{'/'.join(origins)}-{'/'.join(destinations)}"),
        origins=origins,
        destinations=destinations,
        trip_type=trip_type,
        depart_window=DateWindow(start, end),
        return_window=return_window,
        nights_min=nights_min,
        nights_max=nights_max,
        adults=adults,
        cabin=cabin,
        filters=_parse_filters(raw.get("filters"), f"{where}.filters"),
        strategy=_parse_strategy(raw.get("strategy"), f"{where}.strategy"),
        target_price=raw.get("target_price"),
        enabled=bool(raw.get("enabled", True)),
        code=code,
        **names,
    )


def parse_config(raw: dict, today: Optional[date] = None) -> Config:
    today = today or date.today()
    if not isinstance(raw, dict):
        raise ConfigError("config must be a mapping")
    _known(raw, {"currency", "workers", "database", "trips", "providers", "notifiers", "report", "verify"}, "config")

    trips_raw = raw.get("trips") or []
    if not trips_raw:
        raise ConfigError("config: at least one trip is required under 'trips'")
    trips = [_parse_trip(t, i, today) for i, t in enumerate(trips_raw)]
    names = [t.name for t in trips]
    if len(names) != len(set(names)):
        raise ConfigError("config: trip names must be unique")
    codes = [t.code for t in trips]
    if len(codes) != len(set(codes)):
        raise ConfigError("config: trip codes must be unique")

    providers = []
    for i, p in enumerate(raw.get("providers") or [{"name": "mock"}]):
        if isinstance(p, str):
            p = {"name": p}
        if "name" not in p:
            raise ConfigError(f"providers[{i}]: missing 'name'")
        providers.append(ProviderConfig(name=p["name"], options={k: v for k, v in p.items() if k != "name"}))

    notifiers = []
    for i, n in enumerate(raw.get("notifiers") or [{"type": "console"}]):
        if isinstance(n, str):
            n = {"type": n}
        if "type" not in n:
            raise ConfigError(f"notifiers[{i}]: missing 'type'")
        notifiers.append(NotifierConfig(type=n["type"], options={k: v for k, v in n.items() if k != "type"}))

    rep_raw = raw.get("report") or {}
    _known(rep_raw, set(ReportConfig.__dataclass_fields__), "report")
    report = ReportConfig(**rep_raw)

    verify = None
    ver_raw = raw.get("verify")
    if ver_raw:
        _known(ver_raw, {"provider", "top_k", "enabled", "options"}, "verify")
        if "provider" not in ver_raw:
            raise ConfigError("verify: missing 'provider'")
        if ver_raw.get("enabled", True):
            verify = VerifyConfig(provider=ver_raw["provider"], top_k=int(ver_raw.get("top_k", 5)),
                                  options=dict(ver_raw.get("options") or {}))
            if verify.top_k < 1:
                raise ConfigError("verify.top_k must be >= 1")

    workers = int(raw.get("workers", 8))
    if workers < 1:
        raise ConfigError("workers must be >= 1")

    return Config(
        trips=trips,
        providers=providers,
        notifiers=notifiers,
        report=report,
        verify=verify,
        currency=str(raw.get("currency", "USD")).upper(),
        workers=workers,
        database=str(raw.get("database", "data/tracker.db")),
    )


def load_config(path: str | os.PathLike, today: Optional[date] = None) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p} (copy config.example.yaml to config.yaml)")
    with p.open() as fh:
        raw = yaml.safe_load(fh) or {}
    return parse_config(raw, today=today)
