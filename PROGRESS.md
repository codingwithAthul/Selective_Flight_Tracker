# PROGRESS — where the build is right now

> Update this file at the end of every work session. When resuming, read it
> first, then [PROJECT.md](PROJECT.md).

_Last updated: 2026-10-21_

## Status: v0.5 — live on main. T1 Vancouver ⇄ Kochi runs daily: top 5 with return flights

The full pipeline works end to end with the **mock** provider: plan →
parallel search → filter → SQLite → insights → report → notify. That
includes the GitHub Actions daily workflow with matrix shards. Real prices
need an API key. See "Waiting on owner" below.

## Done

- [x] Config schema and validation (`flight_tracker/config.py`, `config.example.yaml`)
- [x] Date planner: smart sample→zoom under a budget, plus exhaustive mode (`planner.py`)
- [x] Providers: `mock`, `serpapi` (Google Flights), `duffel` (`providers/`)
- [x] Parallel worker pool with per-provider concurrency, rate limit, retries and backoff (`workers.py`)
- [x] Filters (stops, price, duration, layovers, airlines, times, weekdays) and cross-provider de-dup (`filters.py`)
- [x] SQLite storage with migrations, shard merge, prune and a same-day cache (`storage.py`)
- [x] Insights: day-over-day change, all-time low, target price, 30-day sparkline, cheapest dates, best value (`insights.py`)
- [x] Reports: Markdown, HTML email and Telegram (`report.py`); files written to `reports/`
- [x] Notifiers: console, GitHub job summary, Telegram, email/SMTP (`notify/`)
- [x] CLI: plan / search / merge / report / run / history / prune (`cli.py`)
- [x] Workflows: `daily.yml` (plan → matrix search → merge+report → save to `data` branch) and `ci.yml`
- [x] Tests (pytest, no network): config, planner, filters, provider parsers, pipeline, CLI shards/merge
- [x] Docs: README, docs/ARCHITECTURE.md, CONFIGURATION.md, PROVIDERS.md, OPERATIONS.md
- [x] Verify stage: Duffel searches, SerpApi re-prices the top-K combos (`config.verify`)
- [x] Duffel test-token detection (`duffel-test`); sandbox fares never outrank real ones
- [x] `probe` command and `live-keys` CI job to check API keys

## Waiting on owner (decisions only they can make)

1. **Report channel.** Reports are committed to `Report/`. Telegram or email isn't chosen yet.
2. **Duffel.** Parked.

## Next up

1. Watch the auto budget: each day uses about (left - 10) / days_left searches, minus 5 for return flights.
2. Optional: nearby airports (SEA, TRV) if Athul wants them.

## How to resume quickly

```bash
pip install -r requirements-dev.txt
python -m pytest -q                                   # everything should pass
python -m flight_tracker plan                         # see the search plan
python -m flight_tracker --db /tmp/t.db run --provider mock --no-notify
```

## Log

- 2026-10-21: Built v0.1 from an empty repo (README + LICENSE only). Amadeus Self-Service was shut down in July 2026 and Kiwi Tequila is closed to new sign-ups, so they are not supported.
- 2026-10-21: Athul chose Duffel for search with SerpApi as the final check, and set a rule: one commit per small chunk of code. Added the verify stage, sandbox handling, probe and the live-keys CI job.
- 2026-10-21: Athul set the SERPAPI_API_KEY secret, parked Duffel, and gave the route Vancouver ⇄ Kochi (T1). Reports now go to `Report/<code> <from> - <to> (Round|One-Way)/` on main. Added rotate_daily and the 7-day best fares section.
- 2026-10-22: Athul confirmed the dates (out: last week of Nov or 1st/2nd week of Dec; back: 1st/2nd week of Jan) and asked for no transit limits. Added return_window and budget: auto. There are 308 date pairs; auto budget gives about 23/day for the rest of October and about 8/day in November.
- 2026-10-24: PR #1 merged; the first live run produced real YVR⇄COK fares (cheapest CAD 3,337, Air India). The repo was renamed to Selective_Flight_Tracker; nothing in the code depends on the name. Removed the stray mock `reports/` (Toronto–London test data). Now: top 5, return flights fetched via SerpApi departure_token, minimal README.
- 2026-10-26: First real return-flight run (GitHub ran the 11:30 UTC cron 6h late): 4 of the top 5 got return flights. For #1 (EVA Air + Air India), Google returned an empty return list for its departure_token. That case is now logged, and the report says "not listed by Google today" instead of "not fetched".
