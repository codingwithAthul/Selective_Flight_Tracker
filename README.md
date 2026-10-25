# Selective Flight Tracker

Every day, this bot searches Google Flights for the cheapest flights between two fixed places, across a range of dates. It saves the top 5 options, with both the outbound and return flights, to `Report/`.

## Where to find the reports

`Report/<trip folder>/latest.md` is today's report. Older days are saved as `YYYY-MM-DD.md`.

Example: [`Report/T1 Vancouver - Kochi (Round)/latest.md`](Report/T1%20Vancouver%20-%20Kochi%20(Round)/latest.md)

## One-time setup

1. Get a SerpApi key at https://serpapi.com. The free plan gives 250 searches a month.
2. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret**.
   Name it `SERPAPI_API_KEY` and paste the key.

The bot then runs every day at 11:30 UTC on its own.

## Change the trip

Edit `config.yaml` under `trips:`:

```yaml
- name: Vancouver to Kochi
  code: T1                    # report folder: "T1 Vancouver - Kochi (Round)"
  from_name: Vancouver
  to_name: Kochi
  origin: [YVR]               # airport codes; a list is allowed, e.g. [YVR, SEA]
  destination: [COK]
  trip_type: round_trip       # or one_way
  depart_window: {start: 2026-11-23, end: 2026-12-14}
  return_window: {start: 2027-01-01, end: 2027-01-14}   # round trips only
```

To add another trip, copy the block and give it the next code (`T2`, `T3`, …).

## Run it now

Go to **Actions → Daily flight search → Run workflow**. The report appears in `Report/` a few minutes later.

## Good to know

- **Search budget:** with `budget: auto`, the bot spreads your remaining monthly SerpApi searches evenly over the days left in the month.
- **Report size:** `top_n` in `config.yaml` sets how many options each report lists. Each option with return details costs 1 extra search (`return_details`).
- **Prices** are for all passengers, round trip, in `currency` (CAD).

More detail is in [`docs/`](docs/): configuration, providers, operations and architecture.
