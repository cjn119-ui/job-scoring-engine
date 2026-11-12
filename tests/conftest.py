import copy
import json
from pathlib import Path

import pytest

from job_scoring import evaluate as CE
from job_scoring import sources as CS

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
NOW = "2026-10-28T12:00:00Z"

PLATFORM_DESC = ("You will design and run data pipelines in Python and SQL, own data quality end to end, and work with cross-functional stakeholders. "
                 "End-to-end ownership from scratch with a small team. Pay range: $150,000 - $190,000 per year.")


@pytest.fixture
def prof():
    return CE.load_profile(ROOT / "config" / "profile.example.json")


@pytest.fixture
def prof_floor(prof):
    """The shipped profile has no pay floor; this variant sets a test-only one so floor behaviour can be exercised."""
    p = copy.deepcopy(prof)
    p["min_comp_usd"] = 100000
    return p


def posting(title="Senior Data Platform Engineer", desc=PLATFORM_DESC, comp=None, locations=("Remote - United States",), workplace="remote",
            company="Acme", jid="1", employment=None):
    """A normalized posting exactly as the adapters produce it."""
    return CS._posting("greenhouse", "acme-example", company, jid, title, list(locations), workplace, f"https://example.test/{jid}", f"https://example.test/{jid}",
                       desc, "Engineering", "2026-10-20", "2026-10-20", comp if comp is not None else CS.text_comp(desc), employment)


class Net:
    """Scripted provider: url -> (status, body). Records every request."""

    def __init__(self, routes=None):
        self.routes, self.log = routes or {}, []

    def __call__(self, url):
        self.log.append(url)
        status, body = self.routes.get(url, (404, None))
        return status, json.dumps(body).encode() if body is not None else b""


def fixture_routes():
    index = json.loads((FIXTURES / "index.json").read_text())
    return {url: (200, json.loads((FIXTURES / name).read_text())) for url, name in index.items()}


def fetcher(net):
    return CS.Fetcher(transport=net, sleep=lambda s: None, gap=0)
