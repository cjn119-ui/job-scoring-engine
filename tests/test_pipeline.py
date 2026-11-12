import copy
import json

import pytest

from conftest import NOW, ROOT, Net, fetcher, fixture_routes, posting
from job_scoring import collect, pipeline as P, report, sources as CS
from job_scoring import evaluate as CE

BOARDS = ROOT / "config" / "boards.example.json"


def run_all(prof, routes=None, state=None, now=NOW):
    net = Net(fixture_routes() if routes is None else routes)
    return collect.run(state if state is not None else {}, prof, fetcher(net), now, BOARDS), net


def test_end_to_end_over_recorded_payloads(prof):
    state, net = run_all(prof)
    assert {b["status"] for b in state["boards"].values()} == {"ok"}
    top = P.shortlist(state, NOW)
    assert {e["pid"] for e in top} == {"ashby:globex-example:a1", "greenhouse:acme-example:101"}
    # the clerk and the front-end role never cost a detail request; the UK-only role is evaluated and excluded
    assert not any("/jobs/103" in u or "/jobs/105" in u for u in net.log)
    assert "greenhouse:acme-example:104" not in state["postings"]


def test_only_public_provider_hosts_were_contacted(prof):
    _, net = run_all(prof)
    assert net.log and all(CS.url_ok(u) for u in net.log)


def test_duplicates_collapse_to_one_shortlist_entry(prof):
    state, _ = run_all(prof)
    dups = [e for e in state["postings"].values() if e.get("dup_of")]
    assert len(dups) == 1 and state["stats"]["duplicates"] == 1
    assert sum(1 for e in P.shortlist(state, NOW) if e["title"] == "Analytics Engineer") == 1


def test_same_title_roles_with_different_descriptions_are_not_collapsed(prof):
    st = P.ensure_state({}, BOARDS)
    a = posting("Data Platform Engineer", "Python SQL pipelines. Own the warehouse. Fintech payments data.", jid="1")
    b = posting("Data Platform Engineer", "Python SQL pipelines. Own the warehouse. Healthcare claims data and reporting.", jid="2")
    P.ingest(st, prof, [a, b], "acme-example", {"1", "2"}, NOW, {})
    assert P.mark_duplicates(st) == 0


def test_a_posting_that_disappears_is_recorded_closed_and_a_change_is_described(prof):
    state, _ = run_all(prof)
    pid = "greenhouse:acme-example:101"
    assert state["postings"][pid]["open"]
    routes = fixture_routes()
    gh_list = routes[CS.gh_list_url("acme-example")][1]
    routes[CS.gh_list_url("acme-example")] = (200, {"jobs": [j for j in gh_list["jobs"] if j["id"] != 101]})
    state, _ = run_all(prof, routes, state, "2026-10-29T12:00:00Z")
    e = state["postings"][pid]
    assert e["open"] is False and e["closed_at"] == "2026-10-29T12:00:00Z"
    assert pid not in {x["pid"] for x in P.shortlist(state, "2026-10-29T12:00:00Z")}


def test_a_changed_posting_records_what_changed(prof):
    state, _ = run_all(prof)
    pid = "greenhouse:acme-example:101"
    routes = fixture_routes()
    detail = copy.deepcopy(routes[CS.gh_detail_url("acme-example", 101)][1])
    detail["pay_input_ranges"] = []
    routes[CS.gh_detail_url("acme-example", 101)] = (200, detail)
    state, _ = run_all(prof, routes, state, "2026-10-29T12:00:00Z")
    hist = state["postings"][pid]["history"]
    assert hist and "pay" in hist[-1]["change"] or "description" in hist[-1]["change"]
    assert state["postings"][pid]["first_seen"] == NOW


def test_a_provider_block_is_recorded_and_not_retried_around(prof):
    routes = fixture_routes()
    url = CS.ashby_url("globex-example")
    routes[url] = (429, {})
    state, net = run_all(prof, routes)
    b = state["boards"]["globex-analytics-fictional"]
    assert b["status"] == "blocked" and net.log.count(url) == 1
    assert state["boards"]["acme-robotics-fictional"]["status"] == "ok"        # other boards unaffected


def test_unknown_board_is_marked_not_found(prof):
    state, _ = run_all(prof, routes={})
    assert {b["status"] for b in state["boards"].values()} == {"not_found"}


def test_one_company_cannot_fill_the_whole_shortlist(prof):
    st = P.ensure_state({}, BOARDS)
    ps = [posting(company="Acme", jid=str(i), desc=f"Python SQL pipelines ownership end-to-end stakeholders. Variant {i} unique text {'x' * i}.") for i in range(5)]
    P.ingest(st, prof, ps, "acme-example", {str(i) for i in range(5)}, NOW, {})
    assert len(P.shortlist(st, NOW, tiers=("strong", "stretch"), n=5)) == P.MAX_PER_COMPANY


def test_eviction_drops_weak_entries_not_old_strong_ones(prof, monkeypatch):
    monkeypatch.setattr(P, "MAX_FULL_ENTRIES", 2)
    st = P.ensure_state({}, BOARDS)
    strong = posting(jid="s", desc="Python SQL pipelines ownership end-to-end stakeholders small team. Pay: $150,000 - $190,000.")
    weak = [posting("Software Engineer", "Some Python.", jid=f"w{i}", comp=[]) for i in range(3)]
    P.ingest(st, prof, [strong] + weak, "acme-example", {"s", "w0", "w1", "w2"}, NOW, {})
    assert len(st["postings"]) <= 2 and "greenhouse:acme-example:s" in st["postings"]


def test_report_neutralises_hostile_posting_text(prof):
    st = P.ensure_state({}, BOARDS)
    p = posting(title="Data Platform Engineer `|<img src=x onerror=alert(1)>[click](http://evil.example)", jid="h")
    p["url"] = "javascript:alert(1)"
    P.ingest(st, prof, [p], "acme-example", {"h"}, NOW, {})
    text, _ = report.render_report(st, prof, NOW)
    assert "<img" not in text and "`|" not in text and "javascript:" not in text and "](http" not in text


def test_report_lists_shortlist_and_sig_changes_with_it(prof):
    state, _ = run_all(prof)
    text, meta = report.render_report(state, prof, NOW)
    assert "Strongest current matches" in text and "candidate attainment UNKNOWN" in text and len(meta["top"]) == 2
    state["postings"][meta["top"][0]]["content_hash"] = "changed"
    assert report.render_report(state, prof, NOW)[1]["sig"] != meta["sig"]


def test_precision_at_k():
    assert P.precision_at_k(["a", "b", "c"], {"a", "c"}, 3) == pytest.approx(2 / 3)
    assert P.precision_at_k([], {"a"}) == 0.0


def test_state_round_trips_through_json(prof):
    state, _ = run_all(prof)
    assert json.loads(json.dumps(state)) == state
