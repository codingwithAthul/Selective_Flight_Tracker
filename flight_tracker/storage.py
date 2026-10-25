"""SQLite persistence: runs, searches and offers, plus history queries.

Primary keys are UUID strings so that databases produced by parallel shard
jobs can be merged into the main database without collisions
(see `merge_from`). Schema changes go in MIGRATIONS (append only).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from .models import FlightOffer, Leg, SearchQuery

MIGRATIONS = [
    # v1
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        run_date TEXT NOT NULL,            -- YYYY-MM-DD (UTC) the run belongs to
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',  -- running | ok | partial | failed
        shard TEXT,                         -- e.g. '1/3' when run as a matrix shard
        searches_planned INTEGER DEFAULT 0,
        searches_ok INTEGER DEFAULT 0,
        searches_failed INTEGER DEFAULT 0,
        notes TEXT
    );
    CREATE TABLE searches (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        provider TEXT NOT NULL,
        trip_name TEXT NOT NULL,
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        return_date TEXT,
        phase TEXT,                         -- sample | refine | exhaustive
        searched_at TEXT NOT NULL,
        status TEXT NOT NULL,               -- ok | error
        error TEXT,
        offers_found INTEGER DEFAULT 0,
        offers_kept INTEGER DEFAULT 0,
        min_price REAL                      -- cheapest offer after filters
    );
    CREATE INDEX idx_searches_trip ON searches(trip_name, depart_date, return_date);
    CREATE TABLE offers (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id),
        search_id TEXT NOT NULL REFERENCES searches(id),
        run_date TEXT NOT NULL,
        trip_name TEXT NOT NULL,
        provider TEXT NOT NULL,
        origin TEXT NOT NULL,
        destination TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        return_date TEXT,
        price REAL NOT NULL,
        currency TEXT NOT NULL,
        stops INTEGER NOT NULL,
        duration_minutes INTEGER,
        airlines TEXT,                      -- comma separated display names
        outbound_json TEXT NOT NULL,
        inbound_json TEXT,
        booking_url TEXT,
        fingerprint TEXT NOT NULL,
        also_seen_on TEXT,
        found_at TEXT NOT NULL
    );
    CREATE INDEX idx_offers_trip_date ON offers(trip_name, run_date, price);
    CREATE INDEX idx_offers_combo ON offers(trip_name, origin, destination, depart_date, return_date, run_date);
    CREATE TABLE notifications (
        id TEXT PRIMARY KEY,
        run_date TEXT NOT NULL,
        channel TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        status TEXT NOT NULL,
        error TEXT
    );
    """,
    # v2: provider-specific data per offer (e.g. the token needed to fetch return flights)
    """
    ALTER TABLE offers ADD COLUMN extra_json TEXT;
    """,
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id() -> str:
    return uuid.uuid4().hex


SANDBOX_SUFFIX = "-test"  # providers tagged like "duffel-test" return synthetic, not real, fares


