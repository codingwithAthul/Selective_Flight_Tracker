"""Command line interface: `python -m flight_tracker <command>`.

Commands
  plan      Show what would be searched and how many API calls it costs.
  search    Run searches and store results (use --shard i/n for parallel jobs).
  merge     Merge shard databases into the main database.
  report    Build today's report from the database and deliver it.
  run       search + report in one go (the usual local / single-job command).
  probe     One live search per provider, to check that the API keys work.
  history   Print a trip's daily cheapest-fare history.
  prune     Delete old offers (keeps each day's cheapest forever).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Config, ConfigError, ProviderConfig, Trip, load_config
from .insights import build_all
from .notify import NotifyError, RenderedReport, create_notifier
from .planner import all_combinations, plan_exhaustive, plan_sample
from .providers import Provider, ProviderError, create_provider
from .report import render_html, render_markdown, render_telegram, write_report_files
from .storage import Storage
from .workers import SearchEngine

log = logging.getLogger("flight_tracker")


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def parse_shard(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    try:
        i, n = (int(x) for x in value.split("/"))
    except ValueError:
        raise SystemExit(f"--shard must look like 1/3, got {value!r}")
    if not (1 <= i <= n):
        raise SystemExit(f"--shard {value}: need 1 <= i <= n")
    return i, n


def select_trips(cfg: Config, shard: Optional[tuple[int, int]], only: Optional[str] = None) -> list[Trip]:
    trips = [t for t in cfg.trips if t.enabled and (only is None or t.name == only)]
    if shard:
        i, n = shard
        trips = [t for idx, t in enumerate(trips) if idx % n == i - 1]
    return trips


def build_providers(cfg: Config, override: Optional[str], run_date: date) -> list[Provider]:
    specs = [ProviderConfig(name=n.strip()) for n in override.split(",")] if override else cfg.providers
    ready = []
    for spec in specs:
        opts = dict(spec.options)
        if spec.name == "mock":
            opts.setdefault("today", run_date)
        p = create_provider(spec.name, **opts)
        try:
            p.check_ready()
        except ProviderError as e:
            log.error("skipping provider %s: %s", spec.name, e)
            continue
        ready.append(p)
    if not ready:
        raise SystemExit("no provider is ready (missing API keys?) — see docs/PROVIDERS.md")
    return ready


def days_left_in_month(d: date) -> int:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return (nxt - d).days  # including today


def apply_auto_budgets(cfg: Config, providers: list[Provider], run_date: date) -> None:
    """For trips with `budget: auto`, share the remaining monthly quota evenly over the days left.

    Uses the first provider that reports a quota (SerpApi does, via its free account endpoint).
    The quota is shared across all enabled auto trips, so parallel shards compute the same split.
    """
    auto = [t for t in cfg.trips if t.enabled and t.strategy.budget_auto]
    if not auto:
        return
    left = None
    for p in providers:
        if hasattr(p, "account"):
            try:
                left = int(p.account()["plan_searches_left"])
                break
            except (ProviderError, TypeError, KeyError, ValueError) as e:
                log.warning("auto budget: couldn't read %s quota (%s)", p.name, e)
    if left is None:
        log.warning("auto budget: no quota available, using each trip's fallback budget")
        return
    reserve = max(t.strategy.auto_reserve for t in auto)
    per_day = max(0, left - reserve) // days_left_in_month(run_date)
    share = per_day // len(auto)
    for t in auto:
        # Return-flight lookups come out of the same quota.
        extra = t.strategy.return_details if t.is_round_trip else 0
        t.strategy.budget = max(1, share - extra) if left > reserve else 0
    log.info("auto budget: %d searches left, %d days left in month -> %d per trip today",
             left, days_left_in_month(run_date), share)


def build_verifier(cfg: Config, args, run_date: date) -> Optional[Provider]:
    """The final-check provider, or None when disabled, overridden or missing its key."""
    if not cfg.verify or args.no_verify or args.provider:
        return None
    opts = dict(cfg.verify.options)
    if cfg.verify.provider == "mock":
        opts.setdefault("today", run_date)
    p = create_provider(cfg.verify.provider, **opts)
    try:
        p.check_ready()
    except ProviderError as e:
        log.error("verify stage disabled: %s", e)
        return None
    return p


# ---------------------------------------------------------------- commands


def cmd_plan(cfg: Config, args) -> int:
    n_providers = len(args.provider.split(",")) if args.provider else len(cfg.providers)
    total = verify_calls = 0
    for t in select_trips(cfg, parse_shard(args.shard), args.trip):
        combos = len(all_combinations(t, cfg.currency))
        if t.strategy.mode == "exhaustive":
            q = len(plan_exhaustive(t, cfg.currency))
            desc = f"exhaustive: {q} searches"
        else:
            p1 = plan_sample(t, cfg.currency)
            q = t.strategy.budget
            desc = f"smart: {len(p1)} sample searches + up to {max(q - len(p1), 0)} zoom searches"
            if t.strategy.budget_auto:
                desc += " (budget: auto — real number depends on the quota left; fallback shown)"
            if args.verbose:
                for x in p1:
                    print(f"    {x.origin}->{x.destination} {x.depart_date} -> {x.return_date or '-'}")
        calls = q * n_providers
        total += calls
        print(f"- {t.name}: {combos} possible date/airport combinations; {desc}; "
              f"≈{calls} API calls/day across {n_providers} provider(s)")
        if cfg.verify and not args.provider:
            verify_calls += cfg.verify.top_k
    print(f"Total ≈{total} search calls per day (≈{total * 30} per month).")
    if verify_calls:
        print(f"Final check on {cfg.verify.provider}: ≈{verify_calls} calls per day (≈{verify_calls * 30} per month).")
    return 0


def cmd_search(cfg: Config, args, storage: Storage, run_date: date) -> int:
    shard = parse_shard(args.shard)
    trips = select_trips(cfg, shard, args.trip)
    if not trips:
        log.warning("no trips for this shard; nothing to do")
        return 0
    providers = build_providers(cfg, args.provider, run_date)
    apply_auto_budgets(cfg, providers, run_date)
    trips = [t for t in trips if t.strategy.budget > 0]
    if not trips:
        log.warning("monthly search quota used up (only the reserve is left); skipping today's search")
        return 0
    run_id = storage.start_run(run_date, args.shard)
    verifier = build_verifier(cfg, args, run_date)
    engine = SearchEngine(cfg, providers, storage, run_id, run_date, retries=args.retries,
                          verifier=verifier, verify_top_k=cfg.verify.top_k if verifier else 0)
    stats = engine.run(trips)
    planned = sum(s.planned for s in stats.values())
    ok = sum(s.ok + s.skipped_cached for s in stats.values())
    failed = sum(s.failed for s in stats.values())
    notes = "; ".join(f"{k}: {v.offers_kept}/{v.offers_found} offers kept, rejected={v.rejected}"
                      for k, v in stats.items())
    storage.finish_run(run_id, planned, ok, failed, notes)
    for name, s in stats.items():
        print(f"[{name}] searches ok={s.ok} cached={s.skipped_cached} failed={s.failed} verified={s.verified} returns={s.returns_fetched} "
              f"offers kept {s.offers_kept}/{s.offers_found} rejected={s.rejected}")
    return 1 if planned and ok == 0 else 0


def cmd_report(cfg: Config, args, storage: Storage, run_date: date) -> int:
    summaries = build_all(storage, cfg, run_date)
    stats = storage.run_stats(run_date)
    errors = storage.search_errors(run_date)
    md = render_markdown(summaries, run_date, stats, errors)
    html_body = render_html(summaries, run_date, stats)
    out_dir = args.output_dir or cfg.report.output_dir
    if getattr(args, "provider", None) and not args.output_dir:
        out_dir = "reports"  # test runs (e.g. --provider mock) never overwrite the real Report/ folder
    paths = write_report_files(out_dir, run_date, summaries)
    log.info("report written: %s", ", ".join(map(str, paths)))

    cheapest = [s for s in summaries if s.today_min is not None]
    subject = f"✈️ Flights {run_date:%d %b}: " + (
        ", ".join(f"{s.trip.name} {s.options[0].currency} {s.today_min:,.0f}" for s in cheapest) or "no fares found")
    rendered = RenderedReport(subject=subject, markdown=md, html=html_body,
                              telegram=render_telegram(summaries, run_date),
                              has_alerts=any(s.alerts for s in summaries))
    if args.no_notify:
        print(md)
        return 0
    failures = 0
    for spec in cfg.notifiers:
        try:
            n = create_notifier(spec.type, **spec.options)
            if n.only_on_alert and not rendered.has_alerts:
                log.info("notifier %s: no alerts today, skipped", spec.type)
                continue
            n.send(rendered)
            storage.record_notification(run_date, spec.type, None)
            log.info("notifier %s: sent", spec.type)
        except NotifyError as e:
            failures += 1
            storage.record_notification(run_date, spec.type, str(e))
            log.error("notifier %s failed: %s", spec.type, e)
    return 1 if failures else 0


def cmd_probe(cfg: Config, args, run_date: date) -> int:
    """One real search per configured provider (and the verify provider) to prove the keys work."""
    from datetime import timedelta

    from .models import SearchQuery

    trip = select_trips(cfg, None, args.trip)[0]
    days = trip.depart_window.days()
    dep = days[len(days) // 2]
    q = SearchQuery(trip_name=trip.name, origin=trip.origins[0], destination=trip.destinations[0],
                    depart_date=dep, return_date=dep + timedelta(days=trip.nights_min) if trip.is_round_trip else None,
                    adults=trip.adults, cabin=trip.cabin, currency=cfg.currency, max_stops=trip.filters.max_stops)
    names = [p.name for p in cfg.providers] + ([cfg.verify.provider] if cfg.verify else [])
    failures = 0
    print(f"probe: {q.origin}->{q.destination} {q.depart_date} -> {q.return_date or '-'}")
    for name in dict.fromkeys(names):
        p = create_provider(name, **({"today": run_date} if name == "mock" else {}))
        try:
            p.check_ready()
            offers = p.search(q)
        except ProviderError as e:
            failures += 1
            print(f"  {name}: FAILED: {e}")
            continue
        if hasattr(p, "account"):
            try:
                acct = p.account()
                print(f"  {name}: plan {acct['plan_name']}, {acct['plan_searches_left']} of "
                      f"{acct['searches_per_month']} searches left this month")
            except ProviderError as e:
                print(f"  {name}: could not read plan quota: {e}")
        sandbox = " (TEST key: sandbox data, not real fares)" if getattr(p, "sandbox", False) else ""
        if offers:
            cheapest = min(offers, key=lambda o: o.price)
            print(f"  {name}: OK{sandbox}: {len(offers)} offers, cheapest {cheapest.currency} {cheapest.price:,.2f} "
                  f"on {', '.join(cheapest.airlines)}")
        else:
            print(f"  {name}: OK{sandbox}: no offers for this date")
    return 1 if failures else 0


def cmd_merge(storage: Storage, args) -> int:
    total = 0
    for src in args.sources:
        if not Path(src).exists():
            log.warning("shard db %s not found, skipping", src)
            continue
        n = storage.merge_from(src)
        total += n
        print(f"merged {n} offers from {src}")
    print(f"total merged: {total}")
    return 0


def cmd_history(cfg: Config, args, storage: Storage) -> int:
    trips = select_trips(cfg, None, args.trip)
    for t in trips:
        print(f"{t.name}:")
        for d, p in storage.daily_minimums(t.name, limit_days=args.days):
            print(f"  {d}  {cfg.currency} {p:,.0f}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="flight_tracker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", help="override the database path from the config")
    ap.add_argument("--date", help="run date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, search=False, report=False):
        p.add_argument("--trip", help="only this trip name")
        if search:
            p.add_argument("--shard", help="i/n: only run trips where index %% n == i-1")
            p.add_argument("--provider", help="comma-separated override, e.g. 'mock'")
            p.add_argument("--retries", type=int, default=3)
            p.add_argument("--no-verify", action="store_true", help="skip the final-check provider")
        if report:
            p.add_argument("--no-notify", action="store_true", help="print the report instead of sending")
            p.add_argument("--output-dir")

    common(sub.add_parser("plan", help="show the search plan and API cost"), search=True)
    common(sub.add_parser("search", help="run searches only"), search=True)
    common(sub.add_parser("report", help="build and send the report"), report=True)
    common(sub.add_parser("run", help="search + report"), search=True, report=True)
    pb = sub.add_parser("probe", help="one live search per provider to check API keys")
    pb.add_argument("--trip")
    m = sub.add_parser("merge", help="merge shard DBs into the main DB")
    m.add_argument("sources", nargs="+")
    h = sub.add_parser("history", help="daily cheapest fares")
    h.add_argument("--trip")
    h.add_argument("--days", type=int, default=30)
    pr = sub.add_parser("prune", help="delete old offers")
    pr.add_argument("--keep-days", type=int, default=120)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_date = date.fromisoformat(args.date) if args.date else utc_today()

    # merge and prune only touch the database, so they don't need a valid config when --db is given.
    if args.command in ("merge", "prune") and args.db:
        cfg = None
    else:
        try:
            cfg = load_config(args.config, today=run_date)
        except ConfigError as e:
            print(f"config error: {e}", file=sys.stderr)
            return 2

    if args.command == "plan":
        return cmd_plan(cfg, args)
    if args.command == "probe":
        return cmd_probe(cfg, args, run_date)

    storage = Storage(args.db or cfg.database)
    try:
        if args.command == "search":
            return cmd_search(cfg, args, storage, run_date)
        if args.command == "report":
            return cmd_report(cfg, args, storage, run_date)
        if args.command == "run":
            rc = cmd_search(cfg, args, storage, run_date)
            return max(rc, cmd_report(cfg, args, storage, run_date))
        if args.command == "merge":
            return cmd_merge(storage, args)
        if args.command == "history":
            return cmd_history(cfg, args, storage)
        if args.command == "prune":
            print(f"deleted {storage.prune(args.keep_days, run_date)} old offers")
            return 0
    finally:
        storage.close()
    return 0
