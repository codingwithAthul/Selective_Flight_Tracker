# PROJECT — Selective Flight Tracker

> This file holds the project's goals and design. It changes rarely.
> For "where are we right now", read [PROGRESS.md](PROGRESS.md).

## Goal

A bot that runs every day and works out the cheapest way to fly between two
**fixed** places. The **dates are flexible**, so the bot searches across a
date window to find the lowest fares.

1. **Fixed endpoints.** The trip's origin and destination never change. Each
   can be a list of airports, e.g. `[YYZ, YTZ]` for Toronto.
2. **Optional hard constraints** are applied to every result: max stops, max
   price, max total duration, max layover, airline include/exclude lists, and
   departure-time windows.
3. **Flexible dates.** You give a departure window and, for return trips, a
   range of nights. The bot searches the combinations to find the cheapest.
4. **Daily report.** The best options sorted by price, with brief details
   (dates, airlines, stops, duration, times, link), plus price-change
   insights, delivered by Telegram, email, a file or the GitHub job summary.
5. **Parallel workers.** Searches fan out over a thread pool with per-provider
   concurrency and rate limits. Trips can also be sharded across parallel
   GitHub Actions jobs.
6. **History.** Every search and offer is stored in SQLite, so you get trends,
   all-time lows and price-drop alerts.
7. **Resumable.** PROJECT.md and PROGRESS.md let anyone pick the work up
   cold.

## Non-goals

- Booking or paying for tickets. The bot links out to book.
- Scraping websites in ways that break their terms. Only official or
  commercial APIs are used.

## Key design decisions

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Simple, few dependencies (`requests`, `PyYAML`) |
| Storage | SQLite (`data/tracker.db`) | Zero-ops, a single file, easy to persist on a git branch |
| Scheduler | GitHub Actions cron | Free, no server, secrets management built in |
| Persistence across runs | `data` branch holding the DB and reports | Keeps history out of `main` and is auditable |
| Data sources | Duffel searches the window (free); SerpApi (Google Flights) re-prices the top-K as a final check. Pluggable: `mock`, `serpapi`, `duffel` | Amadeus Self-Service shut down in July 2026 and Kiwi Tequila is closed to new sign-ups |
| Search cost control | Daily **search budget** plus a two-phase **sample → zoom** planner | Flexible dates explode combinatorially, and paid APIs charge per search |
| Parallelism | Thread pool (I/O bound) plus Actions matrix sharding by trip | Fast runs without hammering providers |
| Report ordering | Sorted by price, plus a separate "best value" pick (price + time cost) | You asked for price order; value is an optional extra lens |
| Shard IDs | UUID text keys | Shard DBs merge without ID collisions |

## Architecture (one paragraph)

`config.yaml` defines the trips. The **planner** expands each trip into
`SearchQuery` objects within budget. The **worker pool** runs each query
against each enabled **provider**, with retries and rate limits. Offers are
**normalised** into one model, **filtered** by the trip's constraints,
**de-duplicated** across providers and **stored** in SQLite. The **insights**
module compares today with history. The **report** module renders Markdown,
HTML and Telegram text, and the **notifiers** deliver them.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Roadmap / upgrade ideas

- [x] Multi-airport origins and destinations
- [x] Two-phase smart date search under a budget
- [x] Price-drop and all-time-low alerts, target-price alerts
- [x] Best-value ranking (price + value of time)
- [x] Cheapest-by-departure-date calendar in the report
- [ ] Return-leg details for SerpApi round trips (needs a second call per offer, which costs budget)
- [ ] Price prediction ("buy now vs wait") once there is about 30 days of history
- [ ] Open-jaw / multi-city trips
- [ ] Simple web dashboard (static page on GitHub Pages built from the DB)
- [ ] Telegram bot commands (`/top`, `/history`) using a long-running worker
