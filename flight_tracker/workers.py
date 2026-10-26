"""Parallel search engine.

Each (query, provider) pair is a job. Jobs run on a thread pool (searches
are I/O bound). Each provider also enforces its own concurrency cap and rate
limit (see Provider.throttled_search), so one slow API can't starve the
others or get us banned. Transient failures are retried with exponential
backoff and jitter.

Only the main thread writes to SQLite: results are recorded as futures
complete, which keeps the database single-writer and safe.
"""

from __future__ import annotations

import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from .config import Config, Trip
from .filters import apply_filters
from .models import FlightOffer, SearchQuery
from .planner import plan_exhaustive, plan_refine, plan_sample
from .providers import Provider, ProviderError, TransientProviderError
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    query: SearchQuery
    provider: str
    offers: list[FlightOffer] = field(default_factory=list)
    error: Optional[str] = None
    attempts: int = 0


@dataclass
class TripStats:
    planned: int = 0
    ok: int = 0
    failed: int = 0
    skipped_cached: int = 0
    offers_found: int = 0
    offers_kept: int = 0
    verified: int = 0
    returns_fetched: int = 0
    rejected: dict[str, int] = field(default_factory=dict)


def run_with_retries(provider: Provider, query: SearchQuery, retries: int = 3, base_delay: float = 1.0,
                     sleep: Callable[[float], None] = time.sleep) -> JobResult:
    res = JobResult(query=query, provider=provider.name)
    for attempt in range(1, retries + 2):
        res.attempts = attempt
        try:
            res.offers = provider.throttled_search(query)
            res.error = None
            return res
        except TransientProviderError as e:
            res.error = str(e)
            if attempt > retries:
                break
            delay = base_delay * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
            log.warning("%s %s: %s (retry %d in %.1fs)", provider.name, query.key(), e, attempt, delay)
            sleep(delay)
        except ProviderError as e:
            res.error = str(e)
            break
        except Exception as e:  # provider bug: never crash the whole run
            res.error = f"unexpected {type(e).__name__}: {e}"
            log.exception("provider %s crashed on %s", provider.name, query.key())
            break
    return res


def _cache_key(q: SearchQuery) -> str:
    return f"{q.origin}|{q.destination}|{q.depart_date.isoformat()}|{q.return_date.isoformat() if q.return_date else '-'}"


