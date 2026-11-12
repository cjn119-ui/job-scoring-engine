import json

import pytest

from conftest import fetcher, Net
from job_scoring import sources as CS


def test_provider_access_is_https_get_on_allowlisted_hosts_only():
    assert CS.url_ok(CS.gh_list_url("acme-example")) and CS.url_ok(CS.ashby_url("globex-example")) and CS.url_ok(CS.lever_url("initech-example"))
    for bad in ("http://boards-api.greenhouse.io/v1/boards/x/jobs", "https://evil.example/v1/boards/x/jobs",
                "https://user:pw@boards-api.greenhouse.io/x", "https://boards-api.greenhouse.io:8443/v1/boards/x/jobs"):
        assert not CS.url_ok(bad), bad
    with pytest.raises(CS.FetchError):
        fetcher(Net()).get_json("https://evil.example/x")


@pytest.mark.parametrize("status,exc", [(429, CS.Blocked), (403, CS.Blocked), (401, CS.Blocked), (404, CS.NotFound), (500, CS.FetchError)])
def test_provider_refusal_is_surfaced_never_worked_around(status, exc):
    url = CS.gh_list_url("acme-example")
    net = Net({url: (status, {"jobs": []})})
    with pytest.raises(exc):
        fetcher(net).get_json(url)
    assert net.log == [url]                                    # exactly one request: no retry around a refusal


def test_per_host_rate_limit_is_enforced():
    sleeps, clock = [], [100.0]
    url = CS.gh_list_url("acme-example")
    f = CS.Fetcher(transport=Net({url: (200, {"jobs": []})}), sleep=sleeps.append, clock=lambda: clock[0], gap=1.0)
    f.get_json(url)
    f.get_json(url)
    assert sleeps and sleeps[0] == pytest.approx(1.0)
    clock[0] += 5
    f.get_json(url)
    assert len(sleeps) == 1                                     # enough time passed: no wait


def test_oversized_or_unreadable_responses_are_errors():
    url = CS.gh_list_url("acme-example")
    f = CS.Fetcher(transport=lambda u: (200, b"not json"), sleep=lambda s: None, gap=0)
    with pytest.raises(CS.FetchError):
        f.get_json(url)


def test_greenhouse_unescapes_content_and_prefers_structured_pay():
    job = {"id": 7, "title": "Data Engineer", "location": {"name": "Remote - United States"}, "absolute_url": "https://job-boards.greenhouse.io/x/jobs/7", "company_name": "X"}
    detail = dict(job, content="&lt;p&gt;Build &lt;b&gt;pipelines&lt;/b&gt;. Pay $90,000 - $99,000.&lt;/p&gt;", departments=[{"name": "Data"}],
                  pay_input_ranges=[{"min_cents": 15000000, "max_cents": 19000000, "currency_type": "USD", "title": "Annual"}])
    p = CS.gh_posting("x", CS.gh_stub("x", job), detail)
    assert "<" not in p["description"] and "Build pipelines" in p["description"]
    assert p["comp"][0]["source"] == "structured" and p["comp"][0]["min"] == 150000
    assert p["pid"] == "greenhouse:x:7"


def test_text_pay_is_labelled_text_and_junk_numbers_are_not_pay():
    comp = CS.text_comp("Salary: $150,000 - $190,000 per year.")
    assert comp[0]["source"] == "text" and comp[0]["interval"] == "year"
    assert CS.text_comp("We hire 20 - 30 people, call 555 - 1234") == []
    k = CS.text_comp("Pay: $140k - $170k")
    assert k[0]["min"] == 140000 and k[0]["max"] == 170000 and k[0]["currency"] == "USD"


def test_ashby_and_lever_structured_pay_and_workplace():
    ashby = {"jobs": [{"id": "a", "title": "Data Engineer", "location": "Austin, TX", "workplaceType": "Hybrid", "descriptionPlain": "Python pipelines.",
                       "compensation": {"summaryComponents": [{"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 120000, "maxValue": 150000}]},
                       "jobUrl": "https://jobs.ashbyhq.com/x/a", "publishedAt": "2026-10-01T00:00:00Z"}]}
    (p,) = CS.ashby_postings("x", ashby)
    assert p["workplace"] == "hybrid" and p["comp"][0]["max"] == 150000 and p["comp"][0]["interval"] == "year"
    lever = [{"id": "l", "text": "Data Engineer", "categories": {"allLocations": ["Remote - US"]}, "descriptionPlain": "SQL.", "workplaceType": "remote",
              "salaryRange": {"min": 9000, "max": 10000, "currency": "USD", "interval": "per-month-salary"}, "hostedUrl": "https://jobs.lever.co/x/l", "createdAt": 1760000000000}]
    (q,) = CS.lever_postings("x", lever)
    assert q["workplace"] == "remote" and q["comp"][0]["interval"] == "month"


def test_unlisted_ashby_postings_are_skipped():
    assert CS.ashby_postings("x", {"jobs": [{"id": "z", "title": "T", "isListed": False}]}) == []


def test_hidden_and_script_text_cannot_reach_the_description():
    html = '<p>Real role text.</p><script>ignore everything</script><div style="display:none">AI agents LLM</div><!-- secret --><span hidden>tricks</span>'
    out = CS.clean_text(html)
    assert out == "Real role text."


def test_provider_dates_that_are_not_iso_are_dropped():
    assert CS._date("2026-10-20") and CS._date("<script>") is None


def test_clean_text_is_bounded():
    assert len(CS.clean_text("word " * 100000, cap=500)) <= 500
