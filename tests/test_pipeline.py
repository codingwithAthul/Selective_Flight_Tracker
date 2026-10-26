from datetime import date, timedelta

from flight_tracker.cli import main, select_trips
from flight_tracker.config import parse_config
from flight_tracker.insights import build_all
from flight_tracker.providers.base import Provider, TransientProviderError
from flight_tracker.providers.mock import MockProvider
from flight_tracker.report import render_html, render_markdown, render_telegram
from flight_tracker.storage import Storage
from flight_tracker.workers import SearchEngine, run_with_retries

from .conftest import TODAY, base_raw


def run_day(cfg, storage, day):
    run_id = storage.start_run(day)
    engine = SearchEngine(cfg, [MockProvider(today=day)], storage, run_id, day, base_delay=0)
    stats = engine.run(cfg.trips)
    s = stats[cfg.trips[0].name]
    storage.finish_run(run_id, s.planned, s.ok, s.failed)
    return s


def test_engine_respects_budget_and_stores(cfg, storage):
    s = run_day(cfg, storage, TODAY)
    assert s.failed == 0
    assert s.ok <= cfg.trips[0].strategy.budget
    rows = storage.offers_for("T1", TODAY)
    assert rows and [r["price"] for r in rows] == sorted(r["price"] for r in rows)


def test_rerun_same_day_uses_cache(cfg, storage):
    first = run_day(cfg, storage, TODAY)
    second = run_day(cfg, storage, TODAY)
    assert first.ok > 0
    assert second.ok == 0 and second.failed == 0 and second.skipped_cached > 0


def test_report_and_alerts_over_days(cfg, storage):
    for i in range(3):
        run_day(cfg, storage, TODAY + timedelta(days=i))
    day = TODAY + timedelta(days=2)
    cfg.trips[0].target_price = 10_000  # always hit
    summaries = build_all(storage, cfg, day)
    s = summaries[0]
    assert s.options and s.options[0].price == s.today_min
    assert [o.price for o in s.options] == sorted(o.price for o in s.options)
    assert s.prev_min is not None and len(s.history) == 3 and s.sparkline
    assert any("Target price hit" in a for a in s.alerts)
    md = render_markdown(summaries, day, storage.run_stats(day))
    assert "Daily flight report" in md and "T1" in md
    assert "<table" in render_html(summaries, day)
    assert all(len(m) <= 4096 for m in render_telegram(summaries, day))


class Flaky(Provider):
    name = "flaky"

    def __init__(self, fails):
        super().__init__()
        self.fails = fails

    def search(self, query):
        if self.fails:
            self.fails -= 1
            raise TransientProviderError("boom")
        return []


def test_retries(cfg):
    q = select_trips(cfg, None)[0]
    from flight_tracker.planner import plan_sample
    query = plan_sample(q, "USD")[0]
    res = run_with_retries(Flaky(2), query, retries=3, sleep=lambda s: None)
    assert res.error is None and res.attempts == 3
    res = run_with_retries(Flaky(10), query, retries=2, sleep=lambda s: None)
    assert res.error == "boom" and res.attempts == 3


def test_failures_recorded(cfg, storage):
    run_id = storage.start_run(TODAY)
    engine = SearchEngine(cfg, [MockProvider(today=TODAY, fail_rate=1.0)], storage, run_id, TODAY,
                          retries=0, base_delay=0)
    s = engine.run(cfg.trips)["T1"]
    assert s.ok == 0 and s.failed > 0
    assert storage.search_errors(TODAY)


def test_shard_selection():
    raw = base_raw()
    raw["trips"] = [dict(raw["trips"][0], name=f"T{i}") for i in range(5)]
    cfg = parse_config(raw, today=TODAY)
    names = [[t.name for t in select_trips(cfg, (i, 2))] for i in (1, 2)]
    assert names == [["T0", "T2", "T4"], ["T1", "T3"]]