def _real(real_only: bool) -> str:
    return f"AND provider NOT LIKE '%{SANDBOX_SUFFIX}'" if real_only else ""


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ schema

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.executescript(sql)
            self.conn.execute(f"PRAGMA user_version = {i}")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------ writes

    def start_run(self, run_date: date, shard: Optional[str] = None) -> str:
        run_id = _id()
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs (id, run_date, started_at, shard) VALUES (?, ?, ?, ?)",
                (run_id, run_date.isoformat(), _now(), shard),
            )
        return run_id

    def finish_run(self, run_id: str, planned: int, ok: int, failed: int, notes: str = "") -> None:
        status = "ok" if failed == 0 else ("failed" if ok == 0 else "partial")
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, searches_planned=?, searches_ok=?, searches_failed=?, notes=? WHERE id=?",
                (_now(), status, planned, ok, failed, notes, run_id),
            )

    def record_search(
        self,
        run_id: str,
        run_date: date,
        provider: str,
        query: SearchQuery,
        phase: str,
        offers_found: int,
        kept: list[FlightOffer],
        error: Optional[str] = None,
        keep_offers: int = 5,
    ) -> str:
        search_id = _id()
        min_price = min((o.price for o in kept), default=None)
        with self.tx() as c:
            c.execute(
                """INSERT INTO searches (id, run_id, provider, trip_name, origin, destination, depart_date,
                   return_date, phase, searched_at, status, error, offers_found, offers_kept, min_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (search_id, run_id, provider, query.trip_name, query.origin, query.destination,
                 query.depart_date.isoformat(), query.return_date.isoformat() if query.return_date else None,
                 phase, _now(), "error" if error else "ok", error, offers_found, len(kept), min_price),
            )
            for o in sorted(kept, key=lambda x: x.price)[:keep_offers]:
                c.execute(
                    """INSERT INTO offers (id, run_id, search_id, run_date, trip_name, provider, origin, destination,
                       depart_date, return_date, price, currency, stops, duration_minutes, airlines, outbound_json,
                       inbound_json, booking_url, fingerprint, also_seen_on, found_at, extra_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_id(), run_id, search_id, run_date.isoformat(), query.trip_name, o.provider, query.origin,
                     query.destination, query.depart_date.isoformat(),
                     query.return_date.isoformat() if query.return_date else None,
                     o.price, o.currency, o.max_stops, o.total_duration_minutes, ", ".join(o.airlines),
                     json.dumps(o.outbound.to_dict()), json.dumps(o.inbound.to_dict()) if o.inbound else None,
                     o.booking_url, o.fingerprint(), ",".join(o.also_seen_on), o.found_at.isoformat(),
                     json.dumps(o.extra) if o.extra else None),
                )
        return search_id

    def offers_missing_return(self, trip_name: str, run_date: date, limit: int) -> list[sqlite3.Row]:
        """Today's cheapest round-trip offers (one per itinerary) that still lack return-flight details."""
        return self.conn.execute(
            """SELECT * FROM offers WHERE id IN (
                   SELECT id FROM (
                       SELECT id, fingerprint, price,
                              ROW_NUMBER() OVER (PARTITION BY fingerprint ORDER BY price) AS rn
                       FROM offers WHERE trip_name=? AND run_date=? AND return_date IS NOT NULL)
                   WHERE rn = 1 ORDER BY price LIMIT ?)
               AND inbound_json IS NULL AND extra_json IS NOT NULL
               ORDER BY price""",
            (trip_name, run_date.isoformat(), limit),
        ).fetchall()

    def set_return(self, offer_id: str, inbound: Leg, price: float) -> None:
        """Attach return-flight details; the price becomes the full round trip with that return."""
        with self.tx() as c:
            c.execute(
                """UPDATE offers SET inbound_json=?, price=?, duration_minutes=COALESCE(duration_minutes,0)+?,
                   stops=MAX(stops, ?) WHERE id=?""",
                (json.dumps(inbound.to_dict()), price, inbound.duration_minutes or 0, inbound.stops, offer_id),
            )

    def record_notification(self, run_date: date, channel: str, error: Optional[str]) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO notifications (id, run_date, channel, sent_at, status, error) VALUES (?,?,?,?,?,?)",
                (_id(), run_date.isoformat(), channel, _now(), "error" if error else "ok", error),
            )

    def merge_from(self, other_path: str | Path) -> int:
        """Copy runs/searches/offers from another tracker DB (e.g. a shard). Returns offers merged."""
        before = self.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        self.conn.execute("ATTACH DATABASE ? AS other", (str(other_path),))
        try:
            with self.tx() as c:
                for table in ("runs", "searches", "offers", "notifications"):
                    c.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM other.{table}")
        finally:
            self.conn.execute("DETACH DATABASE other")
        after = self.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
        return after - before

    def prune(self, keep_days: int, today: Optional[date] = None) -> int:
        """Delete offers older than keep_days, except each trip's daily cheapest (kept forever)."""
        cutoff = ((today or date.today()) - timedelta(days=keep_days)).isoformat()
        with self.tx() as c:
            cur = c.execute(
                """DELETE FROM offers WHERE run_date < ? AND id NOT IN (
                       SELECT id FROM (
                           SELECT id, ROW_NUMBER() OVER (PARTITION BY trip_name, run_date ORDER BY price) AS rn
                           FROM offers WHERE run_date < ?
                       ) WHERE rn = 1
                   )""",
                (cutoff, cutoff),
            )
            return cur.rowcount

    # ------------------------------------------------------------ reads

    def offers_for(self, trip_name: str, run_date: date, real_only: bool = False) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT * FROM offers WHERE trip_name=? AND run_date=? {_real(real_only)} ORDER BY price",
            (trip_name, run_date.isoformat()),
        ).fetchall()

    def daily_minimums(self, trip_name: str, limit_days: int = 60, real_only: bool = False) -> list[tuple[str, float]]:
        rows = self.conn.execute(
            f"""SELECT run_date, MIN(price) AS p FROM offers WHERE trip_name=? {_real(real_only)}
               GROUP BY run_date ORDER BY run_date DESC LIMIT ?""",
            (trip_name, limit_days),
        ).fetchall()
        return [(r["run_date"], r["p"]) for r in reversed(rows)]

    def all_time_low(self, trip_name: str, before: Optional[date] = None, real_only: bool = False) -> Optional[sqlite3.Row]:
        sql = f"SELECT * FROM offers WHERE trip_name=? {_real(real_only)}"
        args: list = [trip_name]
        if before:
            sql += " AND run_date < ?"
            args.append(before.isoformat())
        return self.conn.execute(sql + " ORDER BY price LIMIT 1", args).fetchone()

    def previous_run_date(self, trip_name: str, run_date: date, real_only: bool = False) -> Optional[str]:
        row = self.conn.execute(
            f"SELECT MAX(run_date) FROM offers WHERE trip_name=? AND run_date < ? {_real(real_only)}",
            (trip_name, run_date.isoformat()),
        ).fetchone()
        return row[0] if row else None

    def combo_price(self, trip_name: str, origin: str, dest: str, depart: str, ret: Optional[str], run_date: str,
                    real_only: bool = False) -> Optional[float]:
        row = self.conn.execute(
            f"""SELECT MIN(price) FROM offers WHERE trip_name=? AND origin=? AND destination=? AND depart_date=?
               AND COALESCE(return_date,'')=COALESCE(?, '') AND run_date=? {_real(real_only)}""",
            (trip_name, origin, dest, depart, ret, run_date),
        ).fetchone()
        return row[0] if row else None

    def recent_best(self, trip_name: str, run_date: date, days: int = 7, limit: int = 5,
                    real_only: bool = False) -> list[sqlite3.Row]:
        """Cheapest date combos seen in the last `days` runs, each at its most recently observed price.

        With a small daily budget each run only checks a few dates, so this keeps good fares found
        on earlier days visible in today's report.
        """
        since = (run_date - timedelta(days=days - 1)).isoformat()
        return self.conn.execute(
            f"""WITH latest AS (
                    SELECT origin, destination, depart_date, return_date, MAX(run_date) AS seen
                    FROM offers WHERE trip_name=? AND run_date BETWEEN ? AND ? {_real(real_only)}
                    GROUP BY origin, destination, depart_date, return_date)
                SELECT o.origin, o.destination, o.depart_date, o.return_date, l.seen AS seen_on,
                       MIN(o.price) AS price, o.currency
                FROM offers o JOIN latest l ON o.origin=l.origin AND o.destination=l.destination
                     AND o.depart_date=l.depart_date AND COALESCE(o.return_date,'')=COALESCE(l.return_date,'')
                     AND o.run_date=l.seen
                WHERE o.trip_name=? AND o.depart_date > ? {_real(real_only).replace("provider", "o.provider")}
                GROUP BY o.origin, o.destination, o.depart_date, o.return_date
                ORDER BY price LIMIT ?""",
            (trip_name, since, run_date.isoformat(), trip_name, run_date.isoformat(), limit),
        ).fetchall()

    def verify_results(self, trip_name: str, run_date: date) -> list[sqlite3.Row]:
        """Each combo re-checked by the verify provider today, with the search providers' best price for it."""
        return self.conn.execute(
            """SELECT v.origin, v.destination, v.depart_date, v.return_date, v.provider AS verify_provider,
                      MIN(v.min_price) AS verify_price, MAX(v.status) AS status,
                      (SELECT MIN(o.price) FROM offers o JOIN searches s2 ON s2.id = o.search_id
                        WHERE s2.phase != 'verify' AND o.trip_name = v.trip_name AND o.run_date = r.run_date
                          AND o.origin = v.origin AND o.destination = v.destination AND o.depart_date = v.depart_date
                          AND COALESCE(o.return_date,'') = COALESCE(v.return_date,'')) AS search_price,
                      (SELECT GROUP_CONCAT(DISTINCT o.provider) FROM offers o JOIN searches s2 ON s2.id = o.search_id
                        WHERE s2.phase != 'verify' AND o.trip_name = v.trip_name AND o.run_date = r.run_date
                          AND o.origin = v.origin AND o.destination = v.destination AND o.depart_date = v.depart_date
                          AND COALESCE(o.return_date,'') = COALESCE(v.return_date,'')) AS search_providers
               FROM searches v JOIN runs r ON r.id = v.run_id
               WHERE v.phase = 'verify' AND v.trip_name = ? AND r.run_date = ?
               GROUP BY v.origin, v.destination, v.depart_date, v.return_date, v.provider
               ORDER BY COALESCE(MIN(v.min_price), search_price)""",
            (trip_name, run_date.isoformat()),
        ).fetchall()

    def run_stats(self, run_date: date) -> dict:
        row = self.conn.execute(
            """SELECT COUNT(*) AS runs, COALESCE(SUM(searches_planned),0) AS planned,
                      COALESCE(SUM(searches_ok),0) AS ok, COALESCE(SUM(searches_failed),0) AS failed
               FROM runs WHERE run_date=?""",
            (run_date.isoformat(),),
        ).fetchone()
        return dict(row)

    def search_errors(self, run_date: date, limit: int = 5) -> list[str]:
        rows = self.conn.execute(
            """SELECT s.provider, s.error, COUNT(*) AS n FROM searches s JOIN runs r ON r.id = s.run_id
               WHERE r.run_date=? AND s.status='error' GROUP BY s.provider, s.error ORDER BY n DESC LIMIT ?""",
            (run_date.isoformat(), limit),
        ).fetchall()
        return [f"{r['provider']}: {r['error']} (x{r['n']})" for r in rows]

    def searched_keys_today(self, trip_name: str, provider: str, run_date: date) -> set[str]:
        """Combos already searched successfully today (lets a re-run skip paid calls)."""
        rows = self.conn.execute(
            """SELECT s.origin, s.destination, s.depart_date, s.return_date FROM searches s
               JOIN runs r ON r.id = s.run_id
               WHERE s.trip_name=? AND s.provider=? AND r.run_date=? AND s.status='ok'""",
            (trip_name, provider, run_date.isoformat()),
        ).fetchall()
        return {f"{r[0]}|{r[1]}|{r[2]}|{r[3] or '-'}" for r in rows}


def row_legs(row: sqlite3.Row) -> tuple[Leg, Optional[Leg]]:
    out = Leg.from_dict(json.loads(row["outbound_json"]))
    inb = Leg.from_dict(json.loads(row["inbound_json"])) if row["inbound_json"] else None
    return out, inb
