"""Public ATS job-board adapters: fetch and normalize postings.

Only the documented, public, read-only board APIs of Greenhouse, Ashby and Lever are used: HTTPS GET, fixed host allowlist, no redirects, no cookies, no accounts,
no credentials, byte and time caps, one request per second per host. Nothing here scrapes career pages, bypasses access controls or contacts a recruiter. A 401/403/429
response marks the board blocked; it is never retried around. Posting text is untrusted data: it is parsed, never executed or obeyed.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

from .util import sha256_hex

UA = "job-scoring-engine/0.1 (public board APIs only)"
HOSTS = {"greenhouse": "boards-api.greenhouse.io", "ashby": "api.ashbyhq.com", "lever": "api.lever.co"}
MAX_BYTES = 40_000_000
TIMEOUT_S = 45
MIN_GAP_S = 1.0
DESC_CAP = 9000                      # characters of description kept per candidate posting (never the raw payload)
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Blocked(Exception):
    """The provider refused (401/403/429): back off, do not work around."""


class NotFound(Exception):
    pass


class FetchError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def url_ok(url: str) -> bool:
    """HTTPS, a fixed provider host, no credentials in the URL, default port only."""
    p = urlparse(url)
    return bool(p.scheme == "https" and p.hostname in HOSTS.values() and not p.username and not p.password and p.port in (None, 443))


class Fetcher:
    """GET-only transport with a per-host rate limit. `transport(url) -> (status, bytes)` is injectable so tests never touch the network."""

    def __init__(self, transport=None, sleep=time.sleep, clock=time.monotonic, gap: float = MIN_GAP_S):
        self.transport, self.sleep, self.clock, self.gap = transport or self._real, sleep, clock, gap
        self.last: dict[str, float] = {}
        self.requests = 0

    @staticmethod
    def _real(url: str) -> tuple[int, bytes]:
        opener = urllib.request.build_opener(_NoRedirect())
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with opener.open(req, timeout=15) as r:
                chunks, total, t0 = [], 0, time.monotonic()
                while True:
                    c = r.read(1 << 20)
                    if not c:
                        break
                    chunks.append(c)
                    total += len(c)
                    if total > MAX_BYTES or time.monotonic() - t0 > TIMEOUT_S:            # a total deadline, not just a per-read timeout (slow-drip responses)
                        raise FetchError("response too large or too slow")
                return r.status, b"".join(chunks)
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"{type(e).__name__}") from e

    def get_json(self, url: str):
        if not url_ok(url):
            raise FetchError("url outside the provider allowlist")
        host = urlparse(url).hostname
        wait = self.gap - (self.clock() - self.last.get(host, -1e9))
        if wait > 0:
            self.sleep(wait)
        self.last[host] = self.clock()
        self.requests += 1
        status, body = self.transport(url)
        if status in (401, 403, 429):
            raise Blocked(f"HTTP {status} from {host}")
        if status == 404:
            raise NotFound(url)
        if status != 200 or len(body) > MAX_BYTES:
            raise FetchError(f"HTTP {status} / {len(body)} bytes")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise FetchError("unreadable JSON") from exc


# ------------------------------------------------------------------------------------------------ text helpers
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|br)>|<br\s*/?>", re.I)
_WS = re.compile(r"[ \t ]+")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


_HIDDEN_BLOCK = re.compile(r"<(script|style|noscript|template|iframe|svg|head)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HIDDEN_EL = re.compile(r"<([a-z0-9]+)\b[^>]*(?:\bhidden\b|aria-hidden\s*=\s*[\"']true|display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0)[^>]*>.*?</\1\s*>", re.I | re.S)


def clean_text(s: str | None, cap: int = DESC_CAP) -> str:
    """Visible text only: scripts, styles, comments and hidden elements are removed BEFORE tags are stripped, so text a human reader would never see cannot influence scoring."""
    if not s:
        return ""
    s = str(s)[: cap * 8]                                  # bound the work done on hostile input before any regex runs
    s = html.unescape(s)
    for _ in range(2):                                      # Greenhouse content is double-escaped
        s = _COMMENT.sub(" ", s)
        s = _HIDDEN_BLOCK.sub(" ", s)
        s = _HIDDEN_EL.sub(" ", s)
        if "&lt;" in s or "&amp;" in s:
            s = html.unescape(s)
    s = _BLOCK.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    s = _CTRL.sub("", s)
    lines = [_WS.sub(" ", ln).strip() for ln in s.split("\n")]
    s = "\n".join(ln for ln in lines if ln)
    return s[:cap]


_FREE_COMP = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])", re.I)


def _num(tok: str) -> float:
    t = tok.replace(",", "").replace(" ", "").lower()
    return float(t[:-1]) * 1000 if t.endswith("k") else float(t)


def text_comp(text: str) -> list[dict]:
    """Salary ranges quoted in posting text. `source` is 'text' (stated by the posting, not structured data). Hourly style numbers are converted by the evaluator."""
    out = []
    for m in _FREE_COMP.finditer(text):
        try:
            lo, hi = _num(m.group(1)), _num(m.group(2))
        except ValueError:
            continue
        if lo > hi:
            lo, hi = hi, lo
        ctx = text[max(0, m.start() - 80): m.end() + 40].lower()
        if "hour" in ctx or "/hr" in ctx:
            interval = "hour"
        elif lo < 1000:
            continue                                     # a bare small number with no unit is not a salary
        else:
            interval = "year"
        after = text[m.end(): m.end() + 24]
        before = text[max(0, m.start() - 6): m.start() + 1]
        cur = "USD"
        if re.search(r"\b(cad|eur|gbp|aud|nzd|chf|sek|nok|dkk|inr|jpy|sgd|mxn|brl|pln)\b", after, re.I) or re.search(r"(?:€|£|¥|₹|\bC\$|\bA\$|\bCA\$)", before + text[m.start(): m.start() + 3]):
            cur = "NON_USD"
        out.append({"min": lo, "max": hi, "currency": cur, "interval": interval, "source": "text", "quote": text[max(0, m.start() - 40): m.end() + 30].replace("\n", " ")})
    return out[:6]


def _interval(iv: str) -> str:
    iv = str(iv or "").lower()
    for k, v in (("hour", "hour"), ("day", "day"), ("week", "week"), ("month", "month"), ("year", "year"), ("annual", "year")):
        if k in iv:
            return v
    return "year" if not iv else "unknown"


def _workplace(*hints) -> str:
    t = " ".join(str(h or "") for h in hints).lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t:
        return "remote"
    if "on-site" in t or "onsite" in t or "on site" in t or "in-office" in t or "in office" in t:
        return "onsite"
    return "unknown"


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


def _date(v):
    v = str(v).strip() if v else ""
    return v if _DATE.match(v) else None                      # a provider value that is not an ISO date is dropped, never printed


def _posting(source, board, company, jid, title, locations, workplace, url, apply_url, desc, dept, published, updated, comp, employment=None, **optional) -> dict:
    title = clean_text(title, 200).replace("\n", " ")
    p = {"source": source, "board": board, "company": company, "job_id": str(jid), "title": title, "locations": [clean_text(x, 100).replace("\n", " ") for x in locations if x][:8],
         "workplace": workplace, "url": url, "apply_url": apply_url, "description": desc, "department": clean_text(dept, 100).replace("\n", " "), "published_at": _date(published),
         "updated_at": _date(updated), "comp": comp, "employment": employment, "desc_fp": sha256_hex(re.sub(r"\W+", " ", desc.lower())[:1500])[:12]}
    p["pid"] = f"{source}:{board}:{jid}"
    p["content_hash"] = sha256_hex({k: p[k] for k in ("title", "locations", "workplace", "comp")} | {"desc": re.sub(r"\W+", " ", desc.lower())[:4000]})[:16]
    for key in ("source_type", "extraction_method", "extraction_confidence", "field_evidence", "first_seen_at", "last_seen_at", "job_status", "seniority", "travel_requirement", "salary_raw"):
        p[key] = optional.get(key)
    p["apply_url"] = apply_url
    return p


# ------------------------------------------------------------------------------------------------ Greenhouse
def gh_list_url(token: str) -> str:
    return f"https://{HOSTS['greenhouse']}/v1/boards/{quote(token)}/jobs"


def gh_detail_url(token: str, jid) -> str:
    return f"https://{HOSTS['greenhouse']}/v1/boards/{quote(token)}/jobs/{int(jid)}?pay_transparency=true"


def gh_stub(token: str, j: dict) -> dict:
    """List-endpoint entry: enough for the coarse title filter and change detection; no description, no pay."""
    meta = {m.get("name"): m.get("value") for m in j.get("metadata") or [] if isinstance(m, dict)}
    return {"job_id": str(j.get("id")), "title": clean_text(j.get("title"), 200), "locations": [(j.get("location") or {}).get("name") or ""], "updated_at": j.get("updated_at"),
            "published_at": j.get("first_published"), "url": j.get("absolute_url"), "workplace_hint": _workplace(meta.get("Location Type"), meta.get("Workplace Type")), "company": j.get("company_name")}


def gh_posting(token: str, stub: dict, d: dict) -> dict:
    desc = clean_text(d.get("content"))
    comp = []
    for r in d.get("pay_input_ranges") or []:
        if isinstance(r, dict) and r.get("min_cents") is not None:
            comp.append({"min": r["min_cents"] / 100, "max": (r.get("max_cents") or r["min_cents"]) / 100, "currency": r.get("currency_type") or "?", "interval": "year", "source": "structured", "label": clean_text(r.get("title"), 60),
                         "blurb": clean_text(r.get("blurb"), 400)})
    if not comp:
        comp = text_comp(desc)
    depts = ", ".join(x.get("name", "") for x in d.get("departments") or [] if isinstance(x, dict))
    meta = {m.get("name"): m.get("value") for m in d.get("metadata") or [] if isinstance(m, dict)}
    wp = _workplace(meta.get("Location Type"), meta.get("Workplace Type"), stub.get("workplace_hint"))
    if wp == "unknown":
        wp = _workplace(" ".join(stub["locations"]))
    return _posting("greenhouse", token, d.get("company_name") or stub.get("company") or token, stub["job_id"], d.get("title") or stub["title"], stub["locations"], wp,
                    d.get("absolute_url") or stub["url"], d.get("absolute_url") or stub["url"], desc, depts, stub.get("published_at"), d.get("updated_at") or stub.get("updated_at"), comp)


# ------------------------------------------------------------------------------------------------ Ashby
def ashby_url(name: str) -> str:
    return f"https://{HOSTS['ashby']}/posting-api/job-board/{quote(name)}?includeCompensation=true"


def ashby_postings(name: str, data: dict, keep=lambda title, dept: True) -> list[dict]:
    out = []
    for j in data.get("jobs") or []:
        if not isinstance(j, dict) or j.get("isListed") is False:
            continue
        title = clean_text(j.get("title"), 200)
        if not keep(title, j.get("department") or ""):
            continue
        desc = clean_text(j.get("descriptionPlain") or j.get("descriptionHtml"))
        comp = []
        c = j.get("compensation") or {}
        for comp_part in c.get("summaryComponents") or []:
            if isinstance(comp_part, dict) and comp_part.get("compensationType") == "Salary" and comp_part.get("minValue") is not None:
                iv = str(comp_part.get("interval") or "").lower()
                comp.append({"min": float(comp_part["minValue"]), "max": float(comp_part.get("maxValue") or comp_part["minValue"]), "currency": comp_part.get("currencyCode") or "?",
                             "interval": _interval(iv), "source": "structured"})
        if not comp:
            comp = text_comp(desc)
        wp = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}.get(str(j.get("workplaceType") or "").lower()) or ("remote" if j.get("isRemote") else _workplace(j.get("location")))
        locs = [j.get("location")] + [x.get("location") if isinstance(x, dict) else x for x in j.get("secondaryLocations") or []]
        out.append(_posting("ashby", name, name, j.get("id"), title, locs, wp, j.get("jobUrl"), j.get("applyUrl") or j.get("jobUrl"), desc, j.get("department") or j.get("team"),
                            j.get("publishedAt"), j.get("publishedAt"), comp, j.get("employmentType")))
    return out


# ------------------------------------------------------------------------------------------------ Lever
def lever_url(name: str) -> str:
    return f"https://{HOSTS['lever']}/v0/postings/{quote(name)}?mode=json"


def lever_postings(name: str, data, keep=lambda title, dept: True) -> list[dict]:
    out = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict):
            continue
        title = clean_text(j.get("text"), 200)
        cats = j.get("categories") or {}
        if not keep(title, cats.get("team") or ""):
            continue
        desc = clean_text(j.get("descriptionPlain") or j.get("description"))
        extra = clean_text(j.get("additionalPlain") or "")
        if extra:
            desc = (desc + "\n" + extra)[:DESC_CAP]
        comp = []
        sr = j.get("salaryRange") or {}
        if isinstance(sr, dict) and sr.get("min") is not None:
            iv = str(sr.get("interval") or "").lower()
            comp.append({"min": float(sr["min"]), "max": float(sr.get("max") or sr["min"]), "currency": sr.get("currency") or "?",
                         "interval": _interval(iv), "source": "structured"})
        if not comp:
            comp = text_comp(desc)
        wp = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}.get(str(j.get("workplaceType") or "").lower(), "unknown")
        created = j.get("createdAt")
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created / 1000)) if isinstance(created, (int, float)) else None
        locs = cats.get("allLocations") or [cats.get("location")]
        out.append(_posting("lever", name, name, j.get("id"), title, locs, wp, j.get("hostedUrl"), j.get("applyUrl") or j.get("hostedUrl"), desc, cats.get("team"), ts, ts, comp, cats.get("commitment")))
    return out