def test_cli_shards_merge_report_prune(tmp_path):
    import yaml
    raw = base_raw()
    raw["trips"].append(dict(raw["trips"][0], name="T2", trip_type="one_way"))
    raw["trips"][1].pop("nights")
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(raw))
    d = "2026-10-21"
    common = ["--config", str(cfgp), "--date", d]
    for i in (1, 2):
        assert main(common + ["--db", str(tmp_path / f"s{i}.db"), "search", "--shard", f"{i}/2"]) == 0
    main_db = str(tmp_path / "main.db")
    assert main(common + ["--db", main_db, "merge", str(tmp_path / "s1.db"), str(tmp_path / "s2.db")]) == 0
    st = Storage(main_db)
    assert st.offers_for("T1", date(2026, 10, 21)) and st.offers_for("T2", date(2026, 10, 21))
    st.close()
    out = tmp_path / "reports"
    assert main(common + ["--db", main_db, "report", "--output-dir", str(out)]) == 0
    assert (out / "T1 YYZ - LHR+LGW (Round)" / "latest.md").exists()
    assert (out / "T2 YYZ - LHR+LGW (One-Way)" / f"{d}.html").exists()
    # merging twice is idempotent
    main(common + ["--db", main_db, "merge", str(tmp_path / "s1.db")])
    assert main(["--date", "2027-06-01", "--db", main_db, "prune", "--keep-days", "30"]) == 0
    st = Storage(main_db)
    # only each trip's single cheapest offer for the day survives
    assert len(st.offers_for("T1", date(2026, 10, 21))) == 1
    st.close()


def test_plan_command(tmp_path, capsys):
    import yaml
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(base_raw()))
    assert main(["--config", str(cfgp), "--date", "2026-10-21", "plan"]) == 0
    assert "API calls" in capsys.readouterr().out


def test_bad_config_exit_code(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("trips: []\n")
    assert main(["--config", str(p), "plan"]) == 2


class Verifier(MockProvider):
    name = "verifier"


def test_verify_stage_rechecks_top_combos(cfg, storage):
    run_id = storage.start_run(TODAY)
    engine = SearchEngine(cfg, [MockProvider(today=TODAY)], storage, run_id, TODAY, base_delay=0,
                          verifier=Verifier(today=TODAY), verify_top_k=3)
    s = engine.run(cfg.trips)["T1"]
    assert s.verified == 3
    rows = storage.conn.execute("SELECT provider, phase FROM searches WHERE phase='verify'").fetchall()
    assert len(rows) == 3 and {r["provider"] for r in rows} == {"verifier"}
    # a same-day re-run doesn't pay for the verify calls again
    engine2 = SearchEngine(cfg, [MockProvider(today=TODAY)], storage, storage.start_run(TODAY), TODAY,
                           base_delay=0, verifier=Verifier(today=TODAY), verify_top_k=3)
    assert engine2.run(cfg.trips)["T1"].verified == 3
    assert storage.conn.execute("SELECT COUNT(*) FROM searches WHERE phase='verify'").fetchone()[0] == 3


class Sandbox(MockProvider):
    name = "duffel-test"


def test_sandbox_fares_never_outrank_real_ones_and_verification_is_reported(cfg, storage):
    run_id = storage.start_run(TODAY)
    engine = SearchEngine(cfg, [Sandbox(today=TODAY)], storage, run_id, TODAY, base_delay=0,
                          verifier=Verifier(today=TODAY), verify_top_k=3)
    engine.run(cfg.trips)
    s = build_all(storage, cfg, TODAY)[0]
    assert s.sandbox_providers == ["duffel-test"] and s.sandbox_excluded
    assert s.options and all(p == "verifier" for o in s.options for p in o.providers)
    assert len(s.verification) == 3 and all(v.ok and v.search_price and v.verify_price for v in s.verification)
    assert all(v.search_providers == "duffel-test" for v in s.verification)


def test_sandbox_only_is_still_reported(cfg, storage):
    run_id = storage.start_run(TODAY)
    SearchEngine(cfg, [Sandbox(today=TODAY)], storage, run_id, TODAY, base_delay=0).run(cfg.trips)
    s = build_all(storage, cfg, TODAY)[0]
    assert s.options and not s.sandbox_excluded and s.sandbox_providers == ["duffel-test"]


def test_report_shows_sandbox_warning_and_final_check(cfg, storage):
    run_id = storage.start_run(TODAY)
    SearchEngine(cfg, [Sandbox(today=TODAY)], storage, run_id, TODAY, base_delay=0,
                 verifier=Verifier(today=TODAY), verify_top_k=2).run(cfg.trips)
    summaries = build_all(storage, cfg, TODAY)
    md = render_markdown(summaries, TODAY)
    assert "TEST key" in md and "Final check on verifier" in md
    assert "Final check" in render_html(summaries, TODAY)
    assert "Final check" in render_telegram(summaries, TODAY)[0]


def test_probe_command(tmp_path, capsys):
    import yaml
    raw = base_raw()
    raw["verify"] = {"provider": "mock", "top_k": 2}
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(raw))
    assert main(["--config", str(cfgp), "--date", "2026-10-21", "probe"]) == 0
    assert "mock: OK" in capsys.readouterr().out