class SearchEngine:
    def __init__(self, config: Config, providers: list[Provider], storage: Storage, run_id: str, run_date: date,
                 retries: int = 3, base_delay: float = 1.0, verifier: Optional[Provider] = None,
                 verify_top_k: int = 0):
        self.config = config
        self.providers = providers
        self.verifier = verifier
        self.verify_top_k = verify_top_k
        self.storage = storage
        self.run_id = run_id
        self.run_date = run_date
        self.retries = retries
        self.base_delay = base_delay

    def _execute(self, trip: Trip, queries: list[SearchQuery], phase: str, stats: TripStats,
                 pool: ThreadPoolExecutor, providers: Optional[list[Provider]] = None,
                 ) -> dict[str, tuple[SearchQuery, float]]:
        """Run queries on all providers; return cheapest filtered price per query key."""
        cheapest: dict[str, tuple[SearchQuery, float]] = {}
        futures = {}
        for provider in providers or self.providers:
            done_today = self.storage.searched_keys_today(trip.name, provider.name, self.run_date)
            for q in queries:
                stats.planned += 1
                if _cache_key(q) in done_today:
                    stats.skipped_cached += 1
                    continue
                fut = pool.submit(run_with_retries, provider, q, self.retries, self.base_delay)
                futures[fut] = provider.name
        for fut in as_completed(futures):
            res: JobResult = fut.result()
            if res.error and not res.offers:
                stats.failed += 1
                self.storage.record_search(self.run_id, self.run_date, res.provider, res.query, phase, 0, [],
                                           error=res.error)
                continue
            stats.ok += 1
            kept, rejected = apply_filters(res.offers, trip.filters)
            stats.offers_found += len(res.offers)
            stats.offers_kept += len(kept)
            for k, v in rejected.items():
                stats.rejected[k] = stats.rejected.get(k, 0) + v
            self.storage.record_search(self.run_id, self.run_date, res.provider, res.query, phase,
                                       len(res.offers), kept, keep_offers=trip.strategy.keep_offers_per_search)
            if kept:
                p = min(o.price for o in kept)
                k = res.query.key()
                if k not in cheapest or p < cheapest[k][1]:
                    cheapest[k] = (res.query, p)
        # Cached combos still count as seeds for refinement.
        for q in queries:
            if q.key() in cheapest:
                continue
            price = self.storage.combo_price(trip.name, q.origin, q.destination, q.depart_date.isoformat(),
                                             q.return_date.isoformat() if q.return_date else None,
                                             self.run_date.isoformat())
            if price is not None:
                cheapest[q.key()] = (q, price)
        return cheapest

    def run_trip(self, trip: Trip, pool: ThreadPoolExecutor) -> TripStats:
        stats = TripStats()
        cur = self.config.currency
        if trip.strategy.mode == "exhaustive":
            cheapest = self._execute(trip, plan_exhaustive(trip, cur), "exhaustive", stats, pool)
        else:
            phase1 = plan_sample(trip, cur, self.run_date.toordinal() if trip.strategy.rotate_daily else 0)
            cheapest = self._execute(trip, phase1, "sample", stats, pool)
            remaining = trip.strategy.budget - len(phase1)
            phase2 = plan_refine(trip, cur, cheapest, [q.key() for q in phase1], remaining)
            if phase2:
                cheapest.update(self._execute(trip, phase2, "refine", stats, pool))
            log.info("trip %s: %d sample + %d refine searches", trip.name, len(phase1), len(phase2))
        self._verify(trip, cheapest, stats, pool)
        self._fetch_returns(trip, stats, pool)
        return stats

    def _fetch_returns(self, trip: Trip, stats: TripStats, pool: ThreadPoolExecutor) -> None:
        """Round trips: some providers (SerpApi) price the round trip but only describe the outbound.
        For the day's top `return_details` offers, make one extra call each to get the return flights."""
        n = trip.strategy.return_details
        if not n or not trip.is_round_trip:
            return
        by_name = {p.name: p for p in [*self.providers, *([self.verifier] if self.verifier else [])]
                   if hasattr(p, "fetch_return")}
        rows = [r for r in self.storage.offers_missing_return(trip.name, self.run_date, n) if r["provider"] in by_name]
        futures = {}
        for r in rows:
            token = json.loads(r["extra_json"]).get("departure_token")
            if not token:
                continue
            q = SearchQuery(trip_name=trip.name, origin=r["origin"], destination=r["destination"],
                            depart_date=date.fromisoformat(r["depart_date"]),
                            return_date=date.fromisoformat(r["return_date"]), adults=trip.adults,
                            cabin=trip.cabin, currency=r["currency"], max_stops=trip.filters.max_stops)
            futures[pool.submit(by_name[r["provider"]].fetch_return, q, token)] = r
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                options = fut.result()
            except ProviderError as e:
                log.warning("return details for %s %s failed: %s", r["depart_date"], r["return_date"], e)
                continue
            if not options:
                log.warning("return details for %s %s: Google listed no return flights", r["depart_date"], r["return_date"])
                continue
            leg, price = options[0]
            self.storage.set_return(r["id"], leg, price)
            stats.returns_fetched += 1

    def _verify(self, trip: Trip, cheapest: dict[str, tuple[SearchQuery, float]], stats: TripStats,
                pool: ThreadPoolExecutor) -> None:
        """Final check: re-price the top-K cheapest combos on the verify provider (e.g. Google Flights)."""
        if not self.verifier or not self.verify_top_k or not cheapest:
            return
        top = [q for q, _ in sorted(cheapest.values(), key=lambda qp: qp[1])[: self.verify_top_k]]
        verify_stats = TripStats()
        self._execute(trip, top, "verify", verify_stats, pool, providers=[self.verifier])
        stats.verified = verify_stats.ok + verify_stats.skipped_cached
        stats.failed += verify_stats.failed
        log.info("trip %s: verified %d/%d combos on %s", trip.name, stats.verified, len(top), self.verifier.name)

    def run(self, trips: list[Trip]) -> dict[str, TripStats]:
        out: dict[str, TripStats] = {}
        with ThreadPoolExecutor(max_workers=self.config.workers, thread_name_prefix="search") as pool:
            for trip in trips:
                out[trip.name] = self.run_trip(trip, pool)
        return out