def test_recent_best_includes_earlier_days(cfg, storage):
    for i in range(3):
        run_day(cfg, storage, TODAY + timedelta(days=i))
    day = TODAY + timedelta(days=2)
    s = build_all(storage, cfg, day)[0]
    assert s.recent_best and [p for *_, p, _ in s.recent_best] == sorted(p for *_, p, _ in s.recent_best)
    assert all(dep > day for dep, *_ in s.recent_best)


class Quota(MockProvider):
    name = "quota"

    def __init__(self, left, **kw):
        super().__init__(**kw)
        self.left = left

    def account(self):
        return {"plan_searches_left": self.left}


def test_auto_budget_spreads_quota_over_month():
    from flight_tracker.cli import apply_auto_budgets, days_left_in_month
    raw = base_raw(strategy={"budget": "auto"})
    raw["trips"].append(dict(raw["trips"][0], name="T2", code="T2"))
    cfg = parse_config(raw, today=TODAY)
    assert cfg.trips[0].strategy.budget_auto
    assert days_left_in_month(date(2026, 10, 22)) == 10 and days_left_in_month(date(2026, 12, 31)) == 1
    apply_auto_budgets(cfg, [Quota(210, today=TODAY)], date(2026, 10, 22))
    assert [t.strategy.budget for t in cfg.trips] == [10, 10]  # (210 - 10 reserve) / 10 days / 2 trips
    apply_auto_budgets(cfg, [Quota(5, today=TODAY)], date(2026, 10, 22))
    assert [t.strategy.budget for t in cfg.trips] == [0, 0]  # only the reserve is left
    cfg = parse_config(base_raw(strategy={"budget": "auto"}), today=TODAY)
    apply_auto_budgets(cfg, [MockProvider(today=TODAY)], TODAY)  # no quota info: keep fallback
    assert cfg.trips[0].strategy.budget == 7


class OutboundOnly(MockProvider):
    """Like SerpApi: round-trip price, outbound details only, plus a token for the return call."""
    name = "outbound-only"

    def search(self, query):
        offers = super().search(query)
        for o in offers:
            o.inbound = None
            o.extra = {"departure_token": f"tok-{o.fingerprint()}"}
        return offers

    def fetch_return(self, query, token):
        full = MockProvider.search(self, query)[0]
        return [(full.inbound, 999.0)]


def test_return_details_fetched_for_top_offers(storage):
    cfg = parse_config(base_raw(strategy={"budget": 10, "return_details": 3}), today=TODAY)
    run_id = storage.start_run(TODAY)
    s = SearchEngine(cfg, [OutboundOnly(today=TODAY)], storage, run_id, TODAY, base_delay=0).run(cfg.trips)["T1"]
    assert s.returns_fetched == 3
    rows = storage.conn.execute("SELECT price FROM offers WHERE inbound_json IS NOT NULL").fetchall()
    assert len(rows) == 3 and all(r["price"] == 999.0 for r in rows)
    summary = build_all(storage, cfg, TODAY)[0]
    assert sum(o.inbound is not None for o in summary.options) == 3


class NoReturns(OutboundOnly):
    def fetch_return(self, query, token):
        return []


def test_empty_return_list_is_skipped_and_noted(storage):
    cfg = parse_config(base_raw(strategy={"budget": 10, "return_details": 3}), today=TODAY)
    run_id = storage.start_run(TODAY)
    s = SearchEngine(cfg, [NoReturns(today=TODAY)], storage, run_id, TODAY, base_delay=0).run(cfg.trips)["T1"]
    assert s.returns_fetched == 0
    summary = build_all(storage, cfg, TODAY)[0]
    assert all(o.inbound is None for o in summary.options)
    assert "not listed by Google today" in render_markdown([summary], TODAY)


def test_auto_budget_leaves_room_for_return_details():
    from flight_tracker.cli import apply_auto_budgets
    cfg = parse_config(base_raw(strategy={"budget": "auto", "return_details": 5}), today=TODAY)
    apply_auto_budgets(cfg, [Quota(210, today=TODAY)], date(2026, 10, 22))
    assert cfg.trips[0].strategy.budget == 20 - 5
